import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const currentUser = vi.hoisted(() => ({
  value: { id: 'user-1', email: 'user@example.com', user_metadata: {} },
}))

vi.mock('../../context/AuthContext', () => ({
  useAuth: () => ({
    user: currentUser.value,
    signOut: vi.fn(),
  }),
}))

vi.mock('../../lib/api.js', () => ({
  api: {
    get: vi.fn(),
  },
}))

import DashboardLayout from './DashboardLayout.jsx'
import { api } from '../../lib/api.js'

function renderLayout() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
    },
  })

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route element={<DashboardLayout />}>
            <Route path="/dashboard" element={<p>Dashboard route</p>} />
            <Route path="/dashboard/connectors" element={<p>Connectors route</p>} />
            <Route path="/dashboard/gmail" element={<p>Gmail route</p>} />
            <Route path="/dashboard/calendar" element={<p>Calendar route</p>} />
            <Route path="/dashboard/drive" element={<p>Drive route</p>} />
            <Route path="/dashboard/contacts" element={<p>Contacts route</p>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Dashboard integration navigation', () => {
  beforeEach(() => {
    currentUser.value = {
      id: 'user-1',
      email: 'user@example.com',
      user_metadata: {},
    }
    api.get.mockImplementation(async (path) => {
      if (path === '/api/v1/oauth/connections') {
        return { connections: [] }
      }
      return []
    })
  })

  it('links each Google service to an independent route with its official icon', async () => {
    renderLayout()

    const destinations = [
      ['Gmail', '/dashboard/gmail', 'Gmail route', 'gmail'],
      ['Google Calendar', '/dashboard/calendar', 'Calendar route', 'calendar'],
      ['Google Drive', '/dashboard/drive', 'Drive route', 'drive'],
      ['Google Contacts', '/dashboard/contacts', 'Contacts route', 'contacts'],
    ]

    for (const [label, href, routeText, icon] of destinations) {
      const link = screen.getByRole('link', { name: new RegExp(label) })
      expect(link).toHaveAttribute('href', href)
      expect(link.querySelector(`[data-testid="google-logo-${icon}"]`))
        .toBeInTheDocument()
      fireEvent.click(link)
      expect(await screen.findByText(routeText)).toBeInTheDocument()
    }
  }, 15_000)

  it('counts unique backend-granted services rather than Google accounts', async () => {
    api.get.mockImplementation(async (path) => {
      if (path === '/api/v1/oauth/connections') {
        return {
          connections: [
            {
              id: 'gmail-1',
              provider: 'google',
              status: 'connected',
              capabilities: { gmail: true },
            },
            {
              id: 'gmail-2',
              provider: 'google',
              status: 'connected',
              capabilities: { gmail: true },
            },
            {
              id: 'calendar',
              provider: 'google',
              status: 'connected',
              capabilities: { calendar_events_read: true },
            },
            {
              id: 'disconnected-drive',
              provider: 'google',
              status: 'disconnected',
              capabilities: { drive_files_read: true },
            },
          ],
        }
      }
      return []
    })

    renderLayout()

    const connectorsLink = screen.getByRole('link', {
      name: /Connectors/,
    })
    await waitFor(() => {
      expect(connectorsLink).toHaveTextContent('2')
    })
  })
})
