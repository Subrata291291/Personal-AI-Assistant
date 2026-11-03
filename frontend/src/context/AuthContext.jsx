
import {
  createContext,
  useContext,
  useEffect,
  useState,
} from 'react'

import { supabase } from '../lib/supabase'

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

  useEffect(() => {
    let mounted = true

    async function initializeAuth() {
      const {
        data: { session },
        error,
      } = await supabase.auth.getSession()

      if (!mounted) return

      if (error) {
        console.error(
          'Session initialization failed:',
          error.message
        )
      }

      const authenticatedUser = session?.user ?? null

      if (
        session &&
        authenticatedUser &&
        !isVerifiedUser(authenticatedUser)
      ) {
        await supabase.auth.signOut()

        if (!mounted) return

        setSession(null)
        setUser(null)
      } else {
        setSession(session)
        setUser(authenticatedUser)
      }

      setLoading(false)
    }

    initializeAuth()

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (!mounted) return

      const authenticatedUser = session?.user ?? null

      if (
        authenticatedUser &&
        !isVerifiedUser(authenticatedUser)
      ) {
        setSession(null)
        setUser(null)

        // Remove an unexpected unverified session.
        void supabase.auth.signOut()
        return
      }

      setSession(session)
      setUser(authenticatedUser)
      setLoading(false)
    })

    return () => {
      mounted = false
      subscription.unsubscribe()
    }
  }, [])

  async function signUp({ fullName, email, password }) {
    const { data, error } = await supabase.auth.signUp({
      email,
      password,
      options: {
        data: {
          full_name: fullName,
        },
      },
    })

    if (error) throw error

    return data
  }

  async function signIn({ email, password }) {
    const { data, error } =
      await supabase.auth.signInWithPassword({
        email,
        password,
      })

    if (error) throw error

    if (!isVerifiedUser(data.user)) {
      await supabase.auth.signOut()

      throw new Error(
        'Please verify your email address before signing in. Check your inbox for the confirmation link.'
      )
    }

    return data
  }

  async function signOut() {
    const { error } = await supabase.auth.signOut()

    if (error) throw error
  }

  async function updateProfile({ fullName }) {
    const { data, error } = await supabase.auth.updateUser({
      data: {
        full_name: fullName,
      },
    })

    if (error) throw error

    setUser(data.user)
    return data.user
  }

  async function updatePassword(password) {
    const { data, error } = await supabase.auth.updateUser({
      password,
    })

    if (error) throw error

    setUser(data.user)
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
