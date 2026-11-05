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

const googleConnection = {
  id: 'connection-1',
  provider: 'google',
  status: 'connected',
  email: 'person@example.com',
  connected_at: '2026-11-04T10:00:00Z',
  capabilities: { gmail: true, calendar: false },
}

const noCalendarCapabilities = {
  connected: false,
  events_read: false,
  freebusy: false,
  api_available: false,
  api_complete: false,
  chat_available: false,
  google_connection_available: false,
  permissions_need_consent: false,
  write_available: false,
}

function connectedResponse(connections, calendarCapabilities) {
  return {
    connections,
    calendar_capabilities: calendarCapabilities || noCalendarCapabilities,
  }
}

describe('Connected Apps', () => {
  beforeEach(() => {
    updateUserMetadata.mockReset()
    api.get.mockResolvedValue(connectedResponse([]))
    api.post.mockResolvedValue({
      authorization_url: 'https://accounts.google.com/oauth',
    })
    api.delete.mockResolvedValue({ message: 'disconnected' })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  it('renders recognizable Google product logos for all service cards', async () => {
    renderConnectors()

    await screen.findByRole('button', { name: 'Connect Gmail' })
    for (const service of ['Gmail', 'calendar', 'drive', 'contacts']) {
      expect(screen.getByRole('img', { name: new RegExp(`${service} logo`, 'i') }))
        .toBeInTheDocument()
    }
  })

  it('requests Gmail scopes independently', async () => {
    renderConnectors()

    const connectButton = await screen.findByRole('button', {
      name: 'Connect Gmail',
    })
    await waitFor(() => expect(connectButton).toBeEnabled())
    fireEvent.click(connectButton)

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/v1/oauth/google/connect?service=gmail',
      )
    })
  })

  it('requests Calendar scopes independently', async () => {
    renderConnectors()

    const connectButton = await screen.findByRole('button', {
      name: 'Connect Google Calendar',
    })
    await waitFor(() => expect(connectButton).toBeEnabled())
    fireEvent.click(connectButton)

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/v1/oauth/google/connect?service=calendar',
      )
    })
  })

  it('reports Calendar unconnected when its actual scope is absent', async () => {
    api.get.mockResolvedValue(connectedResponse(
      [googleConnection],
      {
        ...noCalendarCapabilities,
        google_connection_available: true,
        permissions_need_consent: true,
      },
    ))
    renderConnectors()

    await screen.findByText(/Calendar is not connected\./)
    expect(screen.getByText('Gmail: Connected · Google Calendar: Not connected'))
      .toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Grant missing Calendar permissions',
    }))
      .toBeEnabled()
  })

  it('lets existing partial Calendar grants request the missing scopes', async () => {
    api.get.mockResolvedValue(connectedResponse([{
        ...googleConnection,
        capabilities: {
          gmail: true,
          calendar: true,
          calendar_events_read: true,
          calendar_freebusy: false,
          calendar_api_available: true,
          calendar_api_complete: false,
          calendar_chat_available: false,
        },
      }], {
        connected: true,
        events_read: true,
        freebusy: false,
        api_available: true,
        api_complete: false,
        chat_available: true,
        google_connection_available: true,
        permissions_need_consent: true,
        write_available: false,
      }))
    renderConnectors()

    expect(await screen.findByText('Permissions incomplete')).toBeInTheDocument()
    expect(screen.getByText(
      'Calendar event-read: available · FreeBusy: not granted · Calendar chat: available · Calendar writes: not available.',
    )).toBeInTheDocument()
    const grantButton = screen.getByRole('button', {
      name: 'Grant missing Calendar permissions',
    })
    expect(grantButton).toBeEnabled()
    fireEvent.click(grantButton)

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/v1/oauth/google/connect?service=calendar',
      )
    })
  })

  it('reports both independently granted services on one shared account', async () => {
    api.get.mockResolvedValue(connectedResponse([{
        ...googleConnection,
        capabilities: {
          gmail: true,
          calendar: true,
          calendar_events_read: true,
          calendar_freebusy: true,
          calendar_api_available: true,
          calendar_api_complete: true,
          calendar_chat_available: false,
        },
      }], {
        connected: true,
        events_read: true,
        freebusy: true,
        api_available: true,
        api_complete: true,
        chat_available: true,
        google_connection_available: true,
        permissions_need_consent: false,
        write_available: false,
      }))
    renderConnectors()

    expect(await screen.findByRole('button', {
      name: 'Gmail connected',
    })).toBeDisabled()
    expect(screen.getByRole('button', {
      name: 'Google Calendar connected',
    })).toBeDisabled()
    expect(screen.getByText('Gmail: Connected · Google Calendar: Connected'))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /disconnect calendar/i }))
      .not.toBeInTheDocument()
  })

  it('uses backend aggregate Calendar grants across separate accounts', async () => {
    api.get.mockResolvedValue(connectedResponse([
      {
        ...googleConnection,
        id: 'events-account',
        email: 'events@example.com',
        capabilities: {
          gmail: false,
          calendar: true,
          calendar_events_read: true,
          calendar_freebusy: false,
          calendar_api_available: true,
          calendar_api_complete: false,
          calendar_chat_available: true,
          calendar_write_available: false,
        },
      },
      {
        ...googleConnection,
        id: 'availability-account',
        email: 'availability@example.com',
        capabilities: {
          gmail: false,
          calendar: true,
          calendar_events_read: false,
          calendar_freebusy: true,
          calendar_api_available: true,
          calendar_api_complete: false,
          calendar_chat_available: true,
          calendar_write_available: false,
        },
      },
    ], {
      connected: true,
      events_read: true,
      freebusy: true,
      api_available: true,
      api_complete: true,
      chat_available: true,
      google_connection_available: true,
      permissions_need_consent: false,
      write_available: false,
    }))
    renderConnectors()

    expect(await screen.findByRole('button', {
      name: 'Google Calendar connected',
    })).toBeDisabled()
    expect(screen.getByText(
      'Calendar event-read: available · FreeBusy: available · Calendar chat: available · Calendar writes: not available.',
    )).toBeInTheDocument()
    expect(screen.getByRole('option', {
      name: 'events@example.com — event access',
    })).toBeInTheDocument()
    expect(screen.getByRole('option', {
      name: 'availability@example.com — free/busy access',
    })).toBeInTheDocument()
  })

  it('confirms account-wide disconnection and preserves it when declined', async () => {
    const connected = {
      ...googleConnection,
      capabilities: {
        gmail: true,
        calendar: true,
        calendar_events_read: true,
        calendar_freebusy: true,
        calendar_api_available: true,
        calendar_api_complete: true,
        calendar_chat_available: false,
      },
    }
    api.get.mockResolvedValue(connectedResponse([connected], {
      connected: true,
      events_read: true,
      freebusy: true,
      api_available: true,
      api_complete: true,
      chat_available: true,
      google_connection_available: true,
      permissions_need_consent: false,
      write_available: false,
    }))
    window.confirm.mockReturnValue(false)
    renderConnectors()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Disconnect Google account',
    }))

    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining('disables Gmail and Google Calendar'),
    )
    expect(api.delete).not.toHaveBeenCalled()
    expect(screen.getByText('Gmail: Connected · Google Calendar: Connected'))
      .toBeInTheDocument()
  })

  it('deletes the shared connection only after confirming account-wide disconnect', async () => {
    let connected = true
    api.get.mockImplementation(async () => connectedResponse(connected ? [{
        ...googleConnection,
        capabilities: {
          gmail: true,
          calendar: true,
          calendar_events_read: true,
          calendar_freebusy: true,
          calendar_api_available: true,
          calendar_api_complete: true,
          calendar_chat_available: false,
        },
      }] : [], connected ? {
        connected: true,
        events_read: true,
        freebusy: true,
        api_available: true,
        api_complete: true,
        chat_available: true,
        google_connection_available: true,
        permissions_need_consent: false,
        write_available: false,
      } : noCalendarCapabilities))
    api.delete.mockImplementation(async () => {
      connected = false
      return { message: 'disconnected' }
    })
    renderConnectors()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Disconnect Google account',
    }))

    await waitFor(() => {
      expect(api.delete).toHaveBeenCalledWith(
        '/api/v1/oauth/connections/connection-1',
      )
    })
    await screen.findByText('Google account disconnected successfully.')
  })

  it('keeps Calendar and unrelated Google services distinct', async () => {
    renderConnectors()

    expect(await screen.findAllByText('Coming soon')).toHaveLength(2)
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Connect Google Calendar' }))
        .toBeEnabled()
    })
  })

  it('does not report a service connected after a failed authorization request', async () => {
    api.post.mockRejectedValue(new Error('Connection request failed'))
    renderConnectors()

    const connectButton = await screen.findByRole('button', {
      name: 'Connect Gmail',
    })
    await waitFor(() => expect(connectButton).toBeEnabled())
    fireEvent.click(connectButton)

    await screen.findByText('Connection request failed')
    expect(screen.getByRole('button', { name: 'Connect Gmail' }))
      .toBeInTheDocument()
  })

  it('does not hide a connected service after failed shared account disconnect', async () => {
    api.get.mockResolvedValue(connectedResponse([googleConnection]))
    api.delete.mockRejectedValue(new Error('Disconnect request failed'))
    renderConnectors()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Disconnect Google account',
    }))

    await screen.findByText('Disconnect request failed')
    expect(screen.getByText('Gmail: Connected · Google Calendar: Not connected'))
      .toBeInTheDocument()
  })

  it('updates the default Calendar account preference', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        ...googleConnection.capabilities,
        calendar: true,
        calendar_events_read: true,
      },
    }], {
      connected: true,
      events_read: true,
      freebusy: false,
      api_available: true,
      api_complete: false,
      chat_available: true,
      google_connection_available: true,
      permissions_need_consent: true,
      write_available: false,
    }))
    renderConnectors()

    fireEvent.change(await screen.findByLabelText('Default Calendar account'), {
      target: { value: 'connection-1' },
    })

    await waitFor(() => expect(updateUserMetadata).toHaveBeenCalledWith({
      default_calendar_connection_id: 'connection-1',
    }))
  })
})
