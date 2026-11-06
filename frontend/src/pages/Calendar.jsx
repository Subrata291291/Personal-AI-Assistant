import { useEffect, useMemo, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api.js'
import {
  requireConnectionsResponse,
} from '../lib/responseValidation.js'
import {
  isGoogleServiceEnabled,
} from '../lib/googleServiceCapabilities.js'
import { SEMANTIC_STATUS_STYLES } from '../lib/semanticStatusStyles.js'
import { calendarService } from '../services/calendarService.js'

const EMPTY_CONNECTIONS = []
const CONNECTIONS_QUERY_KEY = (userId) => ['oauth-connections', userId]
const DAY_MINUTES = 24 * 60

function getTimeZone(user) {
  const configured = user?.user_metadata?.timezone
  if (typeof configured === 'string' && configured) {
    try {
      new Intl.DateTimeFormat('en', { timeZone: configured })
      return configured
    } catch {
      return 'Asia/Kolkata'
    }
  }
  return 'Asia/Kolkata'
}

function addDays(date, count) {
  const parsed = new Date(`${date}T00:00:00Z`)
  parsed.setUTCDate(parsed.getUTCDate() + count)
  return parsed.toISOString().slice(0, 10)
}

function addMonths(date, count) {
  const [year, month] = date.split('-').map(Number)
  const target = new Date(Date.UTC(year, month - 1 + count, 1))
  return target.toISOString().slice(0, 10)
}

function dateInZone(date, timeZone) {
  const values = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(date).map(({ type, value }) => [type, value]),
  )
  return `${values.year}-${values.month}-${values.day}`
}

function startOfLocalDateToIso(date, timeZone) {
  const utcDate = new Date(`${date}T00:00:00Z`).getTime()
  let low = utcDate - 48 * 60 * 60 * 1000
  let high = utcDate + 48 * 60 * 60 * 1000

  if (dateInZone(new Date(low), timeZone) >= date) {
    throw new Error('Unable to resolve the start of the selected calendar date.')
  }
  if (dateInZone(new Date(high), timeZone) < date) {
    throw new Error('Unable to resolve the start of the selected calendar date.')
  }

  while (high - low > 1) {
    const middle = Math.floor((low + high) / 2)
    if (dateInZone(new Date(middle), timeZone) >= date) {
      high = middle
    } else {
      low = middle
    }
  }

  if (dateInZone(new Date(high), timeZone) !== date) {
    throw new Error('The selected calendar date does not exist in this time zone.')
  }
  return new Date(high).toISOString()
}

function localDateTimeCandidates(value, timeZone) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value)
  if (!match) throw new Error('Enter a valid local date and time.')
  const [, year, month, day, hour, minute] = match.map(Number)
  const wallClockUtc = Date.UTC(year, month - 1, day, hour, minute)
  const requestedParts = [year, month, day, hour, minute]
  const normalizedParts = [
    new Date(wallClockUtc).getUTCFullYear(),
    new Date(wallClockUtc).getUTCMonth() + 1,
    new Date(wallClockUtc).getUTCDate(),
    new Date(wallClockUtc).getUTCHours(),
    new Date(wallClockUtc).getUTCMinutes(),
  ]
  if (requestedParts.some((part, index) => part !== normalizedParts[index])) {
    throw new Error('Enter a valid local date and time.')
  }

  const format = new Intl.DateTimeFormat('en-GB', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  })

  const offsets = new Set()
  for (let hours = -36; hours <= 36; hours += 6) {
    const sample = new Date(wallClockUtc + hours * 60 * 60 * 1000)
    const parts = Object.fromEntries(
      format.formatToParts(sample)
        .map(({ type, value: partValue }) => [type, partValue]),
    )
    const zoneWallClockUtc = Date.UTC(
      Number(parts.year),
      Number(parts.month) - 1,
      Number(parts.day),
      Number(parts.hour),
      Number(parts.minute),
    )
    offsets.add(zoneWallClockUtc - sample.getTime())
  }

  const candidates = [...offsets]
    .map((offset) => new Date(wallClockUtc - offset))
    .filter((instant) => {
      const parts = Object.fromEntries(
        format.formatToParts(instant)
          .map(({ type, value: partValue }) => [type, partValue]),
      )
      return (
        Number(parts.year) === year &&
        Number(parts.month) === month &&
        Number(parts.day) === day &&
        Number(parts.hour) === hour &&
        Number(parts.minute) === minute
      )
    })
    .sort((left, right) => left - right)

  if (candidates.length === 0) {
    throw new Error('That local time does not exist in the selected time zone.')
  }
  return candidates
}

function localDateTimeToIso(value, timeZone, occurrence) {
  const candidates = localDateTimeCandidates(value, timeZone)
  if (candidates.length > 1 && !['earlier', 'later'].includes(occurrence)) {
    throw new Error(
      'That local time occurs twice. Choose the earlier or later occurrence.',
    )
  }
  return (
    candidates[candidates.length > 1 && occurrence === 'later' ? 1 : 0]
      .toISOString()
  )
}

function isAmbiguousLocalDateTime(value, timeZone) {
  try {
    return localDateTimeCandidates(value, timeZone).length > 1
  } catch {
    return false
  }
}

export {
  isAmbiguousLocalDateTime,
  localDateTimeCandidates,
  localDateTimeToIso,
  startOfLocalDateToIso,
}

function getViewRange(date, view) {
  if (view === 'day') {
    return { start: date, end: addDays(date, 1) }
  }
  if (view === 'week') {
    const weekday = new Date(`${date}T00:00:00Z`).getUTCDay()
    const start = addDays(date, -((weekday + 6) % 7))
    return { start, end: addDays(start, 7) }
  }
  const [year, month] = date.split('-').map(Number)
  const first = `${year}-${String(month).padStart(2, '0')}-01`
  const weekday = new Date(`${first}T00:00:00Z`).getUTCDay()
  const start = addDays(first, -((weekday + 6) % 7))
  return { start, end: addDays(start, 42) }
}

function getMonthGrid(date) {
  const { start } = getViewRange(date, 'month')
  return Array.from({ length: 42 }, (_, index) => addDays(start, index))
}

function dateLabel(date, view, timeZone) {
  const parsed = new Date(`${date}T12:00:00Z`)
  const options = view === 'month'
    ? { month: 'long', year: 'numeric', timeZone: 'UTC' }
    : view === 'week'
      ? { month: 'short', day: 'numeric', year: 'numeric', timeZone }
      : { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric', timeZone }
  if (view === 'week') {
    const start = getViewRange(date, 'week').start
    const end = addDays(getViewRange(date, 'week').end, -1)
    return `${new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      timeZone: 'UTC',
    }).format(new Date(`${start}T12:00:00Z`))} – ${new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'UTC',
    }).format(new Date(`${end}T12:00:00Z`))}`
  }
  return new Intl.DateTimeFormat(undefined, options).format(parsed)
}

function eventDateTime(value, timeZone) {
  if (!value) return null
  if (typeof value.date === 'string') return { allDay: true, date: value.date }
  if (typeof value.date_time !== 'string') return null
  const instant = new Date(value.date_time)
  if (Number.isNaN(instant.getTime())) return null
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-GB', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(instant).map(({ type, value: partValue }) => [type, partValue]),
  )
  return {
    allDay: false,
    date: `${parts.year}-${parts.month}-${parts.day}`,
    minutes: Number(parts.hour) * 60 + Number(parts.minute),
  }
}

function eventOccursOnDate(event, date, timeZone) {
  const start = eventDateTime(event.start, timeZone)
  const end = eventDateTime(event.end, timeZone)
  if (!start) return false
  if (start.allDay) {
    const endDate = end?.allDay ? end.date : addDays(start.date, 1)
    return date >= start.date && date < endDate
  }
  const endDate = end?.date || start.date
  return date >= start.date && (
    date < endDate ||
    (date === endDate && (end?.minutes || 0) > 0)
  )
}

function eventPosition(event, date, timeZone) {
  const start = eventDateTime(event.start, timeZone)
  const end = eventDateTime(event.end, timeZone)
  if (!start || start.allDay) return null
  const top = date > start.date ? 0 : start.minutes
  const bottom = end
    ? date < end.date ? DAY_MINUTES : end.minutes
    : top + 60
  return {
    top: `${(top / DAY_MINUTES) * 100}%`,
    height: `${(Math.max(30, bottom - top) / DAY_MINUTES) * 100}%`,
  }
}

function formatTime(value, timeZone) {
  if (!value) return 'Time unavailable'
  if (value.date) return `${value.date} · All day`
  if (!value.date_time) return 'Time unavailable'
  const instant = new Date(value.date_time)
  if (Number.isNaN(instant.getTime())) return 'Time unavailable'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone,
  }).format(instant)
}

function EventButton({ event, timeZone, onClick, compact = false }) {
  return (
    <button
      type="button"
      title={event.summary || 'Untitled event'}
      onClick={onClick}
      className={`block w-full truncate rounded px-1.5 py-1 text-left text-xs ${
        compact
          ? 'bg-violet-500/15 text-violet-700 dark:text-violet-200'
          : 'border border-violet-500/40 bg-violet-500/20 text-violet-800 dark:text-violet-100'
      }`}
    >
      {event.summary?.trim() || 'Untitled event'}
      {!compact && (
        <span className="block truncate text-[10px] opacity-80">
          {formatTime(event.start, timeZone)}
        </span>
      )}
    </button>
  )
}

function CalendarTimeline({ dates, events, timeZone, onSelect, isDark }) {
  const hours = Array.from({ length: 24 }, (_, hour) => hour)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000)
    return () => clearInterval(timer)
  }, [])
  const today = dateInZone(now, timeZone)
  const nowParts = Object.fromEntries(
    new Intl.DateTimeFormat('en-GB', {
      timeZone,
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(now).map(({ type, value }) => [type, value]),
  )
  const currentMinute = Number(nowParts.hour) * 60 + Number(nowParts.minute)
  return (
    <div className="overflow-x-auto">
      <div
        className="grid min-w-[760px]"
        style={{ gridTemplateColumns: `4.5rem repeat(${dates.length}, minmax(0, 1fr))` }}
      >
        <div className="sticky top-0 z-10 border-b border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900" />
        {dates.map((date) => (
          <div
            key={date}
            className="sticky top-0 z-10 border-b border-l border-slate-200 bg-white px-2 py-2 text-center text-xs dark:border-slate-700 dark:bg-slate-900"
          >
            <div className="text-slate-500">
              {new Intl.DateTimeFormat(undefined, {
                weekday: 'short',
                timeZone: 'UTC',
              }).format(new Date(`${date}T12:00:00Z`))}
            </div>
            <div className={`mx-auto mt-1 flex h-7 w-7 items-center justify-center rounded-full font-semibold ${
              date === today ? 'bg-violet-600 text-white' : ''
            }`}>
              {Number(date.slice(-2))}
            </div>
          </div>
        ))}
        <div className="border-b border-slate-200 py-2 pr-2 text-right text-[10px] text-slate-500 dark:border-slate-700">
          All day
        </div>
        {dates.map((date) => {
          const allDay = events.filter((event) =>
            event.start?.date && eventOccursOnDate(event, date, timeZone),
          )
          return (
            <div key={`all-day-${date}`} className="min-h-8 border-b border-l border-slate-200 p-1 dark:border-slate-700">
              {allDay.map((event) => (
                <EventButton
                  key={event.id}
                  event={event}
                  timeZone={timeZone}
                  compact
                  onClick={() => onSelect(event)}
                />
              ))}
            </div>
          )
        })}
        <div className="relative">
          {hours.map((hour) => (
            <div
              key={hour}
              className="h-14 border-b border-slate-200 pr-2 text-right text-[10px] text-slate-500 dark:border-slate-700"
            >
              {hour === 0 ? '' : `${String(hour).padStart(2, '0')}:00`}
            </div>
          ))}
        </div>
        {dates.map((date) => {
          const timed = events.filter((event) =>
            event.start?.date_time && eventOccursOnDate(event, date, timeZone),
          )
          return (
            <div
              key={date}
              className="relative border-l border-slate-200 dark:border-slate-700"
            >
              <div className="relative">
                {hours.map((hour) => (
                  <div
                    key={hour}
                    className="h-14 border-b border-slate-200/80 dark:border-slate-700/80"
                  />
                ))}
                {timed.map((event) => {
                  const position = eventPosition(event, date, timeZone)
                  if (!position) return null
                  return (
                    <div
                      key={event.id}
                      className="absolute z-[2] w-full px-0.5"
                      style={position}
                    >
                      <EventButton
                        event={event}
                        timeZone={timeZone}
                        onClick={() => onSelect(event)}
                      />
                    </div>
                  )
                })}
                {date === today && (
                  <div
                    aria-label="Current time"
                    className={`pointer-events-none absolute z-[3] h-0.5 w-full ${
                      isDark ? 'bg-red-400' : 'bg-red-600'
                    }`}
                    style={{ top: `${(currentMinute / DAY_MINUTES) * 100}%` }}
                  />
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function Calendar() {
  const { user } = useAuth()
  const { isDark } = useOutletContext()
  const userId = user?.id
  const timeZone = useMemo(() => getTimeZone(user), [user])
  const enabled = isGoogleServiceEnabled('calendar', user?.user_metadata)
  const [today, setToday] = useState(() => dateInZone(new Date(), timeZone))
  const [selectedDate, setSelectedDate] = useState(today)
  const [view, setView] = useState('month')
  const [connectionId, setConnectionId] = useState('')
  const [eventId, setEventId] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [freeBusyOpen, setFreeBusyOpen] = useState(false)
  const [freeBusyStart, setFreeBusyStart] = useState(`${today}T14:00`)
  const [freeBusyEnd, setFreeBusyEnd] = useState(`${today}T16:00`)
  const [freeBusyStartOccurrence, setFreeBusyStartOccurrence] = useState('')
  const [freeBusyEndOccurrence, setFreeBusyEndOccurrence] = useState('')
  const [freeBusyResult, setFreeBusyResult] = useState(null)
  const [freeBusyError, setFreeBusyError] = useState('')
  const [freeBusyLoading, setFreeBusyLoading] = useState(false)

  useEffect(() => {
    const timer = setInterval(() => {
      setToday(dateInZone(new Date(), timeZone))
    }, 60_000)
    return () => clearInterval(timer)
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
  const googleConnections = useMemo(
    () => connections.filter((connection) =>
      connection.provider === 'google' &&
      connection.status === 'connected'
    ),
    [connections],
  )
  const calendarConnections = useMemo(
    () => connections.filter((connection) =>
      connection.provider === 'google' &&
      connection.status === 'connected' &&
      (
        connection.capabilities?.calendar_events_read === true ||
        connection.capabilities?.calendar_freebusy === true
      )
    ),
    [connections],
  )
  const configuredDefault = user?.user_metadata?.default_calendar_connection_id

  const selectedConnectionId = calendarConnections.some(
    (connection) => connection.id === connectionId,
  )
    ? connectionId
    : calendarConnections.some(
      (connection) => connection.id === configuredDefault,
    )
      ? configuredDefault
      : calendarConnections.length === 1
        ? calendarConnections[0].id
        : ''
  const selectedConnection = calendarConnections.find(
    (connection) => connection.id === selectedConnectionId,
  )
  const connectionsVerified =
    connectionsQuery.isSuccess &&
    !connectionsQuery.isFetching &&
    !connectionsQuery.isError
  const range = useMemo(() => getViewRange(selectedDate, view), [selectedDate, view])
  const eventsQuery = useQuery({
    queryKey: [
      'calendar-events',
      userId,
      selectedConnectionId,
      timeZone,
      view,
      range.start,
      range.end,
      searchQuery,
    ],
    enabled: Boolean(
      selectedConnectionId &&
      selectedConnection?.capabilities?.calendar_events_read === true &&
      enabled &&
      view !== 'freebusy' &&
      !eventId
    ),
    retry: false,
    queryFn: () => calendarService.listEvents({
      connectionId: selectedConnectionId,
      timeMin: startOfLocalDateToIso(range.start, timeZone),
      timeMax: startOfLocalDateToIso(range.end, timeZone),
      query: searchQuery || undefined,
      timeZone,
    }),
  })
  const eventQuery = useQuery({
    queryKey: ['calendar-event', userId, selectedConnectionId, eventId],
    queryFn: () => calendarService.getEvent({
      connectionId: selectedConnectionId,
      eventId,
    }),
    enabled: Boolean(
      selectedConnectionId &&
      selectedConnection?.capabilities?.calendar_events_read === true &&
      eventId
    ),
    retry: false,
  })
  const events = eventsQuery.data?.events || []
  const gridDates = view === 'month'
    ? getMonthGrid(selectedDate)
    : view === 'week'
      ? Array.from({ length: 7 }, (_, index) => addDays(range.start, index))
      : [selectedDate]
  const monthLabel = dateLabel(selectedDate, view, timeZone)
  const servicePanel = isDark
    ? 'border-[#252838] bg-[#11131c]'
    : 'border-slate-200 bg-white'
  const text = isDark ? 'text-slate-300' : 'text-slate-600'
  const input = isDark
    ? 'border-[#303448] bg-[#171a27] text-slate-100'
    : 'border-slate-200 bg-white text-slate-800'

  function navigateDate(amount) {
    setSelectedDate((current) => view === 'month'
      ? addMonths(current, amount).slice(0, 7) + '-01'
      : addDays(current, amount * (view === 'week' ? 7 : 1)))
    setEventId('')
  }

  function selectEvent(event) {
    setEventId(event.id)
  }

  async function checkFreeBusy(event) {
    event.preventDefault()
    setFreeBusyLoading(true)
    setFreeBusyError('')
    setFreeBusyResult(null)
    try {
      const timeMin = localDateTimeToIso(
        freeBusyStart,
        timeZone,
        freeBusyStartOccurrence,
      )
      const timeMax = localDateTimeToIso(
        freeBusyEnd,
        timeZone,
        freeBusyEndOccurrence,
      )
      if (timeMax <= timeMin) throw new Error('The end time must be after the start time.')
      const result = await calendarService.getFreeBusy({
        connectionId: selectedConnectionId,
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

  return (
    <div className="mx-auto max-w-[1600px] space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <img
            src="/icons/google/calendar.svg"
            alt="Google Calendar logo"
            className="h-10 w-10"
          />
          <div>
            <p className="text-xs font-bold uppercase tracking-widest text-violet-400">
              Google Workspace
            </p>
            <h1 className="text-2xl font-bold">Google Calendar</h1>
            <p className={`text-xs ${text}`}>Read-only · {timeZone}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => { setSelectedDate(today); setEventId('') }}
            className={`rounded-lg border px-3 py-2 text-sm ${input}`}
          >
            Today
          </button>
          <button
            type="button"
            aria-label="Previous"
            onClick={() => navigateDate(-1)}
            className={`rounded-lg border px-3 py-2 ${input}`}
          >
            ‹
          </button>
          <button
            type="button"
            aria-label="Next"
            onClick={() => navigateDate(1)}
            className={`rounded-lg border px-3 py-2 ${input}`}
          >
            ›
          </button>
          <label className="sr-only" htmlFor="calendar-date-picker">Choose date</label>
          <input
            id="calendar-date-picker"
            aria-label="Calendar date"
            type="date"
            value={selectedDate}
            onChange={(event) => {
              if (event.target.value) {
                setSelectedDate(event.target.value)
                setEventId('')
              }
            }}
            className={`rounded-lg border px-3 py-2 text-sm ${input}`}
          />
          <div className="flex rounded-lg border p-1" role="group" aria-label="Calendar view">
            {['month', 'week', 'day'].map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={view === option}
                onClick={() => { setView(option); setEventId('') }}
                className={`rounded-md px-3 py-1.5 text-sm capitalize ${
                  view === option ? 'bg-violet-600 text-white' : text
                }`}
              >
                {option}
              </button>
            ))}
          </div>
          <button
            type="button"
            aria-expanded={searchOpen}
            onClick={() => setSearchOpen((open) => !open)}
            className={`rounded-lg border px-3 py-2 text-sm ${input}`}
          >
            Search
          </button>
        </div>
      </header>

      <div className="grid gap-4 xl:grid-cols-[230px_minmax(0,1fr)]">
        <aside className="space-y-4">
          <section className={`rounded-2xl border p-4 ${servicePanel}`}>
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-lg font-semibold">{monthLabel}</h2>
              <div className="flex gap-1">
                <button type="button" aria-label="Previous mini month" onClick={() => setSelectedDate(addMonths(selectedDate, -1))} className={`rounded px-2 ${text}`}>‹</button>
                <button type="button" aria-label="Next mini month" onClick={() => setSelectedDate(addMonths(selectedDate, 1))} className={`rounded px-2 ${text}`}>›</button>
              </div>
            </div>
            <div className="mt-3 grid grid-cols-7 gap-y-1 text-center text-xs">
              {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((day, index) => (
                <span key={`${day}-${index}`} className="py-1 text-slate-500">{day}</span>
              ))}
              {getMonthGrid(selectedDate).map((date) => {
                const inMonth = date.slice(0, 7) === selectedDate.slice(0, 7)
                return (
                  <button
                    type="button"
                    key={date}
                    aria-label={date}
                    onClick={() => { setSelectedDate(date); setEventId('') }}
                    className={`mx-auto h-7 w-7 rounded-full ${
                      date === selectedDate
                        ? 'bg-violet-600 text-white'
                        : inMonth ? text : 'text-slate-500/60'
                    }`}
                  >
                    {Number(date.slice(-2))}
                  </button>
                )
              })}
            </div>
          </section>
          {calendarConnections.length > 1 && (
            <label className={`block rounded-2xl border p-4 text-sm ${servicePanel}`}>
              <span className="mb-2 block font-semibold">Calendar account</span>
              <select
                aria-label="Calendar account"
                value={selectedConnectionId}
                onChange={(event) => {
                  setConnectionId(event.target.value)
                  setEventId('')
                }}
                className={`w-full rounded-lg border px-3 py-2 ${input}`}
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
          {selectedConnection && (
            <div className={`rounded-2xl border p-4 text-sm ${servicePanel}`}>
              <p className="font-semibold">My calendars</p>
              <p className={`mt-2 truncate ${text}`}>
                {selectedConnection.email || 'Selected Google account'}
              </p>
              {selectedConnection.capabilities?.calendar_freebusy === true && (
                <button
                  type="button"
                  onClick={() => setFreeBusyOpen((open) => !open)}
                  className="mt-3 text-left text-violet-400 underline"
                >
                  {freeBusyOpen ? 'Hide Free/Busy' : 'Check Free/Busy'}
                </button>
              )}
            </div>
          )}
        </aside>

        <main className={`min-w-0 rounded-2xl border p-4 sm:p-5 ${servicePanel}`}>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-xl font-semibold">{monthLabel}</h2>
            <span className={`text-sm font-medium ${
              selectedConnection && enabled
                ? SEMANTIC_STATUS_STYLES.positive.text
                : SEMANTIC_STATUS_STYLES.neutral.text
            }`}>
              {connectionsQuery.isError
                ? 'Connection status unavailable'
                : !connectionsVerified
                  ? 'Checking connection'
                  : selectedConnection
                    ? `Connected · ${enabled ? 'Enabled' : 'Disabled'}`
                    : 'Disconnected'}
            </span>
          </div>

          {searchOpen && (
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
                className={`min-w-0 flex-1 rounded-lg border px-3 py-2 ${input}`}
              />
              <button type="submit" className="rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white">
                Search
              </button>
            </form>
          )}

          {connectionsQuery.isError && (
            <p role="alert" className="rounded-xl border border-red-500/30 p-4 text-sm text-red-400">
              Unable to verify Google Calendar connections. {connectionsQuery.error.message}
            </p>
          )}
          {connectionsQuery.isLoading && (
            <p role="status" className={`rounded-xl border p-4 ${text}`}>Checking Calendar connection…</p>
          )}
          {connectionsVerified && googleConnections.length === 0 && (
            <div className={`rounded-xl border p-5 ${servicePanel}`}>
              <h3 className="font-semibold">Calendar is disconnected</h3>
              <p className={`mt-2 text-sm ${text}`}>
                Connect a Google account with Calendar event-read access to view events.
              </p>
              <Link className="mt-3 inline-block text-sm font-semibold text-violet-400 underline" to="/dashboard/connectors">
                Connect in Connected Apps
              </Link>
            </div>
          )}
          {connectionsVerified && googleConnections.length > 0 &&
            calendarConnections.length === 0 && enabled && (
            <div role="alert" className="rounded-xl border border-amber-500/30 p-5">
              <h3 className="font-semibold">Calendar permission required</h3>
              <p className={`mt-2 text-sm ${text}`}>
                A Google account is connected, but Calendar event-read or Free/Busy access has not been granted.
              </p>
              <Link className="mt-3 inline-block text-sm font-semibold text-violet-400 underline" to="/dashboard/connectors">
                Grant Calendar access
              </Link>
            </div>
          )}
          {connectionsVerified && calendarConnections.length > 0 && !enabled && (
            <div role="status" className="rounded-xl border border-slate-500/30 p-5">
              <h3 className="font-semibold">Google Calendar is connected but disabled</h3>
              <p className={`mt-2 text-sm ${text}`}>
                Enable Google Calendar in Connected Apps to retrieve events. Your Google account remains connected.
              </p>
              <Link className="mt-3 inline-block text-sm font-semibold text-violet-400 underline" to="/dashboard/connectors">
                Open Connected Apps
              </Link>
            </div>
          )}
          {connectionsVerified && calendarConnections.length > 0 && enabled && (
            <>
              {calendarConnections.length > 1 && !selectedConnection && (
                <p role="status" className={`mb-4 rounded-xl border p-4 text-sm ${text}`}>
                  Select the Google account whose calendar you want to view.
                </p>
              )}
              {configuredDefault && !selectedConnection && (
                <p role="alert" className="mb-4 rounded-xl border border-amber-500/30 p-4 text-sm text-amber-400">
                  Your saved default Calendar account is unavailable or lacks event-read access. Choose an account explicitly; another account will not be selected automatically.
                </p>
              )}
              {selectedConnection && (
                <>
                  <p className={`mb-3 text-xs ${text}`}>
                    Viewing {selectedConnection.email || 'the selected Google account'} · {timeZone}
                  </p>
                  {selectedConnection.capabilities?.calendar_events_read !== true && (
                    <p role="alert" className="mb-4 rounded-xl border border-amber-500/30 p-4 text-sm text-amber-400">
                      This account has no Calendar event-read permission. Free/Busy, if separately authorized, remains available below.
                    </p>
                  )}
                  {selectedConnection.capabilities?.calendar_events_read === true &&
                    eventsQuery.isLoading && (
                    <p role="status" className={`py-8 text-center text-sm ${text}`}>
                      Loading live Calendar events…
                    </p>
                  )}
                  {selectedConnection.capabilities?.calendar_events_read === true &&
                    eventsQuery.isError && (
                    <p role="alert" className="rounded-xl border border-red-500/30 p-4 text-sm text-red-400">
                      Live Calendar data could not be retrieved. {eventsQuery.error.message}
                    </p>
                  )}
                  {selectedConnection.capabilities?.calendar_events_read === true &&
                    eventsQuery.isSuccess && !eventId && events.length === 0 && (
                    <p role="status" className={`py-10 text-center text-sm ${text}`}>
                      Google Calendar returned no events for this {view}.
                    </p>
                  )}
                  {selectedConnection.capabilities?.calendar_events_read === true &&
                    eventsQuery.isSuccess && !eventId && (
                    view === 'month'
                      ? (
                        <div className="overflow-x-auto">
                          <div className="min-w-[700px]">
                            <div className="grid grid-cols-7 border-l border-t border-slate-200 dark:border-slate-700">
                              {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day) => (
                                <div key={day} className="border-b border-r border-slate-200 p-2 text-center text-xs text-slate-500 dark:border-slate-700">
                                  {day}
                                </div>
                              ))}
                              {gridDates.map((date) => {
                                const dayEvents = events.filter((event) =>
                                  eventOccursOnDate(event, date, timeZone),
                                )
                                return (
                                  <div
                                    key={date}
                                    className={`min-h-28 border-b border-r border-slate-200 p-1.5 dark:border-slate-700 ${
                                      date.slice(0, 7) !== selectedDate.slice(0, 7)
                                        ? 'bg-slate-50/60 dark:bg-slate-950/30'
                                        : ''
                                    }`}
                                  >
                                    <button
                                      type="button"
                                      onClick={() => { setSelectedDate(date); setView('day') }}
                                      className={`mb-1 flex h-7 w-7 items-center justify-center rounded-full text-xs ${
                                        date === today ? 'bg-violet-600 text-white' : text
                                      }`}
                                    >
                                      {Number(date.slice(-2))}
                                    </button>
                                    <div className="space-y-0.5">
                                      {dayEvents.slice(0, 3).map((event) => (
                                        <EventButton
                                          key={event.id}
                                          event={event}
                                          timeZone={timeZone}
                                          compact
                                          onClick={() => selectEvent(event)}
                                        />
                                      ))}
                                      {dayEvents.length > 3 && (
                                        <button
                                          type="button"
                                          onClick={() => { setSelectedDate(date); setView('day') }}
                                          className="px-1 text-[10px] text-violet-400"
                                        >
                                          +{dayEvents.length - 3} more
                                        </button>
                                      )}
                                    </div>
                                  </div>
                                )
                              })}
                            </div>
                          </div>
                        </div>
                      )
                      : (
                        <CalendarTimeline
                          dates={gridDates}
                          events={events}
                          timeZone={timeZone}
                          onSelect={selectEvent}
                          isDark={isDark}
                        />
                      )
                  )}
                  {eventId &&
                    selectedConnection.capabilities?.calendar_events_read === true && (
                    <div
                      role="dialog"
                      aria-modal="true"
                      aria-labelledby="calendar-event-title"
                      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
                    >
                      <article className={`max-h-[85vh] w-full max-w-xl overflow-y-auto rounded-2xl border p-6 ${servicePanel}`}>
                        <div className="flex items-start justify-between gap-4">
                          <h3 id="calendar-event-title" className="text-xl font-semibold">
                            {eventQuery.data?.summary?.trim() || 'Event details'}
                          </h3>
                          <button
                            type="button"
                            aria-label="Close event details"
                            onClick={() => setEventId('')}
                            className={`rounded-lg px-2 py-1 ${text}`}
                          >
                            Close
                          </button>
                        </div>
                        {eventQuery.isLoading && <p role="status" className="mt-4">Loading event details…</p>}
                        {eventQuery.isError && (
                          <p role="alert" className="mt-4 text-sm text-red-400">
                            Live event details could not be retrieved. {eventQuery.error.message}
                          </p>
                        )}
                        {eventQuery.data && (
                          <div className={`mt-4 space-y-3 text-sm ${text}`}>
                            <p>{formatTime(eventQuery.data.start, timeZone)} – {formatTime(eventQuery.data.end, timeZone)}</p>
                            <p>Timezone: {eventQuery.data.start?.time_zone || eventQuery.data.end?.time_zone || timeZone}</p>
                            <p>Calendar: primary · {selectedConnection?.email || 'Selected Google account'}</p>
                            {eventQuery.data.location && <p>Location: {eventQuery.data.location}</p>}
                            {eventQuery.data.description && (
                              <p className="whitespace-pre-wrap">{eventQuery.data.description}</p>
                            )}
                            {eventQuery.data.attendees?.length > 0 && (
                              <div>
                                <p className="font-semibold">Attendees</p>
                                <ul className="mt-1 list-inside list-disc">
                                  {eventQuery.data.attendees.map((attendee, index) => (
                                    <li key={`${attendee.email || 'attendee'}-${index}`}>
                                      {attendee.display_name || attendee.email || 'Attendee'}
                                    </li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          </div>
                        )}
                      </article>
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </main>
      </div>

      {freeBusyOpen && selectedConnection?.capabilities?.calendar_freebusy === true && (
        <form onSubmit={checkFreeBusy} className={`rounded-2xl border p-5 ${servicePanel}`}>
          <h2 className="font-semibold">Free/Busy · separately permission-gated</h2>
          <div className="mt-3 flex flex-wrap gap-3">
            <label className={`text-sm ${text}`}>
              Start
              <input
                aria-label="Availability start"
                type="datetime-local"
                value={freeBusyStart}
                onChange={(event) => {
                  setFreeBusyStart(event.target.value)
                  setFreeBusyStartOccurrence('')
                }}
                required
                className={`mt-1 block rounded-lg border px-3 py-2 ${input}`}
              />
            </label>
            {isAmbiguousLocalDateTime(freeBusyStart, timeZone) && (
              <label className={`text-sm ${text}`}>
                Start occurrence
                <select
                  aria-label="Availability start occurrence"
                  value={freeBusyStartOccurrence}
                  onChange={(event) => setFreeBusyStartOccurrence(event.target.value)}
                  required
                  className={`mt-1 block rounded-lg border px-3 py-2 ${input}`}
                >
                  <option value="">Choose occurrence</option>
                  <option value="earlier">Earlier occurrence</option>
                  <option value="later">Later occurrence</option>
                </select>
              </label>
            )}
            <label className={`text-sm ${text}`}>
              End
              <input
                aria-label="Availability end"
                type="datetime-local"
                value={freeBusyEnd}
                onChange={(event) => {
                  setFreeBusyEnd(event.target.value)
                  setFreeBusyEndOccurrence('')
                }}
                required
                className={`mt-1 block rounded-lg border px-3 py-2 ${input}`}
              />
            </label>
            {isAmbiguousLocalDateTime(freeBusyEnd, timeZone) && (
              <label className={`text-sm ${text}`}>
                End occurrence
                <select
                  aria-label="Availability end occurrence"
                  value={freeBusyEndOccurrence}
                  onChange={(event) => setFreeBusyEndOccurrence(event.target.value)}
                  required
                  className={`mt-1 block rounded-lg border px-3 py-2 ${input}`}
                >
                  <option value="">Choose occurrence</option>
                  <option value="earlier">Earlier occurrence</option>
                  <option value="later">Later occurrence</option>
                </select>
              </label>
            )}
            <button type="submit" disabled={freeBusyLoading} className="mt-auto rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
              {freeBusyLoading ? 'Checking…' : 'Check availability'}
            </button>
          </div>
          {freeBusyError && <p role="alert" className="mt-3 text-sm text-red-400">{freeBusyError}</p>}
          {freeBusyResult && (
            <FreeBusyResult result={freeBusyResult} timeZone={timeZone} />
          )}
        </form>
      )}
    </div>
  )
}

function FreeBusyResult({ result, timeZone }) {
  const primary = result.calendars.primary
  if (!primary || primary.errors.length > 0) {
    return (
      <p role="alert" className="mt-3 text-sm text-amber-400">
        Google Calendar did not return complete availability data.
      </p>
    )
  }
  if (primary.busy.length === 0) {
    return (
      <p role="status" className={`mt-3 text-sm ${SEMANTIC_STATUS_STYLES.positive.text}`}>
        Google Calendar reports no busy intervals for this period.
      </p>
    )
  }
  return (
    <div role="status" className="mt-3 space-y-1 text-sm">
      <p>Google Calendar reports busy intervals:</p>
      {primary.busy.map((interval, index) => (
        <p key={`${interval.start}-${index}`}>
          {formatTime({ date_time: interval.start }, timeZone)} –{' '}
          {formatTime({ date_time: interval.end }, timeZone)}
        </p>
      ))}
    </div>
  )
}
