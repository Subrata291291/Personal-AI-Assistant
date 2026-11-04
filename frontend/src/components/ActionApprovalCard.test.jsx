import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../services/actionApprovalService.js', () => ({
  actionApprovalService: {
    approve: vi.fn(),
    reject: vi.fn(),
    execute: vi.fn(),
    getProposal: vi.fn(),
  },
}))

import ActionApprovalCard from './ActionApprovalCard.jsx'
import { actionApprovalService } from '../services/actionApprovalService.js'

const parameters = {
  to: ['recipient@example.com'],
  cc: [],
  bcc: [],
  subject: 'Product proposal',
  body: 'A professional product proposal.',
}

function createProposal(status = 'pending') {
  return {
    proposal_id: 'proposal-1',
    action_type: 'gmail.send',
    status,
    connection_id: 'connection-1',
    account_email: 'sender@example.com',
    parameters,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    result: null,
  }
}

function renderCard(initialStatus = 'pending') {
  const message = {
    id: 'approval-message-1',
    metadata: { action_approval: createProposal(initialStatus) },
  }
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  })

  function Harness() {
    const [currentMessage, setCurrentMessage] = useState(message)
    return (
      <QueryClientProvider client={queryClient}>
        <ActionApprovalCard
          message={currentMessage}
          isDark={false}
          onUpdated={setCurrentMessage}
        />
      </QueryClientProvider>
    )
  }

  return render(<Harness />)
}

describe('ActionApprovalCard Gmail send safety', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('requires explicit approval before offering a separate execute action', async () => {
    actionApprovalService.approve.mockResolvedValue({
      proposal: createProposal('approved'),
    })
    actionApprovalService.execute.mockResolvedValue({
      proposal: {
        ...createProposal('completed'),
        result: { outcome: 'sent', gmail_message_id: 'sent-1' },
      },
    })
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => {
      expect(actionApprovalService.approve).toHaveBeenCalledWith('proposal-1')
    })
    expect(actionApprovalService.execute).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', {
      name: 'Execute Gmail send',
    }))
    await waitFor(() => {
      expect(actionApprovalService.execute).toHaveBeenCalledTimes(1)
    })
    expect(await screen.findByText(/Gmail confirmed the message was sent/))
      .toBeInTheDocument()
  })

  it('reconciles an uncertain send and never offers a blind retry', async () => {
    actionApprovalService.execute.mockRejectedValue(
      new Error('The request outcome is uncertain.'),
    )
    actionApprovalService.getProposal.mockResolvedValue(
      createProposal('execution_uncertain'),
    )
    renderCard('approved')

    fireEvent.click(screen.getByRole('button', { name: 'Execute Gmail send' }))
    expect(await screen.findByRole('button', { name: 'Check send status' }))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Execute Gmail send' }))
      .not.toBeInTheDocument()
    expect(actionApprovalService.execute).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Check send status' }))
    await waitFor(() => {
      expect(actionApprovalService.getProposal).toHaveBeenCalledWith('proposal-1')
    })
    expect(screen.queryByRole('button', { name: 'Execute Gmail send' }))
      .not.toBeInTheDocument()
    expect(actionApprovalService.execute).toHaveBeenCalledTimes(1)
  })
})
