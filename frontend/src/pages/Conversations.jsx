
import { useMemo, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'

import { useAuth } from '../context/AuthContext'
import { conversationService } from '../services/conversationService.js'


function formatDate(value) {
  if (!value) return 'Unknown date'

  const date = new Date(value)

  if (Number.isNaN(date.getTime())) return 'Unknown date'

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}


function Conversations() {
  const { user } = useAuth()
  const userId = user?.id

  const { isDark, muted, surface } = useOutletContext()

  const queryClient = useQueryClient()

  const [selectedId, setSelectedId] = useState(null)

  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('active')

  const [error, setError] = useState('')

  const [newTitle, setNewTitle] = useState('')
  const [editingTitle, setEditingTitle] = useState('')
  const [isCreating, setIsCreating] = useState(false)
  const [isRenaming, setIsRenaming] = useState(false)
  const [deleteId, setDeleteId] = useState(null)

  const conversationsKey = ['conversations', userId]

  // --------------------------------------------------
  // 1. Fetch and cache conversations
  // --------------------------------------------------

  const conversationsQuery = useQuery({
    queryKey: conversationsKey,

    queryFn: async () => {
      const result = await conversationService.getConversations()

      if (!Array.isArray(result)) {
        throw new Error(
          'Unexpected conversations response from server.'
        )
      }

      return result
    },

    enabled: Boolean(userId),
  })

  const conversations = conversationsQuery.data || []

  const loading = conversationsQuery.isPending

  // --------------------------------------------------
  // 2. Selected conversation
  // --------------------------------------------------

  const selectedConversation = conversations.find(
    (conversation) => conversation.id === selectedId
  )

  // --------------------------------------------------
  // 3. Fetch and cache selected conversation messages
  // --------------------------------------------------

  const messagesQuery = useQuery({
    queryKey: [
      'conversation-messages',
      userId,
      selectedId,
    ],

    queryFn: () =>
      conversationService.getMessages(selectedId),

    enabled: Boolean(userId && selectedId),
  })

  const messages = messagesQuery.data || []

  const messagesLoading = messagesQuery.isPending

  // --------------------------------------------------
  // 4. Create conversation mutation
  // --------------------------------------------------

  const createMutation = useMutation({
    mutationFn: (title) =>
      conversationService.createConversation(title),

    onSuccess: (created) => {
      queryClient.setQueryData(
        conversationsKey,
        (current = []) => [
          created,
          ...current.filter((item) => item.id !== created.id),
        ]
      )

      setSelectedId(created.id)
      setFilter('active')
      setNewTitle('')
      setIsCreating(false)
      setError('')

      // Refresh the list in the background.
      queryClient.invalidateQueries({
        queryKey: conversationsKey,
      })
    },

    onError: (err) => {
      setError(
        err.message || 'Unable to create conversation.'
      )
    },
  })

  // --------------------------------------------------
  // 5. Rename conversation mutation
  // --------------------------------------------------

  const renameMutation = useMutation({
    mutationFn: ({ id, title }) =>
      conversationService.updateConversation(id, title),

    onSuccess: (updated) => {
      queryClient.setQueryData(
        conversationsKey,
        (current = []) =>
          current.map((item) =>
            item.id === updated.id ? updated : item
          )
      )

      setIsRenaming(false)
      setEditingTitle('')
      setError('')
    },

    onError: (err) => {
      setError(
        err.message || 'Unable to rename conversation.'
      )
    },
  })

  // --------------------------------------------------
  // 6. Archive conversation mutation
  // --------------------------------------------------

  const archiveMutation = useMutation({
    mutationFn: (id) =>
      conversationService.archiveConversation(id),

    onSuccess: (updated) => {
      queryClient.setQueryData(
        conversationsKey,
        (current = []) =>
          current.map((item) =>
            item.id === updated.id ? updated : item
          )
      )

      setSelectedId(null)
      setError('')
    },

    onError: (err) => {
      setError(
        err.message || 'Unable to archive conversation.'
      )
    },
  })

  // --------------------------------------------------
  // 7. Delete conversation mutation
  // --------------------------------------------------

  const deleteMutation = useMutation({
    mutationFn: (id) =>
      conversationService.deleteConversation(id),

    onSuccess: (_, deletedId) => {
      queryClient.setQueryData(
        conversationsKey,
        (current = []) =>
          current.filter((item) => item.id !== deletedId)
      )

      // Remove deleted conversation's cached messages.
      queryClient.removeQueries({
        queryKey: [
          'conversation-messages',
          userId,
          deletedId,
        ],
      })

      if (selectedId === deletedId) {
        setSelectedId(null)
      }

      setDeleteId(null)
      setError('')
    },

    onError: (err) => {
      setError(
        err.message || 'Unable to delete conversation.'
      )
    },
  })

  // --------------------------------------------------
  // 8. Combined action loading state
  // --------------------------------------------------

  const actionLoading =
    createMutation.isPending ||
    renameMutation.isPending ||
    archiveMutation.isPending ||
    deleteMutation.isPending

  // --------------------------------------------------
  // 9. Search and filters
  // --------------------------------------------------

  const filteredConversations = useMemo(() => {
    const query = search.trim().toLowerCase()

    return conversations.filter((conversation) => {
      const matchesFilter =
        filter === 'all' ||
        (filter === 'active' && !conversation.is_archived) ||
        (filter === 'archived' && conversation.is_archived)

      const matchesSearch =
        !query ||
        (conversation.title || '').toLowerCase().includes(query)

      return matchesFilter && matchesSearch
    })
  }, [conversations, filter, search])

  // --------------------------------------------------
  // 10. Create handler
  // --------------------------------------------------

  async function handleCreate(event) {
    event.preventDefault()

    const title = newTitle.trim()

    if (!title || actionLoading) return

    setError('')

    await createMutation.mutateAsync(title).catch(() => {})
  }

  // --------------------------------------------------
  // 11. Rename handler
  // --------------------------------------------------

  async function handleRename(event) {
    event.preventDefault()

    const title = editingTitle.trim()

    if (!title || !selectedId || actionLoading) return

    setError('')

    await renameMutation.mutateAsync({
      id: selectedId,
      title,
    }).catch(() => {})
  }

  // --------------------------------------------------
  // 12. Archive handler
  // --------------------------------------------------

  async function handleArchive() {
    if (!selectedConversation || actionLoading) return

    setError('')

    await archiveMutation.mutateAsync(
      selectedId
    ).catch(() => {})
  }

  // --------------------------------------------------
  // 13. Delete handler
  // --------------------------------------------------

  async function handleDelete() {
    if (!deleteId || actionLoading) return

    setError('')

    await deleteMutation.mutateAsync(
      deleteId
    ).catch(() => {})
  }

  // --------------------------------------------------
  // 14. Input styling
  // --------------------------------------------------

  const inputClass = `w-full rounded-xl border px-4 py-3 text-sm outline-none focus:border-violet-500 ${
    isDark
      ? 'border-[#303448] bg-[#171a27] text-white'
      : 'border-slate-200 bg-white text-slate-800'
  }`

  // --------------------------------------------------
  // 15. UI
  // --------------------------------------------------

  return (
    <div className="mx-auto w-full max-w-6xl">

      {/* Heading */}
      <div className="mb-7 flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
        <div>
          <div className="mb-2 text-xs font-bold tracking-[1.5px] text-violet-400">
            YOUR WORKSPACE
          </div>

          <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">
            Conversations
          </h1>

          <p className={`mt-2 text-sm ${muted}`}>
            View and manage your saved AI conversations.
          </p>
        </div>

        <button
          type="button"
          onClick={() => setIsCreating(true)}
          className="rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-5 py-3 text-sm font-semibold text-white hover:brightness-110"
        >
          + New conversation
        </button>
      </div>

      {/* Error */}
      {(error || conversationsQuery.isError || messagesQuery.isError) && (
        <div className="mb-5 flex items-center justify-between gap-3 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400">
          <span>
            {error ||
              conversationsQuery.error?.message ||
              messagesQuery.error?.message ||
              'Something went wrong.'}
          </span>

          <button
            onClick={() => {
              setError('')

              if (conversationsQuery.isError) {
                conversationsQuery.refetch()
              }

              if (messagesQuery.isError) {
                messagesQuery.refetch()
              }
            }}
            aria-label="Dismiss error"
          >
            ×
          </button>
        </div>
      )}

      {/* Create conversation */}
      {isCreating && (
        <form
          onSubmit={handleCreate}
          className={`mb-5 rounded-2xl border p-5 ${surface}`}
        >
          <h2 className="mb-3 text-sm font-semibold">
            Create a conversation
          </h2>

          <div className="flex flex-col gap-3 sm:flex-row">
            <input
              autoFocus
              maxLength={200}
              value={newTitle}
              onChange={(event) => setNewTitle(event.target.value)}
              placeholder="Enter conversation title"
              className={inputClass}
              required
            />

            <button
              type="submit"
              disabled={!newTitle.trim() || actionLoading}
              className="rounded-xl bg-violet-600 px-5 py-3 text-sm font-semibold text-white disabled:opacity-50"
            >
              {createMutation.isPending ? 'Creating...' : 'Create'}
            </button>

            <button
              type="button"
              onClick={() => setIsCreating(false)}
              className={`rounded-xl px-5 py-3 text-sm ${muted}`}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* Search and filters */}
      <div className="mb-5 flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
        <input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search conversations..."
          className={`w-full max-w-md ${inputClass}`}
        />

        <div className="flex flex-wrap gap-2">
          {['active', 'archived', 'all'].map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setFilter(item)}
              className={`rounded-lg px-4 py-2 text-xs font-semibold capitalize ${
                filter === item
                  ? 'bg-violet-600 text-white'
                  : isDark
                    ? 'bg-[#202333] text-slate-400'
                    : 'bg-slate-100 text-slate-600'
              }`}
            >
              {item}
            </button>
          ))}
        </div>
      </div>

      <p className={`mb-4 text-sm ${muted}`}>
        {filteredConversations.length} conversations
      </p>

      {/* Main content */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr_0.85fr]">

        {/* Conversation list */}
        <section className={`overflow-hidden rounded-2xl border ${surface}`}>
          <div className={`border-b px-5 py-4 ${
            isDark ? 'border-[#252838]' : 'border-slate-200'
          }`}>
            <h2 className="text-sm font-semibold">
              {filter === 'active'
                ? 'Active conversations'
                : filter === 'archived'
                  ? 'Archived conversations'
                  : 'All conversations'}
            </h2>
          </div>

          {loading ? (
            <div className={`p-10 text-center text-sm ${muted}`}>
              Loading conversations...
            </div>
          ) : conversationsQuery.isError ? (
            <div className="p-10 text-center text-sm text-red-400">
              Unable to load conversations.
              <button
                type="button"
                onClick={() => conversationsQuery.refetch()}
                className="mt-3 block w-full text-violet-400"
              >
                Try again
              </button>
            </div>
          ) : filteredConversations.length === 0 ? (
            <div className={`p-10 text-center text-sm ${muted}`}>
              {search
                ? 'No matching conversations found.'
                : 'No conversations here yet.'}
            </div>
          ) : (
            <div className="divide-y divide-slate-700/20">
              {filteredConversations.map((conversation) => (
                <div
                  key={conversation.id}
                  className={`flex items-start gap-3 p-5 transition ${
                    selectedId === conversation.id
                      ? isDark
                        ? 'bg-violet-500/10'
                        : 'bg-violet-50'
                      : isDark
                        ? 'hover:bg-[#202333]'
                        : 'hover:bg-slate-50'
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedId(conversation.id)
                      setError('')
                      setIsRenaming(false)
                    }}
                    className="flex min-w-0 flex-1 items-start gap-3 text-left"
                  >
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-500/10 text-lg text-violet-400">
                      ◈
                    </div>

                    <div className="min-w-0 flex-1">
                      <h3 className="truncate text-sm font-semibold">
                        {conversation.title}
                      </h3>

                      <p className={`mt-2 text-xs ${muted}`}>
                        Updated {formatDate(conversation.updated_at)}
                      </p>

                      {conversation.is_archived && (
                        <span className="mt-2 inline-block rounded-md bg-amber-500/10 px-2 py-1 text-[10px] text-amber-400">
                          Archived
                        </span>
                      )}
                    </div>
                  </button>

                  <button
                    type="button"
                    onClick={() => setDeleteId(conversation.id)}
                    title="Permanently delete"
                    aria-label={`Delete ${conversation.title}`}
                    className="rounded-lg px-2 py-1 text-lg text-slate-500 hover:bg-red-500/10 hover:text-red-400"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Conversation details */}
        <section className={`flex min-h-[350px] flex-col rounded-2xl border ${surface}`}>
          <div className={`border-b px-5 py-4 ${
            isDark ? 'border-[#252838]' : 'border-slate-200'
          }`}>
            <h2 className="text-sm font-semibold">
              Conversation details
            </h2>
          </div>

          {!selectedConversation ? (
            <div className={`flex flex-1 flex-col items-center justify-center p-8 text-center ${muted}`}>
              <div className="mb-4 text-4xl text-violet-400">◈</div>

              <h3 className="text-sm font-semibold">
                Select a conversation
              </h3>

              <p className="mt-2 text-xs">
                Select a conversation to load its messages.
              </p>
            </div>
          ) : (
            <div className="flex flex-1 flex-col p-5">
              {isRenaming ? (
                <form onSubmit={handleRename} className="flex gap-2">
                  <input
                    autoFocus
                    maxLength={200}
                    value={editingTitle}
                    onChange={(event) => setEditingTitle(event.target.value)}
                    className={inputClass}
                    required
                  />

                  <button
                    type="submit"
                    disabled={!editingTitle.trim() || actionLoading}
                    className="rounded-lg bg-violet-600 px-3 text-sm text-white"
                  >
                    {renameMutation.isPending ? 'Saving...' : 'Save'}
                  </button>

                  <button
                    type="button"
                    onClick={() => setIsRenaming(false)}
                    className={`px-2 text-sm ${muted}`}
                  >
                    Cancel
                  </button>
                </form>
              ) : (
                <>
                  <h3 className="text-lg font-bold">
                    {selectedConversation.title}
                  </h3>

                  <p className={`mt-2 text-xs ${muted}`}>
                    Created {formatDate(selectedConversation.created_at)}
                  </p>

                  <div className="mt-4 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setEditingTitle(selectedConversation.title)
                        setIsRenaming(true)
                      }}
                      className="rounded-lg bg-violet-500/10 px-3 py-2 text-xs text-violet-400"
                    >
                      Rename
                    </button>

                    {!selectedConversation.is_archived && (
                      <button
                        type="button"
                        onClick={handleArchive}
                        disabled={actionLoading}
                        className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-400 disabled:opacity-50"
                      >
                        {archiveMutation.isPending ? 'Archiving...' : 'Archive'}
                      </button>
                    )}
                  </div>
                </>
              )}

              <div className={`mt-6 border-t pt-5 ${
                isDark ? 'border-[#252838]' : 'border-slate-200'
              }`}>
                <h4 className="mb-4 text-sm font-semibold">
                  Messages
                </h4>

                {messagesLoading ? (
                  <p className={`text-xs ${muted}`}>
                    Loading messages...
                  </p>
                ) : messagesQuery.isError ? (
                  <div className="text-xs text-red-400">
                    Unable to load messages.
                    <button
                      type="button"
                      onClick={() => messagesQuery.refetch()}
                      className="ml-2 text-violet-400"
                    >
                      Retry
                    </button>
                  </div>
                ) : messages.length === 0 ? (
                  <p className={`text-xs ${muted}`}>
                    No messages in this conversation yet.
                  </p>
                ) : (
                  <div className="flex max-h-[420px] flex-col gap-4 overflow-y-auto">
                    {messages.map((message) => (
                      <div key={message.id}>
                        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-violet-400">
                          {message.role}
                        </div>

                        <div className={`whitespace-pre-wrap break-words rounded-xl p-3 text-sm leading-6 ${
                          message.role === 'user'
                            ? isDark
                              ? 'bg-violet-500/10'
                              : 'bg-violet-50'
                            : isDark
                              ? 'bg-[#202333]'
                              : 'bg-slate-100'
                        }`}>
                          {message.content}
                        </div>

                        <p className={`mt-1 text-[10px] ${muted}`}>
                          {formatDate(message.created_at)}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </section>
      </div>

      {/* Delete confirmation */}
      {deleteId && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4">
          <div className={`w-full max-w-sm rounded-2xl border p-6 shadow-2xl ${
            isDark
              ? 'border-[#303448] bg-[#171a27]'
              : 'border-slate-200 bg-white'
          }`}>
            <h3 className="text-lg font-bold">
              Permanently delete?
            </h3>

            <p className={`mt-3 text-sm leading-6 ${muted}`}>
              This will permanently delete this conversation from the database.
              Its associated messages may also be deleted by the database
              foreign-key cascade.
            </p>

            <div className="mt-6 flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setDeleteId(null)}
                disabled={actionLoading}
                className={`rounded-xl px-4 py-2.5 text-sm font-semibold ${
                  isDark
                    ? 'bg-[#292d3d] text-slate-300'
                    : 'bg-slate-100 text-slate-700'
                }`}
              >
                Cancel
              </button>

              <button
                type="button"
                onClick={handleDelete}
                disabled={actionLoading}
                className="rounded-xl bg-red-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-red-600 disabled:opacity-50"
              >
                {deleteMutation.isPending ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default Conversations
