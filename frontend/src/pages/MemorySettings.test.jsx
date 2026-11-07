import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: {
      id: 'user-1',
      email: 'person@example.com',
      user_metadata: { full_name: 'Person' },
    },
    updateProfile: vi.fn(),
    updatePassword: vi.fn(),
  }),
}))

vi.mock('../services/memoryService', () => ({
  memoryService: {
    getMemories: vi.fn().mockResolvedValue([]),
    getMemory: vi.fn(),
    createMemory: vi.fn(),
    updateMemory: vi.fn(),
    deleteMemory: vi.fn(),
  },
}))

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
  },
  getApiAuthRevision: () => 0,
}))

import MemoryCenter from './MemoryCenter.jsx'
import Settings from './Settings.jsx'
import { api } from '../lib/api.js'

function renderPage(page, {
  isDark = false,
  initialMemoryEnabled = true,
  patchImplementation,
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  let persistedMemoryEnabled = initialMemoryEnabled
  api.get.mockImplementation((path) => {
    if (path === '/api/v1/me/settings') {
      if (persistedMemoryEnabled === null) return Promise.resolve({})
      return Promise.resolve({ memory_enabled: persistedMemoryEnabled })
    }
    if (path.startsWith('/api/v1/memory-reviews')) {
      return Promise.resolve([])
    }
    return Promise.resolve([])
  })
  api.patch.mockImplementation(async (path, payload) => {
    const updated = patchImplementation
      ? await patchImplementation(path, payload)
      : { memory_enabled: payload.memory_enabled }
    persistedMemoryEnabled = updated.memory_enabled
    return updated
  })

  function Layout() {
    return <Outlet context={{ isDark, setTheme: vi.fn(), surface: 'border-slate-200 bg-white', muted: 'text-slate-500', hoverSurface: 'hover:bg-slate-100' }} />
  }

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/settings" element={page} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('memory preference controls', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the long-term memory checkbox checked, accessible, and updates the existing setting', async () => {
    renderPage(<Settings />, { isDark: true, initialMemoryEnabled: true })

    const checkbox = await screen.findByRole('checkbox', {
      name: 'Enable long-term memory',
    })
    expect(checkbox).toBeChecked()
    expect(screen.getByText('Personal details may be saved to help future conversations.'))
      .toBeInTheDocument()
    expect(checkbox.nextElementSibling).toHaveClass('bg-emerald-700')

    fireEvent.click(checkbox)
    await waitFor(() => {
      expect(api.patch).toHaveBeenCalledWith(
        '/api/v1/me/settings',
        { memory_enabled: false },
      )
    })
    await waitFor(() => {
      expect(screen.getByRole('checkbox', {
        name: 'Enable long-term memory',
      })).not.toBeChecked()
    })
  })

  it('renders the checkbox unchecked in light mode and restores state after API failure', async () => {
    renderPage(<Settings />, {
      isDark: false,
      initialMemoryEnabled: false,
      patchImplementation: async () => {
        throw new Error('Settings update failed.')
      },
    })

    const checkbox = await screen.findByRole('checkbox', {
      name: 'Enable long-term memory',
    })
    expect(checkbox).not.toBeChecked()
    expect(checkbox.nextElementSibling).toHaveClass('bg-white')

    fireEvent.click(checkbox)
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Settings update failed.',
    )
    await waitFor(() => expect(checkbox).not.toBeChecked())
  })

  it('renders the automatic memory switch ON/OFF and persists switch interactions', async () => {
    renderPage(<MemoryCenter />, { initialMemoryEnabled: true })

    const toggle = await screen.findByRole('switch', { name: 'Automatic Memory' })
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: 'Automatic Memory' }))
        .toHaveAttribute('aria-checked', 'true')
    })
    expect(screen.getByText('ON')).toBeInTheDocument()
    expect(screen.getByText(
      'Allow the assistant to save new memories automatically. Existing memories remain available when this is disabled.',
    )).toBeInTheDocument()

    fireEvent.click(toggle)
    await waitFor(() => {
      expect(api.patch).toHaveBeenCalledWith(
        '/api/v1/me/settings',
        { memory_enabled: false },
        { userId: 'user-1', authRevision: 0 },
      )
    })
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: 'Automatic Memory' }))
        .toHaveAttribute('aria-checked', 'false')
      expect(screen.getByText('OFF')).toBeInTheDocument()
    })
  })

  it('defaults the Automatic Memory switch ON when the backend has no saved setting', async () => {
    renderPage(<MemoryCenter />, { initialMemoryEnabled: null })

    expect(await screen.findByRole('switch', { name: 'Automatic Memory' }))
      .toHaveAttribute('aria-checked', 'true')
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('rolls back a failed toggle and prevents duplicate updates while saving', async () => {
    let rejectUpdate
    renderPage(<MemoryCenter />, {
      isDark: true,
      initialMemoryEnabled: false,
      patchImplementation: () => new Promise((_resolve, reject) => {
        rejectUpdate = reject
      }),
    })

    const toggle = await screen.findByRole('switch', { name: 'Automatic Memory' })
    await waitFor(() => {
      const currentToggle = screen.getByRole('switch', { name: 'Automatic Memory' })
      expect(currentToggle).toHaveAttribute('aria-checked', 'false')
      expect(currentToggle).toBeEnabled()
    })
    fireEvent.click(toggle)
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1))
    const pendingToggle = screen.getByRole('switch', { name: 'Automatic Memory' })
    expect(pendingToggle).toBeDisabled()
    expect(pendingToggle).toHaveAttribute('aria-busy', 'true')

    fireEvent.click(pendingToggle)
    expect(api.patch).toHaveBeenCalledTimes(1)

    rejectUpdate(new Error('Could not save setting.'))
    expect(await screen.findByText('Could not save setting.')).toBeInTheDocument()
    await waitFor(() => {
      const currentToggle = screen.getByRole('switch', { name: 'Automatic Memory' })
      expect(currentToggle).toHaveAttribute('aria-checked', 'false')
      expect(currentToggle).not.toBeDisabled()
    })
  })

  it('supports keyboard focus and announces the switch state', async () => {
    renderPage(<MemoryCenter />, { isDark: false, initialMemoryEnabled: true })

    const toggle = await screen.findByRole('switch', { name: 'Automatic Memory' })
    await waitFor(() => expect(toggle).toBeEnabled())
    toggle.focus()
    expect(toggle).toHaveFocus()
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(toggle).toHaveAttribute('title', 'Automatic Memory ON')
  })
})
