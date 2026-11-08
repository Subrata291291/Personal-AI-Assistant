import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { Link, MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockUser = vi.hoisted(() => ({
  current: { id: 'user-1', user_metadata: { timezone: 'Asia/Kolkata' } },
}))

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser.current }),
}))

vi.mock('../lib/api.js', () => ({
  api: { get: vi.fn() },
}))

vi.mock('../services/calendarService.js', () => ({
  calendarService: {
    listCalendars: vi.fn(),
    getToday: vi.fn(),
    getSchedule: vi.fn(),
    listEvents: vi.fn(),
    getEvent: vi.fn(),
    getFreeBusy: vi.fn(),
  },
}))

import Calendar from './Calendar.jsx'
import {
  localDateTimeCandidates,
  localDateTimeToIso,
  startOfLocalDateToIso,
} from './Calendar.jsx'
import { api } from '../lib/api.js'
import { calendarService } from '../services/calendarService.js'

describe('Calendar local date-time conversion', () => {
  it('resolves ordinary calendar-day boundaries in the selected timezone', () => {
    expect(startOfLocalDateToIso('2026-02-10', 'America/Los_Angeles'))
      .toBe('2026-02-10T08:00:00.000Z')
    expect(startOfLocalDateToIso('2026-02-11', 'America/Los_Angeles'))
      .toBe('2026-02-11T08:00:00.000Z')
  })

  it('resolves a date whose local midnight is skipped to its first valid instant', () => {
    expect(startOfLocalDateToIso('2026-09-06', 'America/Santiago'))
      .toBe('2026-09-06T04:00:00.000Z')
  })

  it('chooses the first occurrence when local midnight is repeated', () => {
    expect(startOfLocalDateToIso('2026-11-01', 'America/Havana'))
      .toBe('2026-11-01T04:00:00.000Z')
    expect(startOfLocalDateToIso('2026-11-02', 'America/Havana'))
      .toBe('2026-11-02T05:00:00.000Z')
  })

  it('converts an ordinary local time in the selected timezone', () => {
    expect(
      localDateTimeToIso('2026-02-10T12:15', 'America/Los_Angeles'),
    ).toBe('2026-02-10T20:15:00.000Z')
  })

  it('rejects a local time in the daylight-saving spring gap', () => {
    expect(() =>
      localDateTimeToIso('2026-03-08T02:30', 'America/Los_Angeles'),
    ).toThrow('That local time does not exist in the selected time zone.')
  })

  it('requires an explicit choice for a repeated daylight-saving hour', () => {
    expect(() =>
      localDateTimeToIso('2026-11-01T01:30', 'America/Los_Angeles'),
    ).toThrow('Choose the earlier or later occurrence.')
    expect(
      localDateTimeCandidates('2026-11-01T01:30', 'America/Los_Angeles')
        .map((candidate) => candidate.toISOString()),
    ).toEqual([
      '2026-11-01T08:30:00.000Z',
      '2026-11-01T09:30:00.000Z',
    ])
    expect(
      localDateTimeToIso(
        '2026-11-01T01:30',
        'America/Los_Angeles',
        'later',
      ),
    ).toBe('2026-11-01T09:30:00.000Z')
  })

  it('converts valid times immediately after both DST transitions', () => {
    expect(
      localDateTimeToIso('2026-03-08T03:30', 'America/Los_Angeles'),
    ).toBe('2026-03-08T10:30:00.000Z')
    expect(
      localDateTimeToIso('2026-11-01T02:30', 'America/Los_Angeles'),
    ).toBe('2026-11-01T10:30:00.000Z')
  })
})

function CalendarLayout({ isDark = true }) {
  return (
    <>
      <Link to="/dashboard/away">Away</Link>
      <Outlet context={{ isDark }} />
    </>
  )
}

function renderCalendar(isDark = true) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard/calendar']}>
        <Routes>
          <Route element={<CalendarLayout isDark={isDark} />}>
            <Route path="/dashboard/calendar" element={<Calendar />} />
            <Route
              path="/dashboard/away"
              element={<Link to="/dashboard/calendar">Back to Calendar</Link>}
            />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...view, queryClient }
}

const calendarOnlyConnection = {
  id: 'calendar-account',
  provider: 'google',
  status: 'connected',
  email: 'calendar@example.com',
  capabilities: {
    gmail: false,
    calendar: true,
    calendar_events_read: true,
    calendar_list_read: true,
    calendar_freebusy: false,
  },
}

const emptySchedule = {
  calendar_id: 'primary',
  date: '2026-11-06',
  period: 'day',
  time_zone: 'Asia/Kolkata',
  events: [],
  conflicts: [],
  conflicts_complete: true,
  next_page_token: null,
}

const emptyEventList = {
  calendar_id: 'primary',
  events: [],
  next_page_token: null,
  time_zone: 'Asia/Kolkata',
}

async function openEventDetailsWithDescription(description) {
  const event = {
    id: 'description-event',
    summary: 'Description test',
    start: { date: '2026-11-06', date_time: null, time_zone: null },
    end: { date: '2026-11-07', date_time: null, time_zone: null },
    attendees: [],
  }
  calendarService.listEvents.mockResolvedValue({
    ...emptyEventList,
    events: [event],
  })
  calendarService.getEvent.mockResolvedValue({ ...event, description })
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
  renderCalendar()
  fireEvent.click(await screen.findByRole('button', { name: /Description test/ }))
  const dialog = await screen.findByRole('dialog')
  await screen.findByText(/2026-11-06 · All day/)
  return { dialog, description: dialog.querySelector('p.whitespace-pre-wrap') }
}

describe('Calendar workspace', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    mockUser.current = {
      id: 'user-1',
      user_metadata: { timezone: 'Asia/Kolkata' },
    }
    api.get.mockResolvedValue({ connections: [calendarOnlyConnection] })
    calendarService.listCalendars.mockResolvedValue({
      calendars: [{
        id: 'primary',
        summary: 'Primary calendar',
        primary: true,
        access_role: 'owner',
        time_zone: 'Asia/Kolkata',
      }],
      next_page_token: null,
    })
    calendarService.getToday.mockResolvedValue(emptySchedule)
    calendarService.listEvents.mockResolvedValue(emptyEventList)
  })

  it('opens the weekly timeline for a verified Calendar connection', async () => {
    renderCalendar()

    expect(await screen.findByRole('heading', { name: 'Google Calendar' }))
      .toBeInTheDocument()
    await waitFor(() => expect(calendarService.listEvents).toHaveBeenCalled())
    expect(await screen.findByText('Google Calendar returned no events for this week.'))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'week' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('heading', { name: 'Meeting reminder' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Filters' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Other calendars' })).toBeInTheDocument()
    expect(calendarService.listEvents).toHaveBeenCalledWith(expect.objectContaining({
      connectionId: 'calendar-account',
      timeZone: 'Asia/Kolkata',
    }))
    expect(screen.queryByText(/Free\/Busy/)).not.toBeInTheDocument()
    expect(screen.getByText('My calendars')).toBeInTheDocument()
    expect(await screen.findByRole('combobox', {
      name: 'Google Calendar',
    })).toHaveValue('primary')
    expect(calendarService.listCalendars).toHaveBeenCalledWith({
      connectionId: 'calendar-account',
      pageToken: undefined,
      signal: expect.any(AbortSignal),
    })
  })

  it('loads additional event pages, preserves events, and removes overlapping duplicates', async () => {
    const first = {
      id: 'event-page-1',
      summary: 'First page event',
      start: { date_time: '2026-11-06T04:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-06T05:00:00Z', time_zone: 'UTC' },
      attendees: [],
    }
    const overlap = {
      id: 'event-overlap',
      summary: 'Overlapping event',
      start: { date_time: '2026-11-06T06:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-06T07:00:00Z', time_zone: 'UTC' },
      attendees: [],
    }
    const duplicate = { ...overlap, summary: 'Duplicate page event' }
    const second = {
      id: 'event-page-2',
      summary: 'Second page event',
      start: { date_time: '2026-11-06T08:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-06T09:00:00Z', time_zone: 'UTC' },
      attendees: [],
    }
    calendarService.listEvents.mockImplementation(({ pageToken }) => (
      pageToken
        ? Promise.resolve({
          ...emptyEventList,
          events: [duplicate, second],
          next_page_token: null,
        })
        : Promise.resolve({
          ...emptyEventList,
          events: [first, overlap],
          next_page_token: 'events-page-2',
        })
    ))
    renderCalendar()

    expect(await screen.findByRole('button', { name: /First page event/ }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Overlapping event/ }))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Load more events' }))

    expect(await screen.findByRole('button', { name: /Second page event/ }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /First page event/ }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Overlapping event/ }))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Duplicate page event/ }))
      .not.toBeInTheDocument()
    expect(calendarService.listEvents).toHaveBeenLastCalledWith(
      expect.objectContaining({
        connectionId: 'calendar-account',
        calendarId: 'primary',
        timeMin: expect.any(String),
        timeMax: expect.any(String),
        timeZone: 'Asia/Kolkata',
        pageToken: 'events-page-2',
      }),
    )
    expect(screen.getByText('All matching events are loaded.'))
      .toBeInTheDocument()
  })

  it('discovers and paginates accessible calendars', async () => {
    calendarService.listCalendars
      .mockResolvedValueOnce({
        calendars: [{
          id: 'primary',
          summary: 'Primary calendar',
          primary: true,
          access_role: 'owner',
          time_zone: 'Asia/Kolkata',
        }],
        next_page_token: 'calendar-page-2',
      })
      .mockResolvedValueOnce({
        calendars: [{
          id: 'team@example.com',
          summary: 'Team calendar',
          primary: false,
          access_role: 'reader',
          time_zone: 'Asia/Kolkata',
        }],
        next_page_token: null,
      })
    renderCalendar()

    expect(await screen.findByRole('option', {
      name: 'Primary calendar (primary)',
    })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Load more calendars' }))

    expect(await screen.findByRole('option', {
      name: 'Team calendar',
    })).toBeInTheDocument()
    expect(calendarService.listCalendars).toHaveBeenLastCalledWith({
      connectionId: 'calendar-account',
      pageToken: 'calendar-page-2',
      signal: expect.any(AbortSignal),
    })
    fireEvent.change(screen.getByRole('combobox', {
      name: 'Google Calendar',
    }), { target: { value: 'team@example.com' } })
    await waitFor(() => {
      expect(calendarService.listEvents).toHaveBeenLastCalledWith(
        expect.objectContaining({ calendarId: 'team@example.com' }),
      )
    })
  })

  it('retries a failed Calendar list request', async () => {
    calendarService.listCalendars
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce({
        calendars: [{
          id: 'primary',
          summary: 'Primary calendar',
          primary: true,
          access_role: 'owner',
          time_zone: 'Asia/Kolkata',
        }],
        next_page_token: null,
      })
    renderCalendar()

    expect(await screen.findByText(/Calendar list is unavailable/))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry calendar list' }))

    expect(await screen.findByRole('option', {
      name: 'Primary calendar (primary)',
    })).toBeInTheDocument()
  })

  it('keeps already-loaded events when retrying a failed next page', async () => {
    const firstPageEvent = {
      id: 'available-page-one',
      summary: 'Available event',
      start: { date_time: '2026-11-06T04:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-06T05:00:00Z', time_zone: 'UTC' },
      attendees: [],
    }
    const secondPageEvent = {
      id: 'available-page-two',
      summary: 'Recovered event',
      start: { date_time: '2026-11-06T06:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-06T07:00:00Z', time_zone: 'UTC' },
      attendees: [],
    }
    let attempts = 0
    calendarService.listEvents.mockImplementation(({ pageToken }) => {
      if (!pageToken) {
        return Promise.resolve({
          ...emptyEventList,
          events: [firstPageEvent],
          next_page_token: 'events-page-2',
        })
      }
      attempts += 1
      return attempts === 1
        ? Promise.reject(new Error('temporary failure'))
        : Promise.resolve({
          ...emptyEventList,
          events: [secondPageEvent],
          next_page_token: null,
        })
    })
    renderCalendar()

    expect(await screen.findByRole('button', { name: /Available event/ }))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Load more events' }))
    expect(await screen.findByText('More events could not be loaded.'))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Available event/ }))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {
      name: 'Retry loading more events',
    }))

    expect(await screen.findByRole('button', { name: /Recovered event/ }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Available event/ }))
      .toBeInTheDocument()
    expect(screen.getByText('All matching events are loaded.'))
      .toBeInTheDocument()
  })

  it('reuses Calendar events and connection status after route navigation', async () => {
    const event = {
      id: 'cached-calendar-event',
      summary: 'Cached event',
      start: { date_time: '2026-11-06T04:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-06T05:00:00Z', time_zone: 'UTC' },
      attendees: [],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T00:00:00.000Z'))
    renderCalendar(false)

    expect(await screen.findByRole('button', { name: /Cached event/ }))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: 'Away' }))
    fireEvent.click(await screen.findByRole('link', { name: 'Back to Calendar' }))

    expect(await screen.findByRole('button', { name: /Cached event/ }))
      .toBeInTheDocument()
    expect(calendarService.listEvents).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledTimes(1)
  })

  it('restores the selected date and view and reuses their cached events', async () => {
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T00:00:00.000Z'))
    renderCalendar(false)

    await screen.findByRole('heading', { name: 'Google Calendar' })
    await waitFor(() => expect(calendarService.listEvents).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('Calendar date'), {
      target: { value: '2026-11-07' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'day' }))
    await waitFor(() => expect(calendarService.listEvents).toHaveBeenCalledTimes(2))

    fireEvent.click(screen.getByRole('link', { name: 'Away' }))
    fireEvent.click(await screen.findByRole('link', { name: 'Back to Calendar' }))

    expect(screen.getByLabelText('Calendar date')).toHaveValue('2026-11-07')
    expect(screen.getByRole('button', { name: 'day' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(calendarService.listEvents).toHaveBeenCalledTimes(2)
    expect(api.get).toHaveBeenCalledTimes(1)
  })

  it('shows the upcoming meeting date in the reminder card', async () => {
    const event = {
      id: 'reminder-date',
      summary: 'Planning meeting',
      start: { date_time: '2026-11-08T04:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-08T05:00:00Z', time_zone: 'UTC' },
      attendees: [],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-07T00:00:00.000Z'))
    renderCalendar(false)

    const reminderHeading = await screen.findByRole('heading', {
      name: 'Meeting reminder',
    })
    const dateLabel = new Intl.DateTimeFormat(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'UTC',
    }).format(new Date('2026-11-08T12:00:00.000Z'))
    const reminder = reminderHeading.parentElement
    await waitFor(() => {
      expect(reminder).toHaveTextContent('Planning meeting')
      expect(reminder).toHaveTextContent(dateLabel)
      expect(reminder).toHaveTextContent('AM')
    })
  })

  it('renders weekly event times and attendee initials with an overflow count', async () => {
    const event = {
      id: 'weekly-attendees',
      summary: 'UX Weekly',
      start: { date_time: '2026-11-06T04:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-06T05:00:00Z', time_zone: 'UTC' },
      attendees: [
        { display_name: 'Ada Lovelace', email: 'ada@example.com' },
        { display_name: 'Bob Stone', email: 'bob@example.com' },
        { display_name: 'Chloe Green', email: 'chloe@example.com' },
        { display_name: 'Dev Shah', email: 'dev@example.com' },
        { display_name: 'Eve Black', email: 'eve@example.com' },
        { display_name: 'Farah Wells', email: 'farah@example.com' },
      ],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    calendarService.getEvent.mockResolvedValue(event)
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T00:00:00.000Z'))
    renderCalendar(false)

    const eventCard = await screen.findByRole('button', { name: /UX Weekly/ })
    const formatter = new Intl.DateTimeFormat(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      timeZone: 'Asia/Kolkata',
    })
    expect(eventCard).toHaveTextContent(
      `${formatter.format(new Date(event.start.date_time))} – ${formatter.format(new Date(event.end.date_time))}`,
    )
    const attendeeGroup = eventCard.querySelector('[aria-label="Attendees"]')
    expect(attendeeGroup.querySelectorAll('[role="img"]')).toHaveLength(4)
    expect(attendeeGroup.querySelector('[aria-label="Ada Lovelace avatar"]'))
      .toHaveTextContent('AL')
    expect(attendeeGroup).toHaveTextContent('+2')

    fireEvent.click(eventCard)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('filters only supported timed and all-day event types', async () => {
    const timedEvent = {
      id: 'timed-filter',
      summary: 'Timed filter event',
      start: { date_time: '2026-11-06T04:30:00Z', time_zone: 'UTC' },
      end: { date_time: '2026-11-06T05:00:00Z', time_zone: 'UTC' },
    }
    const allDayEvent = {
      id: 'all-day-filter',
      summary: 'All-day filter event',
      start: { date: '2026-11-06' },
      end: { date: '2026-11-07' },
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [timedEvent, allDayEvent],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T00:00:00.000Z'))
    renderCalendar(false)

    expect(await screen.findByRole('button', { name: /Timed filter event/ }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /All-day filter event/ }))
      .toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Timed events'))
    expect(screen.queryByRole('button', { name: /Timed filter event/ }))
      .not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /All-day filter event/ }))
      .toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('All-day events'))
    expect(await screen.findByText('No events match the selected filters.'))
      .toBeInTheDocument()
  })

  it('renders events without attendee data safely', async () => {
    const event = {
      id: 'no-attendees',
      summary: 'No attendee details',
      start: { date_time: '2026-11-06T04:30:00Z' },
      end: { date_time: '2026-11-06T05:00:00Z' },
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T00:00:00.000Z'))
    renderCalendar(false)

    const eventCard = await screen.findByRole('button', { name: /No attendee details/ })
    expect(eventCard.querySelector('[aria-label="Attendees"]')).toBeNull()
  })

  it('queries the full date when local midnight is skipped', async () => {
    mockUser.current = {
      id: 'user-1',
      user_metadata: { timezone: 'America/Santiago' },
    }
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-09-06T12:00:00.000Z'))
    renderCalendar()

    await screen.findByRole('heading', { name: 'Google Calendar' })
    fireEvent.click(screen.getByRole('button', { name: 'day' }))
    await waitFor(() => {
      expect(calendarService.listEvents).toHaveBeenLastCalledWith(expect.objectContaining({
        connectionId: 'calendar-account',
        timeMin: '2026-09-06T04:00:00.000Z',
        timeMax: '2026-09-07T03:00:00.000Z',
        timeZone: 'America/Santiago',
      }))
    })
  })

  it('requires explicit selection when multiple Calendar accounts have event access', async () => {
    api.get.mockResolvedValue({
      connections: [
        calendarOnlyConnection,
        { ...calendarOnlyConnection, id: 'calendar-account-2', email: 'second@example.com' },
      ],
    })
    renderCalendar()

    expect(await screen.findByText('Select the Google account whose calendar you want to view.'))
      .toBeInTheDocument()
    expect(calendarService.getToday).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Calendar account'), {
      target: { value: 'calendar-account-2' },
    })
    await waitFor(() => {
      expect(calendarService.listEvents).toHaveBeenCalledWith(expect.objectContaining({
        connectionId: 'calendar-account-2',
        timeZone: 'Asia/Kolkata',
      }))
    })
  })

  it('renders the complete month grid and navigates to week and day timelines', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar()

    await screen.findByRole('heading', { name: 'Google Calendar' })
    fireEvent.click(screen.getByRole('button', { name: 'month' }))
    await screen.findByText('Google Calendar returned no events for this month.')
    expect(screen.getByText('Monday')).toBeInTheDocument()
    expect(screen.getByText('Sunday')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'week' }))
    await waitFor(() => {
      expect(calendarService.listEvents).toHaveBeenLastCalledWith(expect.objectContaining({
        connectionId: 'calendar-account',
        timeZone: 'Asia/Kolkata',
        timeMin: expect.any(String),
        timeMax: expect.any(String),
      }))
    })

    fireEvent.click(screen.getByRole('button', { name: 'day' }))
    expect(await screen.findByLabelText('Current time')).toBeInTheDocument()
  })

  it('Today returns the calendar to the current date after date selection', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar()

    await screen.findByRole('heading', { name: 'Google Calendar' })
    fireEvent.change(screen.getByLabelText('Calendar date'), {
      target: { value: '2026-11-13' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Today' }))
    expect(screen.getByLabelText('Calendar date')).toHaveValue('2026-11-06')
  })

  it('reports Calendar provider failures instead of showing an empty state', async () => {
    calendarService.listEvents.mockRejectedValue(
      new Error('Google Calendar is temporarily unavailable.'),
    )
    renderCalendar()

    expect(await screen.findByRole('alert'))
      .toHaveTextContent('Live Calendar data could not be retrieved.')
    expect(screen.queryByText(/returned no events/))
      .not.toBeInTheDocument()
  })

  it('changes the weekly range to include November 9, 2026', async () => {
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar()

    await screen.findByRole('heading', { name: 'Google Calendar' })
    fireEvent.change(screen.getByLabelText('Calendar date'), {
      target: { value: '2026-11-09' },
    })

    await waitFor(() => {
      const rangeCalls = calendarService.listEvents.mock.calls.filter(
        ([options]) => options.timeMin === '2026-11-08T18:30:00.000Z',
      )
      expect(rangeCalls.length).toBeGreaterThan(0)
      const latest = rangeCalls.at(-1)?.[0]
      expect(latest.timeMin).toBe('2026-11-08T18:30:00.000Z')
      expect(latest.timeMax).toBe('2026-11-15T18:30:00.000Z')
    })
  })

  it('renders provider event details and all-day events without shifting dates', async () => {
    const event = {
      id: 'event-1',
      summary: 'Project review',
      start: { date: '2026-11-06', date_time: null, time_zone: null },
      end: { date: '2026-11-07', date_time: null, time_zone: null },
      attendees: [],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    calendarService.getEvent.mockResolvedValue({
      ...event,
      location: 'Room 2',
      description: 'Review the launch plan',
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar()

    fireEvent.click(await screen.findByRole('button', { name: /Project review/ }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(await screen.findByText(/2026-11-06 · All day/)).toBeInTheDocument()
    expect(screen.getByText('Location: Room 2')).toBeInTheDocument()
    expect(screen.getByText('Review the launch plan')).toBeInTheDocument()
  })

  it('uses the premium light palette for selected dates and event chips', async () => {
    const event = {
      id: 'light-mode-event',
      summary: 'Light mode event',
      start: { date: '2026-11-06', date_time: null, time_zone: null },
      end: { date: '2026-11-07', date_time: null, time_zone: null },
      attendees: [],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar(false)

    fireEvent.click(screen.getByRole('button', { name: 'month' }))
    const selectedMiniDate = await screen.findByRole('button', {
      name: '2026-11-06',
    })
    expect(selectedMiniDate).toHaveClass('bg-[#DCEFE8]', 'text-[#245C4B]')
    const eventChip = await screen.findByRole('button', {
      name: /Light mode event/,
    })
    expect(eventChip).toHaveClass('border-l-[3px]', 'text-slate-900')
    expect(eventChip.className).toMatch(
      /bg-\[#(?:D5F1F6|F9E3C9|F4D7EE|DDD8F8|DCEFE8)\]/,
    )
    const assignedColor = eventChip.className.match(/bg-\[#(?:D5F1F6|F9E3C9|F4D7EE|DDD8F8|DCEFE8)\]/)[0]
    fireEvent.click(screen.getByRole('button', { name: 'week' }))
    const weeklyEventChip = await screen.findByRole('button', {
      name: /Light mode event/,
    })
    expect(weeklyEventChip).toHaveClass(assignedColor)
    expect(screen.getByRole('heading', { name: 'Google Calendar' })
      .closest('header').parentElement)
      .toHaveClass('bg-[#EFF7F2]')
  })

  it('preserves dark mode date and event colors', async () => {
    const event = {
      id: 'dark-mode-event',
      summary: 'Dark mode event',
      start: { date: '2026-11-06', date_time: null, time_zone: null },
      end: { date: '2026-11-07', date_time: null, time_zone: null },
      attendees: [],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar(true)

    expect(await screen.findByRole('button', {
      name: '2026-11-06',
    })).toHaveClass('bg-violet-600', 'text-white')
    expect(await screen.findByRole('button', {
      name: /Dark mode event/,
    })).toHaveClass('bg-violet-500/15', 'text-violet-200')
  })

  it('shows Month View event titles and timezone-formatted start and end times', async () => {
    const event = {
      id: 'timed-month-event',
      summary: 'UX Huddle Call',
      start: {
        date_time: '2026-11-06T03:30:00Z',
        time_zone: 'UTC',
      },
      end: {
        date_time: '2026-11-06T04:00:00Z',
        time_zone: 'UTC',
      },
      attendees: [],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    calendarService.getEvent.mockResolvedValue(event)
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar(false)

    fireEvent.click(screen.getByRole('button', { name: 'month' }))
    const eventCard = await screen.findByRole('button', {
      name: /UX Huddle Call/,
    })
    const timeLabel = new Intl.DateTimeFormat(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      timeZone: 'Asia/Kolkata',
    })
    expect(eventCard).toHaveTextContent(
      `${timeLabel.format(new Date(event.start.date_time))} – ${timeLabel.format(new Date(event.end.date_time))}`,
    )

    fireEvent.click(eventCard)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(calendarService.getEvent).toHaveBeenCalledWith({
      connectionId: 'calendar-account',
      eventId: event.id,
      calendarId: 'primary',
      signal: expect.any(AbortSignal),
    })
  })

  it('labels Month View all-day events and handles absent time values', async () => {
    const event = {
      id: 'all-day-month-event',
      summary: 'All-day planning',
      start: { date: '2026-11-06' },
      attendees: [],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar(false)

    fireEvent.click(screen.getByRole('button', { name: 'month' }))
    const eventCard = await screen.findByRole('button', {
      name: /All-day planning/,
    })
    expect(eventCard).toHaveTextContent('All day')
  })

  it('renders Month View safely when event times are invalid', async () => {
    const event = {
      id: 'invalid-time-month-event',
      summary: 'Invalid time event',
      start: { date: '2026-11-06' },
      end: { date_time: 'invalid-date' },
      attendees: [],
    }
    calendarService.listEvents.mockResolvedValue({
      ...emptyEventList,
      events: [event],
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    renderCalendar(false)

    fireEvent.click(screen.getByRole('button', { name: 'month' }))
    expect(await screen.findByRole('button', {
      name: /Invalid time event/,
    })).toHaveTextContent('All day')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('formats HTML paragraphs as readable separated text', async () => {
    const result = await openEventDetailsWithDescription(
      '<p>First paragraph.</p><p>Second paragraph.</p>',
    )

    expect(result.description.textContent).toBe(
      'First paragraph.\n\nSecond paragraph.',
    )
  })

  it('preserves linked URLs as readable text without rendering provider HTML', async () => {
    const result = await openEventDetailsWithDescription(
      '<p>Join using <a href="https://example.com/meeting">the meeting link</a>.</p>',
    )

    expect(result.description.textContent).toBe(
      'Join using the meeting link (https://example.com/meeting).',
    )
    expect(result.description.querySelector('a')).toBeNull()
  })

  it('decodes HTML entities in event descriptions', async () => {
    const result = await openEventDetailsWithDescription(
      '<p>Tom &amp; Jerry &lt;3 &quot;Review&quot;</p>',
    )

    expect(result.description.textContent).toBe('Tom & Jerry <3 "Review"')
  })

  it('leaves plain-text descriptions unchanged', async () => {
    const plainText = 'Line one.\n  Line two.\nhttps://example.com/agenda'
    const result = await openEventDetailsWithDescription(plainText)

    expect(result.description.textContent).toBe(plainText)
  })

  it('omits empty event descriptions', async () => {
    const result = await openEventDetailsWithDescription('')

    expect(result.description).toBeNull()
  })

  it('omits missing event descriptions', async () => {
    const result = await openEventDetailsWithDescription(undefined)

    expect(result.description).toBeNull()
  })

  it('keeps HTML list items on separate readable lines', async () => {
    const result = await openEventDetailsWithDescription(
      '<ul><li>First item</li><li>Second item</li></ul>',
    )

    expect(result.description.textContent).toBe('• First item\n• Second item')
  })

  it('wraps long URLs to prevent horizontal overflow', async () => {
    const longUrl = `https://example.com/${'a'.repeat(300)}`
    const result = await openEventDetailsWithDescription(longUrl)

    expect(result.description.textContent).toBe(longUrl)
    expect(result.description).toHaveClass('[overflow-wrap:anywhere]')
  })

  it('does not fetch events when the Calendar application preference is disabled', async () => {
    mockUser.current = {
      id: 'user-1',
      user_metadata: { timezone: 'Asia/Kolkata', calendar_enabled: false },
    }
    renderCalendar()

    expect(await screen.findByText('Google Calendar is connected but disabled'))
      .toBeInTheDocument()
    expect(calendarService.listEvents).not.toHaveBeenCalled()
  })

  it('requires and applies an explicit choice for a repeated availability time', async () => {
    mockUser.current = {
      id: 'user-1',
      user_metadata: { timezone: 'America/Los_Angeles' },
    }
    api.get.mockResolvedValue({
      connections: [{
        ...calendarOnlyConnection,
        capabilities: {
          ...calendarOnlyConnection.capabilities,
          calendar_freebusy: true,
        },
      }],
    })
    calendarService.getFreeBusy.mockResolvedValue({
      calendars: { primary: { errors: [], busy: [] } },
    })
    renderCalendar()

    fireEvent.click(await screen.findByRole('button', { name: 'Check Free/Busy' }))
    fireEvent.change(screen.getByLabelText('Availability start'), {
      target: { value: '2026-11-01T01:30' },
    })
    fireEvent.change(screen.getByLabelText('Availability end'), {
      target: { value: '2026-11-01T02:30' },
    })

    const occurrence = await screen.findByLabelText(
      'Availability start occurrence',
    )
    expect(occurrence).toBeRequired()
    fireEvent.change(occurrence, { target: { value: 'later' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check availability' }))

    await waitFor(() => {
      expect(calendarService.getFreeBusy).toHaveBeenCalledWith({
        connectionId: 'calendar-account',
        timeMin: '2026-11-01T09:30:00.000Z',
        timeMax: '2026-11-01T10:30:00.000Z',
        timeZone: 'America/Los_Angeles',
      })
    })
  })

  it('shows a clear validation message for a nonexistent availability time', async () => {
    mockUser.current = {
      id: 'user-1',
      user_metadata: { timezone: 'America/Los_Angeles' },
    }
    api.get.mockResolvedValue({
      connections: [{
        ...calendarOnlyConnection,
        capabilities: {
          ...calendarOnlyConnection.capabilities,
          calendar_freebusy: true,
        },
      }],
    })
    renderCalendar()

    fireEvent.click(await screen.findByRole('button', { name: 'Check Free/Busy' }))
    fireEvent.change(screen.getByLabelText('Availability start'), {
      target: { value: '2026-03-08T02:30' },
    })
    fireEvent.change(screen.getByLabelText('Availability end'), {
      target: { value: '2026-03-08T04:30' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Check availability' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That local time does not exist in the selected time zone.',
    )
    expect(calendarService.getFreeBusy).not.toHaveBeenCalled()
  })
})
