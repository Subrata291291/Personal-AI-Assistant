const configuredTimeout = Number(
  import.meta.env.VITE_API_REQUEST_TIMEOUT_MS,
)

export const API_REQUEST_TIMEOUT_MS =
  Number.isFinite(configuredTimeout) && configuredTimeout >= 1000
    ? Math.min(configuredTimeout, 120_000)
    : 30_000

export function createRequestTimeoutError(
  message = 'The request timed out. Please try again.',
  phase,
) {
  const error = new Error(message)
  error.name = 'TimeoutError'
  error.code = 'REQUEST_TIMEOUT'
  error.timeout = true
  if (phase) error.phase = phase
  return error
}

export function withAuthTimeout(promise, operation) {
  return withTimeout(promise, {
    message: `${operation} timed out. The operation may still complete; check your account state before retrying.`,
    getPhase: () => 'auth',
  })
}

export function withTimeout(
  promise,
  {
    timeoutMs = API_REQUEST_TIMEOUT_MS,
    message,
    getPhase,
    onTimeout,
  } = {},
) {
  let timeoutId
  const timeoutPromise = new Promise((_, reject) => {
    timeoutId = setTimeout(() => {
      onTimeout?.()
      reject(createRequestTimeoutError(message, getPhase?.()))
    }, timeoutMs)
  })

  return Promise.race([promise, timeoutPromise]).finally(() => {
    clearTimeout(timeoutId)
  })
}
