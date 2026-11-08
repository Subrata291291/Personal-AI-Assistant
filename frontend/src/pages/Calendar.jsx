import { useEffect, useMemo, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api.js'
import {
  requireConnectionsResponse,
} from '../lib/responseValidation.js'
import {
  isGoogleServiceEnabled,
} from '../lib/googleServiceCapabilities.js'
import { SEMANTIC_STATUS_STYLES } from '../lib/semanticStatusStyles.js'
import {
  GOOGLE_CONNECTIONS_STALE_TIME,
  PRIVATE_DATA_GC_TIME,
  PRIVATE_DATA_STALE_TIME,
} from '../lib/queryClient.js'
import { calendarService } from '../services/calendarService.js'

const EMPTY_CONNECTIONS = []
const CONNECTIONS_QUERY_KEY = (userId) => ['oauth-connections', userId]
const DAY_MINUTES = 24 * 60
export function isCalendarRequestCancellation(error) {
  return (
    error?.code === 'REQUEST_CANCELLED' ||
    (
      error?.name === 'AbortError' &&
      !error?.timeout &&
      !error?.networkFailure
    )
  )
}
const CALENDAR_STATE_KEY = (userId, key) =>
  `calendar-workspace-${key}-${userId}`
const HTML_DESCRIPTION_TAG = /<\/?[a-z][^>]*>/i
const DESCRIPTION_BLOCK_TAGS = new Set([
  'ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'DIV', 'FOOTER', 'H1', 'H2',
  'H3', 'H4', 'H5', 'H6', 'HEADER', 'LI', 'OL', 'P', 'PRE', 'SECTION',
  'TABLE', 'TR', 'UL',
])

function formatEventDescription(description) {
  if (typeof description !== 'string' || !description.trim()) return ''
  if (!HTML_DESCRIPTION_TAG.test(description)) return description

  const parsed = new DOMParser().parseFromString(description, 'text/html')
  const parts = []
  const appendLineBreak = (count = 1) => {
    const text = parts.join('')
    if (text && !text.endsWith('\n')) parts.push('\n')
    if (count === 2 && !parts.join('').endsWith('\n\n')) parts.push('\n')
  }
  const visit = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      parts.push(node.nodeValue || '')
      return
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return

    const { tagName } = node
    if (['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT'].includes(tagName)) return
    const isBlock = DESCRIPTION_BLOCK_TAGS.has(tagName)
    if (isBlock) appendLineBreak(tagName === 'P' ? 2 : 1)
    if (tagName === 'BR') {
      parts.push('\n')
      return
    }
    if (tagName === 'LI') parts.push('• ')

    for (const child of node.childNodes) visit(child)

    if (tagName === 'A') {
      const href = node.getAttribute('href')?.trim()
      const linkText = node.textContent.trim()
      if (
        href &&
        /^(https?:\/\/|mailto:)/i.test(href) &&
        !linkText.includes(href)
      ) {
        parts.push(` (${href})`)
      }
    }
    if (isBlock) appendLineBreak(tagName === 'P' ? 2 : 1)
  }

  for (const child of parsed.body.childNodes) visit(child)
  return parts.join('')
    .replace(/[ \t]*\n[ \t]*/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

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

function getCalendarState(userId, key, fallback) {
  if (!userId) return fallback
  return sessionStorage.getItem(CALENDAR_STATE_KEY(userId, key)) ?? fallback
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

function getUpcomingEvent(events, today, timeZone, now) {
  const nowParts = Object.fromEntries(
    new Intl.DateTimeFormat('en-GB', {
      timeZone,
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(now).map(({ type, value }) => [type, value]),
  )
  const currentMinute = Number(nowParts.hour) * 60 + Number(nowParts.minute)
  return events
    .map((event) => ({ event, start: eventDateTime(event.start, timeZone) }))
    .filter(({ start }) => start && (
      start.date > today ||
      (start.date === today && (start.allDay || start.minutes >= currentMinute))
    ))
    .sort((left, right) =>
      left.start.date.localeCompare(right.start.date) ||
      (left.start.minutes || 0) - (right.start.minutes || 0),
    )[0]?.event || null
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

function formatEventTimeRange(event, timeZone) {
  if (typeof event.start?.date === 'string') return 'All day'

  const start = new Date(event.start?.date_time)
  const end = new Date(event.end?.date_time)
  if (
    typeof event.start?.date_time !== 'string' ||
    typeof event.end?.date_time !== 'string' ||
    Number.isNaN(start.getTime()) ||
    Number.isNaN(end.getTime())
  ) {
    return 'Time unavailable'
  }

  const formatter = new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    timeZone,
  })
  return `${formatter.format(start)} – ${formatter.format(end)}`
}

function formatEventDate(event, timeZone) {
  const start = eventDateTime(event.start, timeZone)
  if (!start) return 'Date unavailable'
  return new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(`${start.date}T12:00:00Z`))
}

const EVENT_CARD_PALETTES = [
  'border-[#36899B] bg-[#D5F1F6] text-slate-900',
  'border-[#A96B2C] bg-[#F9E3C9] text-slate-900',
  'border-[#995487] bg-[#F4D7EE] text-slate-900',
  'border-[#6E61A8] bg-[#DDD8F8] text-slate-900',
  'border-[#4E8972] bg-[#DCEFE8] text-slate-900',
]

function eventPaletteIndex(event) {
  const key = String(event.id || event.summary || '')
  let hash = 0
  for (const character of key) {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0
  }
  return hash % EVENT_CARD_PALETTES.length
}

function attendeeLabel(attendee) {
  return attendee.display_name?.trim() || attendee.email?.trim() || 'Attendee'
}

function attendeeInitials(attendee) {
  const words = attendeeLabel(attendee).split(/[\s@._-]+/).filter(Boolean)
  if (words.length > 1) {
    return `${words[0][0]}${words[words.length - 1][0]}`.toUpperCase()
  }
  return (words[0] || '?').slice(0, 2).toUpperCase()
}

function AttendeeAvatars({
  attendees = [],
  isDark,
  userEmail,
  userAvatar,
  maxVisible = 4,
}) {
  const namedAttendees = attendees.filter(
    (attendee) => attendee.display_name?.trim() || attendee.email?.trim(),
  )
  if (namedAttendees.length === 0) return null

  const visibleAttendees = namedAttendees.slice(0, maxVisible)
  const remaining = namedAttendees.length - visibleAttendees.length
  return (
    <span role="group" aria-label="Attendees" className="mt-1.5 flex items-center gap-1">
      {visibleAttendees.map((attendee, index) => {
        const label = attendeeLabel(attendee)
        const isCurrentUser = Boolean(
          userAvatar &&
          userEmail &&
          attendee.email?.toLowerCase() === userEmail.toLowerCase(),
        )
        return (
          <span
            key={`${attendee.email || label}-${index}`}
            role="img"
            aria-label={`${label} avatar`}
            title={label}
            className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[8px] font-semibold ${
              isDark
                ? 'border-slate-600 bg-slate-800 text-slate-200'
                : 'border-white bg-white/80 text-slate-700 shadow-sm'
            }`}
          >
            {isCurrentUser ? (
              <img
                src={userAvatar}
                alt=""
                referrerPolicy="no-referrer"
                className="h-full w-full rounded-full object-cover"
              />
            ) : attendeeInitials(attendee)}
          </span>
        )
      })}
      {remaining > 0 && (
        <span className={`text-[9px] font-medium ${
          isDark ? 'text-slate-300' : 'text-slate-600'
        }`}>
          +{remaining}
        </span>
      )}
    </span>
  )
}

function EventButton({
  event,
  timeZone,
  onClick,
  compact = false,
  showTime = !compact,
  isDark,
  userEmail,
  userAvatar,
}) {
  const lightPalette = EVENT_CARD_PALETTES[eventPaletteIndex(event)]
  return (
    <button
      type="button"
      title={event.summary || 'Untitled event'}
      onClick={onClick}
      className={`block w-full text-left text-xs ${
        isDark
          ? compact
            ? 'rounded bg-violet-500/15 px-1.5 py-1 text-violet-200'
            : 'rounded border border-violet-500/40 bg-violet-500/20 px-1.5 py-1 text-violet-100'
          : `rounded-lg border-l-[3px] ${lightPalette} px-2 py-1.5 font-semibold shadow-sm shadow-slate-900/[0.04] transition-colors hover:brightness-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700`
      }`}
    >
      <span className="block truncate">
        {event.summary?.trim() || 'Untitled event'}
      </span>
      {showTime && (
        <span className={`block truncate text-[10px] ${
          isDark ? 'opacity-80' : 'font-medium text-slate-700'
        }`}>
          {formatEventTimeRange(event, timeZone)}
        </span>
      )}
      <AttendeeAvatars
        attendees={event.attendees}
        isDark={isDark}
        userEmail={userEmail}
        userAvatar={userAvatar}
      />
    </button>
  )
}

function CalendarTimeline({
  dates,
  events,
  timeZone,
  onSelect,
  selectedDate,
  isDark,
  userEmail,
  userAvatar,
}) {
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
        <div className={`sticky top-0 z-10 border-b ${
          isDark
            ? 'border-slate-700 bg-slate-900'
            : 'border-[#E5EDE8] bg-[#F6FAF8]'
        }`} />
        {dates.map((date) => (
          <div
            key={date}
            className={`sticky top-0 z-10 border-b border-l px-2 py-2 text-center text-xs ${
              isDark
                ? 'border-slate-700 bg-slate-900'
                : 'border-[#E5EDE8] bg-[#F6FAF8]'
            }`}
          >
            <div className={isDark ? 'text-slate-500' : 'font-semibold uppercase tracking-wide text-slate-500'}>
              {new Intl.DateTimeFormat(undefined, {
                weekday: 'short',
                timeZone: 'UTC',
              }).format(new Date(`${date}T12:00:00Z`))}
            </div>
            <div className={`mx-auto mt-1 flex items-center justify-center rounded-full font-semibold ${
              isDark
                ? `h-7 w-7 text-xs ${date === today ? 'bg-violet-600 text-white' : ''}`
                : date === selectedDate
                  ? 'h-9 w-9 text-base bg-[#DCEFE8] text-[#245C4B] ring-1 ring-[#B8D8CB]'
                  : date === today
                    ? 'h-9 w-9 text-base text-teal-800 ring-1 ring-teal-300'
                    : 'h-9 w-9 text-base text-slate-800'
            }`}>
              {Number(date.slice(-2))}
            </div>
          </div>
        ))}
        <div className={`border-b py-2 pr-2 text-right text-[10px] ${
          isDark
            ? 'border-slate-700 text-slate-500'
            : 'border-[#E5EDE8] text-slate-500'
        }`}>
          All day
        </div>
        {dates.map((date) => {
          const allDay = events.filter((event) =>
            event.start?.date && eventOccursOnDate(event, date, timeZone),
          )
          return (
            <div key={`all-day-${date}`} className={`min-h-8 border-b border-l p-1 ${
              isDark ? 'border-slate-700' : 'border-[#E5EDE8]'
            }`}>
              {allDay.map((event) => (
                <EventButton
                  key={event.id}
                  event={event}
                  timeZone={timeZone}
                  compact
                  showTime
                  isDark={isDark}
                  userEmail={userEmail}
                  userAvatar={userAvatar}
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
              className={`h-14 border-b pr-2 text-right text-[10px] ${
                isDark
                  ? 'border-slate-700 text-slate-500'
                  : 'border-[#E5EDE8] text-slate-500'
              }`}
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
              className={`relative border-l ${
                isDark ? 'border-slate-700' : 'border-[#E5EDE8]'
              }`}
            >
              <div className="relative">
                {hours.map((hour) => (
                  <div
                    key={hour}
                    className={`h-14 border-b ${
                      isDark ? 'border-slate-700/80' : 'border-[#EDF2EE]'
                    }`}
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
                        isDark={isDark}
                        userEmail={userEmail}
                        userAvatar={userAvatar}
                        onClick={() => onSelect(event)}
                      />
                    </div>
                  )
                })}
                {date === today && (
                  <div
                    aria-label="Current time"
                    className={`pointer-events-none absolute z-[3] h-0.5 w-full ${
                      isDark ? 'bg-red-400' : 'bg-teal-600'
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
  const [selectedDate, setSelectedDate] = useState(() => {
    const savedDate = getCalendarState(userId, 'date', today)
    return /^\d{4}-\d{2}-\d{2}$/.test(savedDate) ? savedDate : today
  })
  const [view, setView] = useState(() => {
    const savedView = getCalendarState(userId, 'view', 'week')
    return ['month', 'week', 'day'].includes(savedView) ? savedView : 'week'
  })
  const [showTimedEvents, setShowTimedEvents] = useState(
    () => getCalendarState(userId, 'timed-events', 'true') !== 'false',
  )
  const [showAllDayEvents, setShowAllDayEvents] = useState(
    () => getCalendarState(userId, 'all-day-events', 'true') !== 'false',
  )
  const [connectionId, setConnectionId] = useState(
    () => getCalendarState(userId, 'connection', ''),
  )
  const [calendarId, setCalendarId] = useState(
    () => getCalendarState(userId, 'calendar-id', 'primary'),
  )
  const [eventId, setEventId] = useState('')
  const [searchOpen, setSearchOpen] = useState(
    () => Boolean(getCalendarState(userId, 'search-query', '')),
  )
  const [searchInput, setSearchInput] = useState(
    () => getCalendarState(userId, 'search-input', ''),
  )
  const [searchQuery, setSearchQuery] = useState(
    () => getCalendarState(userId, 'search-query', ''),
  )
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
    queryFn: async ({ signal }) => requireConnectionsResponse(
      await api.get('/api/v1/oauth/connections', { signal }),
    ),
    enabled: Boolean(userId),
    staleTime: GOOGLE_CONNECTIONS_STALE_TIME,
    gcTime: PRIVATE_DATA_GC_TIME,
    refetchOnMount: true,
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
    Boolean(connectionsQuery.data) &&
    ![401, 403].includes(connectionsQuery.error?.status)
  const calendarListQuery = useInfiniteQuery({
    queryKey: ['calendar-list', userId, selectedConnectionId],
    enabled: Boolean(
      selectedConnectionId &&
      selectedConnection?.capabilities?.calendar_list_read === true &&
      connectionsVerified &&
      enabled
    ),
    initialPageParam: undefined,
    queryFn: ({ pageParam, signal }) => calendarService.listCalendars({
      connectionId: selectedConnectionId,
      pageToken: pageParam,
      signal,
    }),
    getNextPageParam: (lastPage) =>
      lastPage.next_page_token || undefined,
    staleTime: PRIVATE_DATA_STALE_TIME,
    gcTime: PRIVATE_DATA_GC_TIME,
    refetchOnMount: true,
    refetchOnWindowFocus: true,
    retry: false,
  })
  const calendars = useMemo(() => {
    const seen = new Set()
    return (calendarListQuery.data?.pages || [])
      .flatMap((page) => page.calendars || [])
      .filter((calendar) => {
        if (seen.has(calendar.id)) return false
        seen.add(calendar.id)
        return true
      })
  }, [calendarListQuery.data])
  const selectedCalendarId = calendars.length &&
    !calendars.some((calendar) => calendar.id === calendarId)
    ? calendars.find((calendar) => calendar.primary)?.id || calendars[0].id
    : calendarId
  useEffect(() => {
    if (!userId) return
    sessionStorage.setItem(CALENDAR_STATE_KEY(userId, 'date'), selectedDate)
    sessionStorage.setItem(CALENDAR_STATE_KEY(userId, 'view'), view)
    sessionStorage.setItem(
      CALENDAR_STATE_KEY(userId, 'timed-events'),
      String(showTimedEvents),
    )
    sessionStorage.setItem(
      CALENDAR_STATE_KEY(userId, 'all-day-events'),
      String(showAllDayEvents),
    )
    sessionStorage.setItem(CALENDAR_STATE_KEY(userId, 'search-input'), searchInput)
    sessionStorage.setItem(CALENDAR_STATE_KEY(userId, 'search-query'), searchQuery)
  }, [
    userId,
    selectedDate,
    view,
    showTimedEvents,
    showAllDayEvents,
    searchInput,
    searchQuery,
  ])
  useEffect(() => {
    if (!userId || !connectionsQuery.data) return
    const key = CALENDAR_STATE_KEY(userId, 'connection')
    if (selectedConnectionId) {
      sessionStorage.setItem(key, selectedConnectionId)
    } else {
      sessionStorage.removeItem(key)
    }
  }, [userId, connectionsQuery.data, selectedConnectionId])
  useEffect(() => {
    if (!userId) return
    sessionStorage.setItem(
      CALENDAR_STATE_KEY(userId, 'calendar-id'),
      selectedCalendarId,
    )
  }, [userId, selectedCalendarId])
  const range = useMemo(() => getViewRange(selectedDate, view), [selectedDate, view])
  const eventsQuery = useInfiniteQuery({
    queryKey: [
      'calendar-events',
      userId,
      selectedConnectionId,
      timeZone,
      view,
      selectedCalendarId,
      range.start,
      range.end,
      searchQuery,
    ],
    enabled: Boolean(
      selectedConnectionId &&
      connectionsVerified &&
      selectedConnection?.capabilities?.calendar_events_read === true &&
      enabled &&
      view !== 'freebusy' &&
      !eventId
    ),
    staleTime: PRIVATE_DATA_STALE_TIME,
    gcTime: PRIVATE_DATA_GC_TIME,
    refetchOnMount: true,
    refetchOnWindowFocus: true,
    retry: false,
    initialPageParam: undefined,
    queryFn: ({ pageParam, signal }) => calendarService.listEvents({
      connectionId: selectedConnectionId,
      timeMin: startOfLocalDateToIso(range.start, timeZone),
      timeMax: startOfLocalDateToIso(range.end, timeZone),
      query: searchQuery || undefined,
      timeZone,
      calendarId: selectedCalendarId,
      pageToken: pageParam,
      signal,
    }),
    getNextPageParam: (lastPage) =>
      lastPage.next_page_token || undefined,
  })
  const eventQuery = useQuery({
    queryKey: ['calendar-event', userId, selectedConnectionId, eventId],
    queryFn: ({ signal }) => calendarService.getEvent({
      connectionId: selectedConnectionId,
      eventId,
      calendarId: selectedCalendarId,
      signal,
    }),
    enabled: Boolean(
      selectedConnectionId &&
      selectedConnection?.capabilities?.calendar_events_read === true &&
      eventId
    ),
    staleTime: PRIVATE_DATA_STALE_TIME,
    gcTime: PRIVATE_DATA_GC_TIME,
    refetchOnMount: true,
    refetchOnWindowFocus: true,
    retry: false,
  })
  const eventDescription = formatEventDescription(eventQuery.data?.description)
  const allEvents = useMemo(() => {
    const seen = new Set()
    return (eventsQuery.data?.pages || [])
      .flatMap((page) => page.events || [])
      .filter((event) => {
        if (seen.has(event.id)) return false
        seen.add(event.id)
        return true
      })
  }, [eventsQuery.data])
  const events = allEvents.filter((event) => (
    typeof event.start?.date === 'string'
      ? showAllDayEvents
      : typeof event.start?.date_time === 'string' && showTimedEvents
  ))
  const upcomingEvent = getUpcomingEvent(events, today, timeZone, new Date())
  const userEmail = user?.email || ''
  const userAvatar = user?.user_metadata?.avatar_url || user?.user_metadata?.picture || ''
  const gridDates = view === 'month'
    ? getMonthGrid(selectedDate)
    : view === 'week'
      ? Array.from({ length: 7 }, (_, index) => addDays(range.start, index))
      : [selectedDate]
  const monthLabel = dateLabel(selectedDate, view, timeZone)
  const miniMonthLabel = dateLabel(selectedDate, 'month', timeZone)
  const servicePanel = isDark
    ? 'border-[#252838] bg-[#11131c]'
    : 'border-[#E0EBE5] bg-white'
  const text = isDark ? 'text-slate-300' : 'text-slate-600'
  const input = isDark
    ? 'border-[#303448] bg-[#171a27] text-slate-100'
    : 'border-slate-200 bg-white text-slate-800 shadow-sm shadow-slate-900/[0.03]'
  const control = isDark
    ? 'border-[#303448] bg-[#171a27] text-slate-100'
    : 'border-[#DCE8E1] bg-white text-slate-700 shadow-sm shadow-[#315847]/[0.04] hover:border-[#BDD9CC] hover:bg-[#F4FAF6]'
  const calendarGrid = isDark
    ? 'border-slate-700'
    : 'border-[#E5EDE8]'

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
    <div className={`mx-auto max-w-[1600px] ${
      isDark
        ? 'space-y-5'
        : 'space-y-8 rounded-[1.375rem] bg-[#EFF7F2] p-5 text-[#263A32] sm:p-8 lg:p-10'
    }`}>
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <img
            src="/icons/google/calendar.svg"
            alt="Google Calendar logo"
            className="h-10 w-10"
          />
          <div>
            <p className={`text-xs font-bold uppercase tracking-widest ${
              isDark ? 'text-violet-400' : 'text-teal-800'
            }`}>
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
            className={isDark
              ? `rounded-lg border px-3 py-2 text-sm ${input}`
              : `h-10 rounded-lg border px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700 focus-visible:ring-offset-2 focus-visible:ring-offset-white ${control}`}
          >
            Today
          </button>
          <button
            type="button"
            aria-label="Previous"
            onClick={() => navigateDate(-1)}
            className={isDark
              ? `rounded-lg border px-3 py-2 ${input}`
              : `h-10 w-10 rounded-lg border text-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700 focus-visible:ring-offset-2 focus-visible:ring-offset-white ${control}`}
          >
            ‹
          </button>
          <button
            type="button"
            aria-label="Next"
            onClick={() => navigateDate(1)}
            className={isDark
              ? `rounded-lg border px-3 py-2 ${input}`
              : `h-10 w-10 rounded-lg border text-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700 focus-visible:ring-offset-2 focus-visible:ring-offset-white ${control}`}
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
            className={isDark
              ? `rounded-lg border px-3 py-2 text-sm ${input}`
              : `h-10 rounded-lg border px-3 py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700 focus-visible:ring-offset-2 focus-visible:ring-offset-white ${control}`}
          />
          <div className={isDark
            ? 'flex rounded-lg border p-1'
            : 'flex h-10 items-center rounded-lg border border-[#DDE9E2] bg-[#E8F1EB] p-1'
          } role="group" aria-label="Calendar view">
            {['month', 'week', 'day'].map((option) => (
              <button
                key={option}
                type="button"
                aria-label={option}
                aria-pressed={view === option}
                onClick={() => { setView(option); setEventId('') }}
                className={isDark
                  ? `rounded-md px-3 py-1.5 text-sm capitalize ${
                    view === option ? 'bg-violet-600 text-white' : text
                  }`
                  : `h-8 rounded-md px-3 text-sm font-medium capitalize transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700 ${
                    view === option
                      ? 'bg-white text-teal-800 shadow-sm ring-1 ring-[#D9E7DF]'
                      : `${text} hover:bg-white/70`
                  }`}
              >
                {option === 'day' ? 'Daily' : option === 'week' ? 'Weekly' : 'Monthly'}
              </button>
            ))}
          </div>
          <button
            type="button"
            aria-expanded={searchOpen}
            onClick={() => setSearchOpen((open) => !open)}
            className={isDark
              ? `rounded-lg border px-3 py-2 text-sm ${input}`
              : `h-10 rounded-lg border px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700 focus-visible:ring-offset-2 focus-visible:ring-offset-white ${control}`}
          >
            Search
          </button>
        </div>
      </header>

      <div className="grid gap-5 lg:grid-cols-[260px_minmax(0,1fr)] 2xl:grid-cols-[300px_minmax(0,1fr)]">
        <aside className="space-y-4">
          <section className={`rounded-2xl border p-4 ${
            isDark ? '' : 'rounded-[1.25rem] border-[#E0EBE5] shadow-sm shadow-[#315847]/[0.06]'
          } ${servicePanel}`}>
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-lg font-semibold">{miniMonthLabel}</h2>
              <div className="flex gap-1">
                <button type="button" aria-label="Previous mini month" onClick={() => setSelectedDate(addMonths(selectedDate, -1))} className={isDark ? `rounded px-2 ${text}` : 'rounded-md px-2 py-1 text-slate-600 transition-colors hover:bg-[#EFF7F2] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700'}>‹</button>
                <button type="button" aria-label="Next mini month" onClick={() => setSelectedDate(addMonths(selectedDate, 1))} className={isDark ? `rounded px-2 ${text}` : 'rounded-md px-2 py-1 text-slate-600 transition-colors hover:bg-[#EFF7F2] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700'}>›</button>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-7 gap-y-2.5 text-center text-xs">
              {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((day, index) => (
                <span key={`${day}-${index}`} className={`py-1 ${isDark ? 'text-slate-500' : 'font-semibold tracking-wide text-slate-500'}`}>{day}</span>
              ))}
              {getMonthGrid(selectedDate).map((date) => {
                const inMonth = date.slice(0, 7) === selectedDate.slice(0, 7)
                return (
                  <button
                    type="button"
                    key={date}
                    aria-label={date}
                    onClick={() => { setSelectedDate(date); setEventId('') }}
                    className={`mx-auto h-8 w-8 rounded-full text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700 ${
                      date === selectedDate
                        ? isDark
                          ? 'bg-violet-600 text-white'
                          : 'bg-[#DCEFE8] font-semibold text-[#245C4B] ring-1 ring-[#B8D8CB]'
                        : inMonth
                          ? isDark
                            ? `${text} hover:bg-slate-800`
                            : date === today
                              ? 'text-teal-800 ring-1 ring-teal-300'
                              : 'text-slate-700 hover:bg-[#EFF7F2]'
                          : isDark
                            ? 'text-slate-500/60 hover:bg-slate-800'
                            : 'text-slate-500 hover:bg-[#EFF7F2]'
                    }`}
                  >
                    {Number(date.slice(-2))}
                  </button>
                )
              })}
            </div>
          </section>
          <section aria-labelledby="calendar-reminder-heading" className={`rounded-2xl border p-4 ${
            isDark
              ? 'border-[#252838] bg-[#11131c]'
              : 'rounded-[1.25rem] border-[#CBE3D8] bg-[#DCEFE8] shadow-sm shadow-[#315847]/[0.06]'
          }`}>
            <h2 id="calendar-reminder-heading" className={`text-sm font-semibold ${
              isDark ? 'text-slate-100' : 'text-[#245C4B]'
            }`}>
              Meeting reminder
            </h2>
            {eventsQuery.isLoading ? (
              <p role="status" className={`mt-3 text-sm ${text}`}>Loading upcoming events…</p>
            ) : eventsQuery.isError ? (
              <p className={`mt-3 text-sm ${text}`}>Upcoming events are unavailable.</p>
            ) : upcomingEvent ? (
              <div className="mt-3 min-w-0">
                <p className={`truncate text-base font-semibold ${
                  isDark ? 'text-slate-100' : 'text-[#263A32]'
                }`}>
                  {upcomingEvent.summary?.trim() || 'Untitled event'}
                </p>
                <p className={`mt-1 text-xs font-medium ${
                  isDark ? 'text-slate-400' : 'text-[#45695B]'
                }`}>
                  {formatEventDate(upcomingEvent, timeZone)} · {formatEventTimeRange(upcomingEvent, timeZone)}
                </p>
                <AttendeeAvatars
                  attendees={upcomingEvent.attendees}
                  isDark={isDark}
                  userEmail={userEmail}
                  userAvatar={userAvatar}
                />
              </div>
            ) : (
              <p className={`mt-3 text-sm ${isDark ? 'text-slate-400' : 'text-[#45695B]'}`}>
                No upcoming meetings in this calendar view.
              </p>
            )}
          </section>
          <section aria-labelledby="calendar-filters-heading" className={`rounded-2xl border p-4 ${
            isDark ? '' : 'rounded-[1.25rem] border-[#E0EBE5] shadow-sm shadow-[#315847]/[0.06]'
          } ${servicePanel}`}>
            <h2 id="calendar-filters-heading" className="text-sm font-semibold">Filters</h2>
            <div className="mt-3 space-y-3">
              <label className={`flex cursor-pointer items-center gap-2.5 text-sm ${text}`}>
                <input
                  type="checkbox"
                  checked={showTimedEvents}
                  onChange={(event) => setShowTimedEvents(event.target.checked)}
                  className="h-4 w-4 accent-teal-700"
                />
                Timed events
              </label>
              <label className={`flex cursor-pointer items-center gap-2.5 text-sm ${text}`}>
                <input
                  type="checkbox"
                  checked={showAllDayEvents}
                  onChange={(event) => setShowAllDayEvents(event.target.checked)}
                  className="h-4 w-4 accent-teal-700"
                />
                All-day events
              </label>
            </div>
          </section>
          <section aria-labelledby="other-calendars-heading" className={`rounded-2xl border p-4 text-sm ${
            isDark ? '' : 'rounded-[1.25rem] border-[#E0EBE5] shadow-sm shadow-[#315847]/[0.06]'
          } ${servicePanel}`}>
            <h2 id="other-calendars-heading" className="font-semibold">Other calendars</h2>
            {calendarConnections.length > 1 && (
              <label className={`mt-3 block ${text}`}>
                <span className="mb-2 block text-xs font-medium">Calendar account</span>
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
            {selectedConnection ? (
              <div className={`mt-3 border-t pt-3 ${
                isDark ? 'border-slate-700' : 'border-[#E5EDE8]'
              }`}>
                <p className={`font-semibold ${isDark ? 'text-slate-100' : 'text-slate-800'}`}>
                  My calendars
                </p>
                <p className={`mt-1 truncate ${text}`}>
                  {selectedConnection.email || 'Selected Google account'}
                </p>
                {selectedConnection.capabilities?.calendar_list_read === true && (
                  <>
                    {calendarListQuery.isError && (
                      <div className="mt-2">
                        <p role="alert" className="text-xs text-rose-400">
                          Calendar list is unavailable. {calendarListQuery.error.message}
                        </p>
                        <button
                          type="button"
                          onClick={() => calendarListQuery.refetch()}
                          className={`mt-1 text-xs font-semibold underline ${
                            isDark ? 'text-violet-300' : 'text-[#245C4B]'
                          }`}
                        >
                          Retry calendar list
                        </button>
                      </div>
                    )}
                    {calendarListQuery.isPending && (
                      <p role="status" className={`mt-2 text-xs ${text}`}>
                        Loading calendars…
                      </p>
                    )}
                    {calendars.length > 0 && (
                      <label className={`mt-3 block text-xs ${text}`}>
                        <span className="mb-1.5 block font-medium">Calendar</span>
                        <select
                          aria-label="Google Calendar"
                          value={selectedCalendarId}
                          onChange={(event) => {
                            setCalendarId(event.target.value)
                            setEventId('')
                          }}
                          className={`w-full rounded-lg border px-3 py-2 ${input}`}
                        >
                          {calendars.map((calendar) => (
                            <option key={calendar.id} value={calendar.id}>
                              {calendar.summary}{calendar.primary ? ' (primary)' : ''}
                            </option>
                          ))}
                        </select>
                      </label>
                    )}
                    {calendarListQuery.hasNextPage && (
                      <button
                        type="button"
                        onClick={() => calendarListQuery.fetchNextPage()}
                        disabled={calendarListQuery.isFetchingNextPage}
                        className={`mt-2 text-xs font-semibold underline disabled:opacity-50 ${
                          isDark ? 'text-violet-300' : 'text-[#245C4B]'
                        }`}
                      >
                        {calendarListQuery.isFetchingNextPage
                          ? 'Loading calendars…'
                          : 'Load more calendars'}
                      </button>
                    )}
                    {calendarListQuery.isFetchNextPageError && (
                      <p role="alert" className="mt-2 text-xs text-rose-400">
                        More calendars could not be loaded.
                      </p>
                    )}
                  </>
                )}
                {selectedConnection.capabilities?.calendar_events_read === true &&
                  selectedConnection.capabilities?.calendar_list_read !== true && (
                  <p role="status" className={`mt-2 text-xs ${text}`}>
                    Calendar list access is not granted. Use “Grant Calendar
                    List Access” for this account in Connected Apps to choose
                    among calendars.
                  </p>
                )}
                {selectedConnection.capabilities?.calendar_freebusy === true && (
                  <button
                    type="button"
                    onClick={() => setFreeBusyOpen((open) => !open)}
                    className={isDark
                      ? 'mt-3 text-left text-violet-400 underline'
                      : 'mt-3 inline-flex min-h-9 items-center rounded-lg border border-[#C9E2D6] bg-[#EFF7F2] px-3 text-left text-xs font-semibold text-[#245C4B] transition-colors hover:border-[#A9CEBD] hover:bg-[#E3F1E9] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-700'
                    }
                  >
                    {freeBusyOpen ? 'Hide Free/Busy' : 'Check Free/Busy'}
                  </button>
                )}
              </div>
            ) : (
              <p className={`mt-2 text-xs ${text}`}>
                {calendarConnections.length
                  ? 'Choose a connected Google account to view its calendar.'
                  : 'No connected calendars are available.'}
              </p>
            )}
          </section>
        </aside>

        <main className={`min-w-0 rounded-2xl border p-4 sm:p-5 ${
          isDark ? '' : 'rounded-[1.25rem] border-[#E0EBE5] shadow-sm shadow-[#315847]/[0.06]'
        } ${servicePanel}`}>
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
              <button
                type="submit"
                className={`min-h-10 rounded-lg px-4 text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2 ${
                  isDark
                    ? 'bg-violet-600 text-white hover:bg-violet-500'
                    : 'bg-[#286B59] text-white hover:bg-[#205746] focus-visible:ring-offset-white'
                }`}
              >
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
                    eventsQuery.isError && !eventsQuery.data && (
                    isCalendarRequestCancellation(eventsQuery.error)
                      ? (
                        <p role="status" className={`py-4 text-center text-sm ${text}`}>
                          Loading Calendar events for the selected range…
                        </p>
                      )
                      : (
                        <div role="alert" className="rounded-xl border border-red-500/30 p-4 text-sm text-red-400">
                          <p>Live Calendar data could not be retrieved. {eventsQuery.error.message}</p>
                          <button
                            type="button"
                            onClick={() => eventsQuery.refetch()}
                            className="mt-3 font-semibold underline"
                          >
                            Retry
                          </button>
                        </div>
                      )
                  )}
                  {selectedConnection.capabilities?.calendar_events_read === true &&
                    eventsQuery.data && !eventId && events.length === 0 && (
                    <p role="status" className={`py-10 text-center text-sm ${text}`}>
                      {allEvents.length === 0
                        ? `Google Calendar returned no events for this ${view}.`
                        : 'No events match the selected filters.'}
                    </p>
                  )}
                  {selectedConnection.capabilities?.calendar_events_read === true &&
                    eventsQuery.data && !eventId && (
                    view === 'month'
                      ? (
                        <div className="overflow-x-auto">
                          <div className="min-w-[700px]">
                            <div className={isDark
                              ? `grid grid-cols-7 border-l border-t ${calendarGrid}`
                              : 'grid grid-cols-7 gap-px overflow-hidden rounded-xl border border-[#E5EDE8] bg-[#E5EDE8]'
                            }>
                              {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day) => (
                                <div key={day} className={`p-2.5 text-center text-xs ${
                                  isDark
                                    ? 'border-b border-r border-slate-700 text-slate-500'
                                    : 'bg-[#F6FAF8] font-semibold text-slate-600'
                                }`}>
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
                                    className={`${isDark
                                      ? 'min-h-28 border-b border-r border-slate-700 p-1.5'
                                      : 'min-h-36 bg-white p-2 transition-colors hover:bg-[#F4FAF6]'
                                    } ${
                                      date.slice(0, 7) !== selectedDate.slice(0, 7)
                                        ? isDark
                                          ? 'bg-slate-950/30'
                                          : 'bg-[#F7FAF8]'
                                        : isDark
                                          ? ''
                                          : 'bg-white'
                                    }`}
                                  >
                                    <button
                                      type="button"
                                      onClick={() => { setSelectedDate(date); setView('day') }}
                                      className={`${isDark
                                        ? 'mb-1 flex h-7 w-7'
                                        : 'mb-1 flex h-8 w-8'
                                      } items-center justify-center rounded-full text-xs font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 ${
                                        isDark ? 'focus-visible:ring-indigo-500' : 'focus-visible:ring-teal-700'
                                      } ${
                                        isDark
                                          ? date === today
                                            ? 'bg-violet-600 text-white'
                                            : text
                                          : date === selectedDate
                                            ? 'bg-[#DCEFE8] text-[#245C4B] ring-1 ring-[#B8D8CB]'
                                            : date === today
                                              ? 'text-teal-800 ring-1 ring-teal-300'
                                              : date.slice(0, 7) !== selectedDate.slice(0, 7)
                                                ? 'text-slate-500'
                                                : 'text-slate-800 hover:bg-[#EAF5EF]'
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
                                          showTime
                                          isDark={isDark}
                                          userEmail={userEmail}
                                          userAvatar={userAvatar}
                                          onClick={() => selectEvent(event)}
                                        />
                                      ))}
                                      {dayEvents.length > 3 && (
                                        <button
                                          type="button"
                                          onClick={() => { setSelectedDate(date); setView('day') }}
                                          className={`rounded px-1 text-[10px] font-medium focus-visible:outline-none focus-visible:ring-2 ${
                                            isDark
                                              ? 'text-violet-400 hover:text-violet-300 focus-visible:ring-indigo-500'
                                              : 'text-teal-800 hover:text-teal-950 focus-visible:ring-teal-700'
                                          }`}
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
                          selectedDate={selectedDate}
                          isDark={isDark}
                          userEmail={userEmail}
                          userAvatar={userAvatar}
                        />
                      )
                  )}
                  {eventsQuery.data && !eventId && (
                    <div className="mt-4 flex flex-col items-center gap-2">
                      {eventsQuery.isFetchNextPageError && (
                        <p role="alert" className="text-sm text-rose-400">
                          More events could not be loaded.
                        </p>
                      )}
                      {(eventsQuery.hasNextPage ||
                        eventsQuery.isFetchNextPageError) && (
                        <button
                          type="button"
                          onClick={() => eventsQuery.fetchNextPage()}
                          disabled={eventsQuery.isFetchingNextPage}
                          className={`rounded-lg border px-4 py-2 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                            isDark
                              ? 'border-[#34384b] text-slate-300 hover:bg-white/5'
                              : 'border-[#C9E2D6] bg-white text-[#245C4B] hover:bg-[#EFF7F2]'
                          }`}
                        >
                          {eventsQuery.isFetchingNextPage
                            ? 'Loading more events…'
                            : eventsQuery.isFetchNextPageError
                              ? 'Retry loading more events'
                              : 'Load more events'}
                        </button>
                      )}
                      {!eventsQuery.hasNextPage &&
                        !eventsQuery.isFetchNextPageError &&
                        allEvents.length > 0 && (
                        <p className={`text-xs ${text}`}>
                          All matching events are loaded.
                        </p>
                      )}
                    </div>
                  )}
                  {eventId &&
                    selectedConnection.capabilities?.calendar_events_read === true && (
                    <div
                      role="dialog"
                      aria-modal="true"
                      aria-labelledby="calendar-event-title"
                      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:items-center"
                    >
                      <article className={`max-h-[calc(100dvh-2rem)] w-full max-w-xl overflow-x-hidden overflow-y-auto overscroll-contain rounded-2xl border p-6 ${servicePanel}`}>
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
                            {eventDescription && (
                              <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                                {eventDescription}
                              </p>
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
