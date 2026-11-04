import { describe, expect, it } from 'vitest'
import { getOAuthRedirectTo } from './authRedirect'

describe('Google OAuth redirect URL', () => {
  it('returns the production dashboard URL for the production origin', () => {
    expect(getOAuthRedirectTo('https://www.myorbitai.in'))
      .toBe('https://www.myorbitai.in/dashboard')
  })

  it('preserves the local dashboard URL for local development', () => {
    expect(getOAuthRedirectTo('http://localhost:5173'))
      .toBe('http://localhost:5173/dashboard')
  })
})
