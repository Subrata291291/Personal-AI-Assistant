import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { actionApprovalService } from '../services/actionApprovalService.js'
import { formatRequestError } from '../lib/errorMessage.js'

function displayValue(value) {
  if (Array.isArray(value)) return value.join(', ')
  if (value && typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function formatEventDate(value, timeZone) {
  const dateOnly = typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  const date = dateOnly
    ? new Date(`${value}T12:00:00Z`)
    : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value || '')

  try {
    return new Intl.DateTimeFormat('en-US', {
      month: 'long',
      day: 'numeric',
      year: 'numeric',
      timeZone: dateOnly ? 'UTC' : timeZone,
    }).format(date)
  } catch {
    return String(value || '')
  }
}

function formatEventTime(value, timeZone) {
  if (typeof value !== 'string' || !value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value

  try {
    return new Intl.DateTimeFormat('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
      timeZone,
    }).format(date)
  } catch {
    return value
  }
}

function formatAllDayEndDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return value
  }
  const date = new Date(`${value}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() - 1)
  return date.toISOString().slice(0, 10)
}

function eventDateTime(value) {
  if (typeof value === 'string') {
    return { dateTime: value, timeZone: null, allDay: false }
  }
  if (!value || typeof value !== 'object') {
    return { dateTime: null, timeZone: null, allDay: false }
  }
  return {
    dateTime: value.dateTime || value.date_time || value.date || null,
    timeZone: value.timeZone || value.time_zone || null,
    allDay: Boolean(value.date && !value.dateTime),
  }
}

function getCalendarEventDetails(proposal, original = false) {
  const parameters = proposal.parameters
  if (proposal.action_type === 'calendar.create_event') {
    return {
      title: parameters.summary,
      start: eventDateTime(parameters.start),
      end: eventDateTime(parameters.end),
      timeZone: parameters.time_zone,
      calendarId: parameters.calendar_id,
      allDay: parameters.all_day === true,
    }
  }

  const currentEvent = parameters.current_event || {}
  const currentStart = eventDateTime(currentEvent.start)
  const currentEnd = eventDateTime(currentEvent.end)
  if (proposal.action_type === 'calendar.delete_event' || original) {
    return {
      title: currentEvent.summary,
      start: currentStart,
      end: currentEnd,
      timeZone: currentStart.timeZone
        || currentEnd.timeZone
        || parameters.time_zone,
      calendarId: parameters.calendar_id,
      allDay: currentStart.allDay,
      attendees: currentEvent.attendees || [],
    }
  }
  if (proposal.action_type !== 'calendar.update_event') return null

  const changes = parameters.changes || {}
  const start = eventDateTime(changes.start || currentEvent.start)
  const end = eventDateTime(changes.end || currentEvent.end)
  return {
    title: changes.summary || currentEvent.summary,
    start,
    end,
    timeZone: parameters.time_zone || start.timeZone || end.timeZone,
    calendarId: parameters.calendar_id,
    allDay: start.allDay,
    attendees: changes.attendees || currentEvent.attendees || [],
  }
}

function formatEventChangeValue(key, value, timeZone) {
  if (Array.isArray(value)) {
    return value
      .map((item) => (
        typeof item === 'string'
          ? item
          : item?.email || item?.display_name || item?.displayName
      ))
      .filter(Boolean)
      .join(', ') || 'None listed'
  }
  if (key !== 'start' && key !== 'end') return displayValue(value)
  const eventTime = eventDateTime(value)
  if (eventTime.allDay) {
    return `${formatEventDate(eventTime.dateTime, 'UTC')} (all day)`
  }
  if (eventTime.dateTime) {
    const date = formatEventDate(eventTime.dateTime, timeZone)
    const time = formatEventTime(eventTime.dateTime, timeZone)
    return `${date} ${time}`.trim()
  }
  return displayValue(value)
}

function CalendarEventDetails({ details, label, isDark }) {
  if (!details) return null
  const localStartDate = details.start.dateTime
    ? formatEventDate(
      details.start.dateTime,
      details.timeZone || 'UTC',
    )
    : ''
  const localEndDate = details.end.dateTime
    ? formatEventDate(
      details.allDay
        ? formatAllDayEndDate(details.end.dateTime)
        : details.end.dateTime,
      details.timeZone || 'UTC',
    )
    : ''
  const isMultiDay = localStartDate && localEndDate &&
    localStartDate !== localEndDate

  return (
    <section
      aria-label={label}
      className={`rounded-lg px-3 py-3 ${
        isDark ? 'bg-[#171a27] text-slate-200' : 'bg-slate-50 text-slate-700'
      }`}
    >
      <h4 className={`text-[11px] font-bold uppercase tracking-wide ${
        isDark ? 'text-slate-400' : 'text-slate-500'
      }`}>
        {label}
      </h4>
      <dl className="mt-2 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-[110px_1fr]">
        {details.title && (
          <>
            <dt className="font-semibold">Title</dt>
            <dd className="break-words">{details.title}</dd>
          </>
        )}
        {details.start.dateTime && (
          <>
            <dt className="font-semibold">Date</dt>
            <dd>{isMultiDay
              ? `${localStartDate} – ${localEndDate}`
              : localStartDate}
            </dd>
          </>
        )}
        {details.allDay ? (
          <>
            <dt className="font-semibold">Event timing</dt>
            <dd>All day</dd>
          </>
        ) : (
          <>
            {details.start.dateTime && (
              <>
                <dt className="font-semibold">Start time</dt>
                <dd>
                  {formatEventTime(
                    details.start.dateTime,
                    details.timeZone || 'UTC',
                  )}
                </dd>
              </>
            )}
            {details.end.dateTime && (
              <>
                <dt className="font-semibold">End time</dt>
                <dd>
                  {formatEventTime(
                    details.end.dateTime,
                    details.timeZone || 'UTC',
                  )}
                  {isMultiDay && ` (${localEndDate})`}
                </dd>
              </>
            )}
            {details.timeZone && (
              <>
                <dt className="font-semibold">Timezone</dt>
                <dd>{details.timeZone}</dd>
              </>
            )}
          </>
        )}
        {details.calendarId && (
          <>
            <dt className="font-semibold">Calendar</dt>
            <dd>{details.calendarId}</dd>
          </>
        )}
        {details.attendees && (
          <>
            <dt className="font-semibold">Attendees</dt>
            <dd className="break-words">
              {details.attendees.length
                ? details.attendees
                  .map((attendee) => (
                    typeof attendee === 'string'
                      ? attendee
                      : attendee.email || attendee.display_name || attendee.displayName
                  ))
                  .filter(Boolean)
                  .join(', ') || 'None listed'
                : 'None listed'}
            </dd>
          </>
        )}
      </dl>
    </section>
  )
}

function updateProposalMessage(message, proposal, onUpdated) {
  onUpdated({
    ...message,
    metadata: {
      ...message.metadata,
      action_approval: proposal,
    },
  })
}

function ActionApprovalCard({ message, isDark, onUpdated }) {
  const queryClient = useQueryClient()
  const proposal = message.metadata.action_approval
  const isCalendarCreate = proposal.action_type === 'calendar.create_event'
  const isCalendarUpdate = proposal.action_type === 'calendar.update_event'
  const isCalendarDelete = proposal.action_type === 'calendar.delete_event'
  const isCalendarAction = isCalendarCreate || isCalendarUpdate || isCalendarDelete
  const calendarActionLabel = isCalendarCreate
    ? 'create'
    : isCalendarUpdate
      ? 'update'
      : 'delete'
  const calendarActionTitle = {
    create: 'Create',
    update: 'Update',
    delete: 'Delete',
  }[calendarActionLabel]
  const calendarActionNoun = {
    create: 'creation',
    update: 'update',
    delete: 'deletion',
  }[calendarActionLabel]
  const calendarOutcome = {
    create: 'created',
    update: 'updated',
    delete: 'deleted',
  }[calendarActionLabel]
  const calendarActionProgress = {
    create: 'Checking Calendar execution...',
    update: 'Checking Calendar execution...',
    delete: 'Checking Calendar execution...',
  }[calendarActionLabel]
  const [error, setError] = useState('')
  const [expired, setExpired] = useState(false)
  const [executionAttemptFailed, setExecutionAttemptFailed] = useState(false)
  const requestInProgress = useRef(false)
  const availabilityRefreshProposalId = useRef(null)
  useEffect(() => {
    if (proposal.status !== 'pending') return undefined

    const expiresIn = new Date(proposal.expires_at).getTime() - Date.now()
    const timeoutId = window.setTimeout(
      () => setExpired(true),
      Math.max(0, expiresIn),
    )
    return () => window.clearTimeout(timeoutId)
  }, [proposal.expires_at, proposal.status])

  const mutation = useMutation({
    mutationFn: (decision) =>
      decision === 'approve'
        ? actionApprovalService.approve(proposal.proposal_id)
        : actionApprovalService.reject(proposal.proposal_id),
    onSuccess: async (response, decision) => {
      setError('')
      updateProposalMessage(message, response.proposal, onUpdated)
      if (decision === 'approve' && isCalendarAction) {
        try {
          const latest = await actionApprovalService.getProposal(
            proposal.proposal_id,
          )
          updateProposalMessage(message, latest, onUpdated)
        } catch (statusError) {
          setError(formatRequestError(
            statusError,
            'Approval succeeded, but Calendar execution availability could not be refreshed.',
          ))
        }
      }
    },
    onError: (mutationError) => {
      setError(formatRequestError(
        mutationError,
        'Unable to update this approval proposal.',
      ))
    },
    onSettled: () => {
      requestInProgress.current = false
    },
  })

  const executeMutation = useMutation({
    mutationFn: () =>
      actionApprovalService.execute(proposal.proposal_id),
    onSuccess: async (response) => {
      setError('')
      setExecutionAttemptFailed(false)
      updateProposalMessage(message, response.proposal, onUpdated)
      if (
        isCalendarAction &&
        response.proposal.status === 'completed' &&
        response.proposal.result?.outcome === calendarOutcome
      ) {
        await Promise.all([
          queryClient.invalidateQueries({
            queryKey: ['calendar-events'],
            refetchType: 'all',
          }),
          queryClient.invalidateQueries({
            queryKey: ['calendar-event'],
            refetchType: 'all',
          }),
        ])
      }
    },
    onError: async (mutationError) => {
      const fallback = isCalendarAction
        ? 'The Calendar change outcome could not be confirmed.'
        : 'The Gmail send outcome could not be confirmed.'
      setError(formatRequestError(mutationError, fallback))
      setExecutionAttemptFailed(true)
      try {
        const latest = await actionApprovalService.getProposal(
          proposal.proposal_id,
        )
        updateProposalMessage(message, latest, onUpdated)
        if (isCalendarAction && latest.execution_available === false) {
          setError('')
          setExecutionAttemptFailed(false)
        }
      } catch (statusError) {
        setError(
          `${formatRequestError(mutationError, fallback)} Status check failed: ${formatRequestError(
            statusError,
            isCalendarAction
              ? 'Check the Calendar change status before taking further action.'
              : 'Check the send status before taking further action.',
          )}`,
        )
      }
    },
    onSettled: () => {
      requestInProgress.current = false
    },
  })

  const statusMutation = useMutation({
    mutationFn: () =>
      actionApprovalService.getProposal(proposal.proposal_id),
    onSuccess: (latest) => {
      setError('')
      updateProposalMessage(message, latest, onUpdated)
    },
    onError: (mutationError) => {
      setError(formatRequestError(
        mutationError,
        isCalendarAction
          ? 'Unable to refresh the Calendar change status.'
          : 'Unable to refresh the Gmail send status.',
      ))
    },
    onSettled: () => {
      requestInProgress.current = false
    },
  })
  const refreshProposalStatus = statusMutation.mutate

  useEffect(() => {
    if (
      !isCalendarAction ||
      proposal.status !== 'approved' ||
      typeof proposal.execution_available === 'boolean' ||
      availabilityRefreshProposalId.current === proposal.proposal_id
    ) {
      return
    }
    availabilityRefreshProposalId.current = proposal.proposal_id
    refreshProposalStatus()
  }, [
    isCalendarAction,
    proposal.execution_available,
    proposal.proposal_id,
    proposal.status,
    refreshProposalStatus,
  ])

  const isExpired = proposal.status === 'pending' && expired
  const visibleStatus = isExpired ? 'expired' : proposal.status
  const isPending = visibleStatus === 'pending'
  const approvalStatus = {
    pending: 'Pending',
    approved: 'Approved',
    rejected: proposal.result?.outcome === 'superseded'
      ? 'Superseded'
      : 'Rejected',
    expired: 'Expired',
  }[visibleStatus] || 'Approved'
  const executionStatus = visibleStatus === 'approved'
    ? isCalendarAction
      ? proposal.execution_available === false
        ? 'Disabled'
        : executionAttemptFailed
          ? 'Failed'
          : proposal.execution_available === true
            ? 'Available'
            : 'Not confirmed'
      : 'Not started'
    : {
      executing: 'In Progress',
      completed: 'Completed',
      failed: 'Failed',
      execution_uncertain: 'Unconfirmed',
    }[visibleStatus] || 'Not started'
  function resolveProposal(decision) {
    if (requestInProgress.current || mutation.isPending) return
    requestInProgress.current = true
    mutation.mutate(decision)
  }
  function executeProposal() {
    if (
      requestInProgress.current ||
      mutation.isPending ||
      executeMutation.isPending ||
      !['gmail.send', 'calendar.create_event', 'calendar.update_event', 'calendar.delete_event'].includes(proposal.action_type) ||
      proposal.status !== 'approved'
    ) {
      return
    }
    requestInProgress.current = true
    setError('')
    executeMutation.mutate()
  }
  function refreshExecutionStatus() {
    if (requestInProgress.current || statusMutation.isPending) return
    requestInProgress.current = true
    statusMutation.mutate()
  }
  const statusStyles = {
    pending: isDark
      ? 'bg-amber-500/10 text-amber-300'
      : 'bg-amber-50 text-amber-700',
    approved: isDark
      ? 'bg-emerald-500/10 text-emerald-300'
      : 'bg-emerald-50 text-emerald-700',
    rejected: isDark
      ? 'bg-slate-500/10 text-slate-300'
      : 'bg-slate-100 text-slate-600',
    expired: isDark
      ? 'bg-slate-500/10 text-slate-300'
      : 'bg-slate-100 text-slate-600',
    executing: isDark
      ? 'bg-violet-500/10 text-violet-300'
      : 'bg-violet-50 text-violet-700',
    completed: isDark
      ? 'bg-emerald-500/10 text-emerald-300'
      : 'bg-emerald-50 text-emerald-700',
    failed: isDark
      ? 'bg-rose-500/10 text-rose-300'
      : 'bg-rose-50 text-rose-700',
    execution_uncertain: isDark
      ? 'bg-rose-500/10 text-rose-300'
      : 'bg-rose-50 text-rose-700',
  }[visibleStatus]
  const executionStatusStyles = executionStatus === 'Disabled'
    ? isDark
      ? 'bg-slate-500/10 text-slate-300'
      : 'bg-slate-100 text-slate-600'
    : executionStatus === 'Failed' || executionStatus === 'Unconfirmed'
      ? isDark
        ? 'bg-rose-500/10 text-rose-300'
        : 'bg-rose-50 text-rose-700'
      : executionStatus === 'Available' || executionStatus === 'In Progress'
        ? isDark
          ? 'bg-violet-500/10 text-violet-300'
          : 'bg-violet-50 text-violet-700'
        : statusStyles
  const canExecute = (
    ['gmail.send', 'calendar.create_event', 'calendar.update_event', 'calendar.delete_event']
      .includes(proposal.action_type)
    && (!isCalendarAction || proposal.execution_available === true)
  )
  const calendarEventDetails = getCalendarEventDetails(proposal)
  const originalCalendarEventDetails = isCalendarUpdate
    ? getCalendarEventDetails(proposal, true)
    : null
  const updateChanges = isCalendarUpdate
    ? Object.entries(proposal.parameters.changes || {})
    : []

  return (
    <section
      aria-label="Action approval proposal"
      className={`mt-4 overflow-hidden rounded-xl border text-left shadow-sm ${
        isDark
          ? 'border-amber-500/25 bg-[#191b29]'
          : 'border-amber-200 bg-white'
      }`}
    >
      <header className={`flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3 ${
        isDark
          ? 'border-[#303245] bg-amber-500/5'
          : 'border-amber-100 bg-amber-50/70'
      }`}>
        <div>
          <h3 className="text-sm font-semibold">Review proposed action</h3>
          <p className={`mt-0.5 text-xs ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
            {isCalendarAction
              ? `Google Calendar event ${calendarActionNoun} · Approval and execution are separate`
              : `${proposal.action_type} · Approval and execution are separate`}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${statusStyles}`}>
            {`Approval: ${approvalStatus}`}
          </span>
          <span className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${executionStatusStyles}`}>
            {`Execution: ${executionStatus}`}
          </span>
        </div>
      </header>

      <div className="space-y-3 p-4">
        <CalendarEventDetails
          details={calendarEventDetails}
          label={isCalendarUpdate
            ? 'Proposed event'
            : isCalendarDelete
              ? 'Event to delete'
              : 'Event details'}
          isDark={isDark}
        />
        {originalCalendarEventDetails && (
          <CalendarEventDetails
            details={originalCalendarEventDetails}
            label="Original event"
            isDark={isDark}
          />
        )}
        {updateChanges.length > 0 && (
          <section
            aria-label="Proposed changes"
            className={`rounded-lg px-3 py-3 ${
              isDark ? 'bg-[#171a27] text-slate-200' : 'bg-slate-50 text-slate-700'
            }`}
          >
            <h4 className={`text-[11px] font-bold uppercase tracking-wide ${
              isDark ? 'text-slate-400' : 'text-slate-500'
            }`}>
              Proposed changes
            </h4>
            <dl className="mt-2 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-[110px_1fr]">
              {updateChanges.map(([key, value]) => {
                const originalValue = proposal.parameters.current_event?.[key]
                const timeZone = proposal.parameters.time_zone
                  || 'UTC'
                const originalTimeZone = originalCalendarEventDetails.timeZone
                  || timeZone
                return (
                  <div
                    key={key}
                    className="grid gap-1 border-t border-slate-200/50 py-2 first:border-0 first:pt-0 sm:col-span-2 sm:grid-cols-[110px_1fr_1fr]"
                  >
                    <dt className="font-semibold capitalize">
                      {key.replaceAll('_', ' ')}
                    </dt>
                    <dd className="break-words">
                      <span className="font-semibold">Original: </span>
                      {originalValue === undefined
                        ? 'Not set'
                        : formatEventChangeValue(
                          key,
                          originalValue,
                          originalTimeZone,
                        )}
                    </dd>
                    <dd className="break-words">
                      <span className="font-semibold">Proposed: </span>
                      {formatEventChangeValue(key, value, timeZone)}
                    </dd>
                  </div>
                )
              })}
            </dl>
          </section>
        )}
        <div className={`rounded-lg px-3 py-2 text-xs ${
          isDark ? 'bg-[#171a27] text-slate-300' : 'bg-slate-50 text-slate-700'
        }`}>
          <span className="font-semibold">Account: </span>
          {proposal.account_email || 'Google account'}
          <span className="ml-1 font-mono text-[10px] opacity-70">
            ({proposal.connection_id})
          </span>
        </div>

        <dl className={`divide-y text-xs ${
          isDark ? 'divide-[#303245]' : 'divide-slate-100'
        }`}>
          {Object.entries(proposal.parameters)
            .filter(([key]) => ![
              'expected_etag',
              'current_event',
              ...(isCalendarUpdate ? ['changes'] : []),
              ...(isCalendarCreate
                ? ['summary', 'start', 'end', 'time_zone', 'calendar_id', 'all_day']
                : []),
            ].includes(key))
            .map(([key, value]) => (
            <div key={key} className="grid gap-1 py-2 sm:grid-cols-[110px_1fr]">
              <dt className={`font-semibold ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
                {key === 'summary' && isCalendarCreate
                  ? 'Event title'
                  : key === 'changes' && isCalendarUpdate
                    ? 'Requested changes'
                    : key === 'event_id' && (isCalendarUpdate || isCalendarDelete)
                      ? 'Event ID'
                  : key === 'calendar_id' && isCalendarCreate
                    ? 'Calendar'
                    : key === 'start' && isCalendarCreate
                      ? 'Start'
                      : key === 'end' && isCalendarCreate
                        ? 'End (exclusive for all-day)'
                        : key === 'all_day' && isCalendarCreate
                          ? 'All-day event'
                          : key.replaceAll('_', ' ')}
              </dt>
              <dd className={`break-words whitespace-pre-wrap ${
                isDark ? 'text-slate-200' : 'text-slate-700'
              }`}>
                {displayValue(value)}
              </dd>
            </div>
          ))}
        </dl>

        <p className={`text-[11px] ${isDark ? 'text-slate-500' : 'text-slate-400'}`}>
          Expires {new Date(proposal.expires_at).toLocaleString()}
        </p>

        {error && (
          <p role="alert" className="text-xs text-rose-400">
            {error}
          </p>
        )}

        {visibleStatus === 'approved' && (
          <p role="status" className="text-xs text-emerald-500">
            {isCalendarAction && proposal.execution_available === false
              ? 'Your approval has been recorded. Calendar execution is currently disabled. No event has been created.'
              : isCalendarAction
                ? proposal.execution_available === true
                  ? 'Your approval has been recorded. Calendar execution is available, but no Calendar change has been made yet.'
                  : 'Your approval has been recorded. Calendar execution availability has not been confirmed. No Calendar change has been made.'
              : 'Approved by you. No email will be sent until you explicitly choose Execute.'}
          </p>
        )}
        {isCalendarAction &&
          executionAttemptFailed &&
          visibleStatus === 'approved' && (
          <p role="alert" className="text-xs text-rose-400">
            Calendar change execution failed. No successful change was confirmed.
          </p>
          )}
        {visibleStatus === 'executing' && (
          <p role="status" className="text-xs text-violet-400">
            {isCalendarAction
              ? 'The Calendar change was claimed. If interrupted, its outcome will be reconciled without automatically repeating the mutation.'
              : 'The send was claimed. If the worker was interrupted, recovery will mark it uncertain; it will not be sent again automatically.'}
          </p>
        )}
        {['executing', 'execution_uncertain'].includes(visibleStatus) && (
          <div className="flex justify-end">
            <button
              type="button"
              onClick={refreshExecutionStatus}
              disabled={statusMutation.isPending}
              className={`rounded-lg border px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${
                isDark
                  ? 'border-[#34384b] text-slate-300 hover:bg-white/5'
                  : 'border-slate-200 text-slate-700 hover:bg-slate-50'
              }`}
            >
              {statusMutation.isPending
                ? 'Checking...'
                : isCalendarAction
                  ? 'Check Calendar change status'
                  : 'Check send status'}
            </button>
          </div>
        )}
        {visibleStatus === 'completed' && (
          <p role="status" className="text-xs text-emerald-500">
            {isCalendarAction
              ? `Google Calendar confirmed the event was ${proposal.result?.outcome || calendarOutcome}.`
              : 'Gmail confirmed the message was sent.'}
            {isCalendarAction && proposal.result?.google_event_id && (
              <span className="ml-1 font-mono">
                Event ID: {proposal.result.google_event_id}
              </span>
            )}
            {!isCalendarCreate && proposal.result?.gmail_message_id && (
              <span className="ml-1 font-mono">
                Message ID: {proposal.result.gmail_message_id}
              </span>
            )}
          </p>
        )}
        {visibleStatus === 'failed' && (
          <p role="alert" className="text-xs text-rose-400">
            {isCalendarAction && proposal.result?.reason === 'provider_rejected'
              ? `Google Calendar rejected the request${
                Number.isInteger(proposal.result.provider_status_code)
                  ? ` (HTTP ${proposal.result.provider_status_code})`
                  : ''
              }. No Calendar change was made.`
              : isCalendarAction
                ? 'Calendar change execution failed. This proposal cannot be retried.'
              : 'Gmail rejected the send request. This proposal cannot be retried.'}
          </p>
        )}
        {visibleStatus === 'execution_uncertain' && (
          <p role="alert" className="text-xs text-rose-400">
            {isCalendarAction
              ? 'The Calendar change outcome is unconfirmed. Do not retry this proposal or create a replacement action.'
              : 'Gmail may have sent the message, but the result is unconfirmed. Do not retry this proposal.'}
          </p>
        )}
        {isExpired && (
          <p role="status" className="text-xs text-slate-400">
            This proposal has expired and cannot be approved.
          </p>
        )}

        {isPending && (
          <div className="flex flex-col-reverse gap-2 border-t pt-3 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={() => resolveProposal('reject')}
              disabled={mutation.isPending}
              className={`rounded-lg border px-4 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${
                isDark
                  ? 'border-[#34384b] text-slate-300 hover:bg-white/5'
                  : 'border-slate-200 text-slate-700 hover:bg-slate-50'
              }`}
            >
              {mutation.isPending && mutation.variables === 'reject'
                ? 'Rejecting...'
                : 'Reject'}
            </button>
            <button
              type="button"
              onClick={() => resolveProposal('approve')}
              disabled={mutation.isPending}
              className={`rounded-lg px-4 py-2 text-xs font-semibold text-white transition disabled:cursor-not-allowed disabled:opacity-50 ${
                isCalendarDelete
                  ? 'bg-rose-600 hover:bg-rose-500'
                  : 'bg-violet-600 hover:bg-violet-500'
              }`}
            >
              {mutation.isPending && mutation.variables === 'approve'
                ? 'Approving...'
                : 'Approve'}
            </button>
          </div>
        )}
        {proposal.status === 'approved' && canExecute && (
          <div className={`flex justify-end border-t pt-3 ${
            isDark ? 'border-[#303245]' : 'border-slate-100'
          }`}>
            <button
              type="button"
              onClick={executeProposal}
              disabled={mutation.isPending || executeMutation.isPending}
              className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {executeMutation.isPending
                ? isCalendarAction
                  ? calendarActionProgress
                  : 'Sending...'
                : isCalendarAction
                  ? `${calendarActionTitle} Calendar event`
                  : 'Execute Gmail send'}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

export default ActionApprovalCard
