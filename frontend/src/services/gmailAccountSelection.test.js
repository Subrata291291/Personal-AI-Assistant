import { describe, expect, it } from 'vitest'

import { resolveAssistantGmailConnection } from './gmailAccountSelection.js'

const calendarOnly = {
  id: 'calendar-account',
  provider: 'google',
  status: 'connected',
  capabilities: { gmail: false, calendar: true },
}

const gmailAccount = {
  id: 'gmail-account',
  provider: 'google',
  status: 'connected',
  capabilities: { gmail: true, calendar: false },
}

const secondGmailAccount = {
  id: 'gmail-account-2',
  provider: 'google',
  status: 'connected',
  capabilities: { gmail: true, calendar: false },
}

describe('resolveAssistantGmailConnection', () => {
  it('does not select a Calendar-only Google connection for Gmail', () => {
    expect(resolveAssistantGmailConnection([calendarOnly], null)).toBeNull()
  })

  it('selects a Gmail-capable account when it is the only eligible connection', () => {
    expect(resolveAssistantGmailConnection(
      [calendarOnly, gmailAccount],
      null,
    )).toBe(gmailAccount)
  })

  it('requires the saved default to be Gmail-capable when multiple accounts exist', () => {
    expect(resolveAssistantGmailConnection(
      [calendarOnly, gmailAccount, secondGmailAccount],
      calendarOnly.id,
    )).toBeNull()
    expect(resolveAssistantGmailConnection(
      [calendarOnly, gmailAccount, secondGmailAccount],
      gmailAccount.id,
    )).toBe(gmailAccount)
  })
})
