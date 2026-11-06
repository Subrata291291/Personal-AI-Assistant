import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  act,
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
import { api } from '../lib/api.js'
import { calendarService } from '../services/calendarService.js'

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
  })

  it('uses Calendar event-read capability without requiring Gmail or FreeBusy', async () => {
    renderCalendar()

    expect(await screen.findByRole('heading', { name: 'Google Calendar' }))
      .toBeInTheDocument()
    await waitFor(() => expect(calendarService.getToday).toHaveBeenCalled())
    expect(await screen.findByText('No events were returned for this view.'))
      .toBeInTheDocument()
    expect(calendarService.getToday).toHaveBeenCalledWith({
      connectionId: 'calendar-account',
      timeZone: 'Asia/Kolkata',
    })
    expect(screen.queryByRole('tab', { name: 'Free/Busy' })).not.toBeInTheDocument()
    expect(screen.getByText(/Free\/Busy is unavailable/)).toBeInTheDocument()
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
      expect(calendarService.getToday).toHaveBeenCalledWith({
        connectionId: 'calendar-account-2',
        timeZone: 'Asia/Kolkata',
      })
    })
  })

  it('uses the existing schedule and events endpoints for Week and Month views', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-06T12:00:00.000Z'))
    calendarService.getSchedule.mockResolvedValue({
      ...emptySchedule,
      period: 'week',
    })
    calendarService.listEvents.mockResolvedValue({
      calendar_id: 'primary',
      events: [],
      next_page_token: null,
      time_zone: 'Asia/Kolkata',
    })
    renderCalendar()

    await screen.findByRole('heading', { name: 'Google Calendar' })
    fireEvent.click(await screen.findByRole('tab', { name: 'Week' }))
    await waitFor(() => {
      expect(calendarService.getSchedule).toHaveBeenLastCalledWith({
        connectionId: 'calendar-account',
        date: '2026-11-06',
        period: 'week',
        timeZone: 'Asia/Kolkata',
      })
    })

    fireEvent.click(screen.getByRole('tab', { name: 'Month' }))
    await waitFor(() => {
      expect(calendarService.listEvents).toHaveBeenLastCalledWith({
        connectionId: 'calendar-account',
        timeMin: '2026-10-31T18:30:00.000Z',
        timeMax: '2026-11-30T18:30:00.000Z',
        timeZone: 'Asia/Kolkata',
      })
    })
  })

  it('updates the Tomorrow date after local midnight while the page stays open', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(new Date('2026-11-06T18:29:30.000Z'))
    calendarService.getSchedule.mockResolvedValue({
      ...emptySchedule,
      date: '2026-11-07',
    })
    renderCalendar()

    await screen.findByRole('heading', { name: 'Google Calendar' })
    await waitFor(() => expect(calendarService.getToday).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('tab', { name: 'Tomorrow' }))
    await waitFor(() => {
      expect(calendarService.getSchedule).toHaveBeenLastCalledWith({
        connectionId: 'calendar-account',
        date: '2026-11-07',
        timeZone: 'Asia/Kolkata',
      })
    })

    vi.setSystemTime(new Date('2026-11-06T18:30:30.000Z'))
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })

    await waitFor(() => {
      expect(calendarService.getSchedule).toHaveBeenLastCalledWith({
        connectionId: 'calendar-account',
        date: '2026-11-08',
        timeZone: 'Asia/Kolkata',
      })
    })
    vi.useRealTimers()
  })

  it('reports Calendar provider failures instead of showing an empty state', async () => {
    calendarService.getToday.mockRejectedValue(
      new Error('Google Calendar is temporarily unavailable.'),
    )
    renderCalendar()

    expect(await screen.findByRole('alert'))
      .toHaveTextContent('Live Calendar data could not be retrieved.')
    expect(screen.queryByText('No events were returned for this view.'))
      .not.toBeInTheDocument()
  })
})
