import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Outlet, Route, Routes, MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const updateUserMetadata = vi.hoisted(() => vi.fn())

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'user-1', user_metadata: {} },
    updateUserMetadata,
  }),
}))

vi.mock('../lib/api', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

import Connectors from './Connectors.jsx'
import { api } from '../lib/api'

function ConnectorsLayout() {
  return <Outlet context={{ isDark: true }} />
}

function renderConnectors() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard/connectors']}>
        <Routes>
          <Route element={<ConnectorsLayout />}>
            <Route path="/dashboard/connectors" element={<Connectors />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const gmailConnection = {
  id: 'connection-1',
  provider: 'google',
  status: 'connected',
  email: 'person@example.com',
  connected_at: '2026-11-04T10:00:00Z',
  capabilities: { gmail: true },
}

describe('Connected Apps', () => {
  beforeEach(() => {
    updateUserMetadata.mockReset()
    api.get.mockResolvedValue({ connections: [] })
    api.post.mockResolvedValue({
      authorization_url: 'https://accounts.google.com/oauth',
    })
    api.delete.mockResolvedValue({ message: 'disconnected' })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  it('removes the Assistant default Gmail account section', async () => {
    renderConnectors()

    await screen.findByRole('switch', { name: 'Gmail connection' })
    expect(screen.queryByText('Assistant default Gmail account'))
      .not.toBeInTheDocument()
    expect(screen.queryByLabelText('Default Gmail account for Assistant'))
      .not.toBeInTheDocument()
  })

  it('renders recognizable Google product logos for all service cards', async () => {
    renderConnectors()

    await screen.findByRole('switch', { name: 'Gmail connection' })
    for (const service of ['gmail', 'calendar', 'drive', 'contacts']) {
      expect(screen.getByRole('img', { name: new RegExp(`${service === 'gmail' ? 'Gmail' : service} logo`, 'i') }))
        .toBeInTheDocument()
    }
  })

  it('starts the existing Google OAuth flow when Gmail is turned on', async () => {
    renderConnectors()

    await waitFor(() => expect(api.get).toHaveBeenCalledWith(
      '/api/v1/oauth/connections',
    ))
    fireEvent.click(await screen.findByRole('switch', {
      name: 'Gmail connection',
    }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/v1/oauth/google/connect')
    })
  })

  it('disconnects Gmail through the existing API and updates from returned connection state', async () => {
    let connected = true
    api.get.mockImplementation(async () => ({
      connections: connected ? [gmailConnection] : [],
    }))
    api.delete.mockImplementation(async () => {
      connected = false
      return { message: 'disconnected' }
    })
    renderConnectors()

    await screen.findByText('Connected', { selector: 'p' })
    fireEvent.click(screen.getByRole('switch', {
      name: 'Gmail connection',
    }))

    await waitFor(() => {
      expect(api.delete).toHaveBeenCalledWith(
        '/api/v1/oauth/connections/connection-1',
      )
    })
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: 'Gmail connection' }))
        .toHaveAttribute('aria-checked', 'false')
    })
  })

  it('reflects actual Gmail scope capability rather than a generic Google connection', async () => {
    api.get.mockResolvedValue({
      connections: [{
        ...gmailConnection,
        capabilities: { gmail: false },
      }],
    })
    renderConnectors()

    await screen.findByText('Gmail access is not enabled for this account.')
    const toggle = screen.getByRole('switch', {
      name: 'Gmail connection',
    })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
  })

  it('keeps Calendar, Drive, and Contacts disabled and coming soon', async () => {
    renderConnectors()

    await screen.findByRole('switch', { name: 'Gmail connection' })
    for (const service of ['Google Calendar', 'Google Drive', 'Google Contacts']) {
      expect(screen.getByRole('switch', {
        name: `${service} connection`,
      })).toBeDisabled()
    }
    expect(screen.getAllByText('Coming soon')).toHaveLength(3)
  })

  it('does not turn Gmail on after a failed connection request', async () => {
    api.post.mockRejectedValue(new Error('Connection request failed'))
    renderConnectors()

    await waitFor(() => expect(api.get).toHaveBeenCalled())
    const toggle = await screen.findByRole('switch', {
      name: 'Gmail connection',
    })
    fireEvent.click(toggle)

    await waitFor(() => {
      expect(screen.getByText('Connection request failed')).toBeInTheDocument()
    })
    expect(screen.getByRole('switch', { name: 'Gmail connection' }))
      .toHaveAttribute('aria-checked', 'false')
  })

  it('does not turn Gmail off after a failed disconnect request', async () => {
    api.get.mockResolvedValue({ connections: [gmailConnection] })
    api.delete.mockRejectedValue(new Error('Disconnect request failed'))
    renderConnectors()

    await screen.findByText('Connected', { selector: 'p' })
    fireEvent.click(await screen.findByRole('switch', {
      name: 'Gmail connection',
    }))

    await waitFor(() => {
      expect(screen.getByText('Disconnect request failed')).toBeInTheDocument()
    })
    expect(screen.getByRole('switch', { name: 'Gmail connection' }))
      .toHaveAttribute('aria-checked', 'true')
  })
})
