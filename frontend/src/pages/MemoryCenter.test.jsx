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
  authRevision: 0,
}))

const memoryMocks = vi.hoisted(() => ({
  createMemory: vi.fn(),
  deleteMemory: vi.fn(),
  getMemories: vi.fn(),
  updateMemory: vi.fn(),
}))

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: authMock.user }),
}))

vi.mock('../services/memoryService', () => ({
  memoryService: memoryMocks,
}))

vi.mock('../lib/api.js', () => ({
  api: apiMock,
  getApiAuthRevision: () => authMock.authRevision,
}))

import MemoryCenter from './MemoryCenter.jsx'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function memory(id, content) {
  return {
    id,
    memory_type: 'long_term',
    content,
    importance: 5,
    confidence: 0.9,
    created_at: '2026-11-06T00:00:00Z',
  }
}

function tree(queryClient) {
  function Layout() {
    return (
      <Outlet context={{ isDark: false }} />
    )
  }

  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/memory']}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/memory" element={<MemoryCenter />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

function renderMemoryCenter() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return {
    ...render(tree(queryClient)),
    queryClient,
  }
}

describe('Memory Center user-switch isolation', () => {
  beforeEach(() => {
    authMock.user = { id: 'user-a' }
    authMock.authRevision = 0
    vi.clearAllMocks()
    memoryMocks.getMemories.mockImplementation(async () =>
      authMock.user.id === 'user-a'
        ? [memory('memory-a', 'A private memory')]
        : [memory('memory-b', 'B private memory')])
    memoryMocks.updateMemory.mockImplementation(async (id, payload) => ({
      ...memory(id, payload.content),
      ...payload,
    }))
    apiMock.get.mockImplementation(async (path) => {
      if (path === '/api/v1/me/settings') {
        return { memory_enabled: true }
      }
      return []
    })
    apiMock.patch.mockResolvedValue({ memory_enabled: true })
    apiMock.post.mockResolvedValue({})
  })

  it('resets A edit state and ignores its pending save after switching to B', async () => {
    const pendingUpdate = deferred()
    memoryMocks.updateMemory.mockReturnValue(pendingUpdate.promise)
    const view = renderMemoryCenter()

    expect(await screen.findByText('A private memory')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const contentField = screen.getByPlaceholderText(
      'What should the assistant remember?',
    )
    fireEvent.change(contentField, {
      target: { value: 'A unsaved private edit' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save Memory' }))
    await waitFor(() => {
      expect(memoryMocks.updateMemory).toHaveBeenCalledWith(
        'memory-a',
        expect.objectContaining({ content: 'A unsaved private edit' }),
        { userId: 'user-a', authRevision: 0 },
      )
    })

    authMock.user = { id: 'user-b' }
    authMock.authRevision += 1
    view.rerender(tree(view.queryClient))

    expect(await screen.findByText('B private memory')).toBeInTheDocument()
    const userBMemories = view.queryClient.getQueryData([
      'memories',
      'user-b',
      'all',
    ])
    expect(screen.queryByText('A private memory')).not.toBeInTheDocument()
    expect(screen.queryByText('A unsaved private edit'))
      .not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Edit Memory' }))
      .not.toBeInTheDocument()

    await act(async () => {
      pendingUpdate.resolve(memory('memory-a', 'A saved private edit'))
    })

    expect(screen.getByText('B private memory')).toBeInTheDocument()
    expect(view.queryClient.getQueryData([
      'memories',
      'user-b',
      'all',
    ])).toEqual(userBMemories)
    expect(screen.queryByText('A saved private edit')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Edit Memory' }))
      .not.toBeInTheDocument()
  })

  it('continues to edit and save memory for the same user', async () => {
    const view = renderMemoryCenter()

    expect(await screen.findByText('A private memory')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByPlaceholderText(
      'What should the assistant remember?',
    ), {
      target: { value: 'A saved edit' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save Memory' }))

    await waitFor(() => {
      expect(memoryMocks.updateMemory).toHaveBeenCalledWith(
        'memory-a',
        expect.objectContaining({ content: 'A saved edit' }),
        { userId: 'user-a', authRevision: 0 },
      )
    })
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Edit Memory' }))
        .not.toBeInTheDocument()
    })
    expect(view.queryClient.getQueryData(['memories', 'user-a', 'all']))
      .toBeDefined()
  })

  it('does not send a settings mutation after switching while cancellation is pending', async () => {
    const cancellation = deferred()
    const view = renderMemoryCenter()

    await screen.findByRole('switch', { name: 'Automatic Memory' })
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: 'Automatic Memory' }))
        .not.toBeDisabled()
    })
    vi.spyOn(view.queryClient, 'cancelQueries')
      .mockReturnValue(cancellation.promise)

    fireEvent.click(screen.getByRole('switch', {
      name: 'Automatic Memory',
    }))

    expect(view.queryClient.cancelQueries).toHaveBeenCalled()
    authMock.user = { id: 'user-b' }
    authMock.authRevision += 1
    view.rerender(tree(view.queryClient))

    await act(async () => {
      cancellation.resolve()
      await cancellation.promise
    })

    await waitFor(() => {
      expect(apiMock.patch).not.toHaveBeenCalled()
    })
    expect(screen.getByRole('switch', { name: 'Automatic Memory' }))
      .toBeInTheDocument()
  })

  it('sends a same-user settings mutation with its initiating auth context', async () => {
    const view = renderMemoryCenter()

    await screen.findByRole('switch', { name: 'Automatic Memory' })
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: 'Automatic Memory' }))
        .not.toBeDisabled()
    })
    fireEvent.click(screen.getByRole('switch', {
      name: 'Automatic Memory',
    }))

    await waitFor(() => {
      expect(apiMock.patch).toHaveBeenCalledWith(
        '/api/v1/me/settings',
        { memory_enabled: false },
        { userId: 'user-a', authRevision: 0 },
      )
    })
    expect(view.queryClient.getQueryData(['user-settings', 'user-a']))
      .toMatchObject({ memory_enabled: true })
  })

  it('does not start a save mutation after switching before its mutation function runs', async () => {
    const view = renderMemoryCenter()

    expect(await screen.findByText('A private memory')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '+ Add Memory' }))
    fireEvent.change(screen.getByPlaceholderText(
      'What should the assistant remember?',
    ), {
      target: { value: 'A new memory' },
    })

    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Save Memory' }))
      authMock.user = { id: 'user-b' }
      authMock.authRevision += 1
      view.rerender(tree(view.queryClient))
    })

    await screen.findByText('B private memory')
    await waitFor(() => {
      expect(memoryMocks.createMemory).not.toHaveBeenCalled()
      expect(memoryMocks.updateMemory).not.toHaveBeenCalled()
    })
  })
})
