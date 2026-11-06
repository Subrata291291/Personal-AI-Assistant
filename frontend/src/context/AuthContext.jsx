
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react'

import {
  authMutationLockAvailable,
  signOutIfSessionMatches,
  supabase,
} from '../lib/supabase'
import { queryClient } from '../lib/queryClient'
import { notifyApiAuthChange } from '../lib/api'
import {
  withAuthTimeout,
  withTimeout,
} from '../lib/requestTimeout'

const AuthContext = createContext(null)

// Supabase confirms email addresses after successful email
// confirmation or a trusted OAuth authentication flow.
function isVerifiedUser(user) {
  return Boolean(
    user?.email_confirmed_at ||
    user?.confirmed_at
  )
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [session, setSession] = useState(null)
  const [loading, setLoading] = useState(true)
  const authGenerationRef = useRef(0)
  const currentUserIdRef = useRef(null)
  const unverifiedCleanupRef = useRef(null)
  const lastUnverifiedCleanupKeyRef = useRef(null)

  useEffect(() => {
    let mounted = true
    let authEventObserved = false

    function setCurrentSession(nextSession) {
      const nextUser = nextSession?.user ?? null
      const nextUserId = nextUser?.id ?? null
      const previousUserId = currentUserIdRef.current

      if (previousUserId !== nextUserId) {
        notifyApiAuthChange()

        if (previousUserId) {
          const isPreviousUserQuery = (query) =>
            query.queryKey[1] === previousUserId

          void queryClient.cancelQueries({
            predicate: isPreviousUserQuery,
          })
          queryClient.removeQueries({
            predicate: isPreviousUserQuery,
          })
        }
      }

      currentUserIdRef.current = nextUserId
      setSession(nextSession)
      setUser(nextUser)
    }

    async function reconcileSdkSession(expectedGeneration) {
      let actualSession = null
      try {
        const {
          data: { session: currentSession },
          error,
        } = await withAuthTimeout(
          supabase.auth.getSession(),
          'Session reconciliation',
        )
        if (error) throw error
        actualSession = currentSession
      } catch (error) {
        console.error(
          'Unable to reconcile the active auth session:',
          error instanceof Error ? error.message : 'Unknown error',
        )
      }

      if (
        !mounted ||
        authGenerationRef.current !== expectedGeneration
      ) return

      setCurrentSession(
        actualSession && isVerifiedUser(actualSession.user)
          ? actualSession
          : null,
      )
      setLoading(false)
    }

    function cleanupUnverifiedSession(expectedSession, expectedGeneration) {
      const userId = expectedSession?.user?.id
      if (!userId) return Promise.resolve()

      const cleanupKey =
        `${userId}:${expectedSession.access_token || ''}`
      const existingCleanup = unverifiedCleanupRef.current
      if (existingCleanup?.userId === userId) {
        if (!existingCleanup.cancelled) {
          existingCleanup.generation = expectedGeneration
        }
        return existingCleanup.promise
      }
      if (lastUnverifiedCleanupKeyRef.current === cleanupKey) {
        return Promise.resolve()
      }
      lastUnverifiedCleanupKeyRef.current = cleanupKey

      const cleanup = {
        userId,
        generation: expectedGeneration,
        cancelled: false,
        promise: null,
        run: cleanupUnverifiedSession,
      }
      unverifiedCleanupRef.current = cleanup

      cleanup.promise = (async () => {
        if (!authMutationLockAvailable()) {
          throw new Error(
            'Unverified-session cleanup requires cross-tab auth coordination.',
          )
        }

        await withAuthTimeout(
          signOutIfSessionMatches(
            expectedSession,
            (currentSession) =>
              mounted &&
              !cleanup.cancelled &&
              cleanup.generation === authGenerationRef.current &&
              (
                !currentSession ||
                !isVerifiedUser(currentSession.user)
              ),
          ),
          'Session cleanup',
        )
      })()
        .catch((error) => {
          console.error(
            'Unable to clear an unverified session:',
            error instanceof Error ? error.message : 'Unknown error',
          )
        })
        .then(() => reconcileSdkSession(authGenerationRef.current))
        .finally(() => {
          if (unverifiedCleanupRef.current === cleanup) {
            unverifiedCleanupRef.current = null
          }
        })

      return cleanup.promise
    }

    unverifiedCleanupRef.current = {
      run: cleanupUnverifiedSession,
    }

    async function initializeAuth() {
      const initializationGeneration = authGenerationRef.current
      try {
        const {
          data: { session },
          error,
        } = await withTimeout(supabase.auth.getSession(), {
          message: 'Session verification timed out. Please try again.',
          getPhase: () => 'session',
        })

        if (
          !mounted ||
          authEventObserved ||
          initializationGeneration !== authGenerationRef.current
        ) return

        if (error) throw error

        const authenticatedUser = session?.user ?? null

        if (
          session &&
          authenticatedUser &&
          !isVerifiedUser(authenticatedUser)
        ) {
          void cleanupUnverifiedSession(
            session,
            authGenerationRef.current,
          )

          if (
            !mounted ||
            authEventObserved ||
            initializationGeneration !== authGenerationRef.current
          ) return

          setCurrentSession(null)
        } else {
          setCurrentSession(session)
        }
      } catch (error) {
        console.error(
          'Session initialization failed:',
          error instanceof Error ? error.message : 'Unknown error',
        )
        if (
          mounted &&
          !authEventObserved &&
          initializationGeneration === authGenerationRef.current
        ) {
          setCurrentSession(null)
        }
      } finally {
        if (mounted) setLoading(false)
      }
    }

    initializeAuth()

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event, nextSession) => {
      if (!mounted) return
      authEventObserved = true
      authGenerationRef.current += 1

      const eventGeneration = authGenerationRef.current
      const activeCleanup = unverifiedCleanupRef.current
      if (
        activeCleanup?.userId &&
        (event === 'SIGNED_OUT' ||
          (nextSession?.user && isVerifiedUser(nextSession.user)))
      ) {
        activeCleanup.cancelled = true
      }

      const authenticatedUser = nextSession?.user ?? null

      if (
        authenticatedUser &&
        !isVerifiedUser(authenticatedUser)
      ) {
        setCurrentSession(null)
        void cleanupUnverifiedSession(
          nextSession,
          eventGeneration,
        )
        setLoading(false)
        return
      }

      if (nextSession?.user && isVerifiedUser(nextSession.user)) {
        lastUnverifiedCleanupKeyRef.current = null
      }

      if (event === 'SIGNED_OUT') {
        setCurrentSession(null)
        setLoading(false)
        void reconcileSdkSession(eventGeneration)
        return
      }

      setCurrentSession(nextSession)
      setLoading(false)
    })

    return () => {
      mounted = false
      if (
        unverifiedCleanupRef.current?.run === cleanupUnverifiedSession
      ) {
        unverifiedCleanupRef.current = null
      }
      subscription.unsubscribe()
    }
  }, [])

  async function signUp({ fullName, email, password }) {
    const operationGeneration = authGenerationRef.current
    const { data, error } = await withAuthTimeout(
      supabase.auth.signUp({
        email,
        password,
        options: {
          data: {
            full_name: fullName,
          },
        },
      }),
      'Sign-up',
    )

    if (
      authGenerationRef.current !== operationGeneration &&
      data.user?.id &&
      currentUserIdRef.current !== data.user.id
    ) {
      const staleAuthError = new Error(
        'The active account changed during sign-up. Please try again.',
      )
      staleAuthError.code = 'AUTH_SESSION_CHANGED'
      throw staleAuthError
    }

    if (error) throw error

    return data
  }

  async function signIn({ email, password }) {
    const operationGeneration = authGenerationRef.current
    const { data, error } = await withAuthTimeout(
      supabase.auth.signInWithPassword({
        email,
        password,
      }),
      'Sign-in',
    )

    if (error) throw error

    if (
      authGenerationRef.current !== operationGeneration &&
      currentUserIdRef.current !== data.user?.id
    ) {
      const staleAuthError = new Error(
        'The active account changed during sign-in. Please try again.',
      )
      staleAuthError.code = 'AUTH_SESSION_CHANGED'
      throw staleAuthError
    }

    if (!isVerifiedUser(data.user)) {
      const cleanup = unverifiedCleanupRef.current
      if (cleanup?.run) {
        await withAuthTimeout(
          cleanup.run(data.session, authGenerationRef.current),
          'Session cleanup',
        )
      }

      if (
        authGenerationRef.current !== operationGeneration &&
        currentUserIdRef.current
      ) {
        const staleAuthError = new Error(
          'The active account changed during sign-in. Please try again.',
        )
        staleAuthError.code = 'AUTH_SESSION_CHANGED'
        throw staleAuthError
      }

      throw new Error(
        'Please verify your email address before signing in. Check your inbox for the confirmation link.'
      )
    }

    return data
  }

  async function signOut() {
    const userId = currentUserIdRef.current
    if (unverifiedCleanupRef.current) {
      unverifiedCleanupRef.current.cancelled = true
    }
    authGenerationRef.current += 1
    const { error } = await withAuthTimeout(
      supabase.auth.signOut(),
      'Sign-out',
    )

    if (error) throw error
    if (userId && currentUserIdRef.current &&
      currentUserIdRef.current !== userId) {
      const staleAuthError = new Error(
        'The active account changed during sign-out. Please try again.',
      )
      staleAuthError.code = 'AUTH_SESSION_CHANGED'
      throw staleAuthError
    }
    return userId
  }

  async function updateProfile({ fullName }) {
    const userId = currentUserIdRef.current
    const { data, error } = await withAuthTimeout(
      supabase.auth.updateUser({
        data: {
          full_name: fullName,
        },
      }),
      'Profile update',
    )

    if (error) throw error

    if (userId && currentUserIdRef.current === userId) setUser(data.user)
    return data.user
  }

  async function updateUserMetadata(metadata) {
    const userId = currentUserIdRef.current
    const { data, error } = await withAuthTimeout(
      supabase.auth.updateUser({ data: metadata }),
      'Profile update',
    )

    if (error) throw error

    if (userId && currentUserIdRef.current === userId) setUser(data.user)
    return data.user
  }

  async function updatePassword(password) {
    const userId = currentUserIdRef.current
    const { data, error } = await withAuthTimeout(
      supabase.auth.updateUser({ password }),
      'Password update',
    )

    if (error) throw error

    if (userId && currentUserIdRef.current === userId) setUser(data.user)
    return data.user
  }

  const isEmailVerified = isVerifiedUser(user)

  const value = {
    user,
    session,
    loading,
    signUp,
    signIn,
    signOut,
    updateProfile,
    updateUserMetadata,
    updatePassword,
    isEmailVerified,
    isAuthenticated: Boolean(user && isEmailVerified),
  }

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)

  if (!context) {
    throw new Error(
      'useAuth must be used inside AuthProvider'
    )
  }

  return context
}
