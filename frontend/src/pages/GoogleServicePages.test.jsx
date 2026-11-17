import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import Contacts from './Contacts.jsx'
import Drive from './Drive.jsx'

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'user-1', user_metadata: { default_contacts_connection_id: 'conn-1' } },
    updateUserMetadata: vi.fn(),
  }),
}))

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn().mockResolvedValue({
      connections: [],
      contacts_capabilities: { read: false, connected: false },
      calendar_capabilities: {},
      drive_capabilities: {},
    }),
  },
}))

vi.mock('../services/contactsService.js', () => ({
  contactsService: {
    listContacts: vi.fn().mockResolvedValue({ contacts: [] }),
    getContact: vi.fn(),
    createProposal: vi.fn(),
  },
}))

vi.mock('../services/driveContractsService.js', () => ({
  driveContractsService: {
    getPickerConfig: vi.fn(),
    listSelectedFiles: vi.fn(),
    createPickerToken: vi.fn(),
    registerSelectedFile: vi.fn(),
    removeSelectedFile: vi.fn(),
  },
}))

vi.mock('../services/actionApprovalService.js', () => ({
  actionApprovalService: {
    approve: vi.fn(),
    reject: vi.fn(),
    execute: vi.fn(),
    getProposal: vi.fn(),
  },
}))

function renderDrive(connections = []) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/dashboard/drive']}>
        <Routes>
          <Route
            element={(
              <Outlet context={{
                isDark: true,
                connections,
                connectionsLoading: false,
                connectionsError: false,
              }} />
            )}
          >
            <Route path="/dashboard/drive" element={<Drive />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function renderContacts() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/dashboard/contacts']}>
        <Routes>
          <Route element={<Outlet context={{ isDark: true }} />}>
            <Route path="/dashboard/contacts" element={<Contacts />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Google Drive and Contacts pages', () => {
  it('renders Drive as temporarily unavailable coming soon without contract actions', async () => {
    renderDrive()

    expect(screen.getByRole('heading', { name: 'Google Drive' }))
      .toBeInTheDocument()
    expect(
      screen.getAllByText(/temporarily unavailable/i).length,
    ).toBeGreaterThan(0)
    expect(screen.getByText(/available in a future update/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Select contracts' }))
      .not.toBeInTheDocument()
    expect(screen.queryByText(/Grant Drive write access/)).not.toBeInTheDocument()
  })

  it('renders the Contacts page', async () => {
    renderContacts()
    expect(await screen.findByRole('heading', { name: 'Contacts' }))
      .toBeInTheDocument()
  })
})
