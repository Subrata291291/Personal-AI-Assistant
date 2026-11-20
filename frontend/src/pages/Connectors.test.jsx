import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { Outlet, Route, Routes, MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const updateUserMetadata = vi.hoisted(() => vi.fn())
const userMetadata = vi.hoisted(() => ({ current: {} }))

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'user-1', user_metadata: userMetadata.current },
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
import { getConnectedGoogleServiceCount } from '../lib/googleServiceCapabilities.js'

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

  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/dashboard/connectors']}>
          <Routes>
            <Route element={<ConnectorsLayout />}>
              <Route path="/dashboard/connectors" element={<Connectors />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  }
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

function connectedResponse(connections, calendarCapabilities, extra = {}) {
  return {
    connections,
    calendar_capabilities: calendarCapabilities || noCalendarCapabilities,
    ...extra,
  }
}

describe('Connected Apps', () => {
  beforeEach(() => {
    updateUserMetadata.mockReset()
    updateUserMetadata.mockImplementation(async (metadata) => {
      userMetadata.current = { ...userMetadata.current, ...metadata }
      return { user_metadata: userMetadata.current }
    })
    userMetadata.current = {}
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

  it('shows disconnected states for a fresh user with no Google account', async () => {
    renderConnectors()

    const connectButton = await screen.findByRole('button', {
      name: 'Connect Gmail',
    })
    await waitFor(() => expect(connectButton).toBeEnabled())
    await waitFor(() => {
      expect(screen.queryAllByText('Checking connection...'))
        .toHaveLength(0)
    })
    const services = screen
      .getByRole('heading', { name: 'Google Services' })
      .closest('section')
    const [gmailCard, calendarCard] = within(services).getAllByRole('article')
    expect(within(gmailCard).getByText('Disconnected'))
      .toBeInTheDocument()
    expect(await within(calendarCard).findByText('Disconnected'))
      .toBeInTheDocument()
  })

  it('allows retry after the connections request fails', async () => {
    api.get
      .mockRejectedValueOnce(new Error('API temporarily unavailable'))
      .mockResolvedValueOnce(connectedResponse([]))
    renderConnectors()

    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    await screen.findByRole('button', { name: 'Connect Gmail' })
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(screen.queryByText('Checking connection...'))
      .not.toBeInTheDocument()
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

  it('starts OAuth instead of enabling a disconnected service as connected', async () => {
    userMetadata.current = { gmail_enabled: false }
    renderConnectors()

    const gmailSwitch = await screen.findByRole('switch', { name: 'Enable Gmail' })
    // Google Drive is temporarily disabled/coming soon and is not shown as disconnected.
    expect(await screen.findAllByText('Disconnected')).toHaveLength(3)
    expect(screen.getByText('Under development')).toBeInTheDocument()
    fireEvent.click(gmailSwitch)

    await waitFor(() => {
    expect(updateUserMetadata).toHaveBeenCalledWith({ gmail_enabled: true })
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/oauth/google/connect?service=gmail',
    )
    })
    // Google Drive is temporarily disabled/coming soon and is not shown as disconnected.
    expect(screen.getAllByText('Disconnected')).toHaveLength(3)
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

  it('requests write consent for the exact connected Google account', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        ...googleConnection.capabilities,
        calendar: true,
        calendar_events_read: true,
        calendar_write_permission_granted: false,
        calendar_create_execution_available: false,
        calendar_update_execution_available: false,
        calendar_delete_execution_available: false,
      },
    }], {
      ...noCalendarCapabilities,
      connected: true,
      events_read: true,
      api_available: true,
      google_connection_available: true,
    }))
    api.post.mockResolvedValue({
      authorization_url: 'https://accounts.google.test/authorize',
    })
    renderConnectors()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Grant Calendar write access',
    }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        `/api/v1/oauth/google/connect?service=calendar_write&connection_id=${googleConnection.id}`,
      )
    })
  })

  it('requests Calendar List access for the exact connected Google account', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        ...googleConnection.capabilities,
        calendar: true,
        calendar_events_read: true,
        calendar_list_read: false,
      },
    }], {
      ...noCalendarCapabilities,
      connected: true,
      events_read: true,
      api_available: true,
      google_connection_available: true,
    }))
    api.post.mockResolvedValue({
      authorization_url: 'https://accounts.google.test/authorize',
    })
    renderConnectors()

    expect(await screen.findByText(/Calendar list permission is required/))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {
      name: 'Grant Calendar List Access',
    }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        `/api/v1/oauth/google/connect?service=calendar_list_read&connection_id=${googleConnection.id}`,
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

    const calendarCard = screen.getByRole('heading', {
      name: 'Google Calendar',
    }).closest('article')
    expect(await within(calendarCard).findByText('Disconnected'))
      .toBeInTheDocument()
    expect(within(calendarCard).getByText(
      'A Google account is connected, but Google Calendar permission has not been granted.',
    )).toBeInTheDocument()
    expect(screen.getByText('Gmail: Connected · Google Calendar: Not connected'))
      .toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Connect Google Calendar',
    }))
      .toBeEnabled()
  })

  it('keeps Calendar event access connected when optional FreeBusy is missing', async () => {
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

    const calendarCard = screen.getByRole('heading', {
      name: 'Google Calendar',
    }).closest('article')
    expect(await within(calendarCard).findByText('Connected · Enabled'))
      .toBeInTheDocument()
    expect(within(calendarCard).getByText(
      'Assistant Calendar context: Available',
    )).toBeInTheDocument()
    expect(within(calendarCard).getByText('Event-read: Granted'))
      .toBeInTheDocument()
    expect(within(calendarCard).getByText('FreeBusy: Not granted'))
      .toBeInTheDocument()
    expect(within(calendarCard).queryByText(/Calendar chat/))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: 'Grant missing Calendar permissions',
    })).not.toBeInTheDocument()
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

    expect(await screen.findByRole('switch', {
      name: 'Enable Gmail',
    })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('switch', {
      name: 'Enable Google Calendar',
    })).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByRole('button', { name: 'Gmail connected' }))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: 'Google Calendar connected',
    })).not.toBeInTheDocument()
    expect(await screen.findByText(
      'Gmail: Connected · Google Calendar: Connected',
    )).toBeInTheDocument()
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

    expect(await screen.findByRole('switch', {
      name: 'Enable Google Calendar',
    })).toHaveAttribute('aria-checked', 'true')
    const calendarCard = screen.getByRole('heading', {
      name: 'Google Calendar',
    }).closest('article')
    expect(await within(calendarCard).findByText(
      'Assistant Calendar context: Available',
    )).toBeInTheDocument()
    expect(within(calendarCard).getByText('Event-read: Granted'))
      .toBeInTheDocument()
    expect(within(calendarCard).getByText('FreeBusy: Granted'))
      .toBeInTheDocument()
    expect(screen.queryByLabelText('Default Calendar account'))
      .not.toBeInTheDocument()
  })

  it('distinguishes Calendar write permission, feature flags, and execution', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        ...googleConnection.capabilities,
        calendar: true,
        calendar_events_read: true,
        calendar_freebusy: true,
        calendar_api_available: true,
        calendar_write_permission_granted: true,
        calendar_create_feature_enabled: false,
        calendar_update_feature_enabled: false,
        calendar_delete_feature_enabled: false,
        calendar_create_execution_available: false,
        calendar_update_execution_available: false,
        calendar_delete_execution_available: false,
      },
    }], {
      ...noCalendarCapabilities,
      connected: true,
      events_read: true,
      freebusy: true,
      api_available: true,
      google_connection_available: true,
      write_permission_granted: true,
      write_available: false,
    }))
    renderConnectors()

    const calendarCard = screen.getByRole('heading', {
      name: 'Google Calendar',
    }).closest('article')
    expect(await within(calendarCard).findByText('Write permission: Granted'))
      .toBeInTheDocument()
    expect(within(calendarCard).getByText(
      'Access calendar events and availability. Write permission is granted, but event creation, updates and deletion are currently disabled.',
    )).toBeInTheDocument()
    expect(within(calendarCard).getByText(
      'Create: Disabled · Execution not available',
    )).toBeInTheDocument()
    expect(within(calendarCard).getByText(
      'Update: Disabled · Execution not available',
    )).toBeInTheDocument()
    expect(within(calendarCard).getByText(
      'Delete: Disabled · Execution not available',
    )).toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: 'Grant Calendar write access',
    })).not.toBeInTheDocument()
  })

  it('displays enabled Create and Update capabilities from the API response', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        ...googleConnection.capabilities,
        calendar: true,
        calendar_events_read: true,
        calendar_freebusy: true,
        calendar_api_available: true,
        calendar_write_permission_granted: true,
        calendar_create_feature_enabled: true,
        calendar_update_feature_enabled: true,
        calendar_delete_feature_enabled: false,
        calendar_create_execution_available: true,
        calendar_update_execution_available: true,
        calendar_delete_execution_available: false,
      },
    }], {
      ...noCalendarCapabilities,
      connected: true,
      events_read: true,
      freebusy: true,
      api_available: true,
      google_connection_available: true,
      write_permission_granted: true,
      create_feature_enabled: true,
      update_feature_enabled: true,
      delete_feature_enabled: false,
      create_execution_available: true,
      update_execution_available: true,
      delete_execution_available: false,
      write_available: true,
    }))
    renderConnectors()

    const calendarCard = screen.getByRole('heading', {
      name: 'Google Calendar',
    }).closest('article')
    expect(await within(calendarCard).findByText(
      'Create: Enabled · Execution available',
    )).toBeInTheDocument()
    expect(within(calendarCard).getByText(
      'Update: Enabled · Execution available',
    )).toBeInTheDocument()
    expect(within(calendarCard).getByText(
      'Delete: Disabled · Execution not available',
    )).toBeInTheDocument()
  })

  it('omits the connected-account line from the Gmail card but keeps permissions and account management', async () => {
    api.get.mockResolvedValue(connectedResponse([googleConnection]))
    renderConnectors()

    const gmailHeading = await screen.findByRole('heading', { name: 'Gmail' })
    const gmailCard = gmailHeading.closest('article')
    expect(gmailCard).toBeTruthy()
    expect(await within(gmailCard).findByText('Connected · Enabled'))
      .toBeInTheDocument()
    expect(within(gmailCard).queryByText(/Connected account:/i))
      .not.toBeInTheDocument()
    expect(within(gmailCard).queryByText('person@example.com'))
      .not.toBeInTheDocument()

    // Permission and capability information must stay on the card.
    expect(within(gmailCard).getByText('Read permission: Granted'))
      .toBeInTheDocument()
    expect(within(gmailCard).getByText('Email search: Available'))
      .toBeInTheDocument()

    // Account data remains available in the account-management section.
    expect(screen.getByText('person@example.com')).toBeInTheDocument()
  })

  it('does not offer write consent when the backend cannot verify permission status', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        ...googleConnection.capabilities,
        calendar: true,
        calendar_events_read: true,
      },
    }], {
      ...noCalendarCapabilities,
      connected: true,
      events_read: true,
      api_available: true,
      google_connection_available: true,
    }))
    renderConnectors()

    const accountMatches = await screen.findAllByText('person@example.com')
    const accountCard = accountMatches
      .map((element) => element.closest('article'))
      .find((article) =>
        article && within(article).queryByText(/Gmail: Connected/),
      )
    expect(accountCard).toBeTruthy()
    expect(within(accountCard).getByText(/Write permission: Not available/))
      .toBeInTheDocument()
    expect(within(accountCard).queryByRole('button', {
      name: 'Grant Calendar write access',
    })).not.toBeInTheDocument()
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
    const { queryClient } = renderConnectors()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Disconnect Google account',
    }))

    await waitFor(() => {
      expect(api.delete).toHaveBeenCalledWith(
        '/api/v1/oauth/connections/connection-1',
      )
    })
    const notice = await screen.findByText('Google account disconnected successfully.')
    expect(notice.closest('[role="status"]').className).toContain('bg-emerald-50')
    expect(notice.closest('[role="status"]').className).toContain('text-emerald-700')
    expect(await screen.findByRole('button', { name: 'Connect Gmail' }))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', {
      name: 'Disconnect Google account',
    })).not.toBeInTheDocument()
    expect(getConnectedGoogleServiceCount(
      queryClient.getQueryData(['oauth-connections', 'user-1']).connections,
    )).toBe(0)
  })

  it('shows Google Drive as coming soon/unavailable while Contacts remains available', async () => {
    renderConnectors()

    expect(await screen.findByRole('heading', { name: 'Google Drive' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Connect Google Drive' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Grant missing Google Drive permissions' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Grant Drive file access' })).not.toBeInTheDocument()
    const contactsConnectButton = await screen.findByRole('button', { name: 'Connect Google Contacts' })
    await waitFor(() => expect(contactsConnectButton).toBeEnabled())
    const driveCard = screen.getByRole('heading', { name: 'Google Drive' }).closest('article')
    expect(within(driveCard).getByText(/Coming soon/i)).toBeInTheDocument()
    expect(within(driveCard).getByText(/available in a future update/i)).toBeInTheDocument()
    expect(within(driveCard).getByText('Under development')).toBeInTheDocument()
    expect(within(driveCard).getByText('Unavailable')).toBeInTheDocument()
    const driveToggle = within(driveCard).getByRole('switch', { name: 'Enable Google Drive' })
    expect(driveToggle).toBeDisabled()
    expect(driveToggle).toHaveAttribute('aria-checked', 'false')
  })

  it('does not display Drive as connected or operational even if backend reports grants', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        gmail: false,
        calendar: false,
        drive: true,
        drive_contracts_read: true,
      },
    }], noCalendarCapabilities, {
      drive_capabilities: {
        connected: true,
        read: true,
        contracts_read: true,
        google_connection_available: true,
        permissions_need_consent: false,
      },
    }))
    renderConnectors()

    const driveCard = screen.getByRole('heading', { name: 'Google Drive' }).closest('article')
    expect(await within(driveCard).findByText('Under development')).toBeInTheDocument()
    expect(within(driveCard).getByText('Unavailable')).toBeInTheDocument()
    expect(within(driveCard).queryByText('Connected · Enabled')).not.toBeInTheDocument()
    expect(within(driveCard).queryByText('Permissions incomplete')).not.toBeInTheDocument()
    expect(within(driveCard).queryByText('Selected-file read access: Granted')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Grant Drive file access' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Google Drive: Connected/)).not.toBeInTheDocument()
  })

  it('keeps Contacts permission status accurate while Drive remains coming soon', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: { gmail: true, calendar: false },
    }], noCalendarCapabilities, {
      contacts_capabilities: {
        connected: false,
        read: false,
        write_permission_granted: false,
      },
    }))
    renderConnectors()

    const driveCard = screen.getByRole('heading', { name: 'Google Drive' }).closest('article')
    const contactsCard = screen.getByRole('heading', { name: 'Google Contacts' }).closest('article')
    expect(await within(driveCard).findByText('Under development')).toBeInTheDocument()
    expect(within(driveCard).getByText('Unavailable')).toBeInTheDocument()
    expect(within(driveCard).queryByText('Permissions incomplete')).not.toBeInTheDocument()
    expect(within(driveCard).queryByText('Selected-file read access: Not granted')).not.toBeInTheDocument()
    expect(await within(contactsCard).findByText('Permissions incomplete')).toBeInTheDocument()
    expect(within(contactsCard).getByText('Read permission: Not granted')).toBeInTheDocument()
    expect(within(contactsCard).queryByText('Connected · Enabled')).not.toBeInTheDocument()
  })

  it('requires an explicit Contacts account when several accounts grant access', async () => {
    api.get.mockResolvedValue(connectedResponse([
      {
        ...googleConnection,
        id: 'contacts-one',
        email: 'one@example.com',
        capabilities: { gmail: false, calendar: false, contacts_read: true },
      },
      {
        ...googleConnection,
        id: 'contacts-two',
        email: 'two@example.com',
        capabilities: { gmail: false, calendar: false, contacts_read: true },
      },
    ], noCalendarCapabilities, {
      contacts_capabilities: {
        connected: true,
        read: true,
        write_permission_granted: false,
        google_connection_available: true,
        permissions_need_consent: false,
      },
    }))
    renderConnectors()

    const selector = await screen.findByLabelText('Contacts account')
    expect(within(selector).getByRole('option', { name: 'one@example.com' }))
      .toBeInTheDocument()
    fireEvent.change(selector, { target: { value: 'contacts-two' } })

    await waitFor(() => {
      expect(updateUserMetadata).toHaveBeenCalledWith({
        default_contacts_connection_id: 'contacts-two',
      })
    })
  })

  it('turns Contacts off without disconnecting the shared Google account', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: { gmail: false, calendar: false, contacts_read: true },
    }], noCalendarCapabilities, {
      contacts_capabilities: {
        connected: true,
        read: true,
        write_permission_granted: false,
        google_connection_available: true,
        permissions_need_consent: false,
      },
    }))
    renderConnectors()

    const contactsSwitch = await screen.findByRole('switch', {
      name: 'Enable Google Contacts',
    })
    await waitFor(() => expect(contactsSwitch).toBeEnabled())
    fireEvent.click(contactsSwitch)

    await waitFor(() => {
      expect(updateUserMetadata).toHaveBeenCalledWith({
        contacts_enabled: false,
      })
    })
    expect(api.delete).not.toHaveBeenCalled()
    expect(await screen.findByText('Connected · Disabled'))
      .toBeInTheDocument()
  })

  it('requests Contacts read consent for the exact connected account when read is not granted', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        gmail: false,
        calendar: false,
        contacts_read: false,
        contacts_write_permission_granted: false,
      },
    }], noCalendarCapabilities, {
      contacts_capabilities: {
        connected: false,
        read: false,
        write_permission_granted: false,
        google_connection_available: true,
        permissions_need_consent: true,
      },
    }))
    renderConnectors()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Grant Contacts read access',
    }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        `/api/v1/oauth/google/connect?service=contacts_read&connection_id=${googleConnection.id}`,
      )
    })
  })

  it('does not offer Drive file consent actions while Drive is coming soon', async () => {
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        gmail: false,
        calendar: false,
        drive: true,
        drive_contracts_read: false,
      },
    }], noCalendarCapabilities, {
      drive_capabilities: {
        connected: true,
        read: false,
        contracts_read: false,
        google_connection_available: true,
        permissions_need_consent: true,
      },
    }))
    renderConnectors()

    expect(await screen.findByRole('heading', { name: 'Google Drive' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Grant Drive file access' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Grant missing Google Drive permissions' })).not.toBeInTheDocument()
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

  it('shows working service switches separately from OAuth account status', async () => {
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

    const gmailSwitch = await screen.findByRole('switch', {
      name: 'Enable Gmail',
    })
    const calendarSwitch = screen.getByRole('switch', {
      name: 'Enable Google Calendar',
    })
    await waitFor(() => {
      expect(gmailSwitch).toBeEnabled()
      expect(calendarSwitch).toBeEnabled()
    })
    expect(gmailSwitch).toHaveAttribute('aria-checked', 'true')
    expect(calendarSwitch).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByText('Default Calendar account')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Default Calendar account')).not.toBeInTheDocument()

    fireEvent.click(gmailSwitch)

    await waitFor(() => {
      expect(updateUserMetadata).toHaveBeenCalledWith({
        gmail_enabled: false,
      })
    })
    expect(api.delete).not.toHaveBeenCalled()
    expect(await screen.findByText('Connected · Disabled'))
      .toBeInTheDocument()
    expect(screen.getByText('Gmail: Connected · Google Calendar: Connected'))
      .toBeInTheDocument()
  })

  it('persists Calendar OFF without disconnecting the Google account', async () => {
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

    const calendarSwitch = await screen.findByRole('switch', {
      name: 'Enable Google Calendar',
    })
    await waitFor(() => expect(calendarSwitch).toBeEnabled())
    fireEvent.click(calendarSwitch)

    await waitFor(() => {
      expect(updateUserMetadata).toHaveBeenCalledWith({
        calendar_enabled: false,
      })
    })
    expect(api.delete).not.toHaveBeenCalled()
    expect(await screen.findByText('Connected · Disabled'))
      .toBeInTheDocument()
    expect(screen.getByRole('button', {
      name: 'Disconnect Google account',
    })).toBeInTheDocument()
  })

  it('restores persisted OFF states instead of inferring them from OAuth status', async () => {
    userMetadata.current = {
      gmail_enabled: false,
      calendar_enabled: false,
    }
    api.get.mockResolvedValue(connectedResponse([{
      ...googleConnection,
      capabilities: {
        ...googleConnection.capabilities,
        gmail: true,
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

    const gmailSwitch = await screen.findByRole('switch', {
      name: 'Enable Gmail',
    })
    const calendarSwitch = screen.getByRole('switch', {
      name: 'Enable Google Calendar',
    })
    await waitFor(() => {
      expect(gmailSwitch).toBeEnabled()
      expect(calendarSwitch).toBeEnabled()
    })
    expect(gmailSwitch)
      .toHaveAttribute('aria-checked', 'false')
    expect(calendarSwitch).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText('Gmail: Connected · Google Calendar: Connected'))
      .toBeInTheDocument()
  })
})
