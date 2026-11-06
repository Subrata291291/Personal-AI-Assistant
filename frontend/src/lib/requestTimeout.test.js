import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_REQUEST_TIMEOUT_MS,
  withAuthTimeout,
} from './requestTimeout'

describe('Supabase operation deadlines', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('stops waiting when an auth operation exceeds its deadline', async () => {
    vi.useFakeTimers()
    const operation = new Promise(() => {})
    const result = withAuthTimeout(operation, 'Profile update')
    const assertion = expect(result).rejects.toMatchObject({
      code: 'REQUEST_TIMEOUT',
      timeout: true,
      phase: 'auth',
      message: expect.stringContaining(
        'The operation may still complete',
      ),
    })

    await vi.advanceTimersByTimeAsync(API_REQUEST_TIMEOUT_MS)
    await assertion
  })
})
