import { describe, expect, it } from 'vitest'
import {
  getConnectedGoogleServiceCount,
  getConnectedGoogleServices,
  isGoogleServiceConnected,
} from './googleServiceCapabilities.js'

function connection(id, capabilities, status = 'connected') {
  return {
    id,
    provider: 'google',
    status,
    capabilities,
  }
}

describe('Google Workspace capability counting', () => {
  it.each([
    [{ gmail: true }, 1],
    [{ gmail: true, calendar_events_read: true }, 2],
    [{ gmail: true, calendar_events_read: true, drive_files_read: true }, 3],
    [{
      gmail: true,
      calendar_events_read: true,
      drive_files_read: true,
      contacts_read: true,
    }, 4],
  ])('counts each granted service once: %j', (capabilities, expected) => {
    expect(getConnectedGoogleServiceCount([
      connection('google-account', capabilities),
    ])).toBe(expected)
  })

  it('does not count the same service across multiple Google accounts twice', () => {
    const connections = [
      connection('gmail-one', { gmail: true }),
      connection('gmail-two', { gmail: true }),
      connection('calendar', { calendar_events_read: true }),
    ]

    expect(getConnectedGoogleServiceCount(connections)).toBe(2)
    expect(getConnectedGoogleServices(connections)).toEqual(
      new Set(['gmail', 'calendar']),
    )
  })

  it('ignores disconnected, non-Google, and capability-free connections', () => {
    expect(getConnectedGoogleServiceCount([
      connection('disconnected', { gmail: true }, 'disconnected'),
      { ...connection('other-provider', { gmail: true }), provider: 'other' },
      connection('no-capability', {}),
    ])).toBe(0)
  })

  it('checks service access independently', () => {
    const connections = [
      connection('gmail', { gmail: true, calendar_events_read: false }),
    ]

    expect(isGoogleServiceConnected(connections, 'gmail')).toBe(true)
    expect(isGoogleServiceConnected(connections, 'calendar')).toBe(false)
  })
})
