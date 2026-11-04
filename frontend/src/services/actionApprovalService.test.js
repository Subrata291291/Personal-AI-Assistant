import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '../lib/api.js'
import { actionApprovalService } from './actionApprovalService.js'

describe('Gmail draft approval requests', () => {
  beforeEach(() => vi.resetAllMocks())

  it('posts the reviewed draft to the Gmail draft approval endpoint', async () => {
    const payload = {
      connection_id: 'connection-1',
      to: ['recipient@example.com'],
      cc: [],
      bcc: [],
      subject: 'Reviewed subject',
      body: 'Reviewed message',
    }
    const idempotencyKey = 'e747824d-1429-4ad0-9f22-aefb8688ccca'

    await actionApprovalService.createDraftSendApproval(
      'gmail-message-1',
      payload,
      idempotencyKey,
    )

    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/action-approvals/gmail-drafts/gmail-message-1',
      payload,
      { 'Idempotency-Key': idempotencyKey },
    )
  })

  it('reconciles an uncertain proposal using its original idempotency key', async () => {
    const idempotencyKey = 'e747824d-1429-4ad0-9f22-aefb8688ccca'

    await actionApprovalService.getDraftSendApprovalStatus(idempotencyKey)

    expect(api.get).toHaveBeenCalledWith(
      `/api/v1/action-approvals/gmail-drafts/approval/${idempotencyKey}`,
    )
  })
})
