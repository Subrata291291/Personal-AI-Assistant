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

const gmailUser = vi.hoisted(() => ({
  current: { id: 'user-1', user_metadata: {} },
}))

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: gmailUser.current }),
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

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function configureDraftApproval(executeOperation) {
  const proposal = {
    proposal_id: 'proposal-escape-test',
    status: 'pending',
    parameters: {
      to: ['recipient@example.com'],
      cc: [],
      bcc: [],
      subject: 'Draft subject',
      body: 'Draft body',
    },
  }
  api.get.mockResolvedValue({
    connections: [{
      id: 'connection-1',
      provider: 'google',
      status: 'connected',
      email: 'user@example.com',
      capabilities: { gmail: true },
    }],
  })
  api.post.mockImplementation((url) => {
    if (url.endsWith('/approve')) {
      return Promise.resolve({
        proposal: { ...proposal, status: 'approved' },
      })
    }
    if (url.endsWith('/execute')) return executeOperation()
    if (url.includes('/gmail-drafts/')) {
      return Promise.resolve({ proposal })
    }
    return Promise.resolve({
      authorization_url: 'https://accounts.google.com/',
    })
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
    gmailUser.current = { id: 'user-1', user_metadata: {} }
    sessionStorage.clear()
    api.get.mockResolvedValue({
      connections: [{
        id: 'connection-1',
        provider: 'google',
        status: 'connected',
        email: 'user@example.com',
        capabilities: { gmail: true },
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

  it('loads ten messages initially and uses the next page token', async () => {
    gmailService.getMessages.mockImplementation(async ({ pageToken }) => ({
      messages: [message],
      next_page_token: pageToken ? null : 'next-page',
    }))
    renderGmail()

    await waitFor(() => {
      expect(gmailService.getMessages).toHaveBeenCalledWith({
        connectionId: 'connection-1',
        maxResults: 10,
        pageToken: undefined,
        query: '',
        folder: 'inbox',
      })
    })

    fireEvent.click(await screen.findByRole('button', {
      name: 'Load more emails',
    }))

    await waitFor(() => {
      expect(gmailService.getMessages).toHaveBeenLastCalledWith({
        connectionId: 'connection-1',
        maxResults: 10,
        pageToken: 'next-page',
        query: '',
        folder: 'inbox',
      })
    })
  })

  it('shows malformed message-list responses as errors, not empty inboxes', async () => {
    gmailService.getMessages.mockRejectedValue(
      new Error('Gmail returned an invalid message list response.'),
    )
    renderGmail()

    expect(await screen.findByText(
      'Gmail returned an invalid message list response.',
    )).toBeInTheDocument()
    expect(screen.queryByText('No messages found')).not.toBeInTheDocument()
  })

  it('preserves selected folder and search query when loading a page', async () => {
    gmailService.getMessages.mockImplementation(async ({ pageToken }) => ({
      messages: [message],
      next_page_token: pageToken ? null : 'search-page',
    }))
    renderGmail()

    fireEvent.click(
      within(await screen.findByRole('navigation', { name: 'Gmail folders' }))
        .getByRole('button', { name: /Sent/ }),
    )
    await waitFor(() => {
      expect(gmailService.getMessages).toHaveBeenLastCalledWith(
        expect.objectContaining({
          maxResults: 10,
          pageToken: undefined,
          query: '',
          folder: 'sent',
        }),
      )
    })

    fireEvent.change(screen.getByPlaceholderText('Search in Sent...'), {
      target: { value: 'from:sender@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    await waitFor(() => {
      expect(gmailService.getMessages).toHaveBeenLastCalledWith(
        expect.objectContaining({
          maxResults: 10,
          pageToken: undefined,
          query: 'from:sender@example.com',
          folder: 'sent',
        }),
      )
    })

    fireEvent.click(await screen.findByRole('button', {
      name: 'Load more emails',
    }))

    await waitFor(() => {
      expect(gmailService.getMessages).toHaveBeenLastCalledWith(
        expect.objectContaining({
          maxResults: 10,
          pageToken: 'search-page',
          query: 'from:sender@example.com',
          folder: 'sent',
        }),
      )
    })
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

  it('blocks proposal creation when the operation ID cannot be persisted', async () => {
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
    const setItem = Storage.prototype.setItem
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(function (key, value) {
        if (key.startsWith('gmail-draft-send-approval:')) {
          throw new DOMException('Storage blocked', 'SecurityError')
        }
        return setItem.call(this, key, value)
      })
    renderGmail()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Send' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Browser storage is unavailable',
    )
    expect(api.post.mock.calls.some(([url]) =>
      url.includes('/action-approvals/gmail-drafts/')
    )).toBe(false)
    storageSpy.mockRestore()
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
    const reviewDialog = screen.getByRole('dialog', {
      name: 'Review draft before sending',
    })
    expect(reviewDialog).toHaveClass(
      'max-h-[calc(100dvh-32px)]',
      'overflow-hidden',
      'flex-col',
    )
    expect(reviewDialog.querySelectorAll('.gmail-scrollbar.overflow-y-auto'))
      .toHaveLength(1)
    expect(within(reviewDialog).getByLabelText('Message'))
      .toHaveClass('max-h-[40dvh]', 'overflow-y-auto')
    expect(within(reviewDialog).getByRole('button', { name: 'Cancel' }))
      .toBeVisible()
    for (const field of ['To', 'Cc', 'Bcc', 'Subject', 'Message']) {
      expect(within(reviewDialog).getByLabelText(field)).toBeEnabled()
    }
    expect(document.body.style.overflow).toBe('hidden')
    fireEvent.change(within(reviewDialog).getByLabelText('To'), {
      target: { value: 'recipient@example.com, second@example.com' },
    })
    fireEvent.change(within(reviewDialog).getByLabelText('Cc'), {
      target: { value: 'copy@example.com' },
    })
    fireEvent.change(within(reviewDialog).getByLabelText('Bcc'), {
      target: { value: 'hidden@example.com' },
    })
    fireEvent.change(within(reviewDialog).getByLabelText('Subject'), {
      target: { value: 'Reviewed subject' },
    })
    fireEvent.change(within(reviewDialog).getByLabelText('Message'), {
      target: { value: 'Reviewed message' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue to approval' }))

    expect(await screen.findByText('Approval status:')).toBeInTheDocument()
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/action-approvals/gmail-drafts/message-123',
      expect.objectContaining({
        connection_id: 'connection-1',
        to: ['recipient@example.com', 'second@example.com'],
        cc: ['copy@example.com'],
        bcc: ['hidden@example.com'],
        subject: 'Reviewed subject',
        body: 'Reviewed message',
      }),
      {
        'Idempotency-Key': expect.stringMatching(
          /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
        ),
      },
    )
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

  it('reconciles an uncertain approval request before allowing a retry', async () => {
    const requestError = Object.assign(
      new TypeError('Failed to fetch'),
      { networkFailure: true, requestId: 'request-create-1' },
    )
    const statusError = Object.assign(
      new TypeError('Failed to fetch'),
      { networkFailure: true, requestId: 'request-status-1' },
    )
    let statusChecks = 0
    api.post.mockImplementation(async (url) => {
      if (url.includes('/gmail-drafts/')) throw requestError
      return { authorization_url: 'https://accounts.google.com/' }
    })
    api.get.mockImplementation(async (url) => {
      if (url.includes('/gmail-drafts/approval/')) {
        statusChecks += 1
        if (statusChecks === 1) throw statusError
        const notFound = statusChecks === 2
          ? new Error('Not Found')
          : new Error('No approval proposal was created for this request.')
        notFound.status = 404
        throw notFound
      }
      return {
        connections: [{
          id: 'connection-1',
          provider: 'google',
          status: 'connected',
          email: 'user@example.com',
          capabilities: { gmail: true },
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

    expect(await screen.findByRole('button', {
      name: 'Check approval status',
    })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Continue to approval' }))
      .toBeDisabled()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Do not retry until status is checked.',
    )
    expect(screen.getByRole('alert')).toHaveTextContent('request-status-1')

    fireEvent.click(screen.getByRole('button', {
      name: 'Check approval status',
    }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Do not retry until status is checked.',
    )
    expect(screen.getByRole('button', { name: 'Continue to approval' }))
      .toBeDisabled()

    fireEvent.click(screen.getByRole('button', {
      name: 'Check approval status',
    }))
    expect(await screen.findByText(
      /No proposal was found for this operation ID/,
    )).toBeInTheDocument()
    expect(await screen.findByText(
      /this does not approve or send the email/,
    )).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue to approval' }))
      .toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Continue to approval' }))
    await waitFor(() => {
      expect(api.post.mock.calls.filter(([url]) =>
        url.includes('/gmail-drafts/'),
      )).toHaveLength(2)
    })
    const proposalCalls = api.post.mock.calls.filter(([url]) =>
      url.includes('/gmail-drafts/'),
    )
    expect(proposalCalls[0][2]['Idempotency-Key']).toBe(
      proposalCalls[1][2]['Idempotency-Key'],
    )
  })

  it('renders structured validation errors as readable text', async () => {
    const validationError = Object.assign(new Error(''), {
      detail: [{
        loc: ['body', 'subject'],
        msg: 'Subject is too long',
        input: 'private value',
      }],
      status: 422,
      requestId: 'request-validation-1',
    })
    api.post.mockImplementation(async (url) => {
      if (url.includes('/gmail-drafts/')) throw validationError
      return { authorization_url: 'https://accounts.google.com/' }
    })
    api.get.mockImplementation(async (url) => {
      if (url.includes('/gmail-drafts/approval/')) {
        const notFound = new Error(
          'No approval proposal was created for this request.',
        )
        notFound.status = 404
        throw notFound
      }
      return {
        connections: [{
          id: 'connection-1',
          provider: 'google',
          status: 'connected',
          email: 'user@example.com',
          capabilities: { gmail: true },
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

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('body.subject: Subject is too long')
    expect(alert).toHaveTextContent('request-validation-1')
    expect(alert.textContent).not.toContain('[object Object]')
    expect(alert.textContent).not.toContain('private value')
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
          capabilities: { gmail: true },
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

  it('keeps message details open when Escape is pressed during execution', async () => {
    configureDraftApproval(() => new Promise(() => {}))
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

    expect(await screen.findByText(
      'Gmail is processing the approved draft. Do not retry.',
    )).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })

    expect(screen.getByRole('dialog', { name: 'Email Details' }))
      .toBeInTheDocument()
  })

  it('keeps message details open when Escape is pressed after an uncertain send outcome', async () => {
    configureDraftApproval(async () => {
      throw new Error('Connection lost.')
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

    expect(await screen.findByText(
      'The send outcome is uncertain. Check status to reconcile; do not retry.',
    )).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })

    expect(screen.getByRole('dialog', { name: 'Email Details' }))
      .toBeInTheDocument()
  })

  it('closes message details on Escape when no approval blocks closing', async () => {
    renderGmail()
    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))

    expect(screen.getByRole('dialog', { name: 'Email Details' }))
      .toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('keeps one Escape listener across selection and approval changes and removes it on unmount', async () => {
    const execution = deferred()
    configureDraftApproval(() => execution.promise)
    const activeKeydownListeners = new Set()
    const originalAddListener = window.addEventListener
    const originalRemoveListener = window.removeEventListener
    const addListener = vi.spyOn(window, 'addEventListener')
      .mockImplementation((type, listener, options) => {
        if (type === 'keydown') activeKeydownListeners.add(listener)
        return originalAddListener.call(window, type, listener, options)
      })
    const removeListener = vi.spyOn(window, 'removeEventListener')
      .mockImplementation((type, listener, options) => {
        if (type === 'keydown') activeKeydownListeners.delete(listener)
        return originalRemoveListener.call(window, type, listener, options)
      })
    const view = renderGmail()

    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))
    await waitFor(() => expect(activeKeydownListeners.size).toBe(1))

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(activeKeydownListeners.size).toBe(0)
    fireEvent.click(await screen.findByRole('button', {
      name: 'Open email: Quarterly update',
    }))
    await waitFor(() => expect(activeKeydownListeners.size).toBe(1))

    fireEvent.click(await screen.findByRole('button', { name: 'Send' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue to approval' }))
    await screen.findByRole('button', { name: 'Approve' })
    expect(activeKeydownListeners.size).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    fireEvent.click(await screen.findByRole('button', {
      name: 'Send approved draft',
    }))
    expect(await screen.findByText(
      'Gmail is processing the approved draft. Do not retry.',
    )).toBeInTheDocument()
    await waitFor(() => expect(activeKeydownListeners.size).toBe(1))

    view.unmount()
    expect(activeKeydownListeners.size).toBe(0)
    expect(addListener.mock.calls.filter(([type]) => type === 'keydown'))
      .toHaveLength(removeListener.mock.calls.filter(
        ([type]) => type === 'keydown',
      ).length)
  })

  it('clears cached emails and the selected account after disconnect', async () => {
    let disconnected = false
    api.get.mockImplementation(async () => ({
      connections: disconnected
        ? []
        : [{
            id: 'connection-1',
            provider: 'google',
            status: 'connected',
            email: 'user@example.com',
            capabilities: { gmail: true },
          }],
    }))
    gmailService.getMessages.mockImplementation(async () => {
      if (disconnected) {
        const error = new Error('Connected Google account was not found.')
        error.status = 404
        throw error
      }
      return {
        messages: [message],
        next_page_token: null,
      }
    })
    renderGmail()

    expect(await screen.findByText('Quarterly update')).toBeInTheDocument()
    expect(screen.getByText('Connected: user@example.com')).toBeInTheDocument()

    disconnected = true
    fireEvent.click(screen.getByRole('button', { name: '↻ Refresh' }))

    expect(await screen.findByText('Gmail is disconnected'))
      .toBeInTheDocument()
    expect(screen.queryByText('Quarterly update')).not.toBeInTheDocument()
    expect(screen.queryByText('Connected: user@example.com'))
      .not.toBeInTheDocument()
  })

  it('resolves to the disconnected state for a fresh user', async () => {
    api.get.mockResolvedValue({
      connections: [],
      calendar_capabilities: {},
    })
    renderGmail()

    expect(await screen.findByText('Gmail is disconnected'))
      .toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Connect Gmail' }))
      .toHaveAttribute('href', '/dashboard/connectors')
    expect(screen.queryByText('Checking connected accounts...'))
      .not.toBeInTheDocument()
    expect(gmailService.getMessages).not.toHaveBeenCalled()
  })

  it('shows connection lookup errors and retries without loading Gmail messages', async () => {
    api.get
      .mockRejectedValueOnce(new Error('API temporarily unavailable'))
      .mockResolvedValueOnce({ connections: [] })
    renderGmail()

    expect(await screen.findAllByText('API temporarily unavailable'))
      .not.toHaveLength(0)
    fireEvent.click(screen.getAllByRole('button', { name: 'Retry' })[0])

    expect(await screen.findByText('Gmail is disconnected'))
      .toBeInTheDocument()
    expect(gmailService.getMessages).not.toHaveBeenCalled()
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  it('keeps the Google account connected while Gmail is switched off', async () => {
    gmailUser.current = {
      id: 'user-1',
      user_metadata: { gmail_enabled: false },
    }
    renderGmail()

    expect(await screen.findByText('Connected · Disabled'))
      .toBeInTheDocument()
    expect(await screen.findByText('Connected: user@example.com'))
      .toBeInTheDocument()
    expect(gmailService.getMessages).not.toHaveBeenCalled()
  })
})
