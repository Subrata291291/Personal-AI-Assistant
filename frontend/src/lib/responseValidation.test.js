import { describe, expect, it } from 'vitest'

import {
  requireArrayResponse,
  requireCalendarEventListResponse,
  requireCalendarScheduleResponse,
  requireConnectionsResponse,
  requireFreeBusyResponse,
} from './responseValidation'

describe('API response shape validation', () => {
  it('accepts empty collection responses', () => {
    expect(requireArrayResponse([], 'memories')).toEqual([])
    expect(requireConnectionsResponse({ connections: [] }))
      .toEqual({ connections: [] })
  })

  it('rejects malformed collections and connection payloads', () => {
    expect(() => requireArrayResponse(null, 'conversations'))
      .toThrow('Invalid conversations response.')
    expect(() => requireConnectionsResponse({ connections: null }))
      .toThrow('Invalid connected accounts response.')
  })

  it('accepts valid empty Calendar responses and rejects malformed event data', () => {
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
    expect(requireCalendarScheduleResponse(emptySchedule)).toEqual(emptySchedule)
    expect(() => requireCalendarScheduleResponse({
      ...emptySchedule,
      events: [{ summary: 'not an event ID' }],
    })).toThrow('invalid schedule response')
    expect(() => requireCalendarEventListResponse({
      calendar_id: 'primary',
      events: [{ id: 'event-1', start: { date_time: 'not a date' } }],
      next_page_token: null,
      time_zone: null,
    })).toThrow('invalid event list response')
  })

  it('validates FreeBusy result shape rather than treating malformed data as empty', () => {
    const response = {
      time_min: '2026-11-06T08:30:00Z',
      time_max: '2026-11-06T10:30:00Z',
      time_zone: 'Asia/Kolkata',
      calendars: { primary: { busy: [], errors: [] } },
    }
    expect(requireFreeBusyResponse(response)).toEqual(response)
    expect(() => requireFreeBusyResponse({
      ...response,
      calendars: { primary: { busy: null, errors: [] } },
    })).toThrow('invalid availability response')
  })
})
