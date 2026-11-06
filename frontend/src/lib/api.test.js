import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'

vi.mock('./supabase.js', () => ({
  supabase: {
    auth: {
      getSession: vi.fn(),
    },
  },
}))

import { supabase } from './supabase.js'

let api
let notifyApiAuthChange
let getApiAuthRevision

beforeAll(async () => {
  vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.test')
  vi.stubEnv('VITE_API_REQUEST_TIMEOUT_MS', '1000')
  ;({ api, notifyApiAuthChange, getApiAuthRevision } =
    await import('./api.js'))
})

describe('API request handling', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  beforeEach(() => {
    vi.clearAllMocks()
    supabase.auth.getSession.mockResolvedValue({
      data: { session: { access_token: 'test-access-token' } },
      error: null,
    })
    vi.spyOn(console, 'info').mockImplementation(() => {})
  })

  it('uses the configured API origin and preserves authorization and idempotency headers', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ proposal: { proposal_id: 'proposal-1' } }),
      {
        status: 201,
        headers: { 'content-type': 'application/json' },
      },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await api.post(
      '/api/v1/action-approvals/gmail-drafts/message-1',
      { connection_id: 'connection-1' },
      { 'Idempotency-Key': 'e747824d-1429-4ad0-9f22-aefb8688ccca' },
    )

    const [url, options] = fetchMock.mock.calls[0]
    const headers = new Headers(options.headers)
    expect(url).toBe(
      'https://api.example.test/api/v1/action-approvals/gmail-drafts/message-1',
    )
    expect(headers.get('Authorization')).toBe('Bearer test-access-token')
    expect(headers.get('Idempotency-Key')).toBe(
      'e747824d-1429-4ad0-9f22-aefb8688ccca',
    )
    expect(headers.get('X-Request-ID')).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    )
  })

  it('formats structured FastAPI errors and attaches the correlation ID', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({
        detail: [
          {
            loc: ['body', 'subject'],
            msg: 'String should have at most 500 characters',
            input: 'private message',
          },
        ],
      }),
      {
        status: 422,
        headers: { 'content-type': 'application/json' },
      },
    )))

    await expect(api.post('/api/v1/action-approvals', {}))
      .rejects.toMatchObject({
        status: 422,
        message: 'body.subject: String should have at most 500 characters',
        requestId: expect.any(String),
        networkFailure: false,
      })
  })

  it('marks fetch failures as network failures without hiding request correlation', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(
      new TypeError('Failed to fetch'),
    ))

    await expect(api.post('/api/v1/action-approvals', {}))
      .rejects.toMatchObject({
        message: 'Failed to fetch',
        networkFailure: true,
        requestId: expect.any(String),
      })
  })

  it('surfaces session retrieval rejection without sending the API request', async () => {
    supabase.auth.getSession.mockRejectedValue(new Error('Session unavailable'))
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.get('/api/v1/oauth/connections'))
      .rejects.toMatchObject({
        message: 'Session unavailable',
        networkFailure: false,
        requestId: expect.any(String),
      })

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('cancels a request still waiting for the session when the account changes', async () => {
    let resolveSession
    supabase.auth.getSession.mockReturnValue(new Promise((resolve) => {
      resolveSession = resolve
    }))
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const request = api.get('/api/v1/oauth/connections')
    notifyApiAuthChange()

    await expect(request).rejects.toMatchObject({
      code: 'AUTH_SESSION_CHANGED',
      networkFailure: false,
    })

    resolveSession({
      data: {
        session: {
          access_token: 'user-b-token',
          user: { id: 'user-b' },
        },
      },
      error: null,
    })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects a mutation whose initiating identity no longer matches the session', async () => {
    supabase.auth.getSession.mockResolvedValue({
      data: {
        session: {
          access_token: 'user-b-token',
          user: { id: 'user-b' },
        },
      },
      error: null,
    })
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.patch(
      '/api/v1/me/settings',
      { memory_enabled: false },
      { userId: 'user-a', authRevision: getApiAuthRevision() },
    )).rejects.toMatchObject({
      code: 'AUTH_SESSION_CHANGED',
      networkFailure: false,
    })

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects an initiating revision changed while the session is resolving', async () => {
    let resolveSession
    supabase.auth.getSession.mockReturnValue(new Promise((resolve) => {
      resolveSession = resolve
    }))
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const request = api.patch(
      '/api/v1/me/settings',
      { memory_enabled: false },
      { userId: 'user-a', authRevision: getApiAuthRevision() },
    )
    notifyApiAuthChange()

    await expect(request).rejects.toMatchObject({
      code: 'AUTH_SESSION_CHANGED',
      networkFailure: false,
    })

    resolveSession({
      data: {
        session: {
          access_token: 'user-b-token',
          user: { id: 'user-b' },
        },
      },
      error: null,
    })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('classifies a session lookup network failure separately from auth failure', async () => {
    supabase.auth.getSession.mockRejectedValue(new TypeError('Network unavailable'))
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.get('/api/v1/oauth/connections'))
      .rejects.toMatchObject({
        message: 'Network unavailable',
        networkFailure: true,
        requestId: expect.any(String),
      })

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('times out session retrieval before sending the API request', async () => {
    vi.useFakeTimers()
    supabase.auth.getSession.mockReturnValue(new Promise(() => {}))
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const request = api.get('/api/v1/oauth/connections')
    const assertion = expect(request).rejects.toMatchObject({
      code: 'REQUEST_TIMEOUT',
      timeout: true,
      phase: 'session',
      status: null,
      networkFailure: false,
    })
    await vi.advanceTimersByTimeAsync(1000)
    await assertion

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('aborts a fetch when the API request deadline expires', async () => {
    vi.useFakeTimers()
    let requestOptions
    vi.stubGlobal('fetch', vi.fn((_url, options) => {
      requestOptions = options
      return new Promise(() => {})
    }))

    const request = api.get('/api/v1/memories')
    const assertion = expect(request).rejects.toMatchObject({
      code: 'REQUEST_TIMEOUT',
      timeout: true,
      phase: 'fetch',
      status: null,
      networkFailure: false,
    })
    await vi.advanceTimersByTimeAsync(1000)
    await assertion

    expect(requestOptions.signal.aborted).toBe(true)
  })

  it.each([401, 403, 404, 500])(
    'preserves HTTP %i as an API error, not a network failure',
    async (status) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
        JSON.stringify({ detail: `HTTP ${status}` }),
        {
          status,
          headers: { 'content-type': 'application/json' },
        },
      )))

      await expect(api.get('/api/v1/oauth/connections'))
        .rejects.toMatchObject({
          status,
          message: `HTTP ${status}`,
          networkFailure: false,
        })
    },
  )

  it('reports an invalid successful JSON response explicitly', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '{invalid',
      {
        status: 200,
        headers: { 'content-type': 'application/json' },
      },
    )))

    await expect(api.get('/api/v1/conversations'))
      .rejects.toMatchObject({
        code: 'API_RESPONSE_INVALID',
        status: 200,
        networkFailure: false,
      })
  })

  it('reports an unexpected empty successful JSON body explicitly', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '',
      {
        status: 200,
        headers: { 'content-type': 'application/json' },
      },
    )))

    await expect(api.get('/api/v1/memories'))
      .rejects.toMatchObject({
        code: 'API_RESPONSE_EMPTY',
        status: 200,
        networkFailure: false,
      })
  })

  it('rejects an empty successful non-JSON response when a body is required', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '',
      {
        status: 200,
        headers: { 'content-type': 'text/plain' },
      },
    )))

    await expect(api.get('/api/v1/conversations'))
      .rejects.toMatchObject({
        code: 'API_RESPONSE_EMPTY',
        status: 200,
        networkFailure: false,
      })
  })

  it('classifies response-body transport failure as a network failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      headers: new Headers({ 'content-type': 'application/json' }),
      text: () => Promise.reject(new TypeError('Connection reset')),
    }))

    await expect(api.get('/api/v1/conversations'))
      .rejects.toMatchObject({
        code: 'API_RESPONSE_READ_FAILED',
        status: 200,
        networkFailure: true,
      })
  })

  it('returns null for a valid no-content response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(null, { status: 204 }),
    ))

    await expect(api.get('/api/v1/conversations')).resolves.toBeNull()
  })

  it('returns empty JSON collections as valid empty states', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '[]',
      {
        status: 200,
        headers: { 'content-type': 'application/json' },
      },
    )))

    await expect(api.get('/api/v1/conversations')).resolves.toEqual([])
  })

  it('bounds time spent parsing a response body', async () => {
    vi.useFakeTimers()
    const response = {
      status: 200,
      ok: true,
      headers: new Headers({ 'content-type': 'application/json' }),
      text: () => new Promise(() => {}),
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))

    const request = api.get('/api/v1/conversations')
    const assertion = expect(request).rejects.toMatchObject({
      code: 'REQUEST_TIMEOUT',
      timeout: true,
      phase: 'response',
      status: 200,
    })
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
  })

  it('preserves a received HTTP status if reading its error body times out', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status: 401,
      ok: false,
      headers: new Headers({ 'content-type': 'application/json' }),
      text: () => new Promise(() => {}),
    }))

    const request = api.get('/api/v1/oauth/connections')
    const assertion = expect(request).rejects.toMatchObject({
      code: 'REQUEST_TIMEOUT',
      timeout: true,
      phase: 'response',
      status: 401,
      networkFailure: false,
    })
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
  })
})
