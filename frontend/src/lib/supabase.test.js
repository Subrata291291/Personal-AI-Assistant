import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const authMock = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  signInAnonymously: vi.fn(),
  signInWithIdToken: vi.fn(),
  signUp: vi.fn(),
  verifyOtp: vi.fn(),
  loadSession: vi.fn(),
  signOutLocked: vi.fn(),
  persistedSession: null,
  createClient: vi.fn(),
  options: null,
}))

vi.mock('@supabase/supabase-js', () => ({
  createClient: authMock.createClient,
}))

function createLockManager() {
  const tails = new Map()
  return {
    request: vi.fn((name, _options, callback) => {
      const previous = tails.get(name) || Promise.resolve()
      let release
      const currentTurn = new Promise((resolve) => {
        release = resolve
      })
      const tail = previous.then(() => currentTurn)
      tails.set(name, tail)

      return previous
        .then(() => callback({ name }))
        .finally(() => {
          release()
          if (tails.get(name) === tail) tails.delete(name)
        })
    }),
  }
}

describe('Supabase auth mutation coordination', () => {
  let originalLocks

  beforeEach(() => {
    vi.resetModules()
    vi.stubEnv('VITE_SUPABASE_URL', 'https://project-ref.supabase.co')
    vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'test-anon-key')
    authMock.options = null
    authMock.createClient.mockReset()
    authMock.createClient.mockImplementation((_url, _key, options) => {
      authMock.options = options
      return {
        auth: {
          __loadSession: authMock.loadSession,
          _signOut: authMock.signOutLocked,
          signInWithPassword: authMock.signInWithPassword,
          signInAnonymously: authMock.signInAnonymously,
          signInWithIdToken: authMock.signInWithIdToken,
          signUp: authMock.signUp,
          verifyOtp: authMock.verifyOtp,
        },
      }
    })
    for (const method of [
      authMock.signInWithPassword,
      authMock.signInAnonymously,
      authMock.signInWithIdToken,
      authMock.signUp,
      authMock.verifyOtp,
    ]) {
      method.mockReset()
      method.mockResolvedValue({ data: {}, error: null })
    }
    authMock.loadSession.mockReset()
    authMock.signOutLocked.mockReset()
    authMock.persistedSession = null
    authMock.loadSession.mockImplementation(async () => ({
      data: { session: authMock.persistedSession },
      error: null,
    }))
    authMock.signOutLocked.mockImplementation(async () => {
      authMock.persistedSession = null
      return { error: null }
    })

    originalLocks = Object.getOwnPropertyDescriptor(
      globalThis.navigator,
      'locks',
    )
    Object.defineProperty(globalThis.navigator, 'locks', {
      configurable: true,
      value: createLockManager(),
    })
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    if (originalLocks) {
      Object.defineProperty(globalThis.navigator, 'locks', originalLocks)
    } else {
      delete globalThis.navigator.locks
    }
  })

  it('serializes password sign-in behind SDK-locked session mutations', async () => {
    const { supabase } = await import('./supabase.js')
    const lockName = `lock:${authMock.options.auth.storageKey}`
    let finishLockedOperation
    let markLockAcquired
    const lockAcquired = new Promise((resolve) => {
      markLockAcquired = resolve
    })

    const lockedOperation = authMock.options.auth.lock(
      lockName,
      -1,
      () => new Promise((resolve) => {
        finishLockedOperation = resolve
        markLockAcquired()
      }),
    )
    await lockAcquired
    const signInOperation = supabase.auth.signInWithPassword({
      email: 'user@example.test',
      password: 'password',
    })

    expect(authMock.signInWithPassword).not.toHaveBeenCalled()
    expect(authMock.options.auth.storageKey)
      .toBe('sb-project-ref-auth-token')

    finishLockedOperation('complete')
    await expect(lockedOperation).resolves.toBe('complete')
    await expect(signInOperation).resolves.toEqual({
      data: {},
      error: null,
    })
    expect(authMock.signInWithPassword).toHaveBeenCalledOnce()
  })

  it('keeps opportunistic SDK locks non-blocking', async () => {
    await import('./supabase.js')
    const operation = vi.fn().mockResolvedValue(undefined)

    await authMock.options.auth.lock(
      `lock:${authMock.options.auth.storageKey}`,
      0,
      operation,
    )

    expect(globalThis.navigator.locks.request).toHaveBeenCalledWith(
      `lock:${authMock.options.auth.storageKey}`,
      { mode: 'exclusive', ifAvailable: true },
      expect.any(Function),
    )
    expect(operation).toHaveBeenCalledOnce()
  })

  it('checks session identity and signs out under one shared lock', async () => {
    const { signOutIfSessionMatches } = await import('./supabase.js')
    const session = {
      access_token: 'session-a-token',
      user: { id: 'user-a' },
    }
    authMock.persistedSession = session

    await expect(
      signOutIfSessionMatches(session, () => true),
    ).resolves.toBe(true)

    expect(authMock.loadSession).toHaveBeenCalledOnce()
    expect(authMock.signOutLocked).toHaveBeenCalledOnce()
    expect(globalThis.navigator.locks.request).toHaveBeenCalledOnce()
    expect(authMock.persistedSession).toBeNull()
  })

  it('does not sign out a session with a different identity', async () => {
    const { signOutIfSessionMatches } = await import('./supabase.js')
    const originalSession = {
      access_token: 'session-a-token',
      user: { id: 'user-a' },
    }
    authMock.persistedSession = {
      access_token: 'session-b-token',
      user: { id: 'user-a' },
    }

    await expect(
      signOutIfSessionMatches(originalSession, () => true),
    ).resolves.toBe(false)

    expect(authMock.signOutLocked).not.toHaveBeenCalled()
    expect(authMock.persistedSession.access_token).toBe('session-b-token')
  })

  it('fails closed when Web Locks are unavailable', async () => {
    const { signOutIfSessionMatches } = await import('./supabase.js')
    const session = {
      access_token: 'session-a-token',
      user: { id: 'user-a' },
    }
    authMock.persistedSession = session
    delete globalThis.navigator.locks

    await expect(
      signOutIfSessionMatches(session, () => true),
    ).resolves.toBe(false)

    expect(authMock.loadSession).not.toHaveBeenCalled()
    expect(authMock.signOutLocked).not.toHaveBeenCalled()
    expect(authMock.persistedSession).toBe(session)
  })
})
