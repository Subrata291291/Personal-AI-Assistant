export function requireArrayResponse(value, resource) {
  if (!Array.isArray(value)) {
    throw new Error(`Invalid ${resource} response.`)
  }

  return value
}

export function requireConnectionsResponse(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    !Array.isArray(value.connections) ||
    (
      value.calendar_capabilities !== undefined &&
      (
        !value.calendar_capabilities ||
        typeof value.calendar_capabilities !== 'object' ||
        Array.isArray(value.calendar_capabilities)
      )
    )
  ) {
    throw new Error('Invalid connected accounts response.')
  }

  return value
}

function isCalendarEventTime(value) {
  if (value === null) return true
  if (
    typeof value === 'object' &&
    !Array.isArray(value) &&
    (value.date === null || typeof value.date === 'string') &&
    (value.date_time === null || typeof value.date_time === 'string') &&
    (value.time_zone === null || typeof value.time_zone === 'string')
  ) {
    const hasDate = typeof value.date === 'string'
    const hasDateTime = typeof value.date_time === 'string'
    const validDate = !hasDate || (
      /^\d{4}-\d{2}-\d{2}$/.test(value.date) &&
      !Number.isNaN(Date.parse(`${value.date}T00:00:00Z`))
    )
    const validDateTime = !hasDateTime ||
      !Number.isNaN(Date.parse(value.date_time))
    return validDate &&
      validDateTime &&
      !(hasDate && hasDateTime)
  }
  return false
}

function isCalendarEvent(value) {
  return Boolean(
    value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof value.id === 'string' &&
    value.id.trim() &&
    (value.summary === null || typeof value.summary === 'string') &&
    isCalendarEventTime(value.start) &&
    isCalendarEventTime(value.end) &&
    Array.isArray(value.attendees) &&
    value.attendees.every((attendee) =>
      attendee &&
      typeof attendee === 'object' &&
      !Array.isArray(attendee) &&
      (attendee.email === null || typeof attendee.email === 'string') &&
      (attendee.display_name === null || typeof attendee.display_name === 'string')
    )
  )
}

export function requireCalendarEventListResponse(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    typeof value.calendar_id !== 'string' ||
    !Array.isArray(value.events) ||
    !value.events.every(isCalendarEvent) ||
    !(
      value.next_page_token === null ||
      typeof value.next_page_token === 'string'
    ) ||
    !(
      value.time_zone === null ||
      typeof value.time_zone === 'string'
    )
  ) {
    throw new Error('Google Calendar returned an invalid event list response.')
  }

  return value
}

export function requireCalendarListResponse(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    !Array.isArray(value.calendars) ||
    !value.calendars.every((calendar) =>
      calendar &&
      typeof calendar === 'object' &&
      !Array.isArray(calendar) &&
      typeof calendar.id === 'string' &&
      calendar.id.trim() &&
      typeof calendar.summary === 'string' &&
      calendar.summary.trim() &&
      (calendar.primary === null || typeof calendar.primary === 'boolean') &&
      (calendar.access_role === null || typeof calendar.access_role === 'string') &&
      (calendar.time_zone === null || typeof calendar.time_zone === 'string')
    ) ||
    !(
      value.next_page_token === null ||
      typeof value.next_page_token === 'string'
    )
  ) {
    throw new Error('Google Calendar returned an invalid calendar list response.')
  }

  return value
}

export function requireCalendarScheduleResponse(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    typeof value.calendar_id !== 'string' ||
    typeof value.date !== 'string' ||
    !['day', 'week', 'month'].includes(value.period) ||
    typeof value.time_zone !== 'string' ||
    !Array.isArray(value.events) ||
    !value.events.every(isCalendarEvent) ||
    !Array.isArray(value.conflicts) ||
    !value.conflicts.every((conflict) =>
      conflict &&
      typeof conflict === 'object' &&
      !Array.isArray(conflict) &&
      Object.values(conflict).every((item) => typeof item === 'string')
    ) ||
    typeof value.conflicts_complete !== 'boolean'
  ) {
    throw new Error('Google Calendar returned an invalid schedule response.')
  }

  return value
}

export function requireFreeBusyResponse(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    typeof value.time_min !== 'string' ||
    typeof value.time_max !== 'string' ||
    typeof value.time_zone !== 'string' ||
    !value.calendars ||
    typeof value.calendars !== 'object' ||
    Array.isArray(value.calendars) ||
    Object.values(value.calendars).some((calendar) =>
      !calendar ||
      typeof calendar !== 'object' ||
      Array.isArray(calendar) ||
      !Array.isArray(calendar.busy) ||
      !calendar.busy.every((interval) =>
        interval &&
        typeof interval === 'object' &&
        typeof interval.start === 'string' &&
        !Number.isNaN(Date.parse(interval.start)) &&
        typeof interval.end === 'string' &&
        !Number.isNaN(Date.parse(interval.end))
      ) ||
      !Array.isArray(calendar.errors) ||
      !calendar.errors.every((error) =>
        error &&
        typeof error === 'object' &&
        !Array.isArray(error) &&
        (error.domain === null || typeof error.domain === 'string') &&
        (error.reason === null || typeof error.reason === 'string')
      )
    )
  ) {
    throw new Error('Google Calendar returned an invalid availability response.')
  }

  return value
}
