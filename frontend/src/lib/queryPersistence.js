import { createAsyncStoragePersister } from '@tanstack/query-async-storage-persister'
import {
  persistQueryClientRestore,
  persistQueryClientSave,
} from '@tanstack/react-query-persist-client'
import { clear, createStore, del, get, set } from 'idb-keyval'

const CACHE_SCHEMA_VERSION = 1
const PERSISTED_CACHE_MAX_AGE = 12 * 60 * 60 * 1000
const IDB_DATABASE = 'myorbit-query-cache'
const IDB_STORE = 'query-cache'

const PERSISTABLE_PREFIXES = new Set([
  'oauth-connections',
  'memory-reviews',
  'memories',
  'conversations',
  'conversation-messages',
  'user-settings',
  'gmail-messages',
  'gmail-message',
  'calendar-list',
  'calendar-events',
  'calendar-event',
  'contacts',
  'contact-detail',
])

const SENSITIVE_KEY = /^(?:access[_-]?token|refresh[_-]?token|id[_-]?token|token|password|passwd|secret|client[_-]?secret|api[_-]?key|apikey|authorization|credentials?|oauth[_-]?token|session|cookie)$/i

function supabaseProjectRef() {
  const url = import.meta.env.VITE_SUPABASE_URL
  if (!url) return 'unknown'
  try {
    return new URL(url).hostname.split('.')[0] || 'unknown'
  } catch {
    return 'unknown'
  }
}

const PROJECT_REF = supabaseProjectRef()
const idbStore = createStore(IDB_DATABASE, IDB_STORE)
const persisterByKey = new Map()

const noopPersister = {
  persistClient: async () => {},
  restoreClient: async () => undefined,
  removeClient: async () => {},
}

export function persistedCacheKey(userId) {
  return `myorbit-query-cache:${PROJECT_REF}:${userId}`
}

export function createCacheBuster(userId) {
  return `v${CACHE_SCHEMA_VERSION}:${PROJECT_REF}:${userId || 'anonymous'}`
}

export function shouldPersistQuery(query, userId) {
  if (!userId) return false

  const key = query?.queryKey
  if (!Array.isArray(key) || !PERSISTABLE_PREFIXES.has(key[0])) return false
  if (key[1] !== userId) return false

  const state = query.state || {}
  return (
    state.status === 'success' &&
    state.data !== undefined &&
    state.fetchStatus === 'idle'
  )
}

export function stripSensitiveValues(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object') return value
  if (seen.has(value)) return null
  seen.add(value)

  if (Array.isArray(value)) {
    return value.map((item) => stripSensitiveValues(item, seen))
  }

  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !SENSITIVE_KEY.test(key))
      .map(([key, item]) => [key, stripSensitiveValues(item, seen)]),
  )
}

function serializePersistedClient(client) {
  return JSON.stringify(stripSensitiveValues(client))
}

function deserializePersistedClient(raw) {
  const parsed = JSON.parse(raw)
  if (
    !parsed ||
    typeof parsed !== 'object' ||
    !parsed.clientState ||
    !Array.isArray(parsed.clientState.queries)
  ) {
    throw new Error('Invalid persisted query cache.')
  }
  return parsed
}

export function createQueryPersister(userId) {
  if (!userId) return noopPersister

  const key = persistedCacheKey(userId)
  if (persisterByKey.has(key)) return persisterByKey.get(key)

  const storage = {
    getItem: (storageKey) => get(storageKey, idbStore),
    setItem: (storageKey, value) => set(storageKey, value, idbStore),
    removeItem: (storageKey) => del(storageKey, idbStore),
  }

  const persister = createAsyncStoragePersister({
    storage,
    key,
    // Avoid a throttled window where a confirmed mutation (for example a
    // Calendar deletion) is patched in memory but a stale blob survives a fast
    // reload.
    throttleTime: 0,
    serialize: serializePersistedClient,
    deserialize: deserializePersistedClient,
    retry: ({ error }) => {
      console.warn('Unable to persist the query cache:', error)
      return undefined
    },
  })

  persisterByKey.set(key, persister)
  return persister
}

export function createPersistenceOptions(userId) {
  const normalizedUserId = userId || null
  return {
    persister: createQueryPersister(normalizedUserId),
    maxAge: PERSISTED_CACHE_MAX_AGE,
    buster: createCacheBuster(normalizedUserId),
    dehydrateOptions: {
      shouldDehydrateQuery: (query) => shouldPersistQuery(query, normalizedUserId),
      shouldDehydrateMutation: () => false,
    },
  }
}

export async function savePersistedQueryCache(queryClient, userId) {
  const { persister, buster, dehydrateOptions } = createPersistenceOptions(userId)
  await persistQueryClientSave({
    queryClient,
    persister,
    buster,
    dehydrateOptions,
  })
}

export async function restorePersistedQueryCache(queryClient, userId) {
  const { persister, maxAge, buster } = createPersistenceOptions(userId)
  await persistQueryClientRestore({
    queryClient,
    persister,
    maxAge,
    buster,
  })
}

export async function clearPersistedQueryCache(userId) {
  if (!userId) return
  await createQueryPersister(userId).removeClient()
}

export async function clearAllPersistedQueryCaches() {
  await clear(idbStore)
}

export async function readPersistedCache(userId) {
  if (!userId) return undefined
  try {
    return await get(persistedCacheKey(userId), idbStore)
  } catch {
    return undefined
  }
}

export async function writePersistedCache(userId, raw) {
  if (!userId) return
  await set(persistedCacheKey(userId), raw, idbStore)
}

export function pruneForeignQueries(queryClient, userId) {
  if (!userId) return
  for (const query of queryClient.getQueryCache().findAll()) {
    const key = query.queryKey
    if (Array.isArray(key) && key[1] && key[1] !== userId) {
      queryClient.removeQueries({ queryKey: key, exact: true })
    }
  }
}
