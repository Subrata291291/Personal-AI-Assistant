import { supabase } from './supabase.js'
import { formatErrorMessage } from './errorMessage.js'
import {
  API_REQUEST_TIMEOUT_MS,
  withTimeout,
} from './requestTimeout.js'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL
let authRevision = 0
const activeRequests = new Set()

if (!API_BASE_URL) {
  throw new Error(
    'Missing API configuration. Add VITE_API_BASE_URL to frontend/.env'
  )
}

async function getAccessToken() {
  const {
    data: { session },
    error,
  } = await supabase.auth.getSession()

  if (error) {
    throw new Error('Unable to retrieve your authentication session.')
  }

  if (!session?.access_token) {
    throw new Error('Authentication required. Please sign in again.')
  }

  return {
    accessToken: session.access_token,
    userId: session.user?.id,
  }
}

export function getApiAuthRevision() {
  return authRevision
}

export function notifyApiAuthChange() {
  authRevision += 1
  for (const request of activeRequests) {
    request.authChanged = true
    const error = new Error(
      'Your account changed while the request was in progress. Please try again.',
    )
    error.code = 'AUTH_SESSION_CHANGED'
    request.rejectAuthChange(error)
    request.controller.abort()
  }
}

function createApiResponseError(message, code, status) {
  const error = new Error(message)
  error.code = code
  error.status = status
  return error
}

function authSessionChangedError() {
  const error = new Error(
    'Your account changed while the request was starting. Please try again.',
  )
  error.code = 'AUTH_SESSION_CHANGED'
  return error
}

export function isApiCancellationError(error) {
  return (
    error?.code === 'REQUEST_CANCELLED' ||
    (
      error?.name === 'AbortError' &&
      !error?.timeout &&
      !error?.networkFailure
    )
  )
}

async function readResponseBody(response, requestId, timeoutOptions) {
  const contentType = response.headers.get('content-type') || ''
  let responseText
  try {
    responseText = await withTimeout(response.text(), timeoutOptions)
  } catch (error) {
    if (error?.timeout) {
      error.status = response.status
      throw error
    }
    if (!response.ok) return null
    const readError = createApiResponseError(
      'The API response could not be read. Please try again.',
      'API_RESPONSE_READ_FAILED',
      response.status,
    )
    readError.networkFailure = true
    readError.requestId = requestId || null
    throw readError
  }

  if (!contentType.includes('application/json')) {
    if (response.ok && !responseText.trim()) {
      const emptyResponse = createApiResponseError(
        'The server returned an empty response. Please try again.',
        'API_RESPONSE_EMPTY',
        response.status,
      )
      emptyResponse.requestId = requestId || null
      throw emptyResponse
    }
    return responseText
  }
  if (!responseText) {
    if (!response.ok) return null
    const emptyResponse = createApiResponseError(
      'The server returned an empty response. Please try again.',
      'API_RESPONSE_EMPTY',
      response.status,
    )
    emptyResponse.requestId = requestId || null
    throw emptyResponse
  }

  try {
    return JSON.parse(responseText)
  } catch {
    if (!response.ok) return responseText
    const invalidResponse = createApiResponseError(
      'The server returned an invalid response. Please try again.',
      'API_RESPONSE_INVALID',
      response.status,
    )
    invalidResponse.requestId = requestId || null
    throw invalidResponse
  }
}

async function request(path, options = {}) {
  const startedAt = performance.now()
  const startedTimestamp = new Date().toISOString()
  const requestId = globalThis.crypto?.randomUUID?.() || ''
  const deadline = Date.now() + API_REQUEST_TIMEOUT_MS
  const controller = new AbortController()
  let rejectAuthChange
  const authChangePromise = new Promise((_, reject) => {
    rejectAuthChange = reject
  })
  const activeRequest = {
    controller,
    authChanged: false,
    rejectAuthChange,
  }
  const requestAuthRevision = authRevision
  activeRequests.add(activeRequest)
  let authDurationMs = 0
  let status = 'not_sent'
  let abortSource = null
  let fetchStarted = false
  let phase = 'session'
  const {
    responseType,
    authContext,
    signal: callerSignal,
    ...fetchOptions
  } = options
  const abortFromCaller = () => {
    abortSource = 'caller'
    const reason = callerSignal?.reason
    if (reason instanceof DOMException) {
      controller.abort(reason)
    } else {
      controller.abort(
        typeof DOMException === 'function'
          ? new DOMException('The request was cancelled.', 'AbortError')
          : reason,
      )
    }
  }
  callerSignal?.addEventListener('abort', abortFromCaller, { once: true })
  const remainingTimeout = () =>
    Math.max(0, deadline - Date.now())

  try {
    if (callerSignal?.aborted) {
      abortSource = 'caller'
      throw new DOMException('The request was aborted.', 'AbortError')
    }
    if (
      authContext &&
      (
        authContext.authRevision !== requestAuthRevision ||
        typeof authContext.userId !== 'string' ||
        !authContext.userId
      )
    ) {
      throw authSessionChangedError()
    }

    const authStartedAt = performance.now()
    const { accessToken, userId } = await withTimeout(
      Promise.race([getAccessToken(), authChangePromise]),
      {
        timeoutMs: remainingTimeout(),
        message: 'Session verification timed out. Please try again.',
        getPhase: () => phase,
      },
    )
    authDurationMs = performance.now() - authStartedAt

    if (
      requestAuthRevision !== authRevision ||
      activeRequest.authChanged ||
      (
        authContext &&
        (
          authContext.authRevision !== authRevision ||
          authContext.userId !== userId
        )
      )
    ) {
      throw authSessionChangedError()
    }

    const headers = new Headers(fetchOptions.headers || {})

    headers.set('Authorization', `Bearer ${accessToken}`)

    if (requestId) headers.set('X-Request-ID', requestId)

    if (responseType !== 'blob') {
      headers.set('Accept', 'application/json')
    }

    if (
      fetchOptions.body !== undefined &&
      !(fetchOptions.body instanceof FormData)
    ) {
      headers.set('Content-Type', 'application/json')
    }

    if (
      requestAuthRevision !== authRevision ||
      activeRequest.authChanged ||
      (
        authContext &&
        (
          authContext.authRevision !== authRevision ||
          authContext.userId !== userId
        )
      )
    ) {
      throw authSessionChangedError()
    }

    phase = 'fetch'
    fetchStarted = true
    const response = await withTimeout(
      fetch(
        `${API_BASE_URL.replace(/\/+$/, '')}${path}`,
        {
          ...fetchOptions,
          headers,
          signal: controller.signal,
        }
      ),
      {
        timeoutMs: remainingTimeout(),
        message: 'The API request timed out. Please try again.',
        getPhase: () => phase,
        onTimeout: () => {
          abortSource = 'timeout'
          controller.abort(
            typeof DOMException === 'function'
              ? new DOMException('The API request timed out.', 'TimeoutError')
              : undefined,
          )
        },
      },
    )
    status = response.status

    if (response.status === 204) {
      return null
    }

    phase = 'response'

    if (response.ok && responseType === 'blob') {
      try {
        return await withTimeout(response.blob(), {
          timeoutMs: remainingTimeout(),
          message: 'The API request timed out. Please try again.',
          getPhase: () => phase,
          onTimeout: () => {
            abortSource = 'timeout'
            controller.abort(
              typeof DOMException === 'function'
                ? new DOMException('The API request timed out.', 'TimeoutError')
                : undefined,
            )
          },
        })
      } catch (error) {
        if (error?.timeout) error.status = response.status
        throw error
      }
    }

    const timeoutOptions = {
      timeoutMs: remainingTimeout(),
      message: 'The API request timed out. Please try again.',
      getPhase: () => phase,
      onTimeout: () => {
        abortSource = 'timeout'
        controller.abort(
          typeof DOMException === 'function'
            ? new DOMException('The API request timed out.', 'TimeoutError')
            : undefined,
        )
      },
    }
    let responseData

    try {
      responseData = await readResponseBody(
        response,
        requestId,
        timeoutOptions,
      )
    } catch (error) {
      if (error?.timeout || error?.code === 'API_RESPONSE_INVALID') {
        throw error
      }
      if (response.ok) throw error
      responseData = null
    }

    if (!response.ok) {
      const error = new Error(
        formatErrorMessage(
          responseData,
          `API request failed with status ${response.status}`,
        ),
      )
      error.status = response.status
      error.requestId = requestId || null
      throw error
    }

    return responseData
  } catch (error) {
    if (
      callerSignal?.aborted &&
      (error?.name === 'AbortError' || controller.signal.aborted)
    ) {
      status = 'cancelled'
    } else if (error?.timeout) {
      status = 'timeout'
    } else if (activeRequest.authChanged) {
      status = 'auth_changed'
    } else if (
      error?.networkFailure ||
      (
        ['response', 'session'].includes(phase) &&
        error instanceof TypeError
      )
    ) {
      status = 'network_error'
    } else if (status === 'not_sent') {
      status = fetchStarted ? 'network_error' : 'auth_error'
    }
    const requestError = error instanceof Error
      ? error
      : new Error(formatErrorMessage(error))
    if (!requestError.requestId) {
      requestError.requestId = requestId || null
    }
    if (typeof status === 'number' && requestError.status === undefined) {
      requestError.status = status
    }
    if (requestError.timeout && requestError.status === undefined) {
      requestError.status = null
    }
    requestError.networkFailure =
      status === 'network_error' || Boolean(requestError.networkFailure)
    if (activeRequest.authChanged) {
      requestError.code = 'AUTH_SESSION_CHANGED'
      requestError.networkFailure = false
    }
    if (status === 'cancelled') {
      requestError.code = 'REQUEST_CANCELLED'
      requestError.cancellationSource = 'caller'
      requestError.networkFailure = false
      if (!(requestError.name === 'AbortError')) {
        requestError.name = 'AbortError'
      }
    }
    throw requestError
  } finally {
    callerSignal?.removeEventListener('abort', abortFromCaller)
    activeRequests.delete(activeRequest)
    console.info('api_request_completed', {
      requestId: requestId || null,
      method: fetchOptions.method || 'GET',
      path: String(path).split('?')[0],
      status,
      phase,
      abortSource,
      startedAt: startedTimestamp,
      completedAt: new Date().toISOString(),
      authDurationMs: Number(authDurationMs.toFixed(2)),
      durationMs: Number((performance.now() - startedAt).toFixed(2)),
    })
  }
}

export const api = {
  get(path, options = {}) {
    return request(path, {
      method: 'GET',
      ...options,
    })
  },

  getBlob(path) {
    return request(path, {
      method: 'GET',
      responseType: 'blob',
    })
  },

  post: (path, body, headers = {}, authContext) =>
    request(path, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      authContext,
    }),

  patch(path, body, authContext) {
    return request(path, {
      method: 'PATCH',
      body: JSON.stringify(body),
      authContext,
    })
  },

  delete(path, authContext) {
    return request(path, {
      method: 'DELETE',
      authContext,
    })
  },
}
