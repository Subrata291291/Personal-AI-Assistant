import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

vi.mock('../lib/supabase.js', () => ({
  supabase: {
    auth: {
      getSession: vi.fn(),
    },
  },
}))

import { api } from '../lib/api.js'
import { supabase } from '../lib/supabase.js'
import { conversationService } from './conversationService.js'

const connectionId = '4e703667-ef83-49e5-9fb1-99c89bf2b136'
const operationId = '5f582828-884a-4f2f-a3ab-dd10440e255d'

describe('conversationService Gmail draft save requests', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('sends canonical UUID strings unchanged in the save request', async () => {
    await conversationService.saveEmailDraftToGmail(
      'conversation-id',
      'message-id',
      connectionId,
      operationId,
    )

    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/conversations/conversation-id/messages/message-id/draft/save',
      {
        connection_id: connectionId,
        operation_id: operationId,
      },
    )
  })

  it('checks status with the original operation ID without creating a new one', async () => {
    await conversationService.getEmailDraftSaveStatus(
      'conversation-id',
      'message-id',
      connectionId,
      operationId,
    )

    expect(api.get).toHaveBeenCalledWith(
      '/api/v1/conversations/conversation-id/messages/message-id/draft/save-status?connection_id=4e703667-ef83-49e5-9fb1-99c89bf2b136&operation_id=5f582828-884a-4f2f-a3ab-dd10440e255d',
    )
  })
})

function streamingResponse(payloads, chunkSize = 7) {
  const text = payloads
    .map((payload) =>
      `data: ${typeof payload === 'string' ? payload : JSON.stringify(payload)}\n\n`
    )
    .join('')
  const bytes = new TextEncoder().encode(text)
  const chunks = []
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    chunks.push(bytes.slice(offset, offset + chunkSize))
  }

  let index = 0
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          async read() {
            if (index >= chunks.length) {
              return { done: true, value: undefined }
            }
            return { done: false, value: chunks[index++] }
          },
        }
      },
    },
  }
}

describe('conversationService chat streaming', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    supabase.auth.getSession.mockResolvedValue({
      data: { session: { access_token: 'test-token' } },
      error: null,
    })
  })

  it('delivers token events in order and resolves after done', async () => {
    const payloads = [
      { type: 'status', message: 'Searching Gmail...' },
      { type: 'token', content: 'Hel' },
      { type: 'token', content: 'lo' },
      {
        type: 'done',
        message_id: 'assistant-1',
        user_message_id: 'user-1',
        conversation_id: 'conversation-1',
      },
    ]
    global.fetch = vi.fn().mockResolvedValue(streamingResponse(payloads))
    const received = []

    await conversationService.streamChatMessage(
      'conversation-1',
      'hello',
      'idempotency-key',
      null,
      'UTC',
      (event) => received.push(event),
    )

    expect(received).toEqual(payloads)
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/chat/stream'),
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer test-token',
          'Idempotency-Key': 'idempotency-key',
        }),
      }),
    )
  })

  it('rejects an error event with its partial flag and never reports done', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      streamingResponse([
        { type: 'token', content: 'partial' },
        { type: 'error', message: 'The response was interrupted.', partial: true },
      ])
    )
    const received = []

    await expect(
      conversationService.streamChatMessage(
        'conversation-1',
        'hello',
        'idempotency-key',
        null,
        'UTC',
        (event) => received.push(event),
      ),
    ).rejects.toMatchObject({
      message: 'The response was interrupted.',
      partial: true,
      streamError: true,
    })

    expect(received).toEqual([{ type: 'token', content: 'partial' }])
  })

  it('rejects when the stream ends without a done event', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      streamingResponse([{ type: 'token', content: 'partial' }])
    )

    await expect(
      conversationService.streamChatMessage(
        'conversation-1',
        'hello',
        'idempotency-key',
        null,
        'UTC',
        () => {},
      ),
    ).rejects.toMatchObject({
      message: 'The response stream ended before completion.',
      partial: true,
    })
  })

  it('propagates exceptions thrown by the event handler', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      streamingResponse([
        { type: 'token', content: 'first' },
        { type: 'done', message_id: 'assistant-1' },
      ])
    )

    await expect(
      conversationService.streamChatMessage(
        'conversation-1',
        'hello',
        'idempotency-key',
        null,
        'UTC',
        () => {
          throw new Error('render failed')
        },
      ),
    ).rejects.toThrow('render failed')
  })

  it('reports malformed SSE payloads instead of presenting a complete answer', async () => {
    global.fetch = vi.fn().mockResolvedValue(
      streamingResponse([
        '{"type": "token", "content": "kept"}',
        '{not valid json',
        { type: 'done', message_id: 'assistant-1' },
      ])
    )
    const received = []

    await expect(
      conversationService.streamChatMessage(
        'conversation-1',
        'hello',
        'idempotency-key',
        null,
        'UTC',
        (event) => received.push(event),
      ),
    ).rejects.toMatchObject({
      message: 'Some streamed content could not be parsed.',
      partial: true,
    })

    expect(received).toEqual([{ type: 'token', content: 'kept' }])
  })
})
