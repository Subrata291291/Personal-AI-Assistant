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
