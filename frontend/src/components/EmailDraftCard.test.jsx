import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../services/conversationService.js', () => ({
  conversationService: {
    updateEmailDraft: vi.fn(),
    saveEmailDraftToGmail: vi.fn(),
    getEmailDraftSaveStatus: vi.fn(),
  },
}))

import EmailDraftCard from './EmailDraftCard.jsx'
import { conversationService } from '../services/conversationService.js'

const operationId = 'c5b40d3a-68b7-4e16-bc56-0f38c2ea58b9'
const originalDraft = {
  draft_type: 'new',
  to: [{ email: 'subrata291291@gmail.com' }],
  cc: [],
  bcc: [],
  subject: 'Initial subject',
  body: 'Initial body',
  source_message_id: null,
  source_thread_id: null,
}

function createMessage(saveRecord = undefined) {
  return {
    id: 'message-1',
    conversation_id: 'conversation-1',
    metadata: {
      email_draft: originalDraft,
      ...(saveRecord ? { gmail_draft_save: saveRecord } : {}),
    },
  }
}

function renderCard(message = createMessage()) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })

  function Harness() {
    const [currentMessage, setCurrentMessage] = useState(message)
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <EmailDraftCard
            message={currentMessage}
            isDark={false}
            connectionId="connection-1"
            onUpdated={setCurrentMessage}
          />
        </MemoryRouter>
      </QueryClientProvider>
    )
  }

  return render(<Harness />)
}

describe('EmailDraftCard Gmail save', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(operationId)
  })

  it('sends edited recipient and content to the preview update before Gmail save', async () => {
    conversationService.updateEmailDraft.mockImplementation(
      async (_conversationId, _messageId, draft) => ({
        ...createMessage(),
        metadata: { email_draft: draft },
      }),
    )
    renderCard()

    fireEvent.change(screen.getByLabelText('To'), {
      target: { value: 'edited@example.com' },
    })
    fireEvent.change(screen.getByLabelText('Subject'), {
      target: { value: 'Edited subject' },
    })
    fireEvent.change(screen.getByLabelText('Message'), {
      target: { value: 'Edited body' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save preview changes' }))

    await waitFor(() => {
      expect(conversationService.updateEmailDraft).toHaveBeenCalledWith(
        'conversation-1',
        'message-1',
        expect.objectContaining({
          to: [{ email: 'edited@example.com', name: null }],
          subject: 'Edited subject',
          body: 'Edited body',
        }),
      )
    })
  })

  it('prevents duplicate clicks and reports confirmed Gmail success', async () => {
    let resolveSave
    conversationService.saveEmailDraftToGmail.mockImplementation(
      () => new Promise((resolve) => {
        resolveSave = resolve
      }),
    )
    renderCard()

    const saveButton = screen.getByRole('button', { name: 'Save to Gmail Drafts' })
    expect(saveButton).not.toBeDisabled()
    fireEvent.click(saveButton)
    fireEvent.click(saveButton)

    await waitFor(() => {
      expect(conversationService.saveEmailDraftToGmail).toHaveBeenCalledTimes(1)
    })
    expect(conversationService.saveEmailDraftToGmail).toHaveBeenCalledWith(
      'conversation-1',
      'message-1',
      'connection-1',
      operationId,
    )

    resolveSave({
      state: 'saved',
      connection_id: 'connection-1',
      operation_id: operationId,
      draft_id: 'draft-1',
      fingerprint: 'fingerprint',
      saved_preview: originalDraft,
    })

    expect(await screen.findByText('Draft saved to Gmail. It has not been sent.'))
      .toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Gmail Drafts' }))
      .toHaveAttribute('href', 'https://mail.google.com/mail/u/0/#drafts')
  })

  it('checks status with the original operation ID and preserves edits when recovered', async () => {
    conversationService.saveEmailDraftToGmail.mockRejectedValue(
      new Error('Gmail could not confirm the save'),
    )
    conversationService.getEmailDraftSaveStatus.mockResolvedValue({
      state: 'saved',
      connection_id: 'connection-1',
      operation_id: operationId,
      draft_id: 'draft-1',
      fingerprint: 'fingerprint',
      saved_preview: originalDraft,
      detail: 'Recovered existing draft',
    })
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Save to Gmail Drafts' }))
    await screen.findByRole('button', { name: 'Check save status' })
    fireEvent.change(screen.getByLabelText('Subject'), {
      target: { value: 'Keep my edited subject' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Check save status' }))

    await waitFor(() => {
      expect(conversationService.getEmailDraftSaveStatus).toHaveBeenCalledWith(
        'conversation-1',
        'message-1',
        'connection-1',
        operationId,
      )
    })
    expect(await screen.findByDisplayValue('Keep my edited subject'))
      .toBeInTheDocument()
    expect(await screen.findByText('Gmail confirmed this draft. No second draft was created.'))
      .toBeInTheDocument()
  })

  it('keeps an uncertain result blocked when the backend cannot find the marker', async () => {
    conversationService.getEmailDraftSaveStatus.mockResolvedValue({
      state: 'uncertain',
      connection_id: 'connection-1',
      operation_id: operationId,
      detail: 'No matching draft was found during inspection, but this does not prove creation failed.',
    })
    renderCard(createMessage({
      state: 'uncertain',
      connection_id: 'connection-1',
      operation_id: operationId,
    }))

    expect(await screen.findByText(
      'No matching draft was found during inspection, but this does not prove creation failed.',
    )).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check save status' }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save outcome needs review' }))
      .toBeDisabled()
  })

  it('shows pending status without allowing a second draft save', async () => {
    conversationService.getEmailDraftSaveStatus.mockResolvedValue({
      state: 'saving',
      connection_id: 'connection-1',
      operation_id: operationId,
      detail: 'The Gmail draft operation is still being processed.',
    })
    renderCard(createMessage({
      state: 'uncertain',
      connection_id: 'connection-1',
      operation_id: operationId,
    }))

    expect(await screen.findByText(
      'The original Gmail draft operation is still processing.',
    )).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check save status' }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save outcome needs review' }))
      .toBeDisabled()
  })

  it('displays the backend status-check detail while keeping the save blocked', async () => {
    const statusError = new Error(
      'Gmail draft status could not be checked. The outcome remains uncertain; do not retry the creation.',
    )
    statusError.status = 503
    conversationService.getEmailDraftSaveStatus.mockRejectedValue(statusError)
    renderCard(createMessage({
      state: 'uncertain',
      connection_id: 'connection-1',
      operation_id: operationId,
    }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Gmail draft status could not be checked. The outcome remains uncertain; do not retry the creation.',
    )
    expect(screen.getByRole('button', { name: 'Save outcome needs review' }))
      .toBeDisabled()
  })

  it('retries a definitively failed save using its original operation ID', async () => {
    conversationService.getEmailDraftSaveStatus.mockResolvedValue({
      state: 'failed',
      connection_id: 'connection-1',
      operation_id: operationId,
      detail: 'Gmail rejected the request',
    })
    conversationService.saveEmailDraftToGmail.mockResolvedValue({
      state: 'saved',
      connection_id: 'connection-1',
      operation_id: operationId,
      draft_id: 'draft-2',
      fingerprint: 'fingerprint',
      saved_preview: originalDraft,
    })
    renderCard(createMessage({
      state: 'uncertain',
      connection_id: 'connection-1',
      operation_id: operationId,
    }))

    await waitFor(() => {
      expect(conversationService.getEmailDraftSaveStatus).toHaveBeenCalledWith(
        'conversation-1',
        'message-1',
        'connection-1',
        operationId,
      )
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Save to Gmail Drafts' }))
    await waitFor(() => {
      expect(conversationService.saveEmailDraftToGmail).toHaveBeenCalledWith(
        'conversation-1',
        'message-1',
        'connection-1',
        operationId,
      )
    })
  })
})
