import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const currentUser = vi.hoisted(() => ({
  value: {
    id: 'user-a',
    email: 'a@example.test',
    user_metadata: {},
  },
}))

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

  it('shows malformed collection responses as errors instead of empty states', async () => {
    api.get.mockImplementation((path) => {
      if (path === '/api/v1/oauth/connections') {
        return Promise.resolve({ connections: null })
      }
      if (path.startsWith('/api/v1/memories?')) {
        return Promise.resolve({ memories: [] })
      }
      if (path.startsWith('/api/v1/memory-reviews?')) {
        return Promise.resolve({ reviews: [] })
      }
      return Promise.resolve([])
    })

    renderDashboard()

    expect(await screen.findByText('Unavailable')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
    expect(screen.queryByText('0 / 4')).not.toBeInTheDocument()
  })
})
