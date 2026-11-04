import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  configure,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 'user-1' } }),
}))

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

vi.mock('../services/gmailService.js', () => ({
  gmailService: {
    getMessages: vi.fn(),
    getMessage: vi.fn(),
    trashMessage: vi.fn(),
    restoreMessage: vi.fn(),
    archiveMessage: vi.fn(),
    permanentlyDeleteMessage: vi.fn(),
    sendMessage: vi.fn(),
    replyMessage: vi.fn(),
    replyAllMessage: vi.fn(),
    forwardMessage: vi.fn(),
  },
}))

import Gmail from './Gmail.jsx'
import { api } from '../lib/api.js'
import { gmailService } from '../services/gmailService.js'

configure({ asyncUtilTimeout: 5000 })

const message = {
  id: 'message-123',
  subject: 'Quarterly update',
  sender: 'sender@example.com',
  snippet: 'Quarterly details',
  date: '2026-11-04T10:00:00Z',
  labels: ['INBOX', 'UNREAD'],
}

function GmailLayout() {
  return <Outlet context={{ isDark: false }} />
}

function renderGmail() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/gmail']}>
        <Routes>
          <Route element={<GmailLayout />}>
            <Route path="/gmail" element={<Gmail />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Gmail archive flow', () => {
  let archived

  beforeEach(() => {
    archived = false
    sessionStorage.clear()
    api.get.mockResolvedValue({
      connections: [{
        id: 'connection-1',
        provider: 'google',
        status: 'connected',
        email: 'user@example.com',
      }],
    })
    api.post.mockResolvedValue({ authorization_url: 'https://accounts.google.com/' })
    gmailService.getMessages.mockImplementation(({ folder }) => ({
      messages: folder === 'inbox' && archived
        ? []
        : [{ ...message, labels: archived ? ['UNREAD'] : message.labels }],
      next_page_token: null,
    }))
    gmailService.getMessage.mockResolvedValue({ message })
    gmailService.archiveMessage.mockImplementation(async () => {
      archived = true
      return { id: message.id, label_ids: ['UNREAD'] }
    })
  })

  it('uses the Archive icon and neutral purple styling in its confirmation', async () => {
    renderGmail()

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Archive email: Quarterly update',
      }),
    )

    expect(await screen.findByText('Archive email?')).toBeInTheDocument()
    expect(screen.getByTestId('archive-confirmation-icon'))
      .toHaveClass('bg-violet-50', 'text-violet-700')
    expect(screen.getByTestId('archive-confirmation-icon').querySelector('svg path'))
      .toHaveAttribute('d', expect.stringContaining('M4 4h16'))
  })

  it('removes archived email from Inbox, keeps it in All Mail, and reports success', async () => {
    renderGmail()

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Archive email: Quarterly update',
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))

    expect(await screen.findByRole('status')).toHaveTextContent('Email archived.')
    await waitFor(() => {
      expect(screen.queryByText('Quarterly update')).not.toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: /All Mail/ }))
    expect(await screen.findByText('Quarterly update')).toBeInTheDocument()
  })

  it('keeps the message in Inbox and shows an error when archive fails', async () => {
    gmailService.archiveMessage.mockRejectedValue(
      new Error('Gmail API request failed.'),
    )
    renderGmail()

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Archive email: Quarterly update',
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))

    const alerts = await screen.findAllByRole('alert')
    expect(alerts.every((alert) =>
      alert.textContent.includes('Gmail API request failed.')
    )).toBe(true)
    expect(screen.getAllByText('Quarterly update')).not.toHaveLength(0)
  })

  it('keeps Trash confirmation icon and action button destructive', async () => {
    renderGmail()

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Move email to Trash: Quarterly update',
      }),
    )

    expect(await screen.findByText('Move to Trash?')).toBeInTheDocument()
    expect(screen.getByTestId('destructive-confirmation-icon'))
      .toHaveClass('bg-rose-50', 'text-rose-600')
    expect(screen.getByRole('button', { name: 'Move to Trash' }))
      .toHaveClass('bg-rose-600')
  })

  it('keeps permanent Delete confirmation styling destructive', async () => {
    renderGmail()

    fireEvent.click(
      within(screen.getByRole('navigation', { name: 'Gmail folders' }))
        .getByRole('button', { name: /Trash/ }),
    )
    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Delete permanently: Quarterly update',
      }),
    )

    expect(await screen.findByText('Delete permanently?')).toBeInTheDocument()
    expect(screen.getByTestId('destructive-confirmation-icon'))
      .toHaveClass('bg-rose-50', 'text-rose-600')
    expect(screen.getByRole('button', { name: 'Delete permanently' }))
      .toHaveClass('bg-rose-600')
  })
})
