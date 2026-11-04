import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api.js', () => ({
  api: {
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
