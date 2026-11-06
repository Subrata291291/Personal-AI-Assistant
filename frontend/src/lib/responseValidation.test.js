import { describe, expect, it } from 'vitest'

import {
  requireArrayResponse,
  requireConnectionsResponse,
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
})
