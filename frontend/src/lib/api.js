
import { supabase } from './supabase.js'
import { formatErrorMessage } from './errorMessage.js'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL

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

  return session.access_token
}

async function request(path, options = {}) {
  const startedAt = performance.now()
  const requestId = globalThis.crypto?.randomUUID?.() || ''
  let authDurationMs = 0
  let status = 'not_sent'
  let fetchStarted = false
  const { responseType, ...fetchOptions } = options

  try {
    const authStartedAt = performance.now()
    const accessToken = await getAccessToken()
    authDurationMs = performance.now() - authStartedAt

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

    fetchStarted = true
    const response = await fetch(
      `${API_BASE_URL.replace(/\/+$/, '')}${path}`,
      {
        ...fetchOptions,
        headers,
      }
    )
    status = response.status

    if (response.status === 204) {
      return null
    }

    const contentType = response.headers.get('content-type') || ''

    // Handle API errors before processing binary responses.
    if (!response.ok) {
      const responseData = contentType.includes('application/json')
        ? await response.json()
        : await response.text()

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

    // Return binary response for file downloads.
    if (responseType === 'blob') {
      return response.blob()
    }

    const responseData = contentType.includes('application/json')
      ? await response.json()
      : await response.text()

    return responseData
  } catch (error) {
    if (status === 'not_sent') {
      status = fetchStarted ? 'network_error' : 'auth_error'
    }
    const requestError = error instanceof Error
      ? error
      : new Error(formatErrorMessage(error))
    if (!requestError.requestId) {
      requestError.requestId = requestId || null
    }
    requestError.networkFailure = status === 'network_error'
    throw requestError
  } finally {
    console.info('api_request_completed', {
      requestId: requestId || null,
      method: fetchOptions.method || 'GET',
      status,
      authDurationMs: Number(authDurationMs.toFixed(2)),
      durationMs: Number((performance.now() - startedAt).toFixed(2)),
    })
  }
}

export const api = {
  get(path) {
    return request(path, {
      method: 'GET',
    })
  },

  getBlob(path) {
    return request(path, {
      method: 'GET',
      responseType: 'blob',
    })
  },

  post: (path, body, headers = {}) =>
    request(path, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    }),

  patch(path, body) {
    return request(path, {
      method: 'PATCH',
      body: JSON.stringify(body),
    })
  },

  delete(path) {
    return request(path, {
      method: 'DELETE',
    })
  },
}
