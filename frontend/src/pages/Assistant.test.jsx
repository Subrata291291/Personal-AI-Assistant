import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
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
  sendChatMessage: vi.fn(),
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
    conversationMocks.sendChatMessage.mockReturnValue(pendingSend.promise)
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
      expect(conversationMocks.sendChatMessage).toHaveBeenCalledOnce()
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
      pendingSend.resolve({
        conversation_id: 'conversation-a',
        user_message: {
          id: 'a-sent-user',
          conversation_id: 'conversation-a',
          role: 'user',
          content: 'A private draft',
        },
        assistant_message: {
          id: 'a-sent-assistant',
          conversation_id: 'conversation-a',
          role: 'assistant',
          content: 'A private response',
        },
        provider: 'test',
        model: 'test',
      })
    })

    expect(screen.getByPlaceholderText('Message your personal AI...'))
      .toHaveValue('B private draft')
    expect(screen.queryByText('A private response')).not.toBeInTheDocument()
    expect(screen.queryByText('A private draft')).not.toBeInTheDocument()
    expect(conversationMocks.sendChatMessage).toHaveBeenCalledOnce()
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
    conversationMocks.sendChatMessage.mockResolvedValue({
      conversation_id: 'conversation-a',
      user_message: {
        id: 'sent-user',
        conversation_id: 'conversation-a',
        role: 'user',
        content: 'Hello assistant',
      },
      assistant_message: {
        id: 'sent-assistant',
        conversation_id: 'conversation-a',
        role: 'assistant',
        content: 'Hello from the assistant',
      },
      provider: 'test-provider',
      model: 'test-model',
    })
    renderAssistant()

    await screen.findByText('conversation-a-private-message')
    fireEvent.change(
      screen.getByPlaceholderText('Message your personal AI...'),
      { target: { value: 'Hello assistant' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))

    expect(await screen.findByText('Hello from the assistant'))
      .toBeInTheDocument()
    expect(conversationMocks.sendChatMessage).toHaveBeenCalledWith(
      'conversation-a',
      'Hello assistant',
      expect.any(String),
      null,
      'America/New_York',
    )
  })

  it('uses Asia/Kolkata when the user has no configured timezone', async () => {
    authMock.user = { id: 'user-a' }
    conversationMocks.sendChatMessage.mockResolvedValue({
      conversation_id: 'conversation-a',
      user_message: {
        id: 'sent-user',
        conversation_id: 'conversation-a',
        role: 'user',
        content: 'Show my calendar today',
      },
      assistant_message: {
        id: 'sent-assistant',
        conversation_id: 'conversation-a',
        role: 'assistant',
        content: 'No events.',
      },
      provider: 'test-provider',
      model: 'test-model',
    })
    renderAssistant()

    await screen.findByText('conversation-a-private-message')
    fireEvent.change(
      screen.getByPlaceholderText('Message your personal AI...'),
      { target: { value: 'Show my calendar today' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))

    await waitFor(() => {
      expect(conversationMocks.sendChatMessage).toHaveBeenCalledWith(
        'conversation-a',
        'Show my calendar today',
        expect.any(String),
        null,
        'Asia/Kolkata',
      )
    })
  })
})
