import { describe, expect, it, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

vi.mock('./context/AuthContext', () => ({
  useAuth: () => ({
    isAuthenticated: false,
    loading: false,
    signIn: vi.fn(),
    signUp: vi.fn(),
  }),
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

describe('Public policy pages and auth links', () => {
  beforeEach(() => {
    window.history.pushState({}, '', '/')
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
})
