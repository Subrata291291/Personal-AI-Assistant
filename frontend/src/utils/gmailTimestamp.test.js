import { describe, expect, it } from 'vitest'

import {
  formatGmailTimestamp,
  sortGmailMessagesByReceivedTime,
} from './gmailTimestamp.js'

describe('Gmail received timestamps', () => {
  const now = new Date(2026, 10, 4, 20, 0, 0)

  it('shows local time for messages received today', () => {
    const received = new Date(2026, 10, 4, 19, 20, 0)

    expect(formatGmailTimestamp(received.getTime(), now).label)
      .toBe(received.toLocaleTimeString(undefined, {
        hour: 'numeric',
        minute: '2-digit',
      }))
  })

  it('shows Yesterday for messages received yesterday in local time', () => {
    const yesterday = new Date(2026, 10, 3, 23, 59, 0)

    expect(formatGmailTimestamp(yesterday.getTime(), now).label)
      .toBe('Yesterday')
  })

  it('shows month and day for older messages in the current year', () => {
    const received = new Date(2026, 10, 2, 12, 0, 0)

    expect(formatGmailTimestamp(received.getTime(), now).label)
      .toBe(received.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
      }))
  })

  it('shows month, day, and year for messages from previous years', () => {
    const received = new Date(2025, 10, 2, 12, 0, 0)

    expect(formatGmailTimestamp(received.getTime(), now).label)
      .toBe(received.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      }))
  })

  it('formats the full tooltip using the browser-local timezone', () => {
    const received = new Date(2026, 10, 4, 19, 20, 5)

    expect(formatGmailTimestamp(received.toISOString(), now).title)
      .toBe(received.toLocaleString(undefined, {
        month: 'long',
        day: 'numeric',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
      }))
  })

  it.each([undefined, null, '', 'not-a-date', '999999999999999999999'])(
    'gracefully omits invalid or missing timestamps (%s)',
    (value) => {
      expect(formatGmailTimestamp(value, now)).toEqual({
        label: '',
        title: '',
      })
    },
  )

  it('sorts by Gmail internalDate newest first, keeping invalid dates last', () => {
    const messages = [
      { id: 'old', internal_date: '1762041600000', date: '2099-01-01' },
      { id: 'invalid', internal_date: null },
      { id: 'newest', internal_date: '1794000000000', date: '2000-01-01' },
      { id: 'middle', internal_date: '1770000000000' },
    ]

    expect(sortGmailMessagesByReceivedTime(messages).map(({ id }) => id))
      .toEqual(['newest', 'middle', 'old', 'invalid'])
  })
})
