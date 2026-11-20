import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from '@tanstack/react-query'
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

function renderProposalCard(proposal, queryClient = null, children = null) {
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
        {children}
      </QueryClientProvider>
    )
  }

  return render(<Harness />)
}

const calendarEventsQueryKey = (connectionId = 'connection-1') => [
  'calendar-events',
  'user-1',
  connectionId,
  'Asia/Kolkata',
  'week',
  'primary',
  '2026-06-08',
  '2026-06-14',
  '',
]

const calendarViewQueryKey = (view, connectionId = 'connection-1') => [
  'calendar-events',
  'user-1',
  connectionId,
  'Asia/Kolkata',
  view,
  'primary',
  '2026-06-08',
  '2026-06-14',
  '',
]

function multiPageCalendarData(firstEvents, secondEvents) {
  return {
    pages: [
      { events: firstEvents, next_page_token: 'page-2' },
      { events: secondEvents, next_page_token: null },
    ],
    pageParams: [undefined, 'page-2'],
  }
}

function cachedEventIds(queryClient, queryKey) {
  const data = queryClient.getQueryData(queryKey)
  return (data?.pages || []).flatMap((page) => (page.events || []).map((event) => event.id))
}

function CalendarEventsProbe({
  connectionId = 'connection-1',
  eventsAfterConfirmation,
  testId = 'calendar-events-probe',
}) {
  const { data = [] } = useQuery({
    queryKey: calendarEventsQueryKey(connectionId),
    queryFn: async () => eventsAfterConfirmation,
    staleTime: Infinity,
    refetchOnMount: false,
  })

  return (
    <div data-testid={testId}>
      {data.map((event) => event.summary).join(', ') || 'No events'}
    </div>
  )
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

  it('offers approval and execution for the latest revised Calendar proposal', async () => {
    const superseded = createCalendarProposal('rejected')
    superseded.result = { outcome: 'superseded' }
    const revised = {
      ...createCalendarProposal('pending'),
      proposal_id: 'calendar-proposal-2',
      parameters: {
        ...createCalendarProposal('pending').parameters,
        start: '2026-06-10T21:00:00+05:30',
        end: '2026-06-10T22:00:00+05:30',
        time_zone: 'Asia/Kolkata',
      },
    }
    actionApprovalService.approve.mockResolvedValue({
      proposal: { ...revised, status: 'approved' },
    })
    actionApprovalService.getProposal.mockResolvedValue({
      ...revised,
      status: 'approved',
    })
    actionApprovalService.execute.mockResolvedValue({
      proposal: {
        ...revised,
        status: 'completed',
        result: { outcome: 'created', google_event_id: 'event-2' },
      },
    })

    renderProposalCard(superseded)
    renderProposalCard(revised)

    expect(screen.getByText('Approval: Superseded')).toBeInTheDocument()
    expect(screen.getByText('9:00 PM')).toBeInTheDocument()
    expect(screen.getByText('10:00 PM')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => {
      expect(actionApprovalService.approve)
        .toHaveBeenCalledWith('calendar-proposal-2')
    })

    fireEvent.click(await screen.findByRole('button', {
      name: 'Create Calendar event',
    }))
    await waitFor(() => {
      expect(actionApprovalService.execute)
        .toHaveBeenCalledWith('calendar-proposal-2')
    })
    expect(actionApprovalService.execute).toHaveBeenCalledTimes(1)
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

  it.each([
    [
      'calendar.create_event',
      'created',
      [],
      [{ id: 'event-1', summary: 'Design review' }],
      'Design review',
    ],
    [
      'calendar.update_event',
      'updated',
      [{ id: 'event-1', summary: 'Design review' }],
      [{ id: 'event-1', summary: 'Updated design review' }],
      'Updated design review',
    ],
    [
      'calendar.delete_event',
      'deleted',
      [{ id: 'event-1', summary: 'Design review' }],
      [],
      'No events',
    ],
  ])(
    'refreshes the Calendar UI after status-confirmed %s without a reload',
    async (
      actionType,
      outcome,
      initialEvents,
      eventsAfterConfirmation,
      expectedText,
    ) => {
      const queryClient = new QueryClient({
        defaultOptions: {
          queries: { retry: false },
          mutations: { retry: false },
        },
      })
      queryClient.setQueryData(calendarEventsQueryKey(), initialEvents)
      const proposal = actionType === 'calendar.create_event'
        ? createCalendarProposal('execution_uncertain')
        : createCalendarMutationProposal(actionType, 'execution_uncertain')
      actionApprovalService.getProposal.mockResolvedValue({
        ...proposal,
        status: 'completed',
        result: { outcome, google_event_id: 'event-1' },
      })

      renderProposalCard(
        proposal,
        queryClient,
        <CalendarEventsProbe eventsAfterConfirmation={eventsAfterConfirmation} />,
      )

      const initialText = initialEvents.length
        ? initialEvents.map((event) => event.summary).join(', ')
        : 'No events'
      expect(screen.getByTestId('calendar-events-probe'))
        .toHaveTextContent(initialText)
      fireEvent.click(screen.getByRole('button', {
        name: 'Check Calendar change status',
      }))

      await waitFor(() => {
        expect(screen.getByTestId('calendar-events-probe'))
          .toHaveTextContent(expectedText)
      })
    },
  )

  it('does not remove the event when deletion status remains unconfirmed', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries')
    const event = { id: 'event-1', summary: 'Design review' }
    queryClient.setQueryData(calendarEventsQueryKey(), [event])
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

    renderProposalCard(
      proposal,
      queryClient,
      <CalendarEventsProbe eventsAfterConfirmation={[event]} />,
    )

    fireEvent.click(screen.getByRole('button', {
      name: 'Check Calendar change status',
    }))

    expect(await screen.findByText(
      /Google Calendar status could not be verified/,
    )).toBeInTheDocument()
    expect(screen.getByTestId('calendar-events-probe'))
      .toHaveTextContent('Design review')
    expect(invalidateQueries).not.toHaveBeenCalled()
  })

  it('refreshes Calendar data when an execute error is reconciled as completed', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const event = { id: 'event-1', summary: 'Design review' }
    queryClient.setQueryData(calendarEventsQueryKey(), [event])
    const proposal = createCalendarMutationProposal(
      'calendar.delete_event',
      'approved',
    )
    const networkError = new Error('Connection lost.')
    networkError.networkFailure = true
    actionApprovalService.execute.mockRejectedValue(networkError)
    actionApprovalService.getProposal.mockResolvedValue({
      ...proposal,
      status: 'completed',
      result: { outcome: 'deleted', google_event_id: 'event-1' },
    })

    renderProposalCard(
      proposal,
      queryClient,
      <CalendarEventsProbe eventsAfterConfirmation={[]} />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Delete Calendar event' }))

    await waitFor(() => {
      expect(screen.getByTestId('calendar-events-probe'))
        .toHaveTextContent('No events')
    })
  })

  it('never removes similar-titled events from other connections or tenants', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const target = { id: 'event-1', summary: 'Design review' }
    const similar = { id: 'event-2', summary: 'Design review backup' }
    queryClient.setQueryData(calendarEventsQueryKey('connection-1'), [target, similar])
    queryClient.setQueryData(calendarEventsQueryKey('connection-2'), [target])
    const proposal = createCalendarMutationProposal(
      'calendar.delete_event',
      'execution_uncertain',
    )
    actionApprovalService.getProposal.mockResolvedValue({
      ...proposal,
      status: 'completed',
      result: { outcome: 'deleted', google_event_id: 'event-1' },
    })

    renderProposalCard(
      proposal,
      queryClient,
      <>
        <CalendarEventsProbe eventsAfterConfirmation={[similar]} />
        <CalendarEventsProbe
          connectionId="connection-2"
          eventsAfterConfirmation={[target]}
          testId="other-calendar-events-probe"
        />
      </>,
    )

    fireEvent.click(screen.getByRole('button', {
      name: 'Check Calendar change status',
    }))

    await waitFor(() => {
      expect(screen.getByTestId('calendar-events-probe'))
        .toHaveTextContent(/^Design review backup$/)
      expect(screen.getByTestId('other-calendar-events-probe'))
        .toHaveTextContent('Design review')
    })
  })

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

  it('displays approved distinctly from completed', async () => {
    const { unmount } = renderProposalCard(createCalendarProposal('approved'))
    expect(screen.getByText('Approval: Approved')).toBeInTheDocument()
    expect(screen.queryByText('Execution: Completed')).not.toBeInTheDocument()
    unmount()

    renderProposalCard({
      ...createCalendarProposal('completed'),
      result: { outcome: 'created', google_event_id: 'event-1' },
    })
    expect(screen.getByText('Approval: Approved')).toBeInTheDocument()
    expect(screen.getByText('Execution: Completed')).toBeInTheDocument()
    expect(screen.getByText(/Google Calendar confirmed the event was created/)).toBeInTheDocument()
  })

  it('displays executing distinctly from completed', () => {
    const { unmount } = renderProposalCard(createCalendarProposal('executing'))
    expect(screen.getByText('Execution: In Progress')).toBeInTheDocument()
    expect(screen.queryByText('Execution: Completed')).not.toBeInTheDocument()
    expect(screen.queryByText(/confirmed the event was/)).not.toBeInTheDocument()
    unmount()

    renderProposalCard({
      ...createCalendarProposal('completed'),
      result: { outcome: 'created', google_event_id: 'event-1' },
    })
    expect(screen.getByText('Execution: Completed')).toBeInTheDocument()
  })

  it('never presents execution_uncertain as confirmed success', async () => {
    renderProposalCard(createCalendarProposal('execution_uncertain'))
    expect(screen.getByText('Execution: Unconfirmed')).toBeInTheDocument()
    expect(screen.queryByText('Execution: Completed')).not.toBeInTheDocument()
    expect(screen.queryByText(/confirmed the event was/)).not.toBeInTheDocument()
    expect(screen.getByText(/outcome is unconfirmed/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create Calendar event' })).not.toBeInTheDocument()
  })

  it('treats network execution errors as unconfirmed, not provider success or failure', async () => {
    actionApprovalService.execute.mockRejectedValue(
      Object.assign(new Error('Network request failed'), { networkFailure: true }),
    )
    actionApprovalService.getProposal.mockResolvedValue(
      createCalendarProposal('approved'),
    )
    renderProposalCard(createCalendarProposal('approved'))

    fireEvent.click(screen.getByRole('button', { name: 'Create Calendar event' }))

    await waitFor(() => {
      expect(actionApprovalService.getProposal).toHaveBeenCalled()
    })
    expect(screen.getByText('Execution: Available')).toBeInTheDocument()
    expect(screen.queryByText('Execution: Failed')).not.toBeInTheDocument()
    expect(screen.queryByText('Execution: Completed')).not.toBeInTheDocument()
    expect(screen.queryByText(/confirmed the event was/)).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/unconfirmed/i)
  })

  it('refreshes to newer authoritative backend status', async () => {
    const uncertain = createCalendarProposal('execution_uncertain')
    actionApprovalService.getProposal.mockResolvedValue({
      ...createCalendarProposal('completed'),
      result: { outcome: 'created', google_event_id: 'event-99' },
    })
    renderProposalCard(uncertain)

    fireEvent.click(screen.getByRole('button', { name: 'Check Calendar change status' }))

    expect(await screen.findByText('Execution: Completed')).toBeInTheDocument()
    expect(screen.getByText(/confirmed the event was created/)).toBeInTheDocument()
    expect(screen.queryByText('Execution: Unconfirmed')).not.toBeInTheDocument()
  })

  it('applies authoritative status refresh results (guarded against stale overwrite by fetch sequencing)', async () => {
    // UI disables concurrent status-check clicks while pending; sequencing guard in component ensures only the latest
    // in-flight proposal fetch can update state when overlapping refreshes occur (e.g. execute-error refresh vs status check).
    const uncertain = createCalendarProposal('execution_uncertain')
    actionApprovalService.getProposal.mockResolvedValueOnce({
      ...createCalendarProposal('completed'),
      result: { outcome: 'created', google_event_id: 'event-new' },
    })
    renderProposalCard(uncertain)
    fireEvent.click(screen.getByRole('button', { name: 'Check Calendar change status' }))
    expect(await screen.findByText('Execution: Completed')).toBeInTheDocument()
    expect(screen.getByText(/confirmed the event was created/)).toBeInTheDocument()
  })

  it('does not offer destructive retry controls for uncertain or executing states', () => {
    const { unmount } = renderProposalCard(createCalendarProposal('execution_uncertain'))
    expect(screen.queryByRole('button', { name: 'Create Calendar event' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Execute Gmail send' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check Calendar change status' })).toBeInTheDocument()
    unmount()

    renderProposalCard(createCalendarProposal('executing'))
    expect(screen.queryByRole('button', { name: 'Create Calendar event' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Execute Gmail send' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check Calendar change status' })).toBeInTheDocument()
  })

  it('removes a confirmed deletion from inactive month, week and day caches by identity', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const target = { id: 'event-1', summary: 'Design review' }
    const similar = { id: 'event-2', summary: 'Design review backup' }
    const later = { id: 'event-3', summary: 'Later event' }
    for (const view of ['month', 'week', 'day']) {
      queryClient.setQueryData(
        calendarViewQueryKey(view),
        multiPageCalendarData([target, similar], [later]),
      )
    }
    const detailKey = ['calendar-event', 'user-1', 'connection-1', 'event-1']
    queryClient.setQueryData(detailKey, target)
    const otherConnectionKey = calendarViewQueryKey('week', 'connection-2')
    queryClient.setQueryData(
      otherConnectionKey,
      multiPageCalendarData([target], []),
    )
    const proposal = {
      ...createCalendarMutationProposal('calendar.delete_event', 'completed'),
      result: { outcome: 'deleted', google_event_id: 'event-1' },
    }

    renderProposalCard(proposal, queryClient)

    await waitFor(() => {
      for (const view of ['month', 'week', 'day']) {
        expect(cachedEventIds(queryClient, calendarViewQueryKey(view)))
          .not.toContain('event-1')
      }
    })
    for (const view of ['month', 'week', 'day']) {
      expect(cachedEventIds(queryClient, calendarViewQueryKey(view)))
        .toEqual(['event-2', 'event-3'])
    }
    const weekData = queryClient.getQueryData(calendarViewQueryKey('week'))
    expect(weekData.pages[0].next_page_token).toBe('page-2')
    expect(weekData.pages[1].next_page_token).toBeNull()
    expect(weekData.pageParams).toEqual([undefined, 'page-2'])
    expect(queryClient.getQueryData(detailKey)).toBeUndefined()
    expect(cachedEventIds(queryClient, otherConnectionKey)).toContain('event-1')
  })

  it('preserves cached events when the authoritative outcome is not a confirmed delete', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const target = { id: 'event-1', summary: 'Design review' }
    const similar = { id: 'event-2', summary: 'Design review backup' }
    queryClient.setQueryData(
      calendarEventsQueryKey(),
      multiPageCalendarData([target, similar], []),
    )
    const proposal = {
      ...createCalendarMutationProposal('calendar.delete_event', 'failed'),
      result: { outcome: 'delete_failed', reason: 'provider_rejected' },
    }

    renderProposalCard(proposal, queryClient)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Google Calendar rejected the request/,
    )
    expect(cachedEventIds(queryClient, calendarEventsQueryKey()))
      .toEqual(['event-1', 'event-2'])
  })

  it('uses recurring identity for single-instance and whole-series deletions', async () => {
    const master = { id: 'master-1', summary: 'Gym' }
    const instanceOne = {
      id: 'instance-1',
      summary: 'Gym',
      recurring_event_id: 'master-1',
    }
    const instanceTwo = {
      id: 'instance-2',
      summary: 'Gym',
      recurring_event_id: 'master-1',
    }
    const similar = { id: 'other-1', summary: 'Gym backup' }
    const seedEvents = [master, instanceOne, instanceTwo]

    const singleQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    singleQueryClient.setQueryData(
      calendarEventsQueryKey(),
      multiPageCalendarData(seedEvents, [similar]),
    )
    const singleProposal = createCalendarMutationProposal(
      'calendar.delete_event',
      'completed',
    )
    singleProposal.parameters.event_id = 'instance-1'
    singleProposal.status = 'completed'
    singleProposal.result = {
      outcome: 'deleted',
      google_event_id: 'instance-1',
    }
    const firstView = renderProposalCard(singleProposal, singleQueryClient)

    await waitFor(() => {
      expect(cachedEventIds(singleQueryClient, calendarEventsQueryKey()))
        .not.toContain('instance-1')
    })
    expect(cachedEventIds(singleQueryClient, calendarEventsQueryKey()))
      .toEqual(['master-1', 'instance-2', 'other-1'])
    firstView.unmount()

    const seriesQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    seriesQueryClient.setQueryData(
      calendarEventsQueryKey(),
      multiPageCalendarData(seedEvents, [similar]),
    )
    const seriesProposal = createCalendarMutationProposal(
      'calendar.delete_event',
      'completed',
    )
    seriesProposal.parameters.event_id = 'master-1'
    seriesProposal.parameters.delete_entire_series = true
    seriesProposal.status = 'completed'
    seriesProposal.result = { outcome: 'deleted', google_event_id: 'master-1' }

    renderProposalCard(seriesProposal, seriesQueryClient)

    await waitFor(() => {
      expect(cachedEventIds(seriesQueryClient, calendarEventsQueryKey()))
        .not.toContain('master-1')
    })
    expect(cachedEventIds(seriesQueryClient, calendarEventsQueryKey()))
      .toEqual(['other-1'])
  })

  it('synchronizes real cached Calendar data when approval polling reconciles a deletion', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    queryClient.setQueryData(
      calendarEventsQueryKey(),
      multiPageCalendarData(
        [{ id: 'event-1', summary: 'Design review' }],
        [{ id: 'event-2', summary: 'Design review backup' }],
      ),
    )
    const proposal = createCalendarMutationProposal(
      'calendar.delete_event',
      'execution_uncertain',
    )
    actionApprovalService.getProposal.mockResolvedValue({
      ...proposal,
      status: 'completed',
      result: { outcome: 'deleted', google_event_id: 'event-1' },
    })

    renderProposalCard(proposal, queryClient)
    fireEvent.click(screen.getByRole('button', {
      name: 'Check Calendar change status',
    }))

    await waitFor(() => {
      expect(cachedEventIds(queryClient, calendarEventsQueryKey()))
        .not.toContain('event-1')
    })
    expect(cachedEventIds(queryClient, calendarEventsQueryKey()))
      .toEqual(['event-2'])
    expect(await screen.findByText(/confirmed the event was deleted/))
      .toBeInTheDocument()
  })

  it('ignores repeated identical completion responses', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    queryClient.setQueryData(
      calendarEventsQueryKey(),
      multiPageCalendarData(
        [{ id: 'event-1', summary: 'Design review' }],
        [{ id: 'event-2', summary: 'Design review backup' }],
      ),
    )
    const setQueryData = vi.spyOn(queryClient, 'setQueryData')
    const proposal = {
      ...createCalendarMutationProposal('calendar.delete_event', 'completed'),
      result: { outcome: 'deleted', google_event_id: 'event-1' },
    }

    function Harness() {
      const [message, setMessage] = useState(() => ({
        id: 'approval-message-1',
        metadata: { action_approval: proposal },
      }))
      return (
        <QueryClientProvider client={queryClient}>
          <button
            type="button"
            onClick={() => setMessage((current) => ({
              ...current,
              metadata: {
                ...current.metadata,
                action_approval: { ...proposal },
              },
            }))}
          >
            Repeat completion
          </button>
          <ActionApprovalCard
            message={message}
            isDark={false}
            onUpdated={setMessage}
          />
        </QueryClientProvider>
      )
    }

    render(<Harness />)

    await waitFor(() => {
      expect(cachedEventIds(queryClient, calendarEventsQueryKey()))
        .toEqual(['event-2'])
    })
    const listUpdates = () => setQueryData.mock.calls.filter(([key]) => (
      key[0] === 'calendar-events' &&
      key[5] === 'primary' &&
      key[2] === 'connection-1'
    ))
    expect(listUpdates()).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Repeat completion' }))

    expect(cachedEventIds(queryClient, calendarEventsQueryKey()))
      .toEqual(['event-2'])
    expect(listUpdates()).toHaveLength(1)
  })
})
