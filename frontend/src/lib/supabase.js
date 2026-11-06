import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const supabaseKey = import.meta.env.VITE_SUPABASE_ANON_KEY

if (!supabaseUrl || !supabaseKey) {
  throw new Error(
    'Missing Supabase configuration. Check frontend/.env'
  )
}

const projectRef = new URL(supabaseUrl).hostname.split('.')[0]
const storageKey = `sb-${projectRef}-auth-token`
const authLockName = `lock:${storageKey}`
export const SUPABASE_AUTH_REQUEST_TIMEOUT_MS = 30_000

function createAuthRequestFetch(fetchImpl) {
  const projectUrl = new URL(supabaseUrl)
  const authPathPrefix = new URL(
    'auth/v1/',
    projectUrl.href.endsWith('/') ? projectUrl.href : `${projectUrl.href}/`,
  ).pathname

  return async (input, init = {}) => {
    const requestUrl = new URL(
      input instanceof Request ? input.url : input,
      projectUrl,
    )
    if (
      requestUrl.origin !== projectUrl.origin ||
      !requestUrl.pathname.startsWith(authPathPrefix)
    ) {
      return fetchImpl(input, init)
    }

    const controller = new AbortController()
    const callerSignal = init.signal
    const abortFromCaller = () => controller.abort(callerSignal.reason)
    if (callerSignal?.aborted) {
      abortFromCaller()
    } else {
      callerSignal?.addEventListener('abort', abortFromCaller, { once: true })
    }

    const timeoutId = setTimeout(
      () => controller.abort(new DOMException(
        'Supabase Auth request exceeded its deadline.',
        'TimeoutError',
      )),
      SUPABASE_AUTH_REQUEST_TIMEOUT_MS,
    )
    let cleanedUp = false
    const cleanup = () => {
      if (cleanedUp) return
      cleanedUp = true
      clearTimeout(timeoutId)
      callerSignal?.removeEventListener('abort', abortFromCaller)
    }

    try {
      const response = await fetchImpl(input, {
        ...init,
        signal: controller.signal,
      })
      const bodyMethods = new Set([
        'arrayBuffer',
        'blob',
        'formData',
        'json',
        'text',
      ])
      return new Proxy(response, {
        get(target, property) {
          const value = Reflect.get(target, property, target)
          if (typeof value !== 'function') return value

          if (bodyMethods.has(property)) {
            return (...args) => {
              try {
                return Promise.resolve(value.apply(target, args)).finally(cleanup)
              } catch (error) {
                cleanup()
                throw error
              }
            }
          }
          return value.bind(target)
        },
      })
    } catch (error) {
      cleanup()
      throw error
    }
  }
}

export function authMutationLockAvailable() {
  return typeof globalThis.navigator?.locks?.request === 'function'
}

function runWithAuthMutationLock(operation) {
  if (!authMutationLockAvailable()) return operation()
  return globalThis.navigator.locks.request(
    authLockName,
    { mode: 'exclusive' },
    operation,
  )
}

export const supabase = createClient(
  supabaseUrl,
  supabaseKey,
  {
    auth: {
      autoRefreshToken: true,
      persistSession: true,
      detectSessionInUrl: true,
      storageKey,
      lockAcquireTimeout: -1,
      lock: (name, acquireTimeout, operation) => {
        if (!authMutationLockAvailable()) return operation()
        if (acquireTimeout === 0) {
          return globalThis.navigator.locks.request(
            name,
            { mode: 'exclusive', ifAvailable: true },
            (lock) => lock ? operation() : undefined,
          )
        }

        return globalThis.navigator.locks.request(
          name,
          { mode: 'exclusive' },
          operation,
        )
      },
    },
    global: {
      fetch: createAuthRequestFetch(globalThis.fetch.bind(globalThis)),
    },
  },
)

for (const methodName of [
  'signUp',
  'signInWithPassword',
  'signInAnonymously',
  'signInWithIdToken',
  'verifyOtp',
]) {
  const originalMethod = supabase.auth[methodName].bind(supabase.auth)
  supabase.auth[methodName] = (...args) =>
    runWithAuthMutationLock(() => originalMethod(...args))
}

export function signOutIfSessionMatches(expectedSession, shouldContinue) {
  if (!authMutationLockAvailable()) return Promise.resolve(false)

  const expectedUserId = expectedSession?.user?.id
  const expectedAccessToken = expectedSession?.access_token
  if (
    typeof expectedUserId !== 'string' ||
    !expectedUserId ||
    typeof expectedAccessToken !== 'string' ||
    !expectedAccessToken
  ) {
    return Promise.resolve(false)
  }

  return runWithAuthMutationLock(async () => {
    if (!shouldContinue()) return false

    // These lock-free SDK internals avoid re-acquiring the shared Web Lock.
    const {
      data: { session },
      error,
    } = await supabase.auth.__loadSession()

    if (error) throw error
    if (
      !shouldContinue(session) ||
      session?.user?.id !== expectedUserId ||
      session.access_token !== expectedAccessToken
    ) {
      return false
    }

    const { error: signOutError } = await supabase.auth._signOut()
    if (signOutError) throw signOutError
    return true
  })
}
