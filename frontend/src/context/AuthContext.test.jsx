import { QueryClientProvider, useQuery } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const authMock = vi.hoisted(() => ({
  authStateChange: null,
  crossTabLockAvailable: true,
  persistedSession: null,
  lockRunner: null,
  signOutIfSessionMatches: null,
  afterCleanupIdentity: null,
}))

vi.mock('../lib/supabase', () => ({
  authMutationLockAvailable: () => authMock.crossTabLockAvailable,
  signOutIfSessionMatches: (...args) =>
    authMock.signOutIfSessionMatches(...args),
  supabase: {
    auth: {
      getSession: vi.fn(),
      onAuthStateChange: vi.fn(),
      signOut: vi.fn(),
      setSession: vi.fn(),
    },
  },
}))

import { supabase } from '../lib/supabase'
import { AuthProvider, useAuth } from './AuthContext'
import { API_REQUEST_TIMEOUT_MS } from '../lib/requestTimeout'
import { queryClient } from '../lib/queryClient'
import { api } from '../lib/api'

function AuthState() {
  const { loading, user, signOut } = useAuth()
  return (
    <div>
      {loading ? 'Loading authentication' : user?.id || 'Signed out'}
      <button type="button" onClick={signOut}>Sign out</button>
    </div>
  )
}

function emitAuthEvent(event, session) {
  authMock.authStateChange?.(event, session)
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function createQueuedLock() {
  let active = false
  const queue = []

  function runNext() {
    if (active || queue.length === 0) return
    active = true
    const { operation, resolve, reject } = queue.shift()
    Promise.resolve()
      .then(operation)
      .then(resolve, reject)
      .finally(() => {
        active = false
        runNext()
      })
  }

  return {
    run(operation) {
      return new Promise((resolve, reject) => {
        queue.push({ operation, resolve, reject })
        runNext()
      })
    },
  }
}

function renderAuthState() {
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <AuthState />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

function AuthStateWithUserQuery({ queryFn }) {
  const { user, loading } = useAuth()
  useQuery({
    queryKey: ['conversations', user?.id],
    queryFn,
    enabled: Boolean(user?.id),
  })

  return (
    <div>
      {loading ? 'Loading authentication' : user?.id || 'Signed out'}
    </div>
  )
}

describe('AuthProvider initialization and account changes', () => {
  beforeEach(() => {
    queryClient.clear()
    vi.useRealTimers()
    authMock.authStateChange = null
    authMock.crossTabLockAvailable = true
    authMock.persistedSession = null
    authMock.afterCleanupIdentity = null
    authMock.lockRunner = (operation) =>
      Promise.resolve().then(operation)
    vi.spyOn(console, 'error').mockImplementation(() => {})
    supabase.auth.signOut.mockReset()
    supabase.auth.signOut.mockImplementation(async () => {
      authMock.persistedSession = null
      emitAuthEvent('SIGNED_OUT', null)
      return { error: null }
    })
    supabase.auth.setSession.mockReset()
    supabase.auth.setSession.mockImplementation(async () => ({
      data: { session: authMock.persistedSession },
      error: null,
    }))
    supabase.auth.getSession.mockImplementation(async () => ({
      data: { session: authMock.persistedSession },
      error: null,
    }))
    authMock.signOutIfSessionMatches = (
      expectedSession,
      shouldContinue,
    ) => {
      if (!authMock.crossTabLockAvailable) return Promise.resolve(false)

      return authMock.lockRunner(async () => {
        if (!shouldContinue()) return false
        const {
          data: { session },
          error,
        } = await supabase.auth.getSession()
        if (error) throw error
        if (
          !shouldContinue(session) ||
          session?.user?.id !== expectedSession?.user?.id ||
          !expectedSession?.access_token ||
          session.access_token !== expectedSession.access_token
        ) return false

        await authMock.afterCleanupIdentity?.()
        const { error: signOutError } = await supabase.auth.signOut()
        if (signOutError) throw signOutError
        return true
      })
    }
    supabase.auth.onAuthStateChange.mockReturnValue({
      data: {
        subscription: { unsubscribe: vi.fn() },
      },
    })
    supabase.auth.onAuthStateChange.mockImplementation((callback) => {
      authMock.authStateChange = callback
      return {
        data: { subscription: { unsubscribe: vi.fn() } },
      }
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('clears initialization loading when session retrieval rejects', async () => {
    supabase.auth.getSession.mockRejectedValue(new Error('Auth offline'))

    renderAuthState()

    expect(await screen.findByText('Signed out')).toBeInTheDocument()
    expect(screen.queryByText('Loading authentication'))
      .not.toBeInTheDocument()
  })

  it('clears initialization loading when session retrieval times out', async () => {
    vi.useFakeTimers()
    supabase.auth.getSession.mockReturnValue(new Promise(() => {}))
    renderAuthState()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(API_REQUEST_TIMEOUT_MS)
    })

    expect(screen.getByText('Signed out')).toBeInTheDocument()
    expect(screen.queryByText('Loading authentication'))
      .not.toBeInTheDocument()
  })

  it('clears initialization loading when unverified-session sign-out hangs', async () => {
    vi.useFakeTimers()
    supabase.auth.getSession.mockResolvedValue({
      data: {
        session: {
          access_token: 'unverified-access',
          refresh_token: 'unverified-refresh',
          user: {
            id: 'unverified-user',
            email: 'unverified@example.test',
          },
        },
      },
      error: null,
    })
    supabase.auth.signOut.mockReturnValue(new Promise(() => {}))
    renderAuthState()

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(API_REQUEST_TIMEOUT_MS)
    })

    expect(screen.getByText('Signed out')).toBeInTheDocument()
    expect(screen.queryByText('Loading authentication'))
      .not.toBeInTheDocument()
  })

  it('updates the authenticated user when Supabase changes accounts', async () => {
    const confirmedUser = (id) => ({
      id,
      email_confirmed_at: '2026-01-01T00:00:00Z',
    })
    let authStateChange
    supabase.auth.getSession.mockResolvedValue({
      data: { session: { user: confirmedUser('user-a') } },
      error: null,
    })
    supabase.auth.onAuthStateChange.mockImplementation((callback) => {
      authStateChange = callback
      return {
        data: { subscription: { unsubscribe: vi.fn() } },
      }
    })

    renderAuthState()

    expect(await screen.findByText('user-a')).toBeInTheDocument()
    act(() => {
      authStateChange('SIGNED_IN', {
        user: confirmedUser('user-b'),
        access_token: 'user-b-session',
      })
    })

    expect(screen.getByText('user-b')).toBeInTheDocument()
    expect(screen.queryByText('user-a')).not.toBeInTheDocument()
  })

  it('lets a newer sign-in win when cleanup has not acquired the lock', async () => {
    const unverifiedSession = {
      access_token: 'user-a-access',
      refresh_token: 'user-a-refresh',
      user: { id: 'user-a', email: 'a@example.test' },
    }
    const verifiedSession = {
      access_token: 'user-b-access',
      refresh_token: 'user-b-refresh',
      user: {
        id: 'user-b',
        email_confirmed_at: '2026-01-01T00:00:00Z',
      },
    }
    const lock = createQueuedLock()
    const cleanupBeforeLock = deferred()
    const cleanupRequested = deferred()
    let pauseCleanup = true
    authMock.lockRunner = (operation) => {
      if (pauseCleanup) {
        pauseCleanup = false
        cleanupRequested.resolve()
        return cleanupBeforeLock.promise.then(() => lock.run(operation))
      }
      return lock.run(operation)
    }

    renderAuthState()
    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })
    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await cleanupRequested.promise

    await authMock.lockRunner(async () => {
      authMock.persistedSession = verifiedSession
      emitAuthEvent('SIGNED_IN', verifiedSession)
    })
    cleanupBeforeLock.resolve()

    await waitFor(() => {
      expect(screen.getByText('user-b')).toBeInTheDocument()
      expect(authMock.persistedSession).toBe(verifiedSession)
    })
    expect(supabase.auth.signOut).not.toHaveBeenCalled()
  })

  it('cleans the old session before a queued sign-in establishes the new one', async () => {
    const unverifiedSession = {
      access_token: 'user-a-access',
      refresh_token: 'user-a-refresh',
      user: { id: 'user-a', email: 'a@example.test' },
    }
    const verifiedSession = {
      access_token: 'user-b-access',
      refresh_token: 'user-b-refresh',
      user: {
        id: 'user-b',
        email_confirmed_at: '2026-01-01T00:00:00Z',
      },
    }
    const lock = createQueuedLock()
    const identityChecked = deferred()
    const finishCleanup = deferred()
    authMock.lockRunner = (operation) => lock.run(operation)
    authMock.afterCleanupIdentity = () => {
      identityChecked.resolve()
      return finishCleanup.promise
    }

    renderAuthState()
    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })
    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await identityChecked.promise

    const signIn = authMock.lockRunner(async () => {
      authMock.persistedSession = verifiedSession
      emitAuthEvent('SIGNED_IN', verifiedSession)
    })
    finishCleanup.resolve()
    await signIn

    await waitFor(() => {
      expect(screen.getByText('user-b')).toBeInTheDocument()
      expect(authMock.persistedSession).toBe(verifiedSession)
    })
    expect(supabase.auth.signOut).toHaveBeenCalledOnce()
  })

  it('skips cleanup when the persisted session changes before lock entry', async () => {
    const unverifiedSession = {
      access_token: 'user-a-access',
      refresh_token: 'user-a-refresh',
      user: { id: 'user-a', email: 'a@example.test' },
    }
    const replacementSession = {
      access_token: 'user-b-access',
      refresh_token: 'user-b-refresh',
      user: {
        id: 'user-b',
        email_confirmed_at: '2026-01-01T00:00:00Z',
      },
    }
    const lock = createQueuedLock()
    const cleanupBeforeLock = deferred()
    const cleanupRequested = deferred()
    let pauseCleanup = true
    authMock.lockRunner = (operation) => {
      if (pauseCleanup) {
        pauseCleanup = false
        cleanupRequested.resolve()
        return cleanupBeforeLock.promise.then(() => lock.run(operation))
      }
      return lock.run(operation)
    }

    renderAuthState()
    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })
    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await cleanupRequested.promise

    authMock.persistedSession = replacementSession
    cleanupBeforeLock.resolve()
    await waitFor(() => {
      expect(authMock.signOutIfSessionMatches).toBeTypeOf('function')
      expect(screen.getByText('user-b')).toBeInTheDocument()
    })
    expect(supabase.auth.signOut).not.toHaveBeenCalled()
    expect(authMock.persistedSession).toBe(replacementSession)
  })

  it('does not sign out without Web Locks or overwrite a newer auth event', async () => {
    const unverifiedSession = {
      access_token: 'user-a-access',
      refresh_token: 'user-a-refresh',
      user: { id: 'user-a', email: 'a@example.test' },
    }
    const verifiedSession = {
      access_token: 'user-b-access',
      refresh_token: 'user-b-refresh',
      user: {
        id: 'user-b',
        email_confirmed_at: '2026-01-01T00:00:00Z',
      },
    }
    const reconciliation = deferred()
    let getSessionCalls = 0
    supabase.auth.getSession.mockImplementation(() => {
      getSessionCalls += 1
      if (getSessionCalls === 1) {
        return Promise.resolve({
          data: { session: null },
          error: null,
        })
      }
      return reconciliation.promise
    })
    authMock.crossTabLockAvailable = false

    renderAuthState()
    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })
    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await waitFor(() => expect(getSessionCalls).toBe(2))

    authMock.persistedSession = verifiedSession
    act(() => emitAuthEvent('SIGNED_IN', verifiedSession))
    await act(async () => {
      reconciliation.resolve({
        data: { session: null },
        error: null,
      })
    })

    expect(screen.getByText('user-b')).toBeInTheDocument()
    expect(authMock.persistedSession).toBe(verifiedSession)
    expect(supabase.auth.signOut).not.toHaveBeenCalled()
  })

  it('does not restore a previous session after cross-tab logout during cleanup', async () => {
    const confirmedUser = {
      id: 'user-b',
      email_confirmed_at: '2026-01-01T00:00:00Z',
    }
    const newerSession = {
      access_token: 'user-b-access',
      refresh_token: 'user-b-refresh',
      user: confirmedUser,
    }
    const unverifiedSession = {
      access_token: 'unverified-access',
      refresh_token: 'unverified-refresh',
      user: {
        id: 'unverified-user',
        email: 'unverified@example.test',
      },
    }
    let finishSignOut
    supabase.auth.signOut.mockReturnValue(new Promise((resolve) => {
      finishSignOut = () => {
        authMock.persistedSession = null
        emitAuthEvent('SIGNED_OUT', null)
        resolve({ error: null })
      }
    }))

    renderAuthState()
    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })

    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await waitFor(() => expect(supabase.auth.signOut).toHaveBeenCalledOnce())

    authMock.persistedSession = newerSession
    expect(supabase.auth.signOut).toHaveBeenCalledOnce()
    act(() => {
      emitAuthEvent('SIGNED_IN', newerSession)
    })
    expect(screen.getByText('user-b')).toBeInTheDocument()

    authMock.persistedSession = null
    act(() => emitAuthEvent('SIGNED_OUT', null))
    expect(await screen.findByText('Signed out')).toBeInTheDocument()
    await act(async () => {
      finishSignOut()
    })

    await waitFor(() => {
      expect(authMock.persistedSession).toBeNull()
    })
    expect(supabase.auth.setSession).not.toHaveBeenCalled()
    expect(screen.getByText('Signed out')).toBeInTheDocument()
    await expect(supabase.auth.getSession()).resolves.toEqual({
      data: { session: null },
      error: null,
    })
  })

  it('keeps a verified SDK session after a stale cleanup SIGNED_OUT event', async () => {
    const verifiedSession = {
      access_token: 'verified-access',
      refresh_token: 'verified-refresh',
      user: {
        id: 'verified-user',
        email_confirmed_at: '2026-01-01T00:00:00Z',
      },
    }
    const unverifiedSession = {
      access_token: 'unverified-access',
      refresh_token: 'unverified-refresh',
      user: {
        id: 'unverified-user',
        email: 'unverified@example.test',
      },
    }
    let finishSignOut
    supabase.auth.signOut.mockReturnValue(new Promise((resolve) => {
      finishSignOut = () => {
        emitAuthEvent('SIGNED_OUT', null)
        resolve({ error: null })
      }
    }))
    renderAuthState()
    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })

    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await waitFor(() => expect(supabase.auth.signOut).toHaveBeenCalledOnce())

    authMock.persistedSession = verifiedSession
    act(() => emitAuthEvent('SIGNED_IN', verifiedSession))
    await act(async () => finishSignOut())

    await waitFor(() => {
      expect(screen.getByText('verified-user')).toBeInTheDocument()
    })
    expect(authMock.persistedSession).toBe(verifiedSession)
    expect(supabase.auth.setSession).not.toHaveBeenCalled()
  })

  it('reconciles failed cleanup with SDK state without restoring an old session', async () => {
    const unverifiedSession = {
      access_token: 'unverified-access',
      refresh_token: 'unverified-refresh',
      user: {
        id: 'unverified-user',
        email: 'unverified@example.test',
      },
    }
    supabase.auth.signOut.mockRejectedValue(new Error('Cleanup failed'))
    renderAuthState()
    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })

    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))

    await waitFor(() => {
      expect(screen.getByText('Signed out')).toBeInTheDocument()
      expect(supabase.auth.signOut).toHaveBeenCalledOnce()
    })
    expect(supabase.auth.setSession).not.toHaveBeenCalled()
    expect(authMock.persistedSession).toBe(unverifiedSession)
  })

  it('does not start cleanup when cross-tab auth coordination is unavailable', async () => {
    const unverifiedSession = {
      access_token: 'unverified-access',
      refresh_token: 'unverified-refresh',
      user: {
        id: 'unverified-user',
        email: 'unverified@example.test',
      },
    }
    authMock.crossTabLockAvailable = false
    authMock.persistedSession = unverifiedSession
    renderAuthState()
    expect(await screen.findByText('Signed out')).toBeInTheDocument()

    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await waitFor(() => {
      expect(screen.getByText('Signed out')).toBeInTheDocument()
      expect(supabase.auth.signOut).not.toHaveBeenCalled()
    })
  })

  it('keeps a legitimate logout authoritative while cleanup is pending', async () => {
    const verifiedSession = {
      access_token: 'verified-access',
      refresh_token: 'verified-refresh',
      user: {
        id: 'verified-user',
        email_confirmed_at: '2026-01-01T00:00:00Z',
      },
    }
    const unverifiedSession = {
      access_token: 'unverified-access',
      refresh_token: 'unverified-refresh',
      user: {
        id: 'unverified-user',
        email: 'unverified@example.test',
      },
    }
    let finishCleanup
    let signOutCalls = 0
    supabase.auth.signOut.mockImplementation(() => {
      signOutCalls += 1
      if (signOutCalls === 1) {
        return new Promise((resolve) => {
          finishCleanup = () => resolve({ error: null })
        })
      }
      authMock.persistedSession = null
      emitAuthEvent('SIGNED_OUT', null)
      return Promise.resolve({ error: null })
    })
    authMock.persistedSession = verifiedSession
    renderAuthState()
    expect(await screen.findByText('verified-user')).toBeInTheDocument()

    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await waitFor(() => expect(signOutCalls).toBe(1))

    await act(async () => {
      screen.getByRole('button', { name: 'Sign out' }).click()
    })
    expect(await screen.findByText('Signed out')).toBeInTheDocument()

    await act(async () => finishCleanup())
    await waitFor(() => {
      expect(screen.getByText('Signed out')).toBeInTheDocument()
      expect(authMock.persistedSession).toBeNull()
    })
    expect(supabase.auth.setSession).not.toHaveBeenCalled()
  })

  it('does not let an older reconciliation overwrite a newer verified session', async () => {
    const unverifiedSession = {
      access_token: 'unverified-access',
      refresh_token: 'unverified-refresh',
      user: {
        id: 'unverified-user',
        email: 'unverified@example.test',
      },
    }
    const verifiedSession = {
      access_token: 'verified-access',
      refresh_token: 'verified-refresh',
      user: {
        id: 'verified-user',
        email_confirmed_at: '2026-01-01T00:00:00Z',
      },
    }
    const pendingReconciliation = deferred()
    const pendingCleanup = deferred()
    let getSessionCalls = 0
    supabase.auth.getSession.mockImplementation(() => {
      getSessionCalls += 1
      if (getSessionCalls === 1) {
        return Promise.resolve({
          data: { session: null },
          error: null,
        })
      }
      if (getSessionCalls === 2) {
        return Promise.resolve({
          data: { session: unverifiedSession },
          error: null,
        })
      }
      if (getSessionCalls === 3) return pendingReconciliation.promise
      return Promise.resolve({
        data: { session: authMock.persistedSession },
        error: null,
      })
    })
    supabase.auth.signOut.mockReturnValue(pendingCleanup.promise)
    renderAuthState()
    expect(await screen.findByText('Signed out')).toBeInTheDocument()

    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await waitFor(() => expect(supabase.auth.signOut).toHaveBeenCalledOnce())

    authMock.persistedSession = null
    act(() => emitAuthEvent('SIGNED_OUT', null))
    await waitFor(() => expect(getSessionCalls).toBe(3))

    authMock.persistedSession = verifiedSession
    act(() => emitAuthEvent('SIGNED_IN', verifiedSession))
    await act(async () => {
      pendingReconciliation.resolve({
        data: { session: null },
        error: null,
      })
    })
    expect(screen.getByText('verified-user')).toBeInTheDocument()
    expect(authMock.persistedSession).toBe(verifiedSession)
    expect(supabase.auth.setSession).not.toHaveBeenCalled()

    await act(async () => {
      pendingCleanup.resolve({ error: null })
    })
  })

  it('clears a normal unverified session from Supabase storage', async () => {
    const unverifiedSession = {
      access_token: 'unverified-access',
      refresh_token: 'unverified-refresh',
      user: {
        id: 'unverified-user',
        email: 'unverified@example.test',
      },
    }
    renderAuthState()

    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })
    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))

    await waitFor(() => {
      expect(supabase.auth.signOut).toHaveBeenCalledOnce()
      expect(authMock.persistedSession).toBeNull()
    })
    expect(screen.getByText('Signed out')).toBeInTheDocument()
    await expect(supabase.auth.getSession()).resolves.toEqual({
      data: { session: null },
      error: null,
    })
  })

  it('does not repeat cleanup for repeated unverified auth events', async () => {
    const unverifiedSession = {
      access_token: 'unverified-access',
      refresh_token: 'unverified-refresh',
      user: {
        id: 'unverified-user',
        email: 'unverified@example.test',
      },
    }
    let finishSignOut
    supabase.auth.signOut.mockReturnValue(new Promise((resolve) => {
      finishSignOut = () => {
        authMock.persistedSession = null
        emitAuthEvent('SIGNED_OUT', null)
        resolve({ error: null })
      }
    }))
    renderAuthState()

    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })
    authMock.persistedSession = unverifiedSession
    act(() => emitAuthEvent('SIGNED_IN', unverifiedSession))
    await waitFor(() => expect(supabase.auth.signOut).toHaveBeenCalledOnce())

    act(() => {
      emitAuthEvent('SIGNED_IN', unverifiedSession)
      emitAuthEvent('SIGNED_IN', unverifiedSession)
    })
    expect(supabase.auth.signOut).toHaveBeenCalledOnce()

    await act(async () => finishSignOut())
    await waitFor(() => expect(authMock.persistedSession).toBeNull())
    expect(supabase.auth.signOut).toHaveBeenCalledOnce()
  })

  it('preserves normal logout behavior and clears persisted session', async () => {
    const verifiedSession = {
      access_token: 'user-a-access',
      refresh_token: 'user-a-refresh',
      user: {
        id: 'user-a',
        email_confirmed_at: '2026-01-01T00:00:00Z',
      },
    }
    authMock.persistedSession = verifiedSession
    renderAuthState()

    expect(await screen.findByText('user-a')).toBeInTheDocument()
    await act(async () => screen.getByRole('button', { name: 'Sign out' }).click())

    await waitFor(() => {
      expect(screen.getByText('Signed out')).toBeInTheDocument()
      expect(authMock.persistedSession).toBeNull()
    })
    expect(supabase.auth.signOut).toHaveBeenCalledOnce()
    await expect(supabase.auth.getSession()).resolves.toEqual({
      data: { session: null },
      error: null,
    })
  })

  it('keeps repeated cross-tab SIGNED_OUT events reconciled as signed out', async () => {
    renderAuthState()
    await waitFor(() => {
      expect(authMock.authStateChange).toBeTypeOf('function')
    })

    act(() => {
      emitAuthEvent('SIGNED_OUT', null)
      emitAuthEvent('SIGNED_OUT', null)
    })

    expect(await screen.findByText('Signed out')).toBeInTheDocument()
    expect(supabase.auth.setSession).not.toHaveBeenCalled()
  })

  it('removes old-user queries and prevents their pending result from being cached', async () => {
    const confirmedUser = (id) => ({
      id,
      email_confirmed_at: '2026-01-01T00:00:00Z',
    })
    let authStateChange
    supabase.auth.getSession.mockResolvedValue({
      data: { session: { user: confirmedUser('user-a') } },
      error: null,
    })
    supabase.auth.onAuthStateChange.mockImplementation((callback) => {
      authStateChange = callback
      return {
        data: { subscription: { unsubscribe: vi.fn() } },
      }
    })
    let activeSession = {
      data: {
        session: {
          access_token: 'user-a-token',
          user: confirmedUser('user-a'),
        },
      },
      error: null,
    }
    supabase.auth.getSession.mockImplementation(
      () => Promise.resolve(activeSession),
    )
    const fetchMock = vi.fn((_url, options) => {
      const authorization = new Headers(options.headers).get('Authorization')
      if (authorization === 'Bearer user-a-token') {
        return new Promise((_, reject) => {
          options.signal.addEventListener(
            'abort',
            () => reject(new DOMException('Aborted', 'AbortError')),
            { once: true },
          )
        })
      }

      return Promise.resolve(new Response('[]', {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const queryFn = vi.fn(() => api.get('/api/v1/conversations'))

    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AuthStateWithUserQuery queryFn={queryFn} />
        </AuthProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByText('user-a')).toBeInTheDocument()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce())
    expect(queryFn).toHaveBeenCalledOnce()

    activeSession = {
      data: {
        session: {
          access_token: 'user-b-token',
          user: confirmedUser('user-b'),
        },
      },
      error: null,
    }
    act(() => {
      authStateChange('SIGNED_IN', {
        user: confirmedUser('user-b'),
        access_token: 'user-b-session',
      })
    })
    expect(screen.getByText('user-b')).toBeInTheDocument()
    expect(queryClient.getQueryCache().find({
      queryKey: ['conversations', 'user-a'],
    })).toBeUndefined()
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true)

    await waitFor(() => {
      expect(queryClient.getQueryData(['conversations', 'user-b']))
        .toEqual([])
    })
    expect(queryClient.getQueryData(['conversations', 'user-a']))
      .toBeUndefined()
    expect(new Headers(fetchMock.mock.calls[1][1].headers)
      .get('Authorization')).toBe('Bearer user-b-token')
  })
})
