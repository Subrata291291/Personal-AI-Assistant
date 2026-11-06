import { describe, expect, it, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const authState = vi.hoisted(() => ({ authenticated: false }))

vi.mock('./context/AuthContext', () => ({
  useAuth: () => ({
    isAuthenticated: authState.authenticated,
    loading: false,
    signIn: vi.fn(),
    signUp: vi.fn(),
    user: {
      id: 'user-1',
      email: 'user@example.com',
      user_metadata: {},
    },
    signOut: vi.fn(),
  }),
}))

vi.mock('./lib/api.js', () => ({
  api: {
    get: vi.fn(async (path) => (
      path === '/api/v1/oauth/connections'
        ? { connections: [] }
        : []
    )),
  },
}))

vi.mock('./lib/supabase', () => ({
  supabase: {
    auth: {
      signInWithOAuth: vi.fn(),
      resetPasswordForEmail: vi.fn(),
    },
  },
}))

import App from './App.jsx'
import Login from './pages/Login.jsx'
import Signup from './pages/Signup.jsx'
import { queryClient } from './lib/queryClient.js'

describe('Public policy pages and auth links', () => {
  beforeEach(() => {
    window.history.pushState({}, '', '/')
    authState.authenticated = false
    queryClient.clear()
  })

  it('loads the privacy policy page directly without login', () => {
    window.history.pushState({}, '', '/privacy')
    render(<App />)

    expect(
      screen.getByText(/how personal ai handles your data/i)
    ).toBeInTheDocument()
  })

  it('loads the terms page directly without login', () => {
    window.history.pushState({}, '', '/terms')
    render(<App />)

    expect(
      screen.getByText(/use of the personal ai assistant/i)
    ).toBeInTheDocument()
  })

  it('shows the legal policy links on the login page', () => {
    render(
      <MemoryRouter>
        <Login />
      </MemoryRouter>
    )

    expect(screen.getByRole('link', { name: /privacy policy/i })).toHaveAttribute('href', '/privacy')
    expect(screen.getByRole('link', { name: /terms of service/i })).toHaveAttribute('href', '/terms')
  })

  it('shows the legal policy links on the signup page', () => {
    render(
      <MemoryRouter>
        <Signup />
      </MemoryRouter>
    )

    expect(screen.getByRole('link', { name: /privacy policy/i })).toHaveAttribute('href', '/privacy')
    expect(screen.getByRole('link', { name: /terms of service/i })).toHaveAttribute('href', '/terms')
  })

  it.each([
    ['/dashboard/drive', 'Google Drive'],
    ['/dashboard/contacts', 'Google Contacts'],
  ])('opens the independent route %s', async (path, title) => {
    authState.authenticated = true
    window.history.pushState({}, '', path)
    render(<App />)

    expect(await screen.findByRole('heading', { name: title }))
      .toBeInTheDocument()
  })
})
