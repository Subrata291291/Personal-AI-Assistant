
import {
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useOutletContext } from 'react-router-dom'
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'

import { useAuth } from '../context/AuthContext'
import MemorySettingControl from '../components/MemorySettingControl.jsx'
import { memoryService } from '../services/memoryService'
import {
  api,
  getApiAuthRevision,
} from '../lib/api.js'
import {
  memoryReviewQueryKey,
  memoryReviewQueryPrefix,
} from '../lib/queryKeys'


const MEMORY_TYPES = [
  { value: 'all', label: 'All Memories' },
  { value: 'long_term', label: 'Long-term' },
  { value: 'episodic', label: 'Episodic' },
  { value: 'semantic', label: 'Semantic' },
  { value: 'procedural', label: 'Procedural' },
]


export default function MemoryCenter() {
  const { user } = useAuth()
  const userId = user?.id

  return (
    <MemoryCenterContent
      key={userId || 'signed-out'}
      userId={userId}
    />
  )
}

function MemoryCenterContent({ userId }) {
  const scopeActiveRef = useRef(true)

  function isCurrentUserScope(scopeUserId) {
    return scopeActiveRef.current && scopeUserId === userId
  }

  function createMutationAuthContext() {
    return Object.freeze({
      userId,
      authRevision: getApiAuthRevision(),
    })
  }

  function assertCurrentMutationAuthContext(authContext) {
    if (
      !isCurrentUserScope(authContext?.userId) ||
      authContext.authRevision !== getApiAuthRevision()
    ) {
      const error = new Error(
        'Your account changed while the request was starting. Please try again.',
      )
      error.code = 'AUTH_SESSION_CHANGED'
      throw error
    }
  }

  useLayoutEffect(() => {
    scopeActiveRef.current = true
    return () => {
      scopeActiveRef.current = false
    }
  }, [])

  const { isDark } = useOutletContext()
  const queryClient = useQueryClient()

  // --------------------------------------------------
  // Theme
  // --------------------------------------------------

  const pageText = isDark
    ? 'text-slate-100'
    : 'text-slate-800'

  const cardSurface = isDark
    ? 'border-slate-700 bg-slate-900/70'
    : 'border-slate-200 bg-white'

  const mutedText = isDark
    ? 'text-slate-400'
    : 'text-slate-500'

  const inputSurface = isDark
    ? 'border-slate-700 bg-slate-900 text-slate-200 placeholder:text-slate-500'
    : 'border-slate-200 bg-white text-slate-800 placeholder:text-slate-400'

  const secondarySurface = isDark
    ? 'bg-slate-800/60'
    : 'bg-slate-50'

  const hoverSurface = isDark
    ? 'hover:bg-slate-800'
    : 'hover:bg-slate-100'

  // --------------------------------------------------
  // Query keys
  // --------------------------------------------------

  const memoriesKey = ['memories', userId, 'all']

  const reviewsKey = memoryReviewQueryKey(userId, 50)

  const settingsKey = [
    'user-settings',
    userId,
  ]

  // --------------------------------------------------
  // Local UI state
  // --------------------------------------------------

  const [activeType, setActiveType] = useState('all')
  const [search, setSearch] = useState('')

  const [error, setError] = useState('')

  const [showForm, setShowForm] = useState(false)
  const [editingMemory, setEditingMemory] = useState(null)

  const [form, setForm] = useState({
    memory_type: 'long_term',
    content: '',
    importance: 5,
    confidence: 0.9,
  })
  // --------------------------------------------------
  // 1. Memories query
  // --------------------------------------------------

  const memoriesQuery = useQuery({
    queryKey: memoriesKey,

    queryFn: async () => {
      const result = await memoryService.getMemories({
        limit: 100,
      })

      if (!Array.isArray(result)) {
        throw new Error('Invalid memories response.')
      }

      return result
    },

    enabled: Boolean(userId),
  })

  const memories = memoriesQuery.data || []

  const loading = memoriesQuery.isPending

  // --------------------------------------------------
  // 2. User settings query
  // --------------------------------------------------

  const settingsQuery = useQuery({
    queryKey: settingsKey,

    queryFn: () =>
      api.get('/api/v1/me/settings'),

    enabled: Boolean(userId),
  })

  const memoryEnabled = settingsQuery.data?.memory_enabled ?? true

  // --------------------------------------------------
  // 3. Pending reviews query
  // --------------------------------------------------

  const reviewsQuery = useQuery({
    queryKey: reviewsKey,

    queryFn: async () => {
      const data = await api.get(
        '/api/v1/memory-reviews?status_filter=pending&limit=50&offset=0'
      )

      if (!Array.isArray(data)) {
        throw new Error('Invalid memory reviews response.')
      }

      return data
    },

    enabled: Boolean(userId),
  })

  const pendingReviews = reviewsQuery.data || []

  const reviewLoading = reviewsQuery.isPending

  // --------------------------------------------------
  // 4. Combined error handling
  // --------------------------------------------------

  const queryError =
    memoriesQuery.error ||
    settingsQuery.error ||
    reviewsQuery.error

  // --------------------------------------------------
  // 5. Create / Update memory mutation
  // --------------------------------------------------

  const saveMutation = useMutation({
    mutationFn: async ({ memoryId, payload, authContext }) => {
      assertCurrentMutationAuthContext(authContext)
      if (memoryId) {
        return memoryService.updateMemory(
          memoryId,
          payload,
          authContext,
        )
      }

      return memoryService.createMemory(payload, authContext)
    },

    onSuccess: (_, variables) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return

      setShowForm(false)
      setEditingMemory(null)

      setError('')

      // Refresh all memory-related queries.
      queryClient.invalidateQueries({
        queryKey: ['memories', userId],
      })

      // Dashboard's long-term memory query shares this prefix.
    },

    onError: (err, variables) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return
      setError(
        err.message || 'Unable to save memory.'
      )
    },
  })

  const saving = saveMutation.isPending &&
    saveMutation.variables?.requestUserId === userId

  // --------------------------------------------------
  // 6. Delete memory mutation
  // --------------------------------------------------

  const deleteMutation = useMutation({
    mutationFn: ({ memoryId, authContext }) => {
      assertCurrentMutationAuthContext(authContext)
      return memoryService.deleteMemory(memoryId, authContext)
    },

    onSuccess: (_, variables) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return
      setError('')

      queryClient.invalidateQueries({
        queryKey: ['memories', variables.requestUserId],
      })
    },

    onError: (err, variables) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return
      setError(
        err.message || 'Unable to delete memory.'
      )
    },
  })
  const deleteIsPending = deleteMutation.isPending &&
    deleteMutation.variables?.requestUserId === userId

  // --------------------------------------------------
  // 7. Automatic memory setting mutation
  // --------------------------------------------------

  const settingsMutation = useMutation({
    mutationFn: ({ nextValue, authContext }) => {
      assertCurrentMutationAuthContext(authContext)
      return api.patch('/api/v1/me/settings', {
        memory_enabled: nextValue,
      }, authContext)
    },

    onMutate: async (variables) => {
      const mutationSettingsKey = [
        'user-settings',
        variables.requestUserId,
      ]
      await queryClient.cancelQueries({
        queryKey: mutationSettingsKey,
      })

      const previousSettings =
        queryClient.getQueryData(mutationSettingsKey)

      if (isCurrentUserScope(
        variables.requestUserId,
      )) {
        queryClient.setQueryData(
          mutationSettingsKey,
          (current) => ({
            ...(current || {}),
            memory_enabled: variables.nextValue,
          })
        )
      }

      return { previousSettings, mutationSettingsKey }
    },

    onError: (err, variables, context) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return
      if (context?.previousSettings) {
        queryClient.setQueryData(
          context.mutationSettingsKey,
          context.previousSettings
        )
      }

      setError(
        err.message || 'Unable to update memory settings.'
      )
    },

    onSuccess: (updatedSettings, variables, context) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return
      queryClient.setQueryData(
        context.mutationSettingsKey,
        updatedSettings
      )

      setError('')
    },

    onSettled: (_data, _error, variables, context) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return
      queryClient.invalidateQueries({
        queryKey: context.mutationSettingsKey,
      })
    },
  })
  const settingsUpdating = settingsMutation.isPending &&
    settingsMutation.variables?.requestUserId === userId

  // --------------------------------------------------
  // 8. Accept / Reject review mutation
  // --------------------------------------------------

  const reviewMutation = useMutation({
    mutationFn: ({ reviewId, action, authContext }) => {
      assertCurrentMutationAuthContext(authContext)
      return api.post(
        `/api/v1/memory-reviews/${reviewId}/${action}`,
        {},
        {},
        authContext,
      )
    },

    onSuccess: (_, variables) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return
      setError('')

      // Refresh pending reviews.
      queryClient.invalidateQueries({
        queryKey: memoryReviewQueryPrefix(variables.requestUserId),
      })

      // Accepting a review may update an existing memory.
      if (variables.action === 'accept') {
        queryClient.invalidateQueries({
          queryKey: ['memories', variables.requestUserId],
        })
      }
    },

    onError: (err, variables) => {
      if (!isCurrentUserScope(
        variables.requestUserId,
      )) return
      const actionLabel =
        variables.action === 'accept'
          ? 'accept'
          : 'reject'

      setError(
        err.message ||
        `Unable to ${actionLabel} memory review.`
      )
    },
  })

  const reviewIsPending = reviewMutation.isPending &&
    reviewMutation.variables?.requestUserId === userId
  const reviewActionId = reviewIsPending
    ? reviewMutation.variables?.reviewId
    : null

  // --------------------------------------------------
  // 9. Filter memories
  // --------------------------------------------------

  const filteredMemories = useMemo(() => {
    const query = search.trim().toLowerCase()

    return memories.filter((memory) => {
      const matchesType =
        activeType === 'all' ||
        memory.memory_type === activeType

      const matchesSearch =
        memory.content?.toLowerCase().includes(query)

      return matchesType && matchesSearch
    })
  }, [memories, activeType, search])

  // --------------------------------------------------
  // 10. Create form
  // --------------------------------------------------

  function openCreateForm() {
    setEditingMemory(null)

    setForm({
      memory_type: 'long_term',
      content: '',
      importance: 5,
      confidence: 0.9,
    })

    setError('')
    setShowForm(true)
  }

  // --------------------------------------------------
  // 11. Edit form
  // --------------------------------------------------

  function openEditForm(memory) {
    setEditingMemory(memory)

    setForm({
      memory_type: memory.memory_type,
      content: memory.content,
      importance: memory.importance,
      confidence: memory.confidence,
    })

    setError('')
    setShowForm(true)
  }

  // --------------------------------------------------
  // 12. Save memory
  // --------------------------------------------------

  async function handleSave(event) {
    event.preventDefault()

    if (
      !isCurrentUserScope(userId) ||
      !form.content.trim() ||
      saving
    ) return

    setError('')

    await saveMutation.mutateAsync({
      memoryId: editingMemory?.id || null,
      requestUserId: userId,
      authContext: createMutationAuthContext(),

      payload: {
        ...form,
        content: form.content.trim(),
      },
    }).catch(() => {})
  }

  // --------------------------------------------------
  // 13. Delete memory
  // --------------------------------------------------

  async function handleDelete(memory) {
    const confirmed = window.confirm(
      'Are you sure you want to delete this memory?'
    )

    if (
      !confirmed ||
      deleteIsPending ||
      !isCurrentUserScope(userId)
    ) return

    setError('')

    await deleteMutation.mutateAsync({
      memoryId: memory.id,
      requestUserId: userId,
      authContext: createMutationAuthContext(),
    }).catch(() => {})
  }

  // --------------------------------------------------
  // 14. Toggle automatic memory
  // --------------------------------------------------

  function handleMemoryToggle(nextValue) {
    if (
      settingsUpdating ||
      !isCurrentUserScope(userId)
    ) return

    setError('')

    settingsMutation.mutate({
      nextValue,
      requestUserId: userId,
      authContext: createMutationAuthContext(),
    })
  }

  // --------------------------------------------------
  // 15. Accept / Reject review
  // --------------------------------------------------

  async function handleReviewAction(review, action) {
    const actionLabel =
      action === 'accept' ? 'accept' : 'reject'

    const confirmed = window.confirm(
      `Are you sure you want to ${actionLabel} this memory suggestion?`
    )

    if (
      !confirmed ||
      reviewIsPending ||
      !isCurrentUserScope(userId)
    ) return

    setError('')

    await reviewMutation.mutateAsync({
      reviewId: review.id,
      action,
      requestUserId: userId,
      authContext: createMutationAuthContext(),
    }).catch(() => {})
  }

  // --------------------------------------------------
  // 16. Memory type counts
  // --------------------------------------------------

  const counts = MEMORY_TYPES.slice(1).map((type) => ({
    ...type,

    count: memories.filter(
      (memory) => memory.memory_type === type.value
    ).length,
  }))

  // --------------------------------------------------
  // 17. Refresh functions
  // --------------------------------------------------

  function refreshMemories() {
    return queryClient.invalidateQueries({
      queryKey: ['memories', userId],
    })
  }

  function refreshReviews() {
    return queryClient.invalidateQueries({
      queryKey: memoryReviewQueryPrefix(userId),
    })
  }

  // --------------------------------------------------
  // 18. UI
  // --------------------------------------------------

  return (
    <div className={`space-y-6 ${pageText}`}>

      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">
            Memory Center
          </h1>

          <p className={`mt-1 text-sm ${mutedText}`}>
            Manage what your AI assistant remembers about you.
          </p>
        </div>

        <button
          onClick={openCreateForm}
          className="rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-indigo-500"
        >
          + Add Memory
        </button>
      </div>

      {/* Error Message */}
      {(error || queryError) && (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          <span>
            {error || queryError?.message}
          </span>

          <button
            type="button"
            onClick={() => setError('')}
            aria-label="Dismiss error"
          >
            ×
          </button>
        </div>
      )}

      {/* Automatic Memory */}
      <section className={`rounded-2xl border p-5 ${cardSurface}`}>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <h2 className="font-semibold">
              Automatic Memory
            </h2>

            <p className={`mt-1 text-sm ${mutedText}`}>
              Allow the assistant to save new memories automatically.
              Existing memories remain available when this is disabled.
            </p>
          </div>

          <MemorySettingControl
            variant="switch"
            checked={memoryEnabled}
            disabled={
              settingsQuery.isPending ||
              settingsUpdating
            }
            isDark={isDark}
            onChange={handleMemoryToggle}
          />
        </div>
      </section>

      {/* Memory Type Statistics */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {counts.map((item) => (
          <div
            key={item.value}
            className={`rounded-2xl border p-4 ${cardSurface}`}
          >
            <p className={`text-sm ${mutedText}`}>
              {item.label}
            </p>

            <p className="mt-2 text-2xl font-bold">
              {loading ? '—' : item.count}
            </p>
          </div>
        ))}
      </section>

      {/* Pending Memory Reviews */}
      <section className="space-y-4">

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">
              Pending Memory Reviews
            </h2>

            <p className={`mt-1 text-sm ${mutedText}`}>
              Review AI suggestions before updating your existing memories.
            </p>
          </div>

          <div className="flex items-center gap-3">
            <span className="rounded-full bg-amber-500/15 px-3 py-1 text-sm text-amber-500">
              {pendingReviews.length} Pending
            </span>

            <button
              onClick={refreshReviews}
              disabled={reviewLoading}
              className={`rounded-xl border px-3 py-2 text-sm disabled:opacity-50 ${
                isDark
                  ? 'border-slate-700 hover:bg-slate-800'
                  : 'border-slate-200 hover:bg-slate-100'
              }`}
            >
              {reviewLoading ? 'Refreshing...' : 'Refresh'}
            </button>
          </div>
        </div>

        {reviewLoading ? (
          <p className={`py-6 text-center text-sm ${mutedText}`}>
            Loading pending reviews...
          </p>
        ) : reviewsQuery.isError ? (
          <div className={`rounded-2xl border p-6 text-center ${cardSurface}`}>
            <p className="text-sm text-red-400">
              Unable to load pending reviews.
            </p>

            <button
              onClick={() => reviewsQuery.refetch()}
              className="mt-3 text-sm text-indigo-500"
            >
              Try again
            </button>
          </div>
        ) : pendingReviews.length === 0 ? (
          <div className={`rounded-2xl border border-dashed p-8 text-center ${cardSurface}`}>
            <h3 className="font-semibold">
              No pending reviews
            </h3>

            <p className={`mt-2 text-sm ${mutedText}`}>
              New memory suggestions requiring your approval will appear here.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {pendingReviews.map((review) => (
              <article
                key={review.id}
                className={`rounded-2xl border p-5 ${
                  isDark
                    ? 'border-amber-500/20 bg-slate-900/70'
                    : 'border-amber-200 bg-white'
                }`}
              >
                {/* Review Header */}
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <span className="rounded-full bg-amber-500/15 px-3 py-1 text-xs font-medium text-amber-500">
                    Pending Review
                  </span>

                  <span className={`text-xs ${mutedText}`}>
                    Confidence: {Math.round(review.confidence * 100)}%
                  </span>
                </div>

                {/* Existing and Proposed Memory */}
                <div className="mt-5 grid gap-4 md:grid-cols-2">

                  <div className={`rounded-xl border p-4 ${
                    isDark
                      ? 'border-slate-700 bg-slate-800/60'
                      : 'border-slate-200 bg-slate-50'
                  }`}>
                    <p className={`mb-2 text-xs font-semibold uppercase tracking-wide ${mutedText}`}>
                      Existing Memory
                    </p>

                    <p className="whitespace-pre-wrap text-sm leading-6">
                      {review.existing_content}
                    </p>
                  </div>

                  <div className={`rounded-xl border p-4 ${
                    isDark
                      ? 'border-indigo-500/30 bg-indigo-500/5'
                      : 'border-indigo-200 bg-indigo-50'
                  }`}>
                    <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-indigo-500">
                      Proposed Memory
                    </p>

                    <p className="whitespace-pre-wrap text-sm leading-6">
                      {review.proposed_content}
                    </p>
                  </div>

                </div>

                {/* Review Reason */}
                <div className={`mt-4 rounded-xl p-4 ${secondarySurface}`}>
                  <p className={`text-xs font-semibold uppercase tracking-wide ${mutedText}`}>
                    Why does this need review?
                  </p>

                  <p className="mt-2 text-sm leading-6">
                    {review.reason}
                  </p>
                </div>

                {/* Review Date */}
                <p className={`mt-3 text-xs ${mutedText}`}>
                  Created: {new Date(review.created_at).toLocaleString()}
                </p>

                {/* Accept / Reject */}
                <div className="mt-4 flex flex-wrap justify-end gap-3">
                  <button
                    onClick={() => handleReviewAction(review, 'reject')}
                    disabled={reviewIsPending}
                    className="rounded-xl border border-red-500/30 px-4 py-2 text-sm font-medium text-red-500 transition hover:bg-red-500/10 disabled:opacity-50"
                  >
                    {reviewActionId === review.id
                      ? 'Processing...'
                      : 'Reject'}
                  </button>

                  <button
                    onClick={() => handleReviewAction(review, 'accept')}
                    disabled={reviewIsPending}
                    className="rounded-xl bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50"
                  >
                    {reviewActionId === review.id
                      ? 'Processing...'
                      : 'Accept'}
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {/* Memory Search and Filters */}
      <section className="space-y-4">
        <div className="flex flex-col gap-3 md:flex-row">
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search memories..."
            className={`min-w-0 flex-1 rounded-xl border px-4 py-3 text-sm outline-none focus:border-indigo-500 ${inputSurface}`}
          />

          <button
            onClick={refreshMemories}
            disabled={memoriesQuery.isFetching}
            className={`rounded-xl border px-4 py-3 text-sm disabled:opacity-50 ${hoverSurface} ${
              isDark ? 'border-slate-700' : 'border-slate-200'
            }`}
          >
            {memoriesQuery.isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>

        {/* Memory Type Filters */}
        <div className="flex flex-wrap gap-2">
          {MEMORY_TYPES.map((type) => (
            <button
              key={type.value}
              onClick={() => setActiveType(type.value)}
              className={`rounded-full px-4 py-2 text-sm transition ${
                activeType === type.value
                  ? 'bg-indigo-600 text-white'
                  : `border ${
                      isDark
                        ? 'border-slate-700 text-slate-400 hover:bg-slate-800'
                        : 'border-slate-200 text-slate-500 hover:bg-slate-100'
                    }`
              }`}
            >
              {type.label}
            </button>
          ))}
        </div>

        {/* Memory List */}
        {loading ? (
          <p className={`py-10 text-center ${mutedText}`}>
            Loading memories...
          </p>
        ) : memoriesQuery.isError ? (
          <div className={`rounded-2xl border p-8 text-center ${cardSurface}`}>
            <p className="text-sm text-red-400">
              Unable to load memories.
            </p>

            <button
              onClick={() => memoriesQuery.refetch()}
              className="mt-3 text-sm text-indigo-500"
            >
              Try again
            </button>
          </div>
        ) : filteredMemories.length === 0 ? (
          <div className={`rounded-2xl border border-dashed p-10 text-center ${cardSurface}`}>
            <h3 className="font-semibold">
              No memories found
            </h3>

            <p className={`mt-2 text-sm ${mutedText}`}>
              Your memories will appear here when available.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {filteredMemories.map((memory) => (
              <article
                key={memory.id}
                className={`rounded-2xl border p-5 ${cardSurface}`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">

                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-full bg-indigo-500/15 px-3 py-1 text-xs text-indigo-500">
                      {memory.memory_type.replace('_', ' ')}
                    </span>

                    <span className={`text-xs ${mutedText}`}>
                      Importance: {memory.importance}/10
                    </span>
                  </div>

                  <div className="flex gap-3">
                    <button
                      onClick={() => openEditForm(memory)}
                      className="text-sm text-indigo-500 hover:text-indigo-400"
                    >
                      Edit
                    </button>

                    <button
                      onClick={() => handleDelete(memory)}
                      disabled={deleteIsPending}
                      className="text-sm text-red-500 hover:text-red-400 disabled:opacity-50"
                    >
                      {deleteIsPending &&
                      deleteMutation.variables?.memoryId === memory.id
                        ? 'Deleting...'
                        : 'Delete'}
                    </button>
                  </div>
                </div>

                <p className="mt-4 whitespace-pre-wrap text-sm leading-6">
                  {memory.content}
                </p>

                <p className={`mt-3 text-xs ${mutedText}`}>
                  Confidence: {Math.round(memory.confidence * 100)}%
                  {' · '}
                  Created: {new Date(memory.created_at).toLocaleDateString()}
                </p>
              </article>
            ))}
          </div>
        )}
      </section>

      {/* Create / Edit Memory Modal */}
      {showForm && (
        <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-black/70 p-4">
          <form
            onSubmit={handleSave}
            className={`w-full max-w-lg space-y-4 rounded-2xl border p-6 ${
              isDark
                ? 'border-slate-700 bg-slate-900'
                : 'border-slate-200 bg-white text-slate-800'
            }`}
          >
            <h2 className="text-xl font-semibold">
              {editingMemory ? 'Edit Memory' : 'Create Memory'}
            </h2>

            {/* Memory Type */}
            <div>
              <label className={`mb-2 block text-sm ${mutedText}`}>
                Memory type
              </label>

              <select
                value={form.memory_type}
                onChange={(event) =>
                  setForm({
                    ...form,
                    memory_type: event.target.value,
                  })
                }
                className={`w-full rounded-xl border p-3 ${inputSurface}`}
              >
                {MEMORY_TYPES.slice(1).map((type) => (
                  <option key={type.value} value={type.value}>
                    {type.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Memory Content */}
            <div>
              <label className={`mb-2 block text-sm ${mutedText}`}>
                Memory content
              </label>

              <textarea
                required
                rows={5}
                value={form.content}
                onChange={(event) =>
                  setForm({
                    ...form,
                    content: event.target.value,
                  })
                }
                className={`w-full rounded-xl border p-3 ${inputSurface}`}
                placeholder="What should the assistant remember?"
              />
            </div>

            {/* Importance */}
            <div>
              <label className={`mb-2 block text-sm ${mutedText}`}>
                Importance: {form.importance}/10
              </label>

              <input
                type="range"
                min="1"
                max="10"
                value={form.importance}
                onChange={(event) =>
                  setForm({
                    ...form,
                    importance: Number(event.target.value),
                  })
                }
                className="w-full"
              />
            </div>

            {/* Confidence */}
            <div>
              <label className={`mb-2 block text-sm ${mutedText}`}>
                Confidence: {Math.round(form.confidence * 100)}%
              </label>

              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={form.confidence}
                onChange={(event) =>
                  setForm({
                    ...form,
                    confidence: Number(event.target.value),
                  })
                }
                className="w-full"
              />
            </div>

            {/* Modal Actions */}
            <div className="flex justify-end gap-3 pt-2">
              <button
                type="button"
                onClick={() => {
                  setShowForm(false)
                  setEditingMemory(null)
                }}
                disabled={saving}
                className={`rounded-xl border px-4 py-2 disabled:opacity-50 ${
                  isDark
                    ? 'border-slate-700 hover:bg-slate-800'
                    : 'border-slate-200 hover:bg-slate-100'
                }`}
              >
                Cancel
              </button>

              <button
                type="submit"
                disabled={saving || !form.content.trim()}
                className="rounded-xl bg-indigo-600 px-5 py-2 font-medium text-white disabled:opacity-50"
              >
                {saving ? 'Saving...' : 'Save Memory'}
              </button>
            </div>
          </form>
        </div>
      )}

    </div>
  )
}
