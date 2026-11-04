import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '../lib/api.js'
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
