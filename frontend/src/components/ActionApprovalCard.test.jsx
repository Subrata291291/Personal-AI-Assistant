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
  return renderProposalCard(createProposal(initialStatus))
}

function createCalendarProposal(status = 'pending') {
  return {
    proposal_id: 'calendar-proposal-1',
    action_type: 'calendar.create_event',
    status,
    connection_id: 'connection-1',
    account_email: 'owner@example.com',
    parameters: {
      calendar_id: 'primary',
      summary: 'Design review',
      start: '2026-06-10T10:00:00-07:00',
      end: '2026-06-10T11:00:00-07:00',
      time_zone: 'America/Los_Angeles',
      all_day: false,
    },
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    result: null,
  }
}

function createCalendarMutationProposal(actionType, status = 'pending') {
  const isUpdate = actionType === 'calendar.update_event'
  return {
    proposal_id: `calendar-${isUpdate ? 'update' : 'delete'}-proposal`,
    action_type: actionType,
    status,
    connection_id: 'connection-1',
    account_email: 'owner@example.com',
    parameters: {
      calendar_id: 'primary',
      event_id: 'event-1',
      expected_etag: '"etag-1"',
      current_event: {
        summary: 'Design review',
        start: { dateTime: '2026-06-10T10:00:00+00:00', timeZone: 'UTC' },
        end: { dateTime: '2026-06-10T11:00:00+00:00', timeZone: 'UTC' },
        attendees: [],
        etag: '"etag-1"',
      },
      ...(isUpdate ? { changes: { summary: 'Updated design review' } } : {}),
    },
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    result: null,
  }
}

function renderProposalCard(proposal) {
  const message = {
    id: 'approval-message-1',
    metadata: { action_approval: proposal },
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

  it('requires a separate execute action for an approved Calendar event', async () => {
    actionApprovalService.approve.mockResolvedValue({
      proposal: createCalendarProposal('approved'),
    })
    actionApprovalService.execute.mockResolvedValue({
      proposal: {
        ...createCalendarProposal('completed'),
        result: { outcome: 'created', google_event_id: 'event-1' },
      },
    })
    renderProposalCard(createCalendarProposal())

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => {
      expect(actionApprovalService.approve)
        .toHaveBeenCalledWith('calendar-proposal-1')
    })
    expect(actionApprovalService.execute).not.toHaveBeenCalled()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Create Calendar event',
    }))
    await waitFor(() => {
      expect(actionApprovalService.execute)
        .toHaveBeenCalledWith('calendar-proposal-1')
    })
    expect(await screen.findByText(/Google Calendar confirmed the event/))
      .toBeInTheDocument()
  })

  it('reconciles an uncertain Calendar create without offering a retry', async () => {
    actionApprovalService.execute.mockRejectedValue(
      new Error('The Calendar event outcome is uncertain.'),
    )
    actionApprovalService.getProposal.mockResolvedValue(
      createCalendarProposal('execution_uncertain'),
    )
    renderProposalCard(createCalendarProposal('approved'))

    fireEvent.click(screen.getByRole('button', {
      name: 'Create Calendar event',
    }))
    expect(await screen.findByRole('button', {
      name: 'Check Calendar change status',
    }))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create Calendar event' }))
      .not.toBeInTheDocument()
    expect(actionApprovalService.execute).toHaveBeenCalledTimes(1)
  })

  it('shows and executes a Calendar update proposal after approval', async () => {
    const proposal = createCalendarMutationProposal('calendar.update_event')
    actionApprovalService.approve.mockResolvedValue({
      proposal: { ...proposal, status: 'approved' },
    })
    actionApprovalService.execute.mockResolvedValue({
      proposal: {
        ...proposal,
        status: 'completed',
        result: { outcome: 'updated', google_event_id: 'event-1' },
      },
    })
    renderProposalCard(proposal)

    expect(screen.getByText(/Updated design review/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => {
      expect(actionApprovalService.approve).toHaveBeenCalledWith(
        'calendar-update-proposal',
      )
    })
    fireEvent.click(await screen.findByRole('button', {
      name: 'Update Calendar event',
    }))
    await waitFor(() => {
      expect(actionApprovalService.execute).toHaveBeenCalledWith(
        'calendar-update-proposal',
      )
    })
    expect(await screen.findByText(/confirmed the event was updated/))
      .toBeInTheDocument()
  })

  it('shows and executes a Calendar delete proposal after approval', async () => {
    const proposal = createCalendarMutationProposal('calendar.delete_event')
    actionApprovalService.approve.mockResolvedValue({
      proposal: { ...proposal, status: 'approved' },
    })
    actionApprovalService.execute.mockResolvedValue({
      proposal: {
        ...proposal,
        status: 'completed',
        result: { outcome: 'deleted', google_event_id: 'event-1' },
      },
    })
    renderProposalCard(proposal)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => {
      expect(actionApprovalService.approve).toHaveBeenCalledWith(
        'calendar-delete-proposal',
      )
    })
    fireEvent.click(await screen.findByRole('button', {
      name: 'Delete Calendar event',
    }))
    await waitFor(() => {
      expect(actionApprovalService.execute).toHaveBeenCalledWith(
        'calendar-delete-proposal',
      )
    })
    expect(await screen.findByText(/confirmed the event was deleted/))
      .toBeInTheDocument()
  })
})
