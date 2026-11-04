import { describe, expect, it } from 'vitest'
import {
  formatErrorMessage,
  formatRequestError,
} from './errorMessage.js'

describe('formatErrorMessage', () => {
  it('preserves string errors', () => {
    expect(formatErrorMessage(new Error('The request failed.')))
      .toBe('The request failed.')
  })

  it('formats multiple FastAPI validation errors without exposing raw objects', () => {
    const message = formatErrorMessage({
      detail: [
        {
          loc: ['body', 'connection_id'],
          msg: 'Input should be a valid UUID',
          input: 'private-input',
        },
        {
          loc: ['body', 'subject'],
          msg: 'Field required',
        },
      ],
    })

    expect(message).toBe(
      'body.connection_id: Input should be a valid UUID\n' +
      'body.subject: Field required',
    )
    expect(message).not.toContain('private-input')
    expect(message).not.toContain('[object Object]')
  })

  it('uses a readable fallback for unknown errors', () => {
    expect(formatErrorMessage({ code: 'unknown' }, 'Try again later.'))
      .toBe('Try again later.')
  })

  it('labels network failures and includes only the request correlation ID', () => {
    const error = Object.assign(new TypeError('Failed to fetch'), {
      networkFailure: true,
      requestId: 'request-123',
    })
    const message = formatRequestError(error)

    expect(message).toContain('Could not reach the API.')
    expect(message).toContain('Request ID: request-123')
    expect(message).not.toContain('Failed to fetch')
  })
})
