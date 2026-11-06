import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '../lib/api.js'
import { gmailService } from './gmailService.js'

describe('gmailService archive request', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls the archive endpoint with the selected connection', async () => {
    api.post.mockResolvedValue({
      id: 'message-123',
      label_ids: ['UNREAD'],
      status: 'archived',
    })

    await gmailService.archiveMessage({
      connectionId: 'connection-1',
      messageId: 'message-123',
    })

    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/gmail/messages/message-123/archive',
      { connection_id: 'connection-1' },
    )
  })
})

describe('gmailService message list response', () => {
  const request = {
    connectionId: 'connection-1',
    maxResults: 10,
  }
  const message = {
    id: 'message-123',
    thread_id: 'thread-123',
    snippet: 'Preview',
    sender: null,
    recipient: null,
    subject: null,
    internal_date: null,
    date: null,
    labels: ['INBOX'],
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('accepts a valid non-empty response', async () => {
    const response = {
      messages: [message],
      next_page_token: 'next-page',
      result_size_estimate: 1,
    }
    api.get.mockResolvedValue(response)

    await expect(gmailService.getMessages(request)).resolves.toEqual(response)
  })

  it('accepts a valid empty response', async () => {
    const response = {
      messages: [],
      next_page_token: null,
      result_size_estimate: 0,
    }
    api.get.mockResolvedValue(response)

    await expect(gmailService.getMessages(request)).resolves.toEqual(response)
  })

  it.each([
    ['missing messages', { next_page_token: null, result_size_estimate: 0 }],
    ['messages with the wrong type', {
      messages: {},
      next_page_token: null,
      result_size_estimate: 0,
    }],
    ['malformed message reference', {
      messages: [{ ...message, id: '' }],
      next_page_token: null,
      result_size_estimate: 1,
    }],
    ['malformed metadata', {
      messages: [{ ...message, labels: 'INBOX' }],
      next_page_token: null,
      result_size_estimate: 1,
    }],
    ['invalid pagination token', {
      messages: [],
      next_page_token: 7,
      result_size_estimate: 0,
    }],
    ['missing estimate', {
      messages: [],
      next_page_token: null,
    }],
  ])('rejects %s', async (_description, response) => {
    api.get.mockResolvedValue(response)

    await expect(gmailService.getMessages(request)).rejects.toThrow(
      'Gmail returned an invalid message list response.',
    )
  })
})
