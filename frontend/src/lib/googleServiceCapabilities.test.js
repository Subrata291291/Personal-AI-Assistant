import { describe, expect, it } from 'vitest'
import {
  getConnectedGoogleServiceCount,
  getConnectedGoogleServices,
  getGoogleServiceStatus,
  isGoogleServiceConnected,
  isGoogleServiceEnabled,
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
  ])('counts each granted available service once: %j', (capabilities, expected) => {
    expect(getConnectedGoogleServiceCount([
      connection('google-account', capabilities),
    ])).toBe(expected)
  })

  it('does not count Drive while it is temporarily unavailable/coming soon', () => {
    expect(getConnectedGoogleServiceCount([
      connection('google-account', {
        gmail: true,
        calendar_events_read: true,
        drive_contracts_read: true,
      }),
    ])).toBe(2)
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

  it('keeps application preferences separate from OAuth connection status', () => {
    const connections = [connection('gmail', { gmail: true })]

    expect(isGoogleServiceConnected(connections, 'gmail')).toBe(true)
    expect(isGoogleServiceEnabled('gmail', { gmail_enabled: false })).toBe(false)
    expect(isGoogleServiceEnabled('calendar', {})).toBe(true)
    expect(getGoogleServiceStatus(
      connections,
      'gmail',
      { gmail_enabled: false },
    )).toBe('Connected · Disabled')
    expect(getGoogleServiceStatus([], 'gmail')).toBe('Disconnected')
  })

  it('reports Drive as coming soon/unavailable even when scopes exist', () => {
    const connections = [
      connection('google-account', {
        drive_contracts_read: true,
        contacts_read: true,
      }),
    ]

    expect(getConnectedGoogleServiceCount(connections)).toBe(1)
    expect(getConnectedGoogleServices(connections)).toEqual(
      new Set(['drive', 'contacts']),
    )
    expect(getGoogleServiceStatus(connections, 'drive')).toBe('Coming soon')
    expect(isGoogleServiceConnected(connections, 'contacts')).toBe(true)
  })

  it('does not treat Contacts as connected without its scopes', () => {
    const connections = [
      connection('google-account', {
        gmail: true,
        contacts_write_permission_granted: true,
      }),
    ]

    expect(isGoogleServiceConnected(connections, 'contacts')).toBe(false)
  })
})
