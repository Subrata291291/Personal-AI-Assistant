import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api.js'
import { calendarService } from './calendarService.js'

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

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

describe('calendarService', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('uses the authenticated API client and validates schedule responses', async () => {
    api.get.mockResolvedValue(emptySchedule)
    await expect(calendarService.getSchedule({
      connectionId: 'connection-1',
      date: '2026-11-06',
      timeZone: 'Asia/Kolkata',
    })).resolves.toEqual(emptySchedule)
    expect(api.get).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/calendar/schedule?'),
    )
    expect(api.get.mock.calls[0][0]).toContain('connection_id=connection-1')
    expect(api.get.mock.calls[0][0]).toContain('date=2026-11-06')
    expect(api.get.mock.calls[0][0]).toContain('time_zone=Asia%2FKolkata')
  })

  it('supports date-range and text-search event listing', async () => {
    api.get.mockResolvedValue({
      calendar_id: 'primary',
      events: [],
      next_page_token: null,
      time_zone: 'Asia/Kolkata',
    })
    await calendarService.listEvents({
      connectionId: 'connection-1',
      timeMin: '2026-11-06T00:00:00Z',
      timeMax: '2026-11-07T00:00:00Z',
      query: 'planning',
      timeZone: 'Asia/Kolkata',
    })
    expect(api.get.mock.calls[0][0]).toContain('time_min=')
    expect(api.get.mock.calls[0][0]).toContain('time_max=')
    expect(api.get.mock.calls[0][0]).toContain('query=planning')
  })

  it('sends event page tokens without dropping calendar or range filters', async () => {
    api.get.mockResolvedValue({
      calendar_id: 'team@example.com',
      events: [],
      next_page_token: null,
      time_zone: 'Asia/Kolkata',
    })
    await calendarService.listEvents({
      connectionId: 'connection-1',
      calendarId: 'team@example.com',
      timeMin: '2026-11-01T18:30:00.000Z',
      timeMax: '2026-11-30T18:30:00.000Z',
      query: 'planning',
      timeZone: 'Asia/Kolkata',
      pageToken: 'next/page+token',
    })
    const requestUrl = new URL(api.get.mock.calls[0][0], 'http://localhost')
    expect(Object.fromEntries(requestUrl.searchParams)).toEqual({
      connection_id: 'connection-1',
      calendar_id: 'team@example.com',
      max_results: '100',
      time_min: '2026-11-01T18:30:00.000Z',
      time_max: '2026-11-30T18:30:00.000Z',
      query: 'planning',
      time_zone: 'Asia/Kolkata',
      page_token: 'next/page+token',
    })
  })

  it('requests calendar-list continuation pages and validates the response', async () => {
    const response = {
      calendars: [{
        id: 'team@example.com',
        summary: 'Team',
        primary: false,
        access_role: 'reader',
        time_zone: 'Asia/Kolkata',
      }],
      next_page_token: null,
    }
    api.get.mockResolvedValue(response)
    await expect(calendarService.listCalendars({
      connectionId: 'connection-1',
      maxResults: 25,
      pageToken: 'calendar-page-2',
    })).resolves.toEqual(response)
    expect(api.get).toHaveBeenCalledWith(
      '/api/v1/calendar/calendars?connection_id=connection-1&max_results=25&page_token=calendar-page-2',
    )
  })

  it('accepts a monthly schedule response', async () => {
    api.get.mockResolvedValue({ ...emptySchedule, period: 'month' })
    await expect(calendarService.getSchedule({
      connectionId: 'connection-1',
      date: '2026-11-07',
      period: 'month',
      timeZone: 'Asia/Kolkata',
      calendarId: 'primary',
    })).resolves.toMatchObject({ period: 'month' })
    expect(api.get.mock.calls[0][0]).toContain('period=month')
  })

  it('preserves schedule filters when requesting a continuation page', async () => {
    api.get.mockResolvedValue({ ...emptySchedule, next_page_token: null })
    await calendarService.getSchedule({
      connectionId: 'connection-1',
      date: '2026-11-07',
      period: 'month',
      timeZone: 'Asia/Kolkata',
      calendarId: 'team@example.com',
      pageToken: 'schedule-page-2',
    })
    const requestUrl = new URL(api.get.mock.calls[0][0], 'http://localhost')
    expect(Object.fromEntries(requestUrl.searchParams)).toMatchObject({
      connection_id: 'connection-1',
      date: '2026-11-07',
      period: 'month',
      time_zone: 'Asia/Kolkata',
      calendar_id: 'team@example.com',
      page_token: 'schedule-page-2',
    })
  })

  it('rejects malformed responses and missing account IDs', async () => {
    api.get.mockResolvedValue({
      ...emptySchedule,
      events: null,
    })
    await expect(calendarService.getToday({
      connectionId: 'connection-1',
      timeZone: 'Asia/Kolkata',
    })).rejects.toThrow('invalid schedule response')
    await expect(calendarService.getToday({
      connectionId: '',
      timeZone: 'Asia/Kolkata',
    })).rejects.toThrow('connected Google Calendar account is required')
  })

  it('posts FreeBusy queries with the selected account and validates the result', async () => {
    api.post.mockResolvedValue({
      time_min: '2026-11-06T08:30:00Z',
      time_max: '2026-11-06T10:30:00Z',
      time_zone: 'Asia/Kolkata',
      calendars: { primary: { busy: [], errors: [] } },
    })
    await calendarService.getFreeBusy({
      connectionId: 'connection-1',
      timeMin: '2026-11-06T08:30:00Z',
      timeMax: '2026-11-06T10:30:00Z',
      timeZone: 'Asia/Kolkata',
    })
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/calendar/freebusy',
      expect.objectContaining({
        connection_id: 'connection-1',
        calendar_ids: ['primary'],
      }),
    )
  })
})
