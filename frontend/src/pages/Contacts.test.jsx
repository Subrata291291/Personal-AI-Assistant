import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const updateUserMetadata = vi.hoisted(() => vi.fn())
const user = vi.hoisted(() => ({
  current: { id: 'user-1', user_metadata: { default_contacts_connection_id: 'conn-1' } },
}))

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: user.current, updateUserMetadata }),
}))

vi.mock('../lib/api.js', () => ({
  api: { get: vi.fn() },
}))

vi.mock('../services/contactsService.js', () => ({
  contactsService: {
    listContacts: vi.fn(),
    getContact: vi.fn(),
  },
}))

import { api } from '../lib/api.js'
import { contactsService } from '../services/contactsService.js'
import Contacts from './Contacts.jsx'

function Layout() {
  return <Outlet context={{ isDark: true }} />
}

function renderContacts() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard/contacts']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/dashboard/contacts" element={<Contacts />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const baseContact = {
  resource_name: 'people/c1',
  etag: 'etag-1',
  display_name: 'Ada Lovelace',
  given_name: 'Ada',
  family_name: 'Lovelace',
  primary_email: 'ada@example.com',
  primary_phone: '+1 555-0100',
  organization: 'Analytical Engines',
  job_title: 'Mathematician',
  primary_address: { formatted: '1 Example St' },
  photo_url: null,
  has_photo: false,
}

function connectionsResponse(overrides = {}) {
  return {
    connections: [{
      id: 'conn-1',
      provider: 'google',
      status: 'connected',
      email: 'owner@example.com',
      capabilities: {
        contacts_read: true,
        contacts: true,
      },
    }],
    contacts_capabilities: {
      read: true,
      connected: true,
      read_available: true,
      chat_available: true,
      selection_required: false,
      permissions_need_consent: false,
      ...overrides,
    },
    calendar_capabilities: {},
    drive_capabilities: {},
  }
}

describe('Contacts page (read-only)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    user.current = { id: 'user-1', user_metadata: { default_contacts_connection_id: 'conn-1' } }
    updateUserMetadata.mockResolvedValue({ user_metadata: user.current.user_metadata })
    api.get.mockResolvedValue(connectionsResponse())
    contactsService.listContacts.mockResolvedValue({
      mode: 'list',
      selected_account_email: 'owner@example.com',
      contacts: [baseContact],
      next_page_token: null,
    })
    contactsService.getContact.mockResolvedValue({ contact: baseContact })
  })

  it('renders contact list and detail without edit/delete actions', async () => {
    renderContacts()

    expect(await screen.findByText('Ada Lovelace')).toBeInTheDocument()
    expect(screen.getByLabelText('Ada Lovelace avatar')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new contact/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('Ada Lovelace'))

    expect(await screen.findByText('Analytical Engines')).toBeInTheDocument()
    expect(screen.getByText('Mathematician')).toBeInTheDocument()
    expect(screen.getByText('1 Example St')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /create update approval/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /create approval proposal/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/Contacts update is currently disabled/i)).not.toBeInTheDocument()
  })

  it('supports searching contacts', async () => {
    renderContacts()
    await screen.findByText('Ada Lovelace')

    fireEvent.change(screen.getByLabelText(/search contacts/i), { target: { value: 'Ada' } })
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }))

    await waitFor(() => {
      expect(contactsService.listContacts).toHaveBeenCalled()
    })
    const lastCall = contactsService.listContacts.mock.calls.at(-1)[0]
    expect(lastCall.query).toBe('Ada')
  })

  it('prompts for explicit account selection when Contacts selection is required', async () => {
    user.current = { id: 'user-1', user_metadata: {} }
    api.get.mockResolvedValue({
      connections: [
        { id: 'conn-1', provider: 'google', status: 'connected', email: 'one@example.com', capabilities: { contacts_read: true, contacts: true } },
        { id: 'conn-2', provider: 'google', status: 'connected', email: 'two@example.com', capabilities: { contacts_read: true, contacts: true } },
      ],
      contacts_capabilities: {
        read: true,
        connected: true,
        read_available: false,
        chat_available: false,
        selection_required: true,
      },
      calendar_capabilities: {},
      drive_capabilities: {},
    })

    renderContacts()

    expect(await screen.findByText('Select a Contacts account')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'two@example.com' }))
    await waitFor(() => expect(updateUserMetadata).toHaveBeenCalledWith({ default_contacts_connection_id: 'conn-2' }))
  })

  it('shows permission/connect state without mutation controls', async () => {
    contactsService.listContacts.mockRejectedValue(Object.assign(new Error('Google Contacts read permission is not granted for the selected account.'), {
      status: 403,
      detail: 'Google Contacts read permission is not granted for the selected account.',
    }))

    renderContacts()

    expect(await screen.findByText(/Google Contacts read permission/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Review Google Contacts permissions/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new contact/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /create approval proposal/i })).not.toBeInTheDocument()
  })
})
