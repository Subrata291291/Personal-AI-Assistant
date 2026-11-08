import { api } from '../lib/api.js'
import {
  requireCalendarListResponse,
  requireCalendarEventListResponse,
  requireCalendarScheduleResponse,
  requireFreeBusyResponse,
} from '../lib/responseValidation.js'

const BASE_PATH = '/api/v1/calendar'

function requireConnectionId(connectionId) {
  if (!connectionId) {
    throw new Error('A connected Google Calendar account is required.')
  }
}

export const calendarService = {
  async listCalendars({ connectionId, maxResults = 100, pageToken, signal }) {
    requireConnectionId(connectionId)
    const params = new URLSearchParams({
      connection_id: connectionId,
      max_results: String(maxResults),
    })
    if (pageToken) params.set('page_token', pageToken)
    return requireCalendarListResponse(
      signal
        ? await api.get(`${BASE_PATH}/calendars?${params}`, { signal })
        : await api.get(`${BASE_PATH}/calendars?${params}`),
    )
  },

  async getToday({ connectionId, timeZone, calendarId = 'primary' }) {
    requireConnectionId(connectionId)
    const params = new URLSearchParams({
      connection_id: connectionId,
      time_zone: timeZone,
      calendar_id: calendarId,
    })
    return requireCalendarScheduleResponse(
      await api.get(`${BASE_PATH}/schedule/today?${params}`),
    )
  },

  async getSchedule({
    connectionId,
    date,
    period = 'day',
    timeZone,
    calendarId = 'primary',
    pageToken,
  }) {
    requireConnectionId(connectionId)
    const params = new URLSearchParams({
      connection_id: connectionId,
      date,
      period,
      time_zone: timeZone,
      calendar_id: calendarId,
    })
    if (pageToken) params.set('page_token', pageToken)
    return requireCalendarScheduleResponse(
      await api.get(`${BASE_PATH}/schedule?${params}`),
    )
  },

  async listEvents({
    connectionId,
    timeMin,
    timeMax,
    query,
    timeZone,
    calendarId = 'primary',
    pageToken,
    signal,
  }) {
    requireConnectionId(connectionId)
    const params = new URLSearchParams({
      connection_id: connectionId,
      calendar_id: calendarId,
      max_results: '100',
    })
    if (timeMin) params.set('time_min', timeMin)
    if (timeMax) params.set('time_max', timeMax)
    if (query?.trim()) params.set('query', query.trim())
    if (timeZone) params.set('time_zone', timeZone)
    if (pageToken) params.set('page_token', pageToken)
    return requireCalendarEventListResponse(
      await api.get(`${BASE_PATH}/events?${params}`, { signal }),
    )
  },

  async getEvent({ connectionId, eventId, calendarId = 'primary', signal }) {
    requireConnectionId(connectionId)
    if (!eventId) throw new Error('A Calendar event is required.')
    const params = new URLSearchParams({
      connection_id: connectionId,
      calendar_id: calendarId,
    })
    const response = await api.get(
      `${BASE_PATH}/events/${encodeURIComponent(eventId)}?${params}`,
      signal ? { signal } : undefined,
    )
    return requireCalendarEventListResponse({
      calendar_id: calendarId,
      events: [response],
      next_page_token: null,
      time_zone: null,
    }).events[0]
  },

  async getFreeBusy({
    connectionId,
    timeMin,
    timeMax,
    timeZone,
    calendarIds = ['primary'],
  }) {
    requireConnectionId(connectionId)
    return requireFreeBusyResponse(
      await api.post(`${BASE_PATH}/freebusy`, {
        connection_id: connectionId,
        calendar_ids: calendarIds,
        time_min: timeMin,
        time_max: timeMax,
        time_zone: timeZone,
      }),
    )
  },
}
