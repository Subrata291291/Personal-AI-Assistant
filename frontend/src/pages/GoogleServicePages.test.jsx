import { render, screen } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import Contacts from './Contacts.jsx'
import Drive from './Drive.jsx'

function renderPage(path, element, connections = []) {
  return render(
    <MemoryRouter initialEntries={[path]}>
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
          <Route path="/dashboard/drive" element={element} />
          <Route path="/dashboard/contacts" element={element} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('Google Drive and Contacts placeholder pages', () => {
  it('shows Drive as coming soon without claiming file operations', () => {
    renderPage('/dashboard/drive', <Drive />)

    expect(screen.getByRole('heading', { name: 'Google Drive' }))
      .toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Google Drive logo' }))
      .toBeInTheDocument()
    expect(screen.getByText('Coming soon')).toBeInTheDocument()
    expect(screen.getByText(/OAuth connection is not available yet/))
      .toBeInTheDocument()
    expect(screen.getByText(/No files, folders, or contacts are being retrieved/))
      .toBeInTheDocument()
  })

  it('shows Contacts as coming soon and reports a real future capability separately', () => {
    renderPage('/dashboard/contacts', <Contacts />, [{
      id: 'contacts-account',
      provider: 'google',
      status: 'connected',
      capabilities: { contacts_read: true },
    }])

    expect(screen.getByRole('heading', { name: 'Google Contacts' }))
      .toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Google Contacts logo' }))
      .toBeInTheDocument()
    expect(screen.getByText('Coming soon')).toBeInTheDocument()
    expect(screen.getByText(/Google connection detected/))
      .toBeInTheDocument()
    expect(screen.getByText(/Planned, not available/)).toBeInTheDocument()
  })
})
