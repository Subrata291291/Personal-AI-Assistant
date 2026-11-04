import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./supabase.js', () => ({
  supabase: {
    auth: {
      getSession: vi.fn(),
    },
  },
}))

import { supabase } from './supabase.js'

let api

beforeAll(async () => {
  vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.test')
  ;({ api } = await import('./api.js'))
})

describe('API request handling', () => {
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
})
