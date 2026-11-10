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
    execution_available: true,
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
    execution_available: true,
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
        attendees: [{ email: 'attendee@example.com' }],
        etag: '"etag-1"',
      },
      ...(isUpdate ? { changes: { summary: 'Updated design review' } } : {}),
    },
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    result: null,
  }
}

function renderProposalCard(proposal, queryClient = null) {
  const message = {
    id: 'approval-message-1',
    metadata: { action_approval: proposal },
  }
  const activeQueryClient = queryClient || new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  })

  function Harness() {
    const [currentMessage, setCurrentMessage] = useState(message)
    return (
      <QueryClientProvider client={activeQueryClient}>
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

describe('ActionApprovalCard approval and execution lifecycle', () => {
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

  it('displays the changed Calendar time range in the event timezone', () => {
    const proposal = createCalendarMutationProposal('calendar.update_event')
    proposal.parameters.time_zone = 'Asia/Kolkata'
    proposal.parameters.changes = {
      start: {
        dateTime: '2026-06-10T17:00:00+05:30',
        timeZone: 'Asia/Kolkata',
      },
      end: {
        dateTime: '2026-06-10T18:00:00+05:30',
        timeZone: 'Asia/Kolkata',
      },
    }

    renderProposalCard(proposal)

    expect(screen.getByRole('region', { name: 'Original event' }))
      .toHaveTextContent('10:00 AM')
    expect(screen.getByRole('region', { name: 'Proposed event' }))
      .toHaveTextContent('5:00 PM')
    expect(screen.getByRole('region', { name: 'Proposed event' }))
      .toHaveTextContent('6:00 PM')
    expect(screen.getByRole('region', { name: 'Proposed changes' }))
      .toHaveTextContent('Original: June 10, 2026 10:00 AM')
    expect(screen.getByRole('region', { name: 'Proposed changes' }))
      .toHaveTextContent('Proposed: June 10, 2026 5:00 PM')
  })

  it('clearly identifies a series-wide Calendar Delete proposal', () => {
    const proposal = createCalendarMutationProposal('calendar.delete_event')
    proposal.parameters.delete_entire_series = true
    proposal.parameters.current_event = {
      ...proposal.parameters.current_event,
      recurrence: ['RRULE:FREQ=DAILY'],
    }

    renderProposalCard(proposal)

    expect(screen.getByRole('region', { name: 'Event to delete' }))
      .toHaveTextContent('Entire recurring series, including all future occurrences (no end date)')
    expect(screen.getByRole('region', { name: 'Event to delete' }))
      .toHaveTextContent('Daily')
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

  it('shows a superseded Calendar proposal without actionable controls', () => {
    const proposal = createCalendarProposal('rejected')
    proposal.result = { outcome: 'superseded' }
    renderProposalCard(proposal)

    expect(screen.getByText('Approval: Superseded')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' }))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create Calendar event' }))
      .not.toBeInTheDocument()
  })

  it('requires a separate execute action for an approved Calendar event', async () => {
    actionApprovalService.approve.mockResolvedValue({
      proposal: createCalendarProposal('approved'),
    })
    actionApprovalService.getProposal.mockResolvedValue(
      createCalendarProposal('approved'),
    )
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
    expect(await screen.findByText('Execution: Available')).toBeInTheDocument()
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

  it('updates the displayed status from the Calendar status-check response', async () => {
    const proposal = createCalendarMutationProposal(
      'calendar.delete_event',
      'execution_uncertain',
    )
    actionApprovalService.getProposal.mockResolvedValue({
      ...proposal,
      status: 'completed',
      result: {
        outcome: 'deleted',
        google_event_id: 'series-master',
      },
    })
    renderProposalCard(proposal)

    fireEvent.click(screen.getByRole('button', {
      name: 'Check Calendar change status',
    }))

    expect(await screen.findByText(
      /Google Calendar confirmed the event was deleted/,
    )).toBeInTheDocument()
    expect(actionApprovalService.getProposal)
      .toHaveBeenCalledWith(proposal.proposal_id)
    expect(actionApprovalService.execute).not.toHaveBeenCalled()
  })

  it('displays the latest ambiguous Delete verification without clearing uncertainty', async () => {
    const proposal = createCalendarMutationProposal(
      'calendar.delete_event',
      'execution_uncertain',
    )
    actionApprovalService.getProposal.mockResolvedValue({
      ...proposal,
      status: 'execution_uncertain',
      result: {
        outcome: 'unconfirmed',
        reason: 'read_failed',
        checked_now: true,
      },
    })
    renderProposalCard(proposal)

    fireEvent.click(screen.getByRole('button', {
      name: 'Check Calendar change status',
    }))

    expect(await screen.findByText(
      /Google Calendar status could not be verified/,
    )).toBeInTheDocument()
    expect(actionApprovalService.getProposal)
      .toHaveBeenCalledWith(proposal.proposal_id)
    expect(actionApprovalService.execute).not.toHaveBeenCalled()
  })

  it('shows and executes a Calendar update proposal after approval', async () => {
    const proposal = createCalendarMutationProposal('calendar.update_event')
    actionApprovalService.approve.mockResolvedValue({
      proposal: { ...proposal, status: 'approved' },
    })
    actionApprovalService.getProposal.mockResolvedValue({
      ...proposal,
      status: 'approved',
    })
    actionApprovalService.execute.mockResolvedValue({
      proposal: {
        ...proposal,
        status: 'completed',
        result: { outcome: 'updated', google_event_id: 'event-1' },
      },
    })
    renderProposalCard(proposal)

    expect(screen.getAllByText(/Updated design review/).length).toBeGreaterThan(0)
    expect(screen.getByRole('region', { name: 'Original event' }))
      .toHaveTextContent('Design review')
    expect(screen.getByRole('region', { name: 'Proposed event' }))
      .toHaveTextContent('Updated design review')
    expect(screen.getByRole('region', { name: 'Proposed changes' }))
      .toHaveTextContent('Original: Design review')
    expect(screen.getByRole('region', { name: 'Original event' }))
      .toHaveTextContent('10:00 AM')
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

  it('renders the stored recurring event snapshot and title-only proposal', () => {
    const proposal = createCalendarMutationProposal('calendar.update_event')
    proposal.parameters.current_event = {
      summary: 'A recurring google workout',
      start: {
        dateTime: '2026-11-10T10:00:00+05:30',
        timeZone: 'Asia/Kolkata',
      },
      end: {
        dateTime: '2026-11-10T11:00:00+05:30',
        timeZone: 'Asia/Kolkata',
      },
      attendees: [],
      recurrence: ['RRULE:FREQ=WEEKLY'],
      etag: '"etag-1"',
    }
    proposal.parameters.changes = { summary: 'Gym' }
    proposal.parameters.time_zone = 'Asia/Kolkata'
    renderProposalCard(proposal)

    const original = screen.getByRole('region', { name: 'Original event' })
    const proposed = screen.getByRole('region', { name: 'Proposed event' })
    expect(original).toHaveTextContent('A recurring google workout')
    expect(original).toHaveTextContent('November 10, 2026')
    expect(original).toHaveTextContent('10:00 AM')
    expect(original).toHaveTextContent('11:00 AM')
    expect(original).toHaveTextContent('Asia/Kolkata')
    expect(original).toHaveTextContent('RRULE:FREQ=WEEKLY')
    expect(proposed).toHaveTextContent('Gym')
    expect(proposed).toHaveTextContent('RRULE:FREQ=WEEKLY')
    expect(screen.getByRole('region', { name: 'Proposed changes' }))
      .toHaveTextContent('Original: A recurring google workout')
    expect(screen.getByRole('region', { name: 'Proposed changes' }))
      .toHaveTextContent('Proposed: Gym')
  })

  it('shows and executes a Calendar delete proposal after approval', async () => {
    const proposal = createCalendarMutationProposal('calendar.delete_event')
    actionApprovalService.approve.mockResolvedValue({
      proposal: { ...proposal, status: 'approved' },
    })
    actionApprovalService.getProposal.mockResolvedValue({
      ...proposal,
      status: 'approved',
    })
    actionApprovalService.execute.mockResolvedValue({
      proposal: {
        ...proposal,
        status: 'completed',
        result: { outcome: 'deleted', google_event_id: 'event-1' },
      },
    })
    renderProposalCard(proposal)

    expect(screen.getByRole('region', { name: 'Event to delete' }))
      .toHaveTextContent('Design review')
    expect(screen.getByRole('region', { name: 'Event to delete' }))
      .toHaveTextContent('10:00 AM')
    expect(screen.getByRole('region', { name: 'Event to delete' }))
      .toHaveTextContent('attendee@example.com')

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

  it('shows approved and execution disabled when the backend blocks Calendar execution', async () => {
    actionApprovalService.approve.mockResolvedValue({
      proposal: {
        ...createCalendarProposal('approved'),
        execution_available: false,
      },
    })
    actionApprovalService.getProposal.mockResolvedValue({
      ...createCalendarProposal('approved'),
      execution_available: false,
    })
    renderProposalCard(createCalendarProposal())

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    expect(await screen.findByText('Execution: Disabled'))
      .toBeInTheDocument()
    expect(screen.getByText(
      'Your approval has been recorded. Calendar execution is currently disabled. No event has been created.',
    )).toBeInTheDocument()
    expect(screen.queryByText(/separately choose Execute/)).not.toBeInTheDocument()
    expect(actionApprovalService.getProposal)
      .toHaveBeenCalledWith('calendar-proposal-1')
    expect(screen.queryByRole('button', {
      name: 'Create Calendar event',
    })).not.toBeInTheDocument()
    expect(screen.queryByText('Creating event...')).not.toBeInTheDocument()
    expect(actionApprovalService.execute).not.toHaveBeenCalled()
  })

  it('clears the execution loading state after a disabled response and refresh', async () => {
    actionApprovalService.execute.mockRejectedValue(
      Object.assign(new Error('Calendar change execution is currently disabled.'), {
        status: 403,
      }),
    )
    actionApprovalService.getProposal.mockResolvedValue({
      ...createCalendarProposal('approved'),
      execution_available: false,
    })
    renderProposalCard(createCalendarProposal('approved'))

    fireEvent.click(screen.getByRole('button', {
      name: 'Create Calendar event',
    }))

    expect(await screen.findByText(
      'Your approval has been recorded. Calendar execution is currently disabled. No event has been created.',
    )).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.queryByText('Creating event...')).not.toBeInTheDocument()
    })
    expect(screen.queryByRole('button', {
      name: 'Create Calendar event',
    })).not.toBeInTheDocument()
    expect(actionApprovalService.getProposal)
      .toHaveBeenCalledWith('calendar-proposal-1')
  })

  it('shows the pending approval state without an Execute action', () => {
    renderProposalCard(createCalendarProposal())

    expect(screen.getByText('Approval: Pending')).toBeInTheDocument()
    expect(screen.getByText('Execution: Not started')).toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: 'Create Calendar event',
    })).not.toBeInTheDocument()
  })

  it('refreshes missing availability and does not execute when disabled', async () => {
    const proposal = createCalendarProposal('approved')
    delete proposal.execution_available
    actionApprovalService.getProposal.mockResolvedValue({
      ...proposal,
      execution_available: false,
    })
    renderProposalCard(proposal)

    expect(await screen.findByText('Execution: Disabled')).toBeInTheDocument()
    expect(screen.getByText(
      'Your approval has been recorded. Calendar execution is currently disabled. No event has been created.',
    )).toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: 'Create Calendar event',
    })).not.toBeInTheDocument()
    expect(actionApprovalService.getProposal).toHaveBeenCalledWith(
      'calendar-proposal-1',
    )
  })

  it('keeps execution unavailable when backend status refresh fails', async () => {
    const proposal = createCalendarProposal('approved')
    delete proposal.execution_available
    actionApprovalService.getProposal.mockRejectedValue(
      new Error('Status unavailable'),
    )
    renderProposalCard(proposal)

    await waitFor(() => {
      expect(actionApprovalService.getProposal).toHaveBeenCalledWith(
        'calendar-proposal-1',
      )
    })
    expect(screen.getByText('Execution: Not confirmed')).toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: 'Create Calendar event',
    })).not.toBeInTheDocument()
  })

  it.each([
    ['calendar.create_event', 'created', 'Create Calendar event'],
    ['calendar.update_event', 'updated', 'Update Calendar event'],
    ['calendar.delete_event', 'deleted', 'Delete Calendar event'],
  ])(
    'invalidates Calendar event queries only after verified %s',
    async (actionType, outcome, actionLabel) => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries')
      .mockResolvedValue()
    actionApprovalService.execute.mockResolvedValue({
      proposal: {
        ...(actionType === 'calendar.create_event'
          ? createCalendarProposal('completed')
          : createCalendarMutationProposal(actionType, 'completed')),
        result: { outcome, google_event_id: 'event-1' },
      },
    })
    renderProposalCard(
      actionType === 'calendar.create_event'
        ? createCalendarProposal('approved')
        : createCalendarMutationProposal(actionType, 'approved'),
      queryClient,
    )

    fireEvent.click(screen.getByRole('button', { name: actionLabel }))

    expect(await screen.findByText(
      new RegExp(`Google Calendar confirmed the event was ${outcome}`),
    )).toBeInTheDocument()
    await waitFor(() => {
      expect(invalidateQueries).toHaveBeenCalledWith({
        queryKey: ['calendar-events'],
        refetchType: 'all',
      })
      expect(invalidateQueries).toHaveBeenCalledWith({
        queryKey: ['calendar-event'],
        refetchType: 'all',
      })
    })
    },
  )

  it('shows a rejected approval without offering execution', async () => {
    actionApprovalService.reject.mockResolvedValue({
      proposal: createCalendarProposal('rejected'),
    })
    renderProposalCard(createCalendarProposal())

    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))

    expect(await screen.findByText('Approval: Rejected')).toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: 'Create Calendar event',
    })).not.toBeInTheDocument()
    expect(actionApprovalService.execute).not.toHaveBeenCalled()
  })

  it('shows a failed Calendar execution and clears its loading label', async () => {
    actionApprovalService.execute.mockRejectedValue(
      new Error('Google Calendar is unavailable.'),
    )
    actionApprovalService.getProposal.mockResolvedValue(
      createCalendarProposal('approved'),
    )
    renderProposalCard(createCalendarProposal('approved'))

    fireEvent.click(screen.getByRole('button', {
      name: 'Create Calendar event',
    }))

    expect(await screen.findByText('Execution: Failed')).toBeInTheDocument()
    expect(screen.getByText(
      'Calendar change execution failed. No successful change was confirmed.',
    )).toBeInTheDocument()
    expect(screen.queryByText('Creating event...')).not.toBeInTheDocument()
    expect(actionApprovalService.execute).toHaveBeenCalledTimes(1)
  })

  it('renders the recurring Gym Create proposal returned by chat', () => {
    const proposal = {
      ...createCalendarProposal(),
      execution_available: false,
      parameters: {
        ...createCalendarProposal().parameters,
        summary: 'Gym',
        start: '2026-11-11T10:00:00+05:30',
        end: '2026-11-11T11:00:00+05:30',
        time_zone: 'Asia/Kolkata',
        recurrence: ['RRULE:FREQ=DAILY'],
      },
    }
    renderProposalCard(proposal)

    expect(screen.getByText('Review proposed action')).toBeInTheDocument()
    expect(screen.getByText('Title')).toBeInTheDocument()
    expect(screen.getByText('Gym')).toBeInTheDocument()
    expect(screen.getByText('November 11, 2026')).toBeInTheDocument()
    expect(screen.getByText('10:00 AM')).toBeInTheDocument()
    expect(screen.getByText('11:00 AM')).toBeInTheDocument()
    expect(screen.getAllByText('Asia/Kolkata').length).toBeGreaterThan(0)
    expect(screen.getByText('primary')).toBeInTheDocument()
    expect(screen.getByText('Daily')).toBeInTheDocument()
  })

  it('shows all-day Calendar events without misleading time fields', () => {
    const proposal = {
      ...createCalendarProposal(),
      parameters: {
        ...createCalendarProposal().parameters,
        summary: 'Gym',
        start: '2026-11-08',
        end: '2026-11-09',
        time_zone: null,
        all_day: true,
      },
    }
    renderProposalCard(proposal)

    expect(screen.getByText('November 8, 2026')).toBeInTheDocument()
    expect(screen.getByText('All day')).toBeInTheDocument()
    expect(screen.queryByText('Start time')).not.toBeInTheDocument()
    expect(screen.queryByText('End time')).not.toBeInTheDocument()
  })

  it('shows updated Calendar proposal times instead of only raw timestamps', () => {
    const proposal = createCalendarMutationProposal('calendar.update_event')
    proposal.parameters.current_event = {
      ...proposal.parameters.current_event,
      summary: 'Gym',
      start: {
        dateTime: '2026-11-08T10:00:00+05:30',
        timeZone: 'Asia/Kolkata',
      },
      end: {
        dateTime: '2026-11-08T11:00:00+05:30',
        timeZone: 'Asia/Kolkata',
      },
    }
    proposal.parameters.changes = {
      summary: 'Gym',
      start: {
        dateTime: '2026-11-08T14:00:00+05:30',
        timeZone: 'Asia/Kolkata',
      },
      end: {
        dateTime: '2026-11-08T15:30:00+05:30',
        timeZone: 'Asia/Kolkata',
      },
    }
    proposal.parameters.time_zone = 'Asia/Kolkata'
    renderProposalCard(proposal)

    expect(screen.getByText('2:00 PM')).toBeInTheDocument()
    expect(screen.getByText('3:30 PM')).toBeInTheDocument()
    expect(screen.getAllByText('Asia/Kolkata').length).toBeGreaterThan(0)
  })

  it('prevents duplicate approval and execution requests from repeated clicks', async () => {
    let resolveApproval
    actionApprovalService.approve.mockReturnValue(new Promise((resolve) => {
      resolveApproval = resolve
    }))
    renderProposalCard(createCalendarProposal())

    const approveButton = screen.getByRole('button', { name: 'Approve' })
    fireEvent.click(approveButton)
    fireEvent.click(approveButton)
    await waitFor(() => {
      expect(actionApprovalService.approve).toHaveBeenCalledTimes(1)
    })

    resolveApproval({
      proposal: createCalendarProposal('approved'),
    })
    actionApprovalService.getProposal.mockResolvedValue(
      createCalendarProposal('approved'),
    )
    const executeButton = await screen.findByRole('button', {
      name: 'Create Calendar event',
    })
    actionApprovalService.execute.mockReturnValue(new Promise(() => {}))
    fireEvent.click(executeButton)
    fireEvent.click(executeButton)
    await waitFor(() => {
      expect(actionApprovalService.execute).toHaveBeenCalledTimes(1)
    })
    expect(screen.getByText('Checking Calendar execution...'))
      .toBeInTheDocument()
  })
})
