import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
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

function CalendarLayout() {
  return <Outlet context={{ isDark: true }} />
}

function renderCalendar() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard/calendar']}>
        <Routes>
          <Route element={<CalendarLayout />}>
            <Route path="/dashboard/calendar" element={<Calendar />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
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

describe('Calendar workspace', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  beforeEach(() => {
    vi.clearAllMocks()
    mockUser.current = {
      id: 'user-1',
      user_metadata: { timezone: 'Asia/Kolkata' },
    }
    api.get.mockResolvedValue({ connections: [calendarOnlyConnection] })
    calendarService.getToday.mockResolvedValue(emptySchedule)
    calendarService.listEvents.mockResolvedValue(emptyEventList)
  })

  it('loads a verified empty month result with Calendar event-read capability only', async () => {
    renderCalendar()

    expect(await screen.findByRole('heading', { name: 'Google Calendar' }))
      .toBeInTheDocument()
    await waitFor(() => expect(calendarService.listEvents).toHaveBeenCalled())
    expect(await screen.findByText('Google Calendar returned no events for this month.'))
      .toBeInTheDocument()
    expect(calendarService.listEvents).toHaveBeenCalledWith(expect.objectContaining({
      connectionId: 'calendar-account',
      timeZone: 'Asia/Kolkata',
    }))
    expect(screen.queryByText(/Free\/Busy/)).not.toBeInTheDocument()
    expect(screen.getByText('My calendars')).toBeInTheDocument()
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
