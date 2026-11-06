import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const currentUser = vi.hoisted(() => ({
  value: {
    id: 'user-a',
    email: 'a@example.test',
    user_metadata: {},
  },
}))
let connectionsContext = {
  connections: [],
  connectionsLoading: false,
  connectionsError: false,
}

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: currentUser.value }),
}))

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn(),
  },
}))

vi.mock('../services/conversationService.js', () => ({
  conversationService: {
    getConversations: vi.fn(),
  },
}))

import { api } from '../lib/api.js'
import { conversationService } from '../services/conversationService.js'
import Dashboard from './Dashboard.jsx'

function DashboardLayout() {
  return (
    <Outlet context={{
      isDark: true,
      muted: 'text-slate-400',
      surface: 'bg-slate-900',
      hoverSurface: 'hover:bg-slate-800',
      ...connectionsContext,
    }} />
  )
}

function renderDashboard() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
    },
  })

  const tree = () => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route element={<DashboardLayout />}>
            <Route path="/dashboard" element={<Dashboard />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
  const view = render(tree())

  return { ...view, queryClient, tree }
}

describe('Dashboard fresh-user states', () => {
  beforeEach(() => {
    currentUser.value = {
      id: 'user-a',
      email: 'a@example.test',
      user_metadata: {},
    }
    connectionsContext = {
      connections: [],
      connectionsLoading: false,
      connectionsError: false,
    }
    api.get.mockImplementation((path) => {
      if (path === '/api/v1/oauth/connections') {
        return Promise.resolve({
          connections: [],
          calendar_capabilities: {},
        })
      }
      return Promise.resolve([])
    })
    conversationService.getConversations.mockResolvedValue([])
  })

  it('resolves all independent dashboard widgets for a new empty user', async () => {
    renderDashboard()

    expect(await screen.findByText(/No conversations yet/))
      .toBeInTheDocument()
    expect(screen.getByText('0 / 4')).toBeInTheDocument()
    expect(screen.queryByText('Loading recent conversations...'))
      .not.toBeInTheDocument()
    expect(screen.queryByText('...')).not.toBeInTheDocument()
  })

  it('shows independent errors without leaving other widgets pending', async () => {
    api.get.mockImplementation((path) => {
      if (
        path === '/api/v1/oauth/connections' ||
        path.startsWith('/api/v1/memories?')
      ) {
        return Promise.reject(new Error('Temporary service error'))
      }
      return Promise.resolve([])
    })
    conversationService.getConversations.mockRejectedValue(
      new Error('Temporary conversation error'),
    )

    renderDashboard()

    expect(await screen.findByText('Unable to load recent conversations.'))
      .toBeInTheDocument()
    expect(screen.queryByText('Loading recent conversations...'))
      .not.toBeInTheDocument()
    expect(screen.queryByText('...')).not.toBeInTheDocument()
  })

  it('does not show user A conversations after switching to user B', async () => {
    conversationService.getConversations.mockImplementation(() =>
      Promise.resolve(
        currentUser.value.id === 'user-a'
          ? [{ id: 'conversation-a', title: 'User A conversation' }]
          : [],
      ),
    )
    const view = renderDashboard()

    expect(await screen.findByText('User A conversation'))
      .toBeInTheDocument()

    currentUser.value = {
      id: 'user-b',
      email: 'b@example.test',
      user_metadata: {},
    }
    view.rerender(view.tree())

    expect(await screen.findByText(/No conversations yet/))
      .toBeInTheDocument()
    expect(screen.queryByText('User A conversation'))
      .not.toBeInTheDocument()
  })

  it.each([
    ['Gmail and Calendar', [
      {
        id: 'gmail-calendar',
        provider: 'google',
        status: 'connected',
        capabilities: { gmail: true, calendar_events_read: true },
      },
    ], '2 / 4', ['Connected · Enabled', 'Connected · Enabled']],
    ['Gmail only', [
      {
        id: 'gmail',
        provider: 'google',
        status: 'connected',
        capabilities: { gmail: true },
      },
    ], '1 / 4', ['Connected · Enabled', 'Disconnected']],
    ['Calendar only', [
      {
        id: 'calendar',
        provider: 'google',
        status: 'connected',
        capabilities: { calendar_events_read: true },
      },
    ], '1 / 4', ['Disconnected', 'Connected · Enabled']],
    ['multiple Gmail accounts', [
      {
        id: 'gmail-one',
        provider: 'google',
        status: 'connected',
        capabilities: { gmail: true },
      },
      {
        id: 'gmail-two',
        provider: 'google',
        status: 'connected',
        capabilities: { gmail: true },
      },
    ], '1 / 4', ['Connected · Enabled', 'Disconnected']],
    ['disconnected Calendar', [
      {
        id: 'calendar',
        provider: 'google',
        status: 'disconnected',
        capabilities: { calendar_events_read: true },
      },
    ], '0 / 4', ['Disconnected', 'Disconnected']],
  ])('reflects backend capabilities for %s', async (_label, connections, count, statuses) => {
    connectionsContext = {
      connections,
      connectionsLoading: false,
      connectionsError: false,
    }

    renderDashboard()

    expect(await screen.findByText(count)).toBeInTheDocument()
    const gmailRow = screen.getByText('Gmail').parentElement.parentElement
    const calendarRow = screen.getByText('Google Calendar').parentElement.parentElement
    expect(within(gmailRow).getByText(statuses[0])).toBeInTheDocument()
    expect(within(calendarRow).getByText(statuses[1])).toBeInTheDocument()
    expect(screen.getByText('Google Drive')).toBeInTheDocument()
    expect(screen.getAllByText('Coming soon').length).toBe(2)
    expect(api.get).not.toHaveBeenCalledWith('/api/v1/oauth/connections')
  })

  it('shows an unavailable status when the shared capability query fails', async () => {
    connectionsContext = {
      connections: [],
      connectionsLoading: false,
      connectionsError: true,
    }

    renderDashboard()

    expect(await screen.findAllByText('Unavailable')).not.toHaveLength(0)
    expect(screen.queryByText('0 / 4')).not.toBeInTheDocument()
  })

  it('keeps connected count while showing an OAuth-connected disabled service', async () => {
    currentUser.value = {
      ...currentUser.value,
      user_metadata: { gmail_enabled: false },
    }
    connectionsContext = {
      connections: [{
        id: 'gmail',
        provider: 'google',
        status: 'connected',
        capabilities: { gmail: true },
      }],
      connectionsLoading: false,
      connectionsError: false,
    }

    renderDashboard()

    expect(await screen.findByText('1 / 4')).toBeInTheDocument()
    const gmailRow = screen.getByText('Gmail').parentElement.parentElement
    expect(within(gmailRow).getByText('Connected · Disabled'))
      .toBeInTheDocument()
  })
})
