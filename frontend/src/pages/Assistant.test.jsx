import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const authMock = vi.hoisted(() => ({
  user: { id: 'user-a' },
}))

const conversationMocks = vi.hoisted(() => ({
  createConversation: vi.fn(),
  getConversations: vi.fn(),
  getMessages: vi.fn(),
  getChatRequestStatus: vi.fn(),
  sendChatMessage: vi.fn(),
  streamChatMessage: vi.fn(),
}))

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: authMock.user }),
}))

vi.mock('../services/conversationService.js', () => ({
  conversationService: conversationMocks,
}))

vi.mock('../lib/api.js', () => ({
  api: {
    get: vi.fn().mockResolvedValue({ connections: [] }),
  },
}))

vi.mock('../components/EmailDraftCard.jsx', () => ({
  default: () => null,
}))

vi.mock('../components/ActionApprovalCard.jsx', () => ({
  default: () => null,
}))

import Assistant from './Assistant.jsx'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function conversation(id) {
  return {
    id,
    is_archived: false,
    updated_at: '2026-11-06T00:00:00Z',
  }
}

function renderAssistant() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })

  function Layout() {
    return (
      <Outlet context={{
        isDark: false,
        muted: 'text-slate-500',
        surface: 'border-slate-200 bg-white',
      }} />
    )
  }

  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/assistant']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/assistant" element={<Assistant />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )

  return { ...view, queryClient }
}

function renderTree(queryClient) {
  function Layout() {
    return (
      <Outlet context={{
        isDark: false,
        muted: 'text-slate-500',
        surface: 'border-slate-200 bg-white',
      }} />
    )
  }

  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/assistant']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/assistant" element={<Assistant />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('Assistant user-switch isolation', () => {
  beforeEach(() => {
    authMock.user = { id: 'user-a' }
    vi.clearAllMocks()
    HTMLElement.prototype.scrollIntoView = vi.fn()
    conversationMocks.getConversations.mockImplementation(async () =>
      authMock.user.id === 'user-a'
        ? [conversation('conversation-a')]
        : [conversation('conversation-b')])
    conversationMocks.getMessages.mockImplementation(async (id) => [{
      id: `${id}-message`,
      conversation_id: id,
      role: 'assistant',
      content: `${id}-private-message`,
    }])
    conversationMocks.createConversation.mockResolvedValue(
      conversation('created-conversation'),
    )
  })

  it('clears user A conversation and draft and ignores its pending send after switching to B', async () => {
    const pendingSend = deferred()
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        await pendingSend.promise
        onEvent({ type: 'token', content: 'A private response' })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'a-sent-assistant',
          user_message_id: 'a-sent-user',
          provider: 'test',
          model: 'test',
        })
      }
    )
    const view = renderAssistant()

    expect(await screen.findByText('conversation-a-private-message'))
      .toBeInTheDocument()
    const composer = screen.getByPlaceholderText(
      'Message your personal AI...',
    )
    fireEvent.change(composer, { target: { value: 'A private draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))

    expect(await screen.findByText('A private draft')).toBeInTheDocument()
    await waitFor(() => {
      expect(conversationMocks.streamChatMessage).toHaveBeenCalledOnce()
    })

    authMock.user = { id: 'user-b' }
    view.rerender(renderTree(view.queryClient))

    expect(await screen.findByText('conversation-b-private-message'))
      .toBeInTheDocument()
    expect(screen.queryByText('conversation-a-private-message'))
      .not.toBeInTheDocument()
    expect(screen.queryByText('A private draft')).not.toBeInTheDocument()

    const newComposer = screen.getByPlaceholderText(
      'Message your personal AI...',
    )
    fireEvent.change(newComposer, { target: { value: 'B private draft' } })

    await act(async () => {
      pendingSend.resolve()
    })

    expect(screen.getByPlaceholderText('Message your personal AI...'))
      .toHaveValue('B private draft')
    expect(screen.queryByText('A private response')).not.toBeInTheDocument()
    expect(screen.queryByText('A private draft')).not.toBeInTheDocument()
    expect(conversationMocks.streamChatMessage).toHaveBeenCalledOnce()
  })

  it('does not let a delayed user A restoration replace user B conversation', async () => {
    const delayedAList = deferred()
    conversationMocks.getConversations.mockImplementation((..._args) =>
      authMock.user.id === 'user-a'
        ? delayedAList.promise
        : Promise.resolve([conversation('conversation-b')]))

    const view = renderAssistant()
    await waitFor(() => {
      expect(conversationMocks.getConversations).toHaveBeenCalledOnce()
    })

    authMock.user = { id: 'user-b' }
    view.rerender(renderTree(view.queryClient))
    expect(await screen.findByText('conversation-b-private-message'))
      .toBeInTheDocument()

    await act(async () => {
      delayedAList.resolve([conversation('conversation-a')])
    })

    expect(screen.getByText('conversation-b-private-message'))
      .toBeInTheDocument()
    expect(screen.queryByText('conversation-a-private-message'))
      .not.toBeInTheDocument()
  })

  it('preserves normal same-user message sending', async () => {
    authMock.user = {
      id: 'user-a',
      user_metadata: { timezone: 'America/New_York' },
    }
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: 'Hello from the assistant' })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'sent-assistant',
          user_message_id: 'sent-user',
          provider: 'test-provider',
          model: 'test-model',
        })
      }
    )
    // After invalidation/refetch, return the new messages
    conversationMocks.getMessages.mockResolvedValue([
      { id: 'conversation-a-message', conversation_id: 'conversation-a', role: 'assistant', content: 'conversation-a-private-message' },
      { id: 'sent-user', conversation_id: 'conversation-a', role: 'user', content: 'Hello assistant' },
      { id: 'sent-assistant', conversation_id: 'conversation-a', role: 'assistant', content: 'Hello from the assistant' },
    ])
    renderAssistant()

    await screen.findByText('conversation-a-private-message')
    fireEvent.change(
      screen.getByPlaceholderText('Message your personal AI...'),
      { target: { value: 'Hello assistant' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))

    expect(await screen.findByText('Hello from the assistant'))
      .toBeInTheDocument()
    expect(conversationMocks.streamChatMessage).toHaveBeenCalledWith(
      'conversation-a',
      'Hello assistant',
      expect.any(String),
      null,
      'America/New_York',
      expect.any(Function),
      expect.any(AbortSignal),
    )
  })

  it('uses Asia/Kolkata when the user has no configured timezone', async () => {
    authMock.user = { id: 'user-a' }
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: 'No events.' })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'sent-assistant',
          user_message_id: 'sent-user',
          provider: 'test-provider',
          model: 'test-model',
        })
      }
    )
    conversationMocks.getMessages.mockResolvedValue([
      { id: 'conversation-a-message', conversation_id: 'conversation-a', role: 'assistant', content: 'conversation-a-private-message' },
      { id: 'sent-user', conversation_id: 'conversation-a', role: 'user', content: 'Show my calendar today' },
      { id: 'sent-assistant', conversation_id: 'conversation-a', role: 'assistant', content: 'No events.' },
    ])
    renderAssistant()

    await screen.findByText('conversation-a-private-message')
    fireEvent.change(
      screen.getByPlaceholderText('Message your personal AI...'),
      { target: { value: 'Show my calendar today' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))

    await waitFor(() => {
      expect(conversationMocks.streamChatMessage).toHaveBeenCalledWith(
        'conversation-a',
        'Show my calendar today',
        expect.any(String),
        null,
        'Asia/Kolkata',
        expect.any(Function),
        expect.any(AbortSignal),
      )
    })
  })
})

describe('Assistant true streaming', () => {
  beforeEach(() => {
    authMock.user = {
      id: 'user-a',
      user_metadata: { timezone: 'Asia/Kolkata' },
    }
    vi.clearAllMocks()
    HTMLElement.prototype.scrollIntoView = vi.fn()
    conversationMocks.getConversations.mockResolvedValue([
      conversation('conversation-a'),
    ])
    conversationMocks.getMessages.mockResolvedValue([
      {
        id: 'conversation-a-message',
        conversation_id: 'conversation-a',
        role: 'assistant',
        content: 'conversation-a-private-message',
      },
    ])
    conversationMocks.createConversation.mockResolvedValue(
      conversation('created-conversation'),
    )
  })

  async function submitMessage(text) {
    fireEvent.change(
      screen.getByPlaceholderText('Message your personal AI...'),
      { target: { value: text } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  }

  it('shows a placeholder immediately and renders the first chunk before completion', async () => {
    const firstTokenGate = deferred()
    const completionGate = deferred()
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'status', message: 'Preparing your response...' })
        await firstTokenGate.promise
        onEvent({ type: 'token', content: 'First' })
        await completionGate.promise
        onEvent({ type: 'token', content: ' second' })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'server-assistant',
          user_message_id: 'server-user',
          provider: 'test-provider',
          model: 'test-model',
        })
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Hello assistant')

    // A single animated typing indicator is visible before the first token.
    const indicator = await screen.findByTestId('assistant-typing-indicator')
    expect(indicator).toBeInTheDocument()
    expect(indicator.querySelectorAll('.assistant-typing-dot'))
      .toHaveLength(3)
    expect(within(indicator).getByText('Preparing your response...'))
      .toBeInTheDocument()
    expect(screen.queryByText('AI is thinking...')).not.toBeInTheDocument()

    // Release the first token; the indicator must yield to real text.
    await act(async () => {
      firstTokenGate.resolve()
    })

    expect(await screen.findByText('First')).toBeInTheDocument()
    expect(screen.queryByTestId('assistant-typing-indicator'))
      .not.toBeInTheDocument()
    expect(screen.queryByText('Preparing your response...'))
      .not.toBeInTheDocument()

    await act(async () => {
      completionGate.resolve()
    })

    await waitFor(() => {
      expect(screen.getByText('First second')).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.queryByText('Preparing your response...'))
        .not.toBeInTheDocument()
      expect(screen.queryByTestId('assistant-typing-indicator'))
        .not.toBeInTheDocument()
    })
  })

  it('shows the active tool status beside the typing indicator until the first token', async () => {
    const firstTokenGate = deferred()
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'status', message: 'Searching Gmail...' })
        await firstTokenGate.promise
        onEvent({ type: 'token', content: 'Found it' })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'server-assistant',
          user_message_id: 'server-user',
        })
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Check my last mail')

    const indicator = await screen.findByTestId('assistant-typing-indicator')
    expect(within(indicator).getByText('Searching Gmail...'))
      .toBeInTheDocument()
    expect(indicator.querySelectorAll('.assistant-typing-dot'))
      .toHaveLength(3)

    await act(async () => {
      firstTokenGate.resolve()
    })

    expect(await screen.findByText('Found it')).toBeInTheDocument()
    expect(screen.queryByTestId('assistant-typing-indicator'))
      .not.toBeInTheDocument()
  })

  it('removes the empty assistant bubble when stopped before the first token', async () => {
    conversationMocks.streamChatMessage.mockImplementation(
      (convId, content, key, connId, tz, onEvent, signal) => {
        onEvent({ type: 'status', message: 'Checking Google Calendar...' })
        return new Promise((resolve, reject) => {
          signal.addEventListener('abort', () => {
            const error = new Error('aborted')
            error.name = 'AbortError'
            reject(error)
          })
        })
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Check my calendar')

    const indicator = await screen.findByTestId('assistant-typing-indicator')
    expect(within(indicator).getByText('Checking Google Calendar...'))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Stop generating' }))

    await waitFor(() => {
      expect(screen.queryByTestId('assistant-typing-indicator'))
        .not.toBeInTheDocument()
    })
    expect(screen.getByText('Response stopped before it started.'))
      .toBeInTheDocument()
    expect(screen.getByText('Check my calendar')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Message your personal AI...'))
      .toBeEnabled()
  })

  it('keeps the conversation scrollport and composer in the viewport', async () => {
    renderAssistant()
    await screen.findByText('conversation-a-private-message')

    const messages = screen.getByText('conversation-a-private-message')
      .closest('.overflow-y-auto')
    expect(messages).toHaveClass('overflow-y-auto')
    expect(messages).toHaveClass('overscroll-contain')
    expect(messages).toHaveClass('flex-1')
    expect(messages).toHaveClass('assistant-scrollport')
    expect(messages.className).not.toContain('overflow-hidden')
    expect(messages.closest('section')).toHaveClass('min-h-[520px]')
    const shell = messages.closest('section').parentElement
    expect(shell).toHaveClass('max-w-5xl')
    expect(shell.className).toContain('min-h-[calc(100dvh-150px)]')
    expect(shell).not.toHaveClass('h-[calc(100dvh-150px)]')

    const composer = screen.getByPlaceholderText('Message your personal AI...')
      .closest('form').parentElement
    expect(composer.className).not.toContain('fixed')
    expect(composer.className).not.toContain('absolute')
    expect(screen.getByRole('button', { name: 'Send message' }))
      .toBeInTheDocument()
  })

  it('hides the native scrollbar without disabling the conversation scrollport', async () => {
    renderAssistant()
    await screen.findByText('conversation-a-private-message')

    const messages = screen.getByText('conversation-a-private-message')
      .closest('.assistant-scrollport')
    expect(messages).toHaveClass('overflow-y-auto', 'overscroll-contain')
    expect(messages.className).not.toContain('overflow-hidden')

    // The scroll handler remains wired for wheel, touch and keyboard scrolling.
    expect(() => fireEvent.scroll(messages)).not.toThrow()
    expect(messages).toHaveClass('overflow-y-auto')
  })

  it('auto-scrolls streamed content into view', async () => {
    const requestAnimationFrame = vi.spyOn(window, 'requestAnimationFrame')
      .mockImplementation((callback) => {
        callback(0)
        return 1
      })
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: 'Streamed answer' })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'streamed-assistant',
          user_message_id: 'streamed-user',
          provider: 'test-provider',
          model: 'test-model',
        })
      },
    )

    try {
      renderAssistant()
      await screen.findByText('conversation-a-private-message')
      await submitMessage('Summarize my important emails.')

      await waitFor(() => {
        expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledWith({
          behavior: 'smooth',
          block: 'end',
        })
      })
    } finally {
      requestAnimationFrame.mockRestore()
    }
  })

  it('appends chunks to one assistant message without duplicates', async () => {
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: 'Hello' })
        onEvent({ type: 'token', content: ' ' })
        onEvent({ type: 'token', content: 'world' })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'server-assistant',
          user_message_id: 'server-user',
          provider: 'test-provider',
          model: 'test-model',
        })
      }
    )
    conversationMocks.getMessages.mockResolvedValue([
      {
        id: 'conversation-a-message',
        conversation_id: 'conversation-a',
        role: 'assistant',
        content: 'conversation-a-private-message',
      },
      {
        id: 'server-user',
        conversation_id: 'conversation-a',
        role: 'user',
        content: 'Hello assistant',
      },
      {
        id: 'server-assistant',
        conversation_id: 'conversation-a',
        role: 'assistant',
        content: 'Hello world',
      },
    ])

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Hello assistant')

    await waitFor(() => {
      expect(screen.getAllByText('Hello world')).toHaveLength(1)
    })
    expect(screen.getAllByText('Hello assistant')).toHaveLength(1)
  })

  it('renders Markdown progressively while streaming', async () => {
    const release = deferred()
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: '**Bold**' })
        await release.promise
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'server-assistant',
          user_message_id: 'server-user',
          provider: 'test-provider',
          model: 'test-model',
        })
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Hello assistant')

    const bold = await screen.findByText('Bold')
    expect(bold.tagName).toBe('STRONG')

    await act(async () => {
      release.resolve()
    })
  })

  it('replaces streamed unsupported content with the verified final answer', async () => {
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: 'You have a 3pm meeting.' })
        onEvent({
          type: 'replace',
          content: "I can't verify personal Calendar events in a general response.",
        })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'server-assistant',
          user_message_id: 'server-user',
          provider: 'test-provider',
          model: 'test-model',
        })
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Plan my day')

    await waitFor(() => {
      expect(
        screen.getByText(
          "I can't verify personal Calendar events in a general response.",
        ),
      ).toBeInTheDocument()
    })
    expect(screen.queryByText('You have a 3pm meeting.')).not.toBeInTheDocument()
  })

  it('stops the generating indicator on completion', async () => {
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'status', message: 'Checking your calendar...' })
        onEvent({ type: 'token', content: 'Done.' })
        onEvent({
          type: 'done',
          conversation_id: 'conversation-a',
          message_id: 'server-assistant',
          user_message_id: 'server-user',
          provider: 'test-provider',
          model: 'test-model',
        })
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Hello assistant')

    await waitFor(() => {
      expect(screen.queryByText('Checking your calendar...'))
        .not.toBeInTheDocument()
    })
    expect(screen.queryByTestId('assistant-typing-indicator'))
      .not.toBeInTheDocument()
  })

  it('displays a partial error without duplicating the message', async () => {
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: 'Partial answer' })
        const err = new Error('The response was interrupted.')
        err.partial = true
        throw err
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Hello assistant')

    expect(await screen.findByText('Partial answer')).toBeInTheDocument()
    expect(
      await screen.findByText('The response was interrupted.')
    ).toBeInTheDocument()
    expect(screen.getAllByText('Partial answer')).toHaveLength(1)
  })

  it('stops an in-flight stream without duplicating the partial answer', async () => {
    conversationMocks.streamChatMessage.mockImplementation(
      (convId, content, key, connId, tz, onEvent, signal) => {
        onEvent({ type: 'token', content: 'Partial answer' })
        return new Promise((resolve, reject) => {
          signal.addEventListener('abort', () => {
            const err = new Error('Aborted')
            err.name = 'AbortError'
            reject(err)
          })
        })
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('Hello assistant')

    expect(await screen.findByText('Partial answer')).toBeInTheDocument()
    fireEvent.click(
      screen.getByRole('button', { name: 'Stop generating' }),
    )

    expect(
      await screen.findByText('Response stopped before it finished.')
    ).toBeInTheDocument()
    expect(screen.getAllByText('Partial answer')).toHaveLength(1)
    expect(
      screen.queryByRole('button', { name: 'Stop generating' }),
    ).not.toBeInTheDocument()
  })

  it('keeps conversation continuity across streamed turns', async () => {    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: `reply to ${content}` })
        onEvent({
          type: 'done',
          conversation_id: convId,
          message_id: `server-assistant-${content}`,
          user_message_id: `server-user-${content}`,
          provider: 'test-provider',
          model: 'test-model',
        })
      }
    )

    renderAssistant()
    await screen.findByText('conversation-a-private-message')
    await submitMessage('first message')

    await waitFor(() => {
      expect(conversationMocks.streamChatMessage).toHaveBeenCalledTimes(1)
    })

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Send message' }),
      ).toBeInTheDocument()
    })

    await submitMessage('second message')

    await waitFor(() => {
      expect(conversationMocks.streamChatMessage).toHaveBeenCalledTimes(2)
    })
    const secondCall = conversationMocks.streamChatMessage.mock.calls[1]
    expect(secondCall[0]).toBe('conversation-a')
  })
})

describe('Assistant retry idempotency', () => {
  beforeEach(() => {
    authMock.user = {
      id: 'user-a',
      user_metadata: { timezone: 'UTC' },
    }
    vi.clearAllMocks()
    HTMLElement.prototype.scrollIntoView = vi.fn()
    conversationMocks.getConversations.mockResolvedValue([
      conversation('conversation-a'),
    ])
    conversationMocks.getMessages.mockResolvedValue([
      {
        id: 'conversation-a-message',
        conversation_id: 'conversation-a',
        role: 'assistant',
        content: 'conversation-a-private-message',
      },
    ])
    conversationMocks.createConversation.mockResolvedValue(
      conversation('created-conversation'),
    )
    conversationMocks.getChatRequestStatus.mockResolvedValue({
      status: 'processing',
    })
  })

  function keys() {
    return conversationMocks.streamChatMessage.mock.calls.map((call) => call[2])
  }

  function contents() {
    return conversationMocks.streamChatMessage.mock.calls.map((call) => call[1])
  }

  function doneEvent() {
    return {
      type: 'done',
      conversation_id: 'conversation-a',
      message_id: 'server-assistant',
      user_message_id: 'server-user',
      provider: 'test-provider',
      model: 'test-model',
    }
  }

  function successfulStream(text = 'Recovered answer') {
    return async (convId, content, key, connId, tz, onEvent) => {
      onEvent({ type: 'token', content: text })
      onEvent(doneEvent())
    }
  }

  async function ready() {
    renderAssistant()
    await screen.findByText('conversation-a-private-message')
  }

  function submit(text) {
    fireEvent.change(
      screen.getByPlaceholderText('Message your personal AI...'),
      { target: { value: text } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  }

  it('generates a fresh key for each genuinely new message', async () => {
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: `reply to ${content}` })
        onEvent(doneEvent())
      },
    )

    await ready()
    await submit('first message')
    await waitFor(() => expect(keys()).toHaveLength(1))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Send message' }))
        .toBeInTheDocument()
    })

    await submit('second message')
    await waitFor(() => expect(keys()).toHaveLength(2))

    expect(keys()[0]).not.toBe(keys()[1])
    expect(contents()).toEqual(['first message', 'second message'])
  })

  it('uses a fresh key after a confirmed terminal failure (screenshot scenario)', async () => {
    const staleError =
      'This chat request previously failed. Use a new Idempotency-Key for a new attempt.'
    conversationMocks.streamChatMessage
      .mockRejectedValueOnce(
        new Error('The assistant is temporarily unavailable. Please try again.'),
      )
      .mockImplementationOnce(successfulStream())
    conversationMocks.getChatRequestStatus.mockResolvedValue({
      status: 'failed',
      error_message: staleError,
    })

    await ready()
    await submit('Preserve this exact message!')

    expect(
      await screen.findByText(/previous request failed/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(staleError)).not.toBeInTheDocument()
    expect(
      screen.getByText(/Your message is still in the input/i),
    ).toBeInTheDocument()
    expect(
      screen.getByPlaceholderText('Message your personal AI...'),
    ).toHaveValue('Preserve this exact message!')
    expect(keys()).toHaveLength(1)
    expect(conversationMocks.getChatRequestStatus).toHaveBeenCalledWith(
      'conversation-a',
      keys()[0],
    )

    await submit('Preserve this exact message!')
    await waitFor(() => expect(keys()).toHaveLength(2))

    expect(keys()[1]).not.toBe(keys()[0])
    expect(contents()).toEqual([
      'Preserve this exact message!',
      'Preserve this exact message!',
    ])
    expect(await screen.findByText('Recovered answer')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.queryByText(/previous request failed/i))
        .not.toBeInTheDocument()
    })
  })

  it('rotates the key when the 409 error itself confirms terminal failure', async () => {
    conversationMocks.streamChatMessage
      .mockRejectedValueOnce(Object.assign(
        new Error(
          'This chat request previously failed. '
          + 'Use a new Idempotency-Key for a new attempt.',
        ),
        { status: 409 },
      ))
      .mockImplementationOnce(successfulStream())

    await ready()
    await submit('Failed key message')

    expect(
      await screen.findByText(/previous request failed/i),
    ).toBeInTheDocument()
    expect(
      conversationMocks.getChatRequestStatus,
    ).not.toHaveBeenCalled()

    await submit('Failed key message')
    await waitFor(() => expect(keys()).toHaveLength(2))
    expect(keys()[1]).not.toBe(keys()[0])
  })

  it('reuses the original key after an uncertain network failure', async () => {
    conversationMocks.streamChatMessage
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockImplementationOnce(successfulStream())
    conversationMocks.getChatRequestStatus.mockResolvedValue({
      status: 'processing',
    })

    await ready()
    await submit('Network retry message')

    expect(await screen.findByText('Failed to fetch')).toBeInTheDocument()
    expect(
      screen.getByPlaceholderText('Message your personal AI...'),
    ).toHaveValue('Network retry message')

    await submit('Network retry message')
    await waitFor(() => expect(keys()).toHaveLength(2))
    expect(keys()[1]).toBe(keys()[0])
    expect(conversationMocks.getChatRequestStatus).toHaveBeenCalledWith(
      'conversation-a',
      keys()[0],
    )
  })

  it('keeps the key when terminal failure cannot be confirmed', async () => {
    conversationMocks.streamChatMessage
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockImplementationOnce(successfulStream())
    conversationMocks.getChatRequestStatus.mockRejectedValue(
      new Error('status unavailable'),
    )

    await ready()
    await submit('Uncertain message')
    expect(await screen.findByText('Failed to fetch')).toBeInTheDocument()

    await submit('Uncertain message')
    await waitFor(() => expect(keys()).toHaveLength(2))
    expect(keys()[1]).toBe(keys()[0])
  })

  it('does not create a second attempt from a double-click', async () => {
    const pendingSend = deferred()
    conversationMocks.streamChatMessage.mockImplementation(
      () => pendingSend.promise,
    )

    await ready()
    const sendButton = screen.getByRole('button', { name: 'Send message' })
    fireEvent.change(
      screen.getByPlaceholderText('Message your personal AI...'),
      { target: { value: 'Only once' } },
    )
    fireEvent.click(sendButton)
    fireEvent.click(sendButton)

    await waitFor(() => expect(keys()).toHaveLength(1))
    expect(screen.getByRole('button', { name: 'Send message' }))
      .toBeDisabled()
    expect(
      screen.getByRole('button', { name: 'Stop generating' }),
    ).toBeInTheDocument()
    await act(async () => pendingSend.resolve())
  })

  it('does not resubmit a completed request', async () => {
    conversationMocks.streamChatMessage.mockImplementation(successfulStream())

    await ready()
    await submit('Completed message')
    await waitFor(() => expect(keys()).toHaveLength(1))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Send message' }))
        .toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    await act(async () => {})
    expect(conversationMocks.streamChatMessage).toHaveBeenCalledOnce()
    expect(
      screen.getByPlaceholderText('Message your personal AI...'),
    ).toHaveValue('')
  })

  it('keeps the key after a stopped stream but does not duplicate messages on resubmission', async () => {
    conversationMocks.streamChatMessage
      .mockImplementationOnce(
        (convId, content, key, connId, tz, onEvent, signal) => {
          onEvent({ type: 'token', content: 'Partial answer' })
          return new Promise((resolve, reject) => {
            signal.addEventListener('abort', () => {
              const err = new Error('Aborted')
              err.name = 'AbortError'
              reject(err)
            })
          })
        },
      )
      .mockImplementationOnce(successfulStream('Recovered answer'))
    conversationMocks.getChatRequestStatus.mockResolvedValue({
      status: 'processing',
    })
    conversationMocks.getMessages
      .mockResolvedValueOnce([
        {
          id: 'conversation-a-message',
          conversation_id: 'conversation-a',
          role: 'assistant',
          content: 'conversation-a-private-message',
        },
      ])
      .mockResolvedValue([
        {
          id: 'conversation-a-message',
          conversation_id: 'conversation-a',
          role: 'assistant',
          content: 'conversation-a-private-message',
        },
        {
          id: 'server-user',
          conversation_id: 'conversation-a',
          role: 'user',
          content: 'Stopped message',
        },
        {
          id: 'server-assistant',
          conversation_id: 'conversation-a',
          role: 'assistant',
          content: 'Recovered answer',
        },
      ])

    await ready()
    await submit('Stopped message')
    expect(await screen.findByText('Partial answer')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Stop generating' }))

    expect(
      await screen.findByText('Response stopped before it finished.'),
    ).toBeInTheDocument()
    expect(
      await screen.findByText(
        'This response was cancelled before email summaries were generated.',
      ),
    ).toBeInTheDocument()

    await submit('Stopped message')
    await waitFor(() => expect(keys()).toHaveLength(2))
    expect(keys()[1]).toBe(keys()[0])

    await waitFor(() => {
      expect(
        document.querySelectorAll('.assistant-markdown'),
      ).toHaveLength(2)
    })
    expect(screen.getAllByText('Stopped message')).toHaveLength(1)
  })
})

describe('Assistant long-response completion', () => {
  beforeEach(() => {
    authMock.user = {
      id: 'user-a',
      user_metadata: { timezone: 'Asia/Kolkata' },
    }
    vi.clearAllMocks()
    HTMLElement.prototype.scrollIntoView = vi.fn()
    conversationMocks.getConversations.mockResolvedValue([
      conversation('conversation-a'),
    ])
    conversationMocks.createConversation.mockResolvedValue(
      conversation('created-conversation'),
    )
  })

  function initialMessages() {
    return [
      {
        id: 'conversation-a-message',
        conversation_id: 'conversation-a',
        role: 'assistant',
        content: 'conversation-a-private-message',
      },
    ]
  }

  function finalMessages(content, completion) {
    return [
      ...initialMessages(),
      {
        id: 'server-user',
        conversation_id: 'conversation-a',
        role: 'user',
        content: 'Summarize all my emails today.',
      },
      {
        id: 'server-assistant',
        conversation_id: 'conversation-a',
        role: 'assistant',
        content,
        metadata: completion ? { response_completion: completion } : {},
      },
    ]
  }

  function mockPersistedMessages(content, completion) {
    conversationMocks.getMessages
      .mockResolvedValueOnce(initialMessages())
      .mockResolvedValue(finalMessages(content, completion))
  }

  function doneEvent(completion) {
    return {
      type: 'done',
      conversation_id: 'conversation-a',
      message_id: 'server-assistant',
      user_message_id: 'server-user',
      provider: 'test-provider',
      model: 'test-model',
      ...(completion ? { completion } : {}),
    }
  }

  async function ready() {
    renderAssistant()
    await screen.findByText('conversation-a-private-message')
  }

  function submit(text = 'Summarize all my emails today.') {
    fireEvent.change(
      screen.getByPlaceholderText('Message your personal AI...'),
      { target: { value: text } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  }

  it('shows an accurate partial notice from done.completion', async () => {
    const completion = {
      status: 'partial',
      requested_item_count: 30,
      processed_item_count: 30,
      remaining_item_count: 5,
      output_limited: false,
    }
    mockPersistedMessages('#### 1. Subject m1', completion)
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: '#### 1. Subject m1' })
        onEvent(doneEvent(completion))
      },
    )

    await ready()
    await submit()

    expect(
      await screen.findByText(
        'This response is partial. 30 of 30 requested items were processed; 5 items remain.',
      ),
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/may be incomplete because the AI reached its output limit/i),
    ).not.toBeInTheDocument()
  })

  it.each([
    [
      {
        status: 'failed',
        requested_item_count: 3,
        processed_item_count: 0,
        failed_item_count: 3,
      },
      'This response could not be completed. No email details were verified.',
    ],
    [
      {
        status: 'cancelled',
        requested_item_count: 50,
        processed_item_count: 0,
      },
      'This response was cancelled before email summaries were generated.',
    ],
    [
      {
        status: 'truncated',
        finish_reason: 'length',
      },
      'The AI reached its output limit. This answer contains only the verified content shown above; narrow the request for a smaller result.',
    ],
    [
      {
        status: 'unknown',
        finish_reason: 'content_filter',
      },
      'This response may be incomplete; its completion status could not be verified.',
    ],
  ])('shows the correct notice for %j', async (completion, expectedNotice) => {
    mockPersistedMessages('Verified content', completion)
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: 'Verified content' })
        onEvent(doneEvent(completion))
      },
    )

    await ready()
    await submit()

    expect(await screen.findByText(expectedNotice)).toBeInTheDocument()
  })

  it('shows both coverage and output-limit notices when a batch is capped', async () => {
    const completion = {
      status: 'partial',
      requested_item_count: 50,
      processed_item_count: 45,
      remaining_item_count: 5,
      output_limited: true,
      finish_reason: 'length',
    }
    mockPersistedMessages('#### 1. Subject m1', completion)
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: '#### 1. Subject m1' })
        onEvent(doneEvent(completion))
      },
    )

    await ready()
    await submit()

    expect(
      await screen.findByText(
        'This response is partial. 45 of 50 requested items were processed; 5 items remain.',
      ),
    ).toBeInTheDocument()
    expect(
      await screen.findByText(
        'The AI reached its output limit. This answer contains only the verified content shown above; narrow the request for a smaller result.',
      ),
    ).toBeInTheDocument()
  })

  it('does not show a false warning for a complete response', async () => {
    const completion = {
      status: 'complete',
      requested_item_count: 5,
      processed_item_count: 5,
      remaining_item_count: 0,
      output_limited: false,
    }
    mockPersistedMessages('#### 1. Subject m1', completion)
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: '#### 1. Subject m1' })
        onEvent(doneEvent(completion))
      },
    )

    await ready()
    await submit()

    await waitFor(() => {
      expect(screen.getByText(/Subject m1/)).toBeInTheDocument()
    })
    expect(screen.queryByText(/this response is partial/i))
      .not.toBeInTheDocument()
    expect(screen.queryByText(/reached its output limit/i))
      .not.toBeInTheDocument()
    expect(screen.queryByText(/could not be completed/i))
      .not.toBeInTheDocument()
    expect(screen.queryByText(/was cancelled/i)).not.toBeInTheDocument()
  })

  it('keeps batch progress visible after content starts and clears it on done', async () => {
    const secondBatchGate = deferred()
    const completionGate = deferred()
    const completion = {
      status: 'complete',
      requested_item_count: 50,
      processed_item_count: 50,
      remaining_item_count: 0,
    }
    mockPersistedMessages('## Email Summary', completion)
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'status', message: 'Processing email batch 1 of 10...' })
        onEvent({ type: 'token', content: '## Email Summary' })
        onEvent({ type: 'status', message: 'Processing email batch 2 of 10...' })
        await secondBatchGate.promise
        onEvent({ type: 'status', message: 'Processing email batch 3 of 10...' })
        await completionGate.promise
        onEvent(doneEvent(completion))
      },
    )

    await ready()
    await submit()

    expect(
      await screen.findByText('Processing email batch 2 of 10...'),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId('assistant-typing-indicator'),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByText('Processing email batch 1 of 10...'),
    ).not.toBeInTheDocument()

    await act(async () => {
      secondBatchGate.resolve()
    })
    expect(
      await screen.findByText('Processing email batch 3 of 10...'),
    ).toBeInTheDocument()

    await act(async () => {
      completionGate.resolve()
    })
    await waitFor(() => {
      expect(
        screen.queryByText('Processing email batch 3 of 10...'),
      ).not.toBeInTheDocument()
    })
  })

  it('keeps post-token progress visible until the next progress or done event', async () => {
    const completionGate = deferred()
    const completion = { status: 'complete' }
    mockPersistedMessages('## Email Summary', completion)
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'status', message: 'Processing email batch 1 of 3...' })
        onEvent({ type: 'token', content: '## Email Summary' })
        onEvent({ type: 'status', message: 'Processing email batch 2 of 3...' })
        onEvent({ type: 'token', content: '\n\n#### 1. Subject m1' })
        await completionGate.promise
        onEvent(doneEvent(completion))
      },
    )

    await ready()
    await submit()

    expect(
      await screen.findByText('Processing email batch 2 of 3...'),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('Processing email batch 1 of 3...'),
    ).not.toBeInTheDocument()

    await act(async () => {
      completionGate.resolve()
    })
    await waitFor(() => {
      expect(
        screen.queryByText('Processing email batch 2 of 3...'),
      ).not.toBeInTheDocument()
    })
  })

  it('does not render an empty assistant bubble for a done-only replay', async () => {
    const persistedGate = deferred()
    const completion = { status: 'complete' }
    conversationMocks.getMessages
      .mockResolvedValueOnce(initialMessages())
      .mockImplementationOnce(async () => {
        await persistedGate.promise
        return finalMessages('Saved answer', completion)
      })
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent(doneEvent(completion))
      },
    )

    await ready()
    await submit()

    await waitFor(() => {
      expect(document.querySelectorAll('.assistant-markdown')).toHaveLength(1)
    })
    expect(screen.queryByText('Saved answer')).not.toBeInTheDocument()

    await act(async () => {
      persistedGate.resolve()
    })
    expect(await screen.findByText('Saved answer')).toBeInTheDocument()
    expect(document.querySelectorAll('.assistant-markdown')).toHaveLength(2)
  })

  it('replaces raw batch output with the final answer and keeps completion state', async () => {
    const completion = {
      status: 'partial',
      requested_item_count: 30,
      processed_item_count: 30,
      remaining_item_count: 5,
    }
    mockPersistedMessages('#### 1. Subject m1', completion)
    conversationMocks.streamChatMessage.mockImplementation(
      async (convId, content, key, connId, tz, onEvent) => {
        onEvent({ type: 'token', content: '{"summaries":[{"message_id":"m1"}]}' })
        onEvent({
          type: 'replace',
          content: '#### 1. Subject m1',
        })
        onEvent(doneEvent(completion))
      },
    )

    await ready()
    await submit()

    expect(await screen.findByText(/Subject m1/)).toBeInTheDocument()
    expect(
      screen.queryByText('{"summaries":[{"message_id":"m1"}]}'),
    ).not.toBeInTheDocument()
    expect(
      await screen.findByText(/this response is partial/i),
    ).toBeInTheDocument()
  })
})
