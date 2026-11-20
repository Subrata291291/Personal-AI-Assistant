import { QueryClient, useQuery } from '@tanstack/react-query'
import {
  persistQueryClientRestore,
  PersistQueryClientProvider,
} from '@tanstack/react-query-persist-client'
import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearAllPersistedQueryCaches,
  clearPersistedQueryCache,
  createCacheBuster,
  createPersistenceOptions,
  createQueryPersister,
  readPersistedCache,
  restorePersistedQueryCache,
  savePersistedQueryCache,
  shouldPersistQuery,
  writePersistedCache,
} from './queryPersistence'


const USER_A = 'user-a'
const USER_B = 'user-b'
const CONVERSATIONS = ['conversations', USER_A]

function makeClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 60 * 1000,
        gcTime: 30 * 60 * 1000,
        retry: false,
      },
      mutations: { retry: false },
    },
  })
}

function Probe({ queryKey, queryFn }) {
  const { data } = useQuery({ queryKey, queryFn })
  return <div>{data?.[0]?.id ?? 'loading'}</div>
}

describe('persistent TanStack Query cache', () => {
  beforeEach(async () => {
    await clearAllPersistedQueryCaches()
  })

  it('restores cached data into a new QueryClient (simulated reload)', async () => {
    const first = makeClient()
    const conversations = [{ id: 'cached-conversation' }]
    first.setQueryData(CONVERSATIONS, conversations, { updatedAt: Date.now() })

    await savePersistedQueryCache(first, USER_A)
    expect(await readPersistedCache(USER_A)).toContain('cached-conversation')

    const reloaded = makeClient()
    await restorePersistedQueryCache(reloaded, USER_A)

    expect(reloaded.getQueryData(CONVERSATIONS)).toEqual(conversations)
  })

  it('renders fresh cached data before a network response and skips the fetch', async () => {
    const client = makeClient()
    client.setQueryData(
      CONVERSATIONS,
      [{ id: 'cached-conversation' }],
      { updatedAt: Date.now() },
    )
    await savePersistedQueryCache(client, USER_A)
    const queryFn = vi.fn(() => new Promise(() => {}))

    render(
      <PersistQueryClientProvider
        client={makeClient()}
        persistOptions={createPersistenceOptions(USER_A)}
      >
        <Probe queryKey={CONVERSATIONS} queryFn={queryFn} />
      </PersistQueryClientProvider>,
    )

    expect(await screen.findByText('cached-conversation')).toBeInTheDocument()
    expect(queryFn).not.toHaveBeenCalled()
  })

  it('shows stale cached data immediately and refreshes in the background', async () => {
    const client = makeClient()
    client.setQueryData(
      CONVERSATIONS,
      [{ id: 'stale-conversation' }],
      { updatedAt: Date.now() - 10 * 60 * 1000 },
    )
    await savePersistedQueryCache(client, USER_A)
    const queryFn = vi.fn(async () => [{ id: 'fresh-conversation' }])

    render(
      <PersistQueryClientProvider
        client={makeClient()}
        persistOptions={createPersistenceOptions(USER_A)}
      >
        <Probe queryKey={CONVERSATIONS} queryFn={queryFn} />
      </PersistQueryClientProvider>,
    )

    expect(await screen.findByText('stale-conversation')).toBeInTheDocument()
    await waitFor(() => expect(queryFn).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('fresh-conversation')).toBeInTheDocument()
  })

  it('excludes unsafe queries, mutations, errors and credentials', async () => {
    const client = makeClient()
    client.setQueryData(
      CONVERSATIONS,
      [{ id: 'safe-conversation', access_token: 'SUPER_SECRET' }],
      { updatedAt: Date.now() },
    )
    client.setQueryData(
      ['auth-token', USER_A],
      { access_token: 'AUTH_SECRET' },
      { updatedAt: Date.now() },
    )
    client.setQueryData(
      ['conversations', USER_B],
      [{ id: 'other-user-data' }],
      { updatedAt: Date.now() },
    )

    await savePersistedQueryCache(client, USER_A)
    const raw = await readPersistedCache(USER_A)

    expect(raw).toContain('safe-conversation')
    expect(raw).not.toContain('SUPER_SECRET')
    expect(raw).not.toContain('AUTH_SECRET')
    expect(raw).not.toContain('auth-token')
    expect(raw).not.toContain('other-user-data')
    expect(
      shouldPersistQuery(
        {
          queryKey: CONVERSATIONS,
          state: { status: 'error', fetchStatus: 'idle', data: undefined },
        },
        USER_A,
      ),
    ).toBe(false)
    expect(
      createPersistenceOptions(USER_A).dehydrateOptions.shouldDehydrateMutation({}),
    ).toBe(false)
  })

  it('clears private cache on logout and isolates account switches', async () => {
    const client = makeClient()
    client.setQueryData(CONVERSATIONS, [{ id: 'user-a-data' }], {
      updatedAt: Date.now(),
    })
    await savePersistedQueryCache(client, USER_A)

    const switched = makeClient()
    await restorePersistedQueryCache(switched, USER_B)
    expect(switched.getQueryData(CONVERSATIONS)).toBeUndefined()

    await clearPersistedQueryCache(USER_A)
    const signedOut = makeClient()
    await restorePersistedQueryCache(signedOut, USER_A)
    expect(signedOut.getQueryData(CONVERSATIONS)).toBeUndefined()
    expect(await readPersistedCache(USER_A)).toBeUndefined()
  })

  it('discards expired caches and incompatible schema versions', async () => {
    const client = makeClient()
    client.setQueryData(CONVERSATIONS, [{ id: 'expired-data' }], {
      updatedAt: Date.now(),
    })
    await savePersistedQueryCache(client, USER_A)

    const raw = JSON.parse(await readPersistedCache(USER_A))
    raw.timestamp = Date.now() - (13 * 60 * 60 * 1000)
    await writePersistedCache(USER_A, JSON.stringify(raw))

    const expiredClient = makeClient()
    await restorePersistedQueryCache(expiredClient, USER_A)
    expect(expiredClient.getQueryData(CONVERSATIONS)).toBeUndefined()

    await savePersistedQueryCache(client, USER_A)
    const bustedClient = makeClient()
    await persistQueryClientRestore({
      queryClient: bustedClient,
      persister: createQueryPersister(USER_A),
      buster: 'v0:previous-schema',
      maxAge: 12 * 60 * 60 * 1000,
    })
    expect(bustedClient.getQueryData(CONVERSATIONS)).toBeUndefined()
  })

  it('keeps connection-scoped data separate and never resurrects deleted events', async () => {
    const connectionOne = '11111111-1111-1111-1111-111111111111'
    const connectionTwo = '22222222-2222-2222-2222-222222222222'
    const calendarKey = (connectionId) => [
      'calendar-events',
      USER_A,
      connectionId,
      'Asia/Kolkata',
      'month',
      'primary',
      '2026-11-01',
      '2026-11-30',
      '',
    ]
    const client = makeClient()
    client.setQueryData(
      calendarKey(connectionOne),
      {
        pages: [{
          events: [
            { id: 'deleted-event', title: 'Standup' },
            { id: 'kept-event', title: 'Review' },
          ],
        }],
        pageParams: [null],
      },
      { updatedAt: Date.now() },
    )
    client.setQueryData(
      calendarKey(connectionTwo),
      {
        pages: [{ events: [{ id: 'other-connection-event' }] }],
        pageParams: [null],
      },
      { updatedAt: Date.now() },
    )
    await savePersistedQueryCache(client, USER_A)

    const reloaded = makeClient()
    await restorePersistedQueryCache(reloaded, USER_A)
    expect(reloaded.getQueryData(calendarKey(connectionOne)).pages[0].events)
      .toHaveLength(2)
    expect(reloaded.getQueryData(calendarKey(connectionTwo)).pages[0].events[0].id)
      .toBe('other-connection-event')

    // Mirror Phase 16: patch the exact identity in every cached view, then persist.
    await act(async () => {
      reloaded.setQueryData(calendarKey(connectionOne), (old) => ({
        ...old,
        pages: old.pages.map((page) => ({
          ...page,
          events: page.events.filter((event) => event.id !== 'deleted-event'),
        })),
      }))
    })
    await savePersistedQueryCache(reloaded, USER_A)

    const afterReload = makeClient()
    await restorePersistedQueryCache(afterReload, USER_A)
    const events = afterReload.getQueryData(calendarKey(connectionOne)).pages[0].events
    expect(events.map((event) => event.id)).toEqual(['kept-event'])
    expect(
      afterReload.getQueryData(calendarKey(connectionTwo)).pages[0].events[0].id,
    ).toBe('other-connection-event')
  })

  it('uses a versioned, user-scoped buster', () => {
    expect(createCacheBuster(USER_A)).not.toBe(createCacheBuster(USER_B))
    expect(createCacheBuster(USER_A)).toContain('v1:')
  })

  it('uses an no-op persister while signed out', async () => {
    const persister = createQueryPersister(null)
    expect(await persister.restoreClient()).toBeUndefined()
    await expect(persister.persistClient({})).resolves.toBeUndefined()
    await expect(persister.removeClient()).resolves.toBeUndefined()
  })
})
