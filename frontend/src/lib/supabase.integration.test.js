import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'

class InMemoryStorage {
  values = new Map()
  failRemoveKey = null

  get length() {
    return this.values.size
  }

  clear() {
    this.values.clear()
  }

  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null
  }

  key(index) {
    return Array.from(this.values.keys())[index] ?? null
  }

  removeItem(key) {
    if (key === this.failRemoveKey) {
      throw new Error('Storage removal failed.')
    }
    this.values.delete(key)
  }

  setItem(key, value) {
    this.values.set(key, String(value))
  }
}

class DeterministicLockManager {
  tails = new Map()
  active = new Set()
  requests = []

  request = (name, options, callback) => {
    this.requests.push({ name, options })
    if (options?.ifAvailable && this.active.has(name)) {
      return Promise.resolve(callback(null))
    }

    const previous = this.tails.get(name) || Promise.resolve()
    let release
    const turn = new Promise((resolve) => {
      release = resolve
    })
    const tail = previous.then(() => turn)
    this.tails.set(name, tail)

    return previous
      .then(async () => {
        this.active.add(name)
        try {
          return await callback({ name })
        } finally {
          this.active.delete(name)
        }
      })
      .finally(() => {
        release()
        if (this.tails.get(name) === tail) this.tails.delete(name)
      })
  }
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

function encodeBase64Url(value) {
  return btoa(value)
    .replace(/=/g, '')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
}

function accessToken(userId, expiresIn = 3600) {
  const header = encodeBase64Url(JSON.stringify({ alg: 'none', typ: 'JWT' }))
  const payload = encodeBase64Url(JSON.stringify({
    aud: 'authenticated',
    exp: Math.floor(Date.now() / 1000) + expiresIn,
    sub: userId,
    role: 'authenticated',
  }))
  return `${header}.${payload}.test-signature`
}

function userForToken(token) {
  const payload = JSON.parse(
    atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')),
  )
  return {
    id: payload.sub,
    aud: 'authenticated',
    role: 'authenticated',
    email: `${payload.sub}@example.test`,
    app_metadata: { provider: 'email', providers: ['email'] },
    user_metadata: {},
    created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z',
  }
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const USER_A = '11111111-1111-4111-8111-111111111111'
const USER_B = '22222222-2222-4222-8222-222222222222'
let testClientNumber = 0

describe('Supabase Auth SDK cleanup integration', () => {
  let originalStorageDescriptor
  let originalLocksDescriptor
  let storage
  let locks
  let supabase
  let signOutIfSessionMatches
  let authRequestTimeoutMs
  let lockName
  let requests
  let handleLogout
  let handleRefresh
  let unsubscribe

  beforeEach(async () => {
    vi.resetModules()
    testClientNumber += 1
    vi.stubEnv(
      'VITE_SUPABASE_URL',
      `https://project-ref-${testClientNumber}.supabase.co`,
    )
    vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'local-test-anon-key')

    originalStorageDescriptor = Object.getOwnPropertyDescriptor(
      window,
      'localStorage',
    )
    storage = new InMemoryStorage()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: storage,
    })

    originalLocksDescriptor = Object.getOwnPropertyDescriptor(
      globalThis.navigator,
      'locks',
    )
    locks = new DeterministicLockManager()
    Object.defineProperty(globalThis.navigator, 'locks', {
      configurable: true,
      value: locks,
    })

    requests = []
    handleLogout = () => Promise.resolve(new Response(null, { status: 204 }))
    handleRefresh = () => Promise.resolve(jsonResponse({}))
    vi.stubGlobal('fetch', vi.fn(async (input, options = {}) => {
      const url = new URL(String(input))
      requests.push({ url: url.href, method: options.method || 'GET' })

      if (url.pathname.endsWith('/auth/v1/user')) {
        const authorization = new Headers(options.headers).get('authorization')
        const token = authorization?.replace(/^Bearer /i, '')
        if (!token) return jsonResponse({ message: 'Missing token' }, 401)
        return jsonResponse(userForToken(token))
      }

      if (url.pathname.endsWith('/auth/v1/logout')) {
        return handleLogout(url, options)
      }

      if (url.pathname.endsWith('/auth/v1/token')) {
        if (url.searchParams.get('grant_type') === 'password') {
          const token = accessToken(USER_B)
          return jsonResponse({
            access_token: token,
            refresh_token: `refresh-${USER_B}`,
            expires_in: 3600,
            token_type: 'bearer',
            user: userForToken(token),
          })
        }
        return handleRefresh(url, options)
      }

      throw new Error(`Unexpected test network request: ${url.pathname}`)
    }))

    ;({ supabase, signOutIfSessionMatches, SUPABASE_AUTH_REQUEST_TIMEOUT_MS: authRequestTimeoutMs } =
      await import('./supabase.js'))
    await supabase.auth.initialize()
    lockName = `lock:${supabase.auth.storageKey}`
    unsubscribe = null
  })

  afterEach(async () => {
    vi.useRealTimers()
    unsubscribe?.()
    await supabase?.auth.dispose()
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()

    if (originalStorageDescriptor) {
      Object.defineProperty(window, 'localStorage', originalStorageDescriptor)
    } else {
      delete window.localStorage
    }

    if (originalLocksDescriptor) {
      Object.defineProperty(globalThis.navigator, 'locks', originalLocksDescriptor)
    } else {
      delete globalThis.navigator.locks
    }
  })

  async function persistSession(userId, token = accessToken(userId)) {
    const result = await supabase.auth.setSession({
      access_token: token,
      refresh_token: `refresh-${userId}`,
    })
    expect(result.error).toBeNull()
    return result.data.session
  }

  async function readPersistedSession() {
    const { data, error } = await supabase.auth.getSession()
    expect(error).toBeNull()
    return data.session
  }

  function recordAuthEvents() {
    const events = []
    const { data } = supabase.auth.onAuthStateChange((event, session) => {
      events.push({ event, session })
    })
    unsubscribe = () => data.subscription.unsubscribe()
    return events
  }

  it('guards private SDK cleanup assumptions and emits SIGNED_OUT through the real SDK', async () => {
    expect(supabase.auth.__loadSession).toBeTypeOf('function')
    expect(supabase.auth._signOut).toBeTypeOf('function')
    const session = await persistSession(USER_A)
    const events = recordAuthEvents()
    storage.setItem('unrelated-entry', 'preserve-me')
    const lockRequestCount = locks.requests.filter(
      ({ name }) => name === lockName,
    ).length

    await expect(
      signOutIfSessionMatches(session, () => true),
    ).resolves.toBe(true)

    expect(storage.getItem(supabase.auth.storageKey)).toBeNull()
    expect(storage.getItem(`${supabase.auth.storageKey}-user`)).toBeNull()
    expect(storage.getItem('unrelated-entry')).toBe('preserve-me')
    expect(events.filter(({ event }) => event === 'SIGNED_OUT')).toHaveLength(1)
    expect(locks.requests[lockRequestCount]).toEqual({
      name: lockName,
      options: { mode: 'exclusive' },
    })
    expect(locks.active.size).toBe(0)
  })

  it('does not remove or emit SIGNED_OUT for a newer replacement session', async () => {
    const sessionA = await persistSession(USER_A)
    const events = recordAuthEvents()
    const sessionB = await persistSession(USER_B)
    events.length = 0

    await expect(
      signOutIfSessionMatches(sessionA, () => true),
    ).resolves.toBe(false)

    const activeSession = await readPersistedSession()
    expect(activeSession.user.id).toBe(USER_B)
    expect(activeSession.access_token).toBe(sessionB.access_token)
    expect(events.filter(({ event }) => event === 'SIGNED_OUT')).toHaveLength(0)
  })

  it('queues an SDK session writer behind cleanup and leaves the newer session active', async () => {
    const sessionA = await persistSession(USER_A)
    const logoutStarted = deferred()
    const finishLogout = deferred()
    handleLogout = () => {
      logoutStarted.resolve()
      return finishLogout.promise
    }

    const cleanup = signOutIfSessionMatches(sessionA, () => true)
    await logoutStarted.promise

    const writer = supabase.auth.setSession({
      access_token: accessToken(USER_B),
      refresh_token: `refresh-${USER_B}`,
    })
    await vi.waitFor(() => {
      expect(locks.requests.filter(({ name }) => name === lockName).length)
        .toBeGreaterThanOrEqual(4)
    })
    expect(JSON.parse(storage.getItem(lockName.replace(/^lock:/, ''))).user.id)
      .toBe(USER_A)

    finishLogout.resolve(new Response(null, { status: 204 }))
    await expect(cleanup).resolves.toBe(true)
    const writerResult = await writer
    expect(writerResult.error).toBeNull()
    expect((await readPersistedSession()).user.id).toBe(USER_B)
    await vi.waitFor(() => expect(locks.active.size).toBe(0))
  })

  it('serializes the app-wrapped real password sign-in on the shared lock', async () => {
    const lockEntered = deferred()
    const releaseLock = deferred()
    const lockRequest = navigator.locks.request(
      lockName,
      { mode: 'exclusive' },
      () => {
        lockEntered.resolve()
        return releaseLock.promise
      },
    )
    await lockEntered.promise

    const signIn = supabase.auth.signInWithPassword({
      email: 'user-b@example.test',
      password: 'local-test-password',
    })
    await vi.waitFor(() => {
      expect(locks.requests.filter(({ name }) => name === lockName).length)
        .toBeGreaterThanOrEqual(3)
    })
    expect(requests.some(({ url }) =>
      url.includes('/auth/v1/token?grant_type=password'),
    )).toBe(false)

    releaseLock.resolve()
    await lockRequest
    const result = await signIn
    expect(result.error).toBeNull()
    expect((await readPersistedSession()).user.id).toBe(USER_B)
    expect(requests.some(({ url }) =>
      url.includes('/auth/v1/token?grant_type=password'),
    )).toBe(true)
  })

  it('aborts a stalled SDK auth request and releases the lock for later work', async () => {
    await persistSession(USER_A)
    vi.useFakeTimers()
    let requestAborted = false
    handleRefresh = (_url, options) => new Promise((_resolve, reject) => {
      const onAbort = () => {
        requestAborted = true
        const error = new Error('The local test request was aborted.')
        error.name = 'AbortError'
        reject(error)
      }
      if (options.signal.aborted) {
        onAbort()
      } else {
        options.signal.addEventListener('abort', onAbort, { once: true })
      }
    })

    const refresh = supabase.auth.refreshSession()
    await vi.waitFor(() => {
      expect(requests.some(({ url }) =>
        url.includes('/auth/v1/token?grant_type=refresh_token'),
      )).toBe(true)
    })
    await vi.advanceTimersByTimeAsync(authRequestTimeoutMs)

    const result = await refresh
    expect(requestAborted).toBe(true)
    expect(result.error).toBeTruthy()
    await vi.waitFor(() => expect(locks.active.size).toBe(0))

    const writerResult = await supabase.auth.setSession({
      access_token: accessToken(USER_B),
      refresh_token: `refresh-${USER_B}`,
    })
    expect(writerResult.error).toBeNull()
    expect((await readPersistedSession()).user.id).toBe(USER_B)
    expect(locks.active.size).toBe(0)
  })

  it('keeps the auth deadline active while the SDK consumes a response body', async () => {
    await persistSession(USER_A)
    vi.useFakeTimers()
    let responseBodyAborted = false
    handleRefresh = (_url, options) => {
      const body = new ReadableStream({
        start(controller) {
          options.signal.addEventListener('abort', () => {
            responseBodyAborted = true
            controller.error(new Error('The local response body was aborted.'))
          }, { once: true })
        },
      })
      return Promise.resolve(new Response(body, {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }))
    }

    const refresh = supabase.auth.refreshSession()
    await vi.waitFor(() => {
      expect(requests.some(({ url }) =>
        url.includes('/auth/v1/token?grant_type=refresh_token'),
      )).toBe(true)
    })
    await vi.advanceTimersByTimeAsync(authRequestTimeoutMs + 1_000)

    const result = await refresh
    expect(responseBodyAborted).toBe(true)
    expect(result.error).toBeTruthy()
    expect(locks.active.size).toBe(0)
  })

  it('releases the shared lock and rejects when storage cleanup fails', async () => {
    const session = await persistSession(USER_A)
    storage.failRemoveKey = supabase.auth.storageKey

    await expect(
      signOutIfSessionMatches(session, () => true),
    ).rejects.toThrow('Storage removal failed.')

    expect(locks.active.size).toBe(0)
    await expect(
      navigator.locks.request(lockName, { mode: 'exclusive' }, () => 'released'),
    ).resolves.toBe('released')
  })

  it('does not report success when the SDK returns a sign-out error', async () => {
    const session = await persistSession(USER_A)
    handleLogout = () => jsonResponse({ message: 'local test failure' }, 500)

    await expect(
      signOutIfSessionMatches(session, () => true),
    ).rejects.toThrow()

    expect(storage.getItem(supabase.auth.storageKey)).toBeNull()
    await vi.waitFor(() => expect(locks.active.size).toBe(0))
    await expect(
      navigator.locks.request(lockName, { mode: 'exclusive' }, () => 'released'),
    ).resolves.toBe('released')
  })

  it('keeps the SDK lock until logout finishes even after the caller times out', async () => {
    const sessionA = await persistSession(USER_A)
    const logoutStarted = deferred()
    const finishLogout = deferred()
    handleLogout = () => {
      logoutStarted.resolve()
      return finishLogout.promise
    }

    const cleanup = signOutIfSessionMatches(sessionA, () => true)
    await logoutStarted.promise
    vi.stubEnv('VITE_API_REQUEST_TIMEOUT_MS', '1000')
    const { withAuthTimeout } = await import('./requestTimeout.js')
    vi.useFakeTimers()
    const timedCleanup = withAuthTimeout(cleanup, 'Session cleanup')
    const timeoutAssertion = expect(timedCleanup).rejects.toMatchObject({
      timeout: true,
      phase: 'auth',
    })

    await vi.advanceTimersByTimeAsync(1000)
    await timeoutAssertion
    expect(locks.active.has(lockName)).toBe(true)

    const writer = supabase.auth.setSession({
      access_token: accessToken(USER_B),
      refresh_token: `refresh-${USER_B}`,
    })
    await vi.waitFor(() => {
      expect(locks.requests.filter(({ name }) => name === lockName).length)
        .toBeGreaterThanOrEqual(4)
    })
    finishLogout.resolve(new Response(null, { status: 204 }))
    await expect(cleanup).resolves.toBe(true)
    expect((await writer).error).toBeNull()
    expect((await readPersistedSession()).user.id).toBe(USER_B)
    await vi.waitFor(() => expect(locks.active.size).toBe(0))
  })

  it('serializes an actual SDK refresh before stale cleanup and keeps the refreshed session', async () => {
    const sessionA = await persistSession(USER_A)
    const refreshStarted = deferred()
    const finishRefresh = deferred()
    handleRefresh = () => {
      refreshStarted.resolve()
      return finishRefresh.promise
    }

    const events = recordAuthEvents()
    const refresh = supabase.auth.refreshSession()
    await refreshStarted.promise

    const cleanup = signOutIfSessionMatches(sessionA, () => true)
    await vi.waitFor(() => {
      expect(locks.requests.filter(({ name }) => name === lockName).length)
        .toBeGreaterThanOrEqual(4)
    })

    const refreshedToken = accessToken(USER_A, 7200)
    finishRefresh.resolve(jsonResponse({
      access_token: refreshedToken,
      refresh_token: 'refresh-user-a-rotated',
      expires_in: 3600,
      token_type: 'bearer',
      user: userForToken(refreshedToken),
    }))

    const refreshResult = await refresh
    expect(refreshResult.error).toBeNull()
    await expect(cleanup).resolves.toBe(false)
    const activeSession = await readPersistedSession()
    expect(activeSession.access_token).toBe(refreshedToken)
    expect(events.some(({ event }) => event === 'TOKEN_REFRESHED')).toBe(true)
    expect(events.some(({ event }) => event === 'SIGNED_OUT')).toBe(false)
    await vi.waitFor(() => expect(locks.active.size).toBe(0))
  })

  it('restores a persisted SDK session and refreshes it under the configured lock', async () => {
    const session = await persistSession(USER_A)
    await supabase.auth.dispose()

    vi.resetModules()
    ;({ supabase, signOutIfSessionMatches } =
      await import('./supabase.js'))
    await supabase.auth.initialize()

    const restoredSession = await readPersistedSession()
    expect(restoredSession.user.id).toBe(USER_A)
    expect(locks.requests.some(({ name }) => name === lockName)).toBe(true)

    const refreshedToken = accessToken(USER_A, 7200)
    handleRefresh = () => Promise.resolve(jsonResponse({
      access_token: refreshedToken,
      refresh_token: 'refresh-user-a-restored',
      expires_in: 7200,
      token_type: 'bearer',
      user: userForToken(refreshedToken),
    }))
    const result = await supabase.auth.refreshSession({
      refresh_token: session.refresh_token,
    })

    expect(result.error).toBeNull()
    expect((await readPersistedSession()).access_token).toBe(refreshedToken)
    expect(locks.requests.filter(({ name }) => name === lockName).length)
      .toBeGreaterThanOrEqual(3)
  })

  it('makes repeated stale cleanup calls no-ops after successful removal', async () => {
    const session = await persistSession(USER_A)
    const events = recordAuthEvents()
    events.length = 0

    await expect(
      signOutIfSessionMatches(session, () => true),
    ).resolves.toBe(true)
    await expect(
      signOutIfSessionMatches(session, () => true),
    ).resolves.toBe(false)

    expect(events.filter(({ event }) => event === 'SIGNED_OUT')).toHaveLength(1)
  })
})
