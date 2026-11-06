import { useEffect, useMemo, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api.js'
import {
  requireConnectionsResponse,
} from '../lib/responseValidation.js'
import { calendarService } from '../services/calendarService.js'

const EMPTY_CONNECTIONS = []
const CONNECTIONS_QUERY_KEY = (userId) => ['oauth-connections', userId]

function getTimeZone(user) {
  const configured = user?.user_metadata?.timezone
  if (typeof configured === 'string' && configured) {
    try {
      new Intl.DateTimeFormat('en', { timeZone: configured })
      return configured
    } catch {
      // Fall back to the browser's configured IANA timezone.
    }
  }
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Kolkata'
}

function dateInZone(date, timeZone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date)
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]))
  return `${values.year}-${values.month}-${values.day}`
}

function addDays(date, count) {
  const parsed = new Date(`${date}T00:00:00Z`)
  parsed.setUTCDate(parsed.getUTCDate() + count)
  return parsed.toISOString().slice(0, 10)
}

function getMonthDateRange(date) {
  const [year, month] = date.split('-').map(Number)
  const nextMonth = new Date(Date.UTC(year, month, 1))
  return {
    start: `${year}-${String(month).padStart(2, '0')}-01`,
    end: nextMonth.toISOString().slice(0, 10),
  }
}

function localDateTimeToIso(value, timeZone) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value)
  if (!match) throw new Error('Enter a valid local date and time.')
  const [, year, month, day, hour, minute] = match.map(Number)
  const wallClockUtc = Date.UTC(year, month - 1, day, hour, minute)
  const format = new Intl.DateTimeFormat('en-GB', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  })
  const parts = Object.fromEntries(
    format.formatToParts(new Date(wallClockUtc))
      .map(({ type, value: partValue }) => [type, partValue]),
  )
  const zoneWallClockUtc = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(parts.hour),
    Number(parts.minute),
  )
  const instant = new Date(wallClockUtc - (zoneWallClockUtc - wallClockUtc))
  const verified = Object.fromEntries(
    format.formatToParts(instant).map(({ type, value: partValue }) => [
      type,
      partValue,
    ]),
  )
  if (
    Number(verified.year) !== year ||
    Number(verified.month) !== month ||
    Number(verified.day) !== day ||
    Number(verified.hour) !== hour ||
    Number(verified.minute) !== minute
  ) {
    throw new Error('That local time is ambiguous or does not exist.')
  }
  return instant.toISOString()
}

function formatEventTime(value, timeZone) {
  if (!value) return 'Time unavailable'
  if (value.date) return `${value.date} · All day`
  if (!value.date_time) return 'Time unavailable'
  const instant = new Date(value.date_time)
  if (Number.isNaN(instant.getTime())) return 'Time unavailable'
  return new Intl.DateTimeFormat(undefined, {
    timeZone,
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(instant)
}

function EventCard({ event, timeZone, onSelect, isDark }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full rounded-xl border p-4 text-left transition ${
        isDark
          ? 'border-[#252838] bg-[#11131c] hover:border-violet-500/50'
          : 'border-slate-200 bg-white hover:border-violet-400'
      }`}
    >
      <span className="block font-semibold">
        {event.summary?.trim() || 'Untitled event'}
      </span>
      <span className="mt-1 block text-sm text-slate-400">
        {formatEventTime(event.start, timeZone)} – {formatEventTime(event.end, timeZone)}
      </span>
      {event.location && (
        <span className="mt-1 block text-sm text-slate-400">{event.location}</span>
      )}
    </button>
  )
}

export default function Calendar() {
  const { user } = useAuth()
  const { isDark } = useOutletContext()
  const userId = user?.id
  const timeZone = useMemo(() => getTimeZone(user), [user])
  const [today, setToday] = useState(() => dateInZone(new Date(), timeZone))
  const [connectionId, setConnectionId] = useState('')
  const [mode, setMode] = useState('today')
  const [selectedDate, setSelectedDate] = useState(today)
  const [rangeStart, setRangeStart] = useState(today)
  const [rangeEnd, setRangeEnd] = useState(addDays(today, 1))
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [eventId, setEventId] = useState('')
  const [freeBusyStart, setFreeBusyStart] = useState(`${today}T14:00`)
  const [freeBusyEnd, setFreeBusyEnd] = useState(`${today}T16:00`)
  const [freeBusyResult, setFreeBusyResult] = useState(null)
  const [freeBusyError, setFreeBusyError] = useState('')
  const [freeBusyLoading, setFreeBusyLoading] = useState(false)

  useEffect(() => {
    const refreshToday = () => {
      const currentDate = dateInZone(new Date(), timeZone)
      setToday((previousDate) => (
        previousDate === currentDate ? previousDate : currentDate
      ))
    }
    const startupTimeoutId = setTimeout(refreshToday, 0)
    const intervalId = setInterval(refreshToday, 60_000)
    return () => {
      clearTimeout(startupTimeoutId)
      clearInterval(intervalId)
    }
  }, [timeZone])

  const connectionsQuery = useQuery({
    queryKey: CONNECTIONS_QUERY_KEY(userId),
    queryFn: async () => requireConnectionsResponse(
      await api.get('/api/v1/oauth/connections'),
    ),
    enabled: Boolean(userId),
    staleTime: 0,
    gcTime: 30 * 60 * 1000,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  })
  const connections = connectionsQuery.data?.connections || EMPTY_CONNECTIONS
  const calendarConnections = useMemo(
    () => connections.filter((connection) =>
      connection.provider === 'google' &&
      connection.status === 'connected' &&
      connection.capabilities?.calendar_events_read === true
    ),
    [connections],
  )
  const configuredDefault = user?.user_metadata?.default_calendar_connection_id

  useEffect(() => {
    if (!connectionsQuery.isSuccess || connectionsQuery.isFetching) return
    if (connectionId && calendarConnections.some(
      (connection) => connection.id === connectionId,
    )) return
    if (connectionId) {
      setConnectionId('')
      return
    }
    if (configuredDefault) {
      if (calendarConnections.some(
        (connection) => connection.id === configuredDefault,
      )) {
        setConnectionId(configuredDefault)
      }
      return
    }
    if (calendarConnections.length === 1) {
      setConnectionId(calendarConnections[0].id)
    }
  }, [
    calendarConnections,
    configuredDefault,
    connectionId,
    connectionsQuery.isFetching,
    connectionsQuery.isSuccess,
  ])

  const selectedConnection = calendarConnections.find(
    (connection) => connection.id === connectionId,
  )
  const connectionsVerified =
    connectionsQuery.isSuccess &&
    !connectionsQuery.isFetching &&
    !connectionsQuery.isError
  const queryKey = [
    'calendar-events',
    userId,
    connectionId,
    timeZone,
    mode,
    selectedDate,
    rangeStart,
    rangeEnd,
    searchQuery,
    today,
  ]
  const eventsQuery = useQuery({
    queryKey,
    enabled: Boolean(connectionId) && mode !== 'freebusy' && !eventId,
    retry: false,
    queryFn: async () => {
      if (mode === 'today') {
        return calendarService.getToday({ connectionId, timeZone })
      }
      if (mode === 'tomorrow') {
        return calendarService.getSchedule({
          connectionId,
          date: addDays(today, 1),
          timeZone,
        })
      }
      if (mode === 'date' || mode === 'week') {
        return calendarService.getSchedule({
          connectionId,
          date: selectedDate,
          period: mode === 'week' ? 'week' : 'day',
          timeZone,
        })
      }
      if (mode === 'month') {
        const monthRange = getMonthDateRange(selectedDate)
        return calendarService.listEvents({
          connectionId,
          timeMin: localDateTimeToIso(
            `${monthRange.start}T00:00`,
            timeZone,
          ),
          timeMax: localDateTimeToIso(
            `${monthRange.end}T00:00`,
            timeZone,
          ),
          timeZone,
        })
      }
      const startDate = mode === 'range' ? rangeStart : null
      const endDate = mode === 'range' ? rangeEnd : null
      if (startDate && (!endDate || endDate <= startDate)) {
        throw new Error('The end date must be after the start date.')
      }
      return calendarService.listEvents({
        connectionId,
        timeMin: startDate
          ? localDateTimeToIso(`${startDate}T00:00`, timeZone)
          : undefined,
        timeMax: endDate
          ? localDateTimeToIso(`${endDate}T00:00`, timeZone)
          : undefined,
        query: mode === 'search' ? searchQuery : undefined,
        timeZone,
      })
    },
  })

  const eventQuery = useQuery({
    queryKey: ['calendar-event', userId, connectionId, eventId],
    queryFn: () => calendarService.getEvent({ connectionId, eventId }),
    enabled: Boolean(connectionId && eventId),
    retry: false,
  })

  useEffect(() => {
    setEventId('')
    setFreeBusyResult(null)
    setFreeBusyError('')
  }, [connectionId])

  async function checkFreeBusy(event) {
    event.preventDefault()
    setFreeBusyLoading(true)
    setFreeBusyError('')
    setFreeBusyResult(null)
    try {
      const timeMin = localDateTimeToIso(freeBusyStart, timeZone)
      const timeMax = localDateTimeToIso(freeBusyEnd, timeZone)
      if (timeMax <= timeMin) {
        throw new Error('The end time must be after the start time.')
      }
      const result = await calendarService.getFreeBusy({
        connectionId,
        timeMin,
        timeMax,
        timeZone,
      })
      setFreeBusyResult(result)
    } catch (error) {
      setFreeBusyError(error.message || 'Live Calendar availability could not be retrieved.')
    } finally {
      setFreeBusyLoading(false)
    }
  }

  const colors = {
    panel: isDark
      ? 'border-[#252838] bg-[#11131c]'
      : 'border-slate-200 bg-white',
    text: isDark ? 'text-slate-300' : 'text-slate-600',
    input: isDark
      ? 'border-[#303448] bg-[#171a27] text-slate-100'
      : 'border-slate-200 bg-white text-slate-800',
  }
  const events = eventsQuery.data?.events || []
  const viewButtons = [
    ['today', 'Today'],
    ['tomorrow', 'Tomorrow'],
    ['week', 'Week'],
    ['month', 'Month'],
    ['date', 'Choose date'],
    ['range', 'Date range'],
    ['search', 'Search'],
    ...(selectedConnection?.capabilities?.calendar_freebusy === true
      ? [['freebusy', 'Free/Busy']]
      : []),
  ]

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <header>
        <p className="text-xs font-bold uppercase tracking-widest text-violet-400">
          Google Workspace
        </p>
        <h1 className="mt-2 text-2xl font-bold">Google Calendar</h1>
        <p className={`mt-2 text-sm ${colors.text}`}>
          Live, read-only events in {timeZone}. Calendar changes are not available here.
        </p>
      </header>

      {connectionsQuery.isError && (
        <div role="alert" className="rounded-xl border border-red-500/30 p-4 text-sm text-red-400">
          Unable to verify Google Calendar connections. {connectionsQuery.error.message}
          <button
            type="button"
            className="ml-2 underline"
            onClick={() => connectionsQuery.refetch()}
          >
            Retry
          </button>
        </div>
      )}

      {!connectionsQuery.isError && !connectionsVerified && (
        <div role="status" className={`rounded-xl border p-4 ${colors.panel}`}>
          Checking Google Calendar connection...
        </div>
      )}

      {connectionsVerified && calendarConnections.length === 0 && (
        <div className={`rounded-xl border p-5 ${colors.panel}`}>
          <h2 className="font-semibold">Calendar is not connected</h2>
          <p className={`mt-2 text-sm ${colors.text}`}>
            Connect a Google account with Calendar event-read access to view live events.
          </p>
          <Link className="mt-3 inline-block text-sm font-semibold text-violet-400 underline" to="/dashboard/connectors">
            Connect in Connected Apps
          </Link>
        </div>
      )}

      {connectionsVerified && calendarConnections.length > 0 && (
        <>
          {calendarConnections.length > 1 && (
            <label className="block text-sm">
              <span className="mb-2 block font-medium">Calendar account</span>
              <select
                value={connectionId}
                onChange={(event) => {
                  setConnectionId(event.target.value)
                  setEventId('')
                }}
                className={`w-full rounded-xl border px-3 py-2 ${colors.input}`}
              >
                <option value="">Choose an account</option>
                {calendarConnections.map((connection) => (
                  <option key={connection.id} value={connection.id}>
                    {connection.email || 'Google account'}
                  </option>
                ))}
              </select>
            </label>
          )}

          {configuredDefault && !selectedConnection && (
            <p role="alert" className="rounded-xl border border-amber-500/30 p-4 text-sm text-amber-400">
              Your saved default Calendar account is unavailable or lacks event-read access.
              Choose an account explicitly; another account will not be selected automatically.
            </p>
          )}

          {!selectedConnection && !configuredDefault && calendarConnections.length > 1 && (
            <p role="status" className={`rounded-xl border p-4 text-sm ${colors.panel} ${colors.text}`}>
              Select the Google account whose calendar you want to view.
            </p>
          )}

          {selectedConnection && (
            <>
              <p className={`text-sm ${colors.text}`}>
                Viewing {selectedConnection.email || 'the selected Google account'}.
                {selectedConnection.capabilities?.calendar_freebusy !== true && (
                  <span> Free/Busy is unavailable because its optional permission has not been granted.</span>
                )}
              </p>

              <section className={`rounded-2xl border p-4 sm:p-5 ${colors.panel}`}>
                <div className="mb-4 flex flex-wrap gap-2" role="tablist" aria-label="Calendar views">
                  {viewButtons.map(([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      role="tab"
                      aria-selected={mode === id}
                      onClick={() => {
                        setMode(id)
                        setEventId('')
                      }}
                      className={`rounded-lg px-3 py-2 text-sm ${
                        mode === id
                          ? 'bg-violet-600 text-white'
                          : `${colors.text} border border-slate-500/20`
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>

                {['date', 'week', 'month'].includes(mode) && (
                  <label className="mb-4 block text-sm">
                    {mode === 'week'
                      ? 'Date in week'
                      : mode === 'month'
                        ? 'Date in month'
                        : 'Date'}
                    <input
                      aria-label="Calendar date"
                      type="date"
                      value={selectedDate}
                      onChange={(event) => setSelectedDate(event.target.value)}
                      className={`ml-3 rounded-lg border px-3 py-2 ${colors.input}`}
                    />
                  </label>
                )}

                {mode === 'range' && (
                  <div className="mb-4 flex flex-wrap items-end gap-3">
                    <label className="text-sm">
                      Start date
                      <input
                        aria-label="Range start date"
                        type="date"
                        value={rangeStart}
                        onChange={(event) => setRangeStart(event.target.value)}
                        className={`mt-1 block rounded-lg border px-3 py-2 ${colors.input}`}
                      />
                    </label>
                    <label className="text-sm">
                      End date (exclusive)
                      <input
                        aria-label="Range end date"
                        type="date"
                        value={rangeEnd}
                        onChange={(event) => setRangeEnd(event.target.value)}
                        className={`mt-1 block rounded-lg border px-3 py-2 ${colors.input}`}
                      />
                    </label>
                  </div>
                )}

                {mode === 'search' && (
                  <form
                    className="mb-4 flex gap-2"
                    onSubmit={(event) => {
                      event.preventDefault()
                      setSearchQuery(searchInput.trim())
                      setEventId('')
                    }}
                  >
                    <input
                      aria-label="Search Calendar events"
                      value={searchInput}
                      onChange={(event) => setSearchInput(event.target.value)}
                      placeholder="Search events"
                      className={`min-w-0 flex-1 rounded-lg border px-3 py-2 ${colors.input}`}
                    />
                    <button type="submit" className="rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white">
                      Search
                    </button>
                  </form>
                )}

                {mode === 'freebusy' && (
                  <form className="space-y-3" onSubmit={checkFreeBusy}>
                    <div className="flex flex-wrap gap-3">
                      <label className="text-sm">
                        Start
                        <input
                          aria-label="Availability start"
                          type="datetime-local"
                          value={freeBusyStart}
                          onChange={(event) => setFreeBusyStart(event.target.value)}
                          required
                          className={`mt-1 block rounded-lg border px-3 py-2 ${colors.input}`}
                        />
                      </label>
                      <label className="text-sm">
                        End
                        <input
                          aria-label="Availability end"
                          type="datetime-local"
                          value={freeBusyEnd}
                          onChange={(event) => setFreeBusyEnd(event.target.value)}
                          required
                          className={`mt-1 block rounded-lg border px-3 py-2 ${colors.input}`}
                        />
                      </label>
                    </div>
                    <button
                      type="submit"
                      disabled={freeBusyLoading}
                      className="rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                    >
                      {freeBusyLoading ? 'Checking…' : 'Check availability'}
                    </button>
                    {freeBusyError && <p role="alert" className="text-sm text-red-400">{freeBusyError}</p>}
                    {freeBusyResult && (
                      <FreeBusyResult
                        result={freeBusyResult}
                        timeZone={timeZone}
                        isDark={isDark}
                      />
                    )}
                  </form>
                )}

                {mode !== 'freebusy' && (
                  <>
                    {eventsQuery.isLoading && (
                      <p role="status" className={`py-8 text-center text-sm ${colors.text}`}>
                        Loading live Calendar events…
                      </p>
                    )}
                    {eventsQuery.isError && (
                      <p role="alert" className="rounded-xl border border-red-500/30 p-4 text-sm text-red-400">
                        Live Calendar data could not be retrieved. {eventsQuery.error.message}
                      </p>
                    )}
                    {eventsQuery.isSuccess && !eventId && events.length === 0 && (
                      <p role="status" className={`py-8 text-center text-sm ${colors.text}`}>
                        No events were returned for this view.
                      </p>
                    )}
                    {eventsQuery.isSuccess && !eventId && events.length > 0 && (
                      <div className="space-y-3">
                        {events.map((event) => (
                          <EventCard
                            key={event.id}
                            event={event}
                            timeZone={timeZone}
                            isDark={isDark}
                            onSelect={() => setEventId(event.id)}
                          />
                        ))}
                      </div>
                    )}
                    {eventId && (
                      <div className="space-y-3">
                        <button
                          type="button"
                          className="text-sm text-violet-400 underline"
                          onClick={() => setEventId('')}
                        >
                          Back to events
                        </button>
                        {eventQuery.isLoading && <p role="status">Loading event details…</p>}
                        {eventQuery.isError && (
                          <p role="alert" className="text-sm text-red-400">
                            Live event details could not be retrieved. {eventQuery.error.message}
                          </p>
                        )}
                        {eventQuery.data && (
                          <article className={`rounded-xl border p-4 ${colors.panel}`}>
                            <h2 className="text-lg font-semibold">
                              {eventQuery.data.summary?.trim() || 'Untitled event'}
                            </h2>
                            <p className={`mt-2 text-sm ${colors.text}`}>
                              {formatEventTime(eventQuery.data.start, timeZone)} – {formatEventTime(eventQuery.data.end, timeZone)}
                            </p>
                            {eventQuery.data.location && <p className="mt-2 text-sm">{eventQuery.data.location}</p>}
                            {eventQuery.data.description && (
                              <p className={`mt-3 whitespace-pre-wrap text-sm ${colors.text}`}>
                                {eventQuery.data.description}
                              </p>
                            )}
                            {eventQuery.data.attendees.length > 0 && (
                              <ul className={`mt-3 list-inside list-disc text-sm ${colors.text}`}>
                                {eventQuery.data.attendees.map((attendee, index) => (
                                  <li key={`${attendee.email || 'attendee'}-${index}`}>
                                    {attendee.display_name || attendee.email || 'Attendee'}
                                  </li>
                                ))}
                              </ul>
                            )}
                          </article>
                        )}
                      </div>
                    )}
                  </>
                )}
              </section>
            </>
          )}
        </>
      )}
    </div>
  )
}

function FreeBusyResult({ result, timeZone, isDark }) {
  const primary = result.calendars.primary
  if (!primary || primary.errors.length > 0) {
    return (
      <p role="alert" className="text-sm text-amber-400">
        Google Calendar did not return complete availability data.
      </p>
    )
  }
  if (primary.busy.length === 0) {
    return (
      <p role="status" className={`text-sm ${isDark ? 'text-emerald-300' : 'text-emerald-700'}`}>
        Google Calendar reports no busy intervals for this period.
      </p>
    )
  }
  return (
    <div role="status" className="space-y-1 text-sm">
      <p>Google Calendar reports busy intervals:</p>
      {primary.busy.map((interval, index) => (
        <p key={`${interval.start}-${index}`}>
          {formatEventTime({ date_time: interval.start }, timeZone)} –{' '}
          {formatEventTime({ date_time: interval.end }, timeZone)}
        </p>
      ))}
    </div>
  )
}
