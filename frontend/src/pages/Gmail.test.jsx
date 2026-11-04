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

  const view = render(
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
  return { ...view, queryClient }
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

  it('uses internalDate for row timestamps and sorts newest received first', async () => {
    const newest = {
      ...message,
      id: 'newest-message',
      subject: 'Newest received',
      date: 'Mon, 01 Jan 2001 00:00:00 +0000',
      internal_date: String(new Date('2026-11-04T19:20:00Z').getTime()),
    }
    const older = {
      ...message,
      id: 'older-message',
      subject: 'Older received',
      date: 'Wed, 01 Jan 2099 00:00:00 +0000',
      internal_date: String(new Date('2026-11-02T19:20:00Z').getTime()),
    }
    gmailService.getMessages.mockImplementation(() => ({
      messages: [older, newest],
      next_page_token: null,
    }))

    renderGmail()

    const newestRow = await screen.findByRole('button', {
      name: /Open email: Newest received/,
    })
    const olderRow = screen.getByRole('button', {
      name: /Open email: Older received/,
    })
    expect(newestRow.compareDocumentPosition(olderRow) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    const received = new Date(Number(newest.internal_date))
    expect(screen.getByTitle(received.toLocaleString(undefined, {
      month: 'long',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    }))).toBeInTheDocument()
  })

  it('renders the shared sender avatar in every Gmail folder and search results', async () => {
    const folders = [
      'Inbox',
      'Starred',
      'Important',
      'Sent',
      'Drafts',
      'All Mail',
      'Spam',
      'Trash',
    ]
    renderGmail()

    for (const folder of folders) {
      if (folder !== 'Inbox') {
        fireEvent.click(
          within(screen.getByRole('navigation', { name: 'Gmail folders' }))
            .getByRole('button', { name: new RegExp(folder) }),
        )
      }
      expect(await screen.findByRole('img', { name: /^sender@example.com avatar/ }))
        .toBeInTheDocument()
    }

    const search = screen.getByPlaceholderText('Search in Trash...')
    fireEvent.change(search, { target: { value: 'sender' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByRole('img', { name: /^sender@example.com avatar/ }))
      .toBeInTheDocument()
  })

  it('uses the same avatar in email details', async () => {
    gmailService.getMessage.mockResolvedValue({
      message: {
        ...message,
        headers: { from: 'Sender Name <sender@example.com>' },
        internal_date: '1793815200000',
        body: { plain_text: 'Message body', html: '' },
        attachments: [],
      },
    })
    renderGmail()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(await screen.findByRole('img', { name: /^Sender Name avatar/ }))
      .toBeInTheDocument()
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

  it('shows only Send for Gmail drafts and opens an editable review', async () => {
    gmailService.getMessages.mockResolvedValue({
      messages: [{ ...message, labels: ['DRAFT'] }],
      next_page_token: null,
    })
    gmailService.getMessage.mockResolvedValue({
      message: {
        ...message,
        labels: ['DRAFT'],
        headers: {
          to: 'recipient@example.com',
          cc: 'copy@example.com',
          subject: 'Draft subject',
        },
        body: { plain_text: 'Draft body' },
        attachments: [],
      },
    })
    renderGmail()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Send' }))

    expect(await screen.findByRole('dialog', {
      name: 'Review draft before sending',
    })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'To' }))
      .toHaveValue('recipient@example.com')
    expect(screen.getByRole('textbox', { name: 'Cc' }))
      .toHaveValue('copy@example.com')
    expect(screen.getByRole('textbox', { name: 'Subject' }))
      .toHaveValue('Draft subject')
    expect(screen.getByRole('textbox', { name: 'Message' }))
      .toHaveValue('Draft body')
    expect(screen.queryByRole('button', { name: /Reply/ }))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Forward' }))
      .not.toBeInTheDocument()
    expect(api.post.mock.calls.some(([url]) =>
      url.includes('/action-approvals/gmail-drafts/')
    )).toBe(false)
  })

  it('preserves Reply, Reply All, and Forward for received emails', async () => {
    renderGmail()
    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))

    expect(await screen.findByRole('button', { name: /Reply$/ }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Reply All/ }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Forward/ }))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Send' }))
      .not.toBeInTheDocument()
  })

  it('cancelling draft review does not create an approval or send', async () => {
    gmailService.getMessages.mockResolvedValue({
      messages: [{ ...message, labels: ['DRAFT'] }],
      next_page_token: null,
    })
    gmailService.getMessage.mockResolvedValue({
      message: {
        ...message,
        labels: ['DRAFT'],
        headers: { to: 'recipient@example.com', subject: 'Draft subject' },
        body: { plain_text: 'Draft body' },
        attachments: [],
      },
    })
    renderGmail()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Send' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('dialog', {
      name: 'Review draft before sending',
    })).not.toBeInTheDocument()
    expect(api.post.mock.calls.some(([url]) =>
      url.includes('/action-approvals/')
    )).toBe(false)
  })

  it('blocks editable sending for drafts with attachments to prevent losing them', async () => {
    gmailService.getMessages.mockResolvedValue({
      messages: [{ ...message, labels: ['DRAFT'] }],
      next_page_token: null,
    })
    gmailService.getMessage.mockResolvedValue({
      message: {
        ...message,
        labels: ['DRAFT'],
        headers: { to: 'recipient@example.com', subject: 'Draft subject' },
        body: { plain_text: 'Draft body' },
        attachments: [{
          attachment_id: 'attachment-1',
          filename: 'proposal.pdf',
        }],
      },
    })
    renderGmail()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Send' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This draft has attachments.',
    )
    expect(screen.getByRole('button', { name: 'Continue to approval' }))
      .toBeDisabled()
    expect(api.post.mock.calls.some(([url]) =>
      url.includes('/action-approvals/gmail-drafts/')
    )).toBe(false)
  })

  it('requires explicit approval and a second send confirmation', async () => {
    const pendingProposal = {
      proposal_id: 'proposal-123',
      status: 'pending',
      parameters: {
        to: ['recipient@example.com'],
        cc: [],
        bcc: [],
        subject: 'Draft subject',
        body: 'Draft body',
      },
    }
    api.post.mockImplementation(async (url) => {
      if (url.includes('/gmail-drafts/')) {
        return { proposal: pendingProposal }
      }
      if (url.endsWith('/approve')) {
        return { proposal: { ...pendingProposal, status: 'approved' } }
      }
      if (url.endsWith('/execute')) {
        return {
          proposal: { ...pendingProposal, status: 'completed' },
        }
      }
      return { authorization_url: 'https://accounts.google.com/' }
    })
    gmailService.getMessages.mockResolvedValue({
      messages: [{ ...message, labels: ['DRAFT'] }],
      next_page_token: null,
    })
    gmailService.getMessage.mockResolvedValue({
      message: {
        ...message,
        labels: ['DRAFT'],
        headers: { to: 'recipient@example.com', subject: 'Draft subject' },
        body: { plain_text: 'Draft body' },
        attachments: [],
      },
    })
    const { queryClient } = renderGmail()
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries')

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Send' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue to approval' }))

    expect(await screen.findByText('Approval status:')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    expect(api.post.mock.calls.some(([url]) => url.endsWith('/execute')))
      .toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByRole('button', {
      name: 'Send approved draft',
    })).toBeInTheDocument()
    expect(api.post.mock.calls.some(([url]) => url.endsWith('/execute')))
      .toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Send approved draft' }))
    expect(await screen.findByText('Draft sent successfully!'))
      .toBeInTheDocument()
    expect(api.post.mock.calls.filter(([url]) => url.endsWith('/execute')))
      .toHaveLength(1)
    await waitFor(() => {
      const invalidatedFolders = invalidateQueries.mock.calls
        .map(([filters]) => filters.queryKey?.[3])
      expect(invalidatedFolders).toEqual(
        expect.arrayContaining(['drafts', 'sent', 'all']),
      )
    })
  })

  it('does not retry a draft send with an uncertain outcome', async () => {
    const pendingProposal = {
      proposal_id: 'proposal-123',
      status: 'pending',
      parameters: {
        to: ['recipient@example.com'],
        cc: [],
        bcc: [],
        subject: 'Draft subject',
        body: 'Draft body',
      },
    }
    api.post.mockImplementation(async (url) => {
      if (url.includes('/gmail-drafts/')) {
        return { proposal: pendingProposal }
      }
      if (url.endsWith('/approve')) {
        return { proposal: { ...pendingProposal, status: 'approved' } }
      }
      if (url.endsWith('/execute')) {
        throw new Error('Connection lost.')
      }
      return { authorization_url: 'https://accounts.google.com/' }
    })
    api.get.mockImplementation(async (url) => {
      if (url.endsWith('/action-approvals/proposal-123')) {
        return { ...pendingProposal, status: 'execution_uncertain' }
      }
      return {
        connections: [{
          id: 'connection-1',
          provider: 'google',
          status: 'connected',
          email: 'user@example.com',
        }],
      }
    })
    gmailService.getMessages.mockResolvedValue({
      messages: [{ ...message, labels: ['DRAFT'] }],
      next_page_token: null,
    })
    gmailService.getMessage.mockResolvedValue({
      message: {
        ...message,
        labels: ['DRAFT'],
        headers: { to: 'recipient@example.com', subject: 'Draft subject' },
        body: { plain_text: 'Draft body' },
        attachments: [],
      },
    })
    renderGmail()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Send' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue to approval' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    fireEvent.click(await screen.findByRole('button', {
      name: 'Send approved draft',
    }))

    expect(await screen.findByRole('button', { name: 'Check send status' }))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Check send status' }))
    await waitFor(() => {
      expect(api.get).toHaveBeenCalledWith(
        expect.stringContaining('/action-approvals/proposal-123'),
      )
    })
    expect(api.post.mock.calls.filter(([url]) => url.endsWith('/execute')))
      .toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Send approved draft' }))
      .not.toBeInTheDocument()
  })
})
