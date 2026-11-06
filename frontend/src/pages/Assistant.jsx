
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import {
  Link,
  useSearchParams,
  useOutletContext,
} from 'react-router-dom'

import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'

import { useAuth } from '../context/AuthContext'
import EmailDraftCard from '../components/EmailDraftCard.jsx'
import ActionApprovalCard from '../components/ActionApprovalCard.jsx'
import { resolveAssistantGmailConnection } from '../services/gmailAccountSelection.js'
import { conversationService } from '../services/conversationService.js'
import { api } from '../lib/api.js'
import AssistantMarkdown from '../components/AssistantMarkdown.jsx'
import {
  requireArrayResponse,
  requireConnectionsResponse,
} from '../lib/responseValidation'

function Assistant() {
  const { user } = useAuth()
  const userId = user?.id
  const [, setSearchParams] = useSearchParams()
  const previousUserIdRef = useRef(userId)

  useLayoutEffect(() => {
    const previousUserId = previousUserIdRef.current
    if (previousUserId && previousUserId !== userId) {
      setSearchParams({}, { replace: true })
    }
    previousUserIdRef.current = userId
  }, [setSearchParams, userId])

  return (
    <AssistantContent
      key={userId || 'signed-out'}
      user={user}
    />
  )
}

function AssistantContent({ user }) {
  const userId = user?.id
  const scopeActiveRef = useRef(true)

  useLayoutEffect(() => {
    scopeActiveRef.current = true
    return () => {
      scopeActiveRef.current = false
    }
  }, [])

  const isCurrentUserScope = useCallback(
    (scopeUserId) =>
      scopeActiveRef.current && scopeUserId === userId,
    [userId],
  )

  const { isDark, muted, surface } = useOutletContext()

  const [searchParams, setSearchParams] = useSearchParams()
  const requestedConversationId = searchParams.get('conversationId')

  const queryClient = useQueryClient()

  const [message, setMessage] = useState('')
  const [conversationId, setConversationId] = useState(null)
  const [optimisticMessages, setOptimisticMessages] = useState([])

  const [error, setError] = useState('')
  const [gmailSendNotice, setGmailSendNotice] = useState('')
  const [providerInfo, setProviderInfo] = useState(null)

  const messagesEndRef = useRef(null)
  const pendingRequestRef = useRef(null)
  const notifiedSendProposalRef = useRef(null)
  const sendingRef = useRef(false)

  const hasRestoredRef = useRef(false)
  const lastRequestedIdRef = useRef(undefined)
  const newChatModeRef = useRef(false)

  const gmailConnectionsQuery = useQuery({
    queryKey: ['oauth-connections', userId],
    queryFn: async () =>
      requireConnectionsResponse(
        await api.get('/api/v1/oauth/connections'),
      ),
    enabled: Boolean(userId),
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
    refetchOnWindowFocus: true,
  })

  const defaultGmailConnectionId =
    user?.user_metadata?.default_gmail_connection_id
  const selectedGmailConnection = useMemo(
    () => resolveAssistantGmailConnection(
      gmailConnectionsQuery.data?.connections || [],
      defaultGmailConnectionId,
    ),
    [gmailConnectionsQuery.data, defaultGmailConnectionId],
  )
  const selectedGmailConnectionId = selectedGmailConnection?.id || ''

  // --------------------------------------------------
  // 1. Fetch and cache conversation list
  // --------------------------------------------------

  const conversationsQuery = useQuery({
    queryKey: ['conversations', userId],

    queryFn: async () =>
      requireArrayResponse(
        await conversationService.getConversations(),
        'conversations',
      ),

    enabled: Boolean(userId),
  })

  // --------------------------------------------------
  // 2. Restore requested or latest conversation
  // --------------------------------------------------

  useEffect(() => {
    if (!isCurrentUserScope(userId)) return
    if (!userId || conversationsQuery.isPending) return

    if (conversationsQuery.isError) {
      setError(
        conversationsQuery.error?.message ||
        'Unable to load your conversations.'
      )
      return
    }

    if (
      hasRestoredRef.current &&
      lastRequestedIdRef.current === requestedConversationId
    ) {
      return
    }

    lastRequestedIdRef.current = requestedConversationId

    const conversations = conversationsQuery.data

    if (!Array.isArray(conversations)) {
      setError('Invalid conversations response.')
      return
    }

    const activeConversations = conversations
      .filter((item) => !item.is_archived)
      .sort(
        (a, b) =>
          new Date(b.updated_at).getTime() -
          new Date(a.updated_at).getTime()
      )

    if (requestedConversationId) {
      newChatModeRef.current = false

      const selected = activeConversations.find(
        (item) => item.id === requestedConversationId
      )

      if (!selected) {
        setConversationId(null)
        setError('Conversation not found or unavailable.')
      } else {
        setConversationId(selected.id)
        setError('')
      }

      hasRestoredRef.current = true
      return
    }

    // Do not restore the latest conversation after New Chat.
    if (newChatModeRef.current) {
      hasRestoredRef.current = true
      return
    }

    // Restore the latest conversation on initial page load.
    if (!hasRestoredRef.current) {
      const latestConversation = activeConversations[0] || null

      setConversationId(latestConversation?.id || null)
      setError('')
      hasRestoredRef.current = true
    }
  }, [
    userId,
    requestedConversationId,
    conversationsQuery.data,
    conversationsQuery.isPending,
    conversationsQuery.isError,
    conversationsQuery.error,
    isCurrentUserScope,
  ])

  // --------------------------------------------------
  // 3. Fetch and cache messages
  // --------------------------------------------------

  const messagesQuery = useQuery({
    queryKey: [
      'conversation-messages',
      userId,
      conversationId,
    ],

    queryFn: () => {
      if (!isCurrentUserScope(userId)) {
        throw new Error('The active account changed before loading messages.')
      }
      return conversationService.getMessages(conversationId)
    },

    enabled: Boolean(userId && conversationId),
  })

  // Handle message loading errors.
  useEffect(() => {
    if (
      isCurrentUserScope(userId) &&
      messagesQuery.isError
    ) {
      setError(
        messagesQuery.error?.message ||
        'Unable to load conversation messages.'
      )
    }
  }, [
    messagesQuery.isError,
    messagesQuery.error,
    isCurrentUserScope,
    userId,
  ])

  const messages = [
    ...(messagesQuery.data || []),
    ...optimisticMessages,
  ]

  const initializing =
    !userId ||
    conversationsQuery.isPending ||
    (
      Boolean(conversationId) &&
      messagesQuery.isPending
    )

  // --------------------------------------------------
  // 4. Scroll to latest message
  // --------------------------------------------------

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      if (!isCurrentUserScope(userId)) return
      messagesEndRef.current?.scrollIntoView({
        behavior: 'smooth',
        block: 'end',
      })
    })
  }, [isCurrentUserScope, userId])

  function updateApprovalMessage(item, updatedMessage) {
    if (!isCurrentUserScope(userId)) return
    queryClient.setQueryData(
      [
        'conversation-messages',
        userId,
        item.conversation_id,
      ],
      (current = []) => current.map((currentMessage) =>
        currentMessage.id === updatedMessage.id
          ? updatedMessage
          : currentMessage,
      ),
    )
    const proposal = updatedMessage.metadata?.action_approval
    if (
      proposal?.action_type === 'gmail.send' &&
      proposal.status === 'completed' &&
      notifiedSendProposalRef.current !== proposal.proposal_id
    ) {
      notifiedSendProposalRef.current = proposal.proposal_id
      setGmailSendNotice('Email sent successfully.')
      for (const folder of ['sent', 'all']) {
        queryClient.invalidateQueries({
          queryKey: [
            'gmail-messages',
            userId,
            proposal.connection_id,
            folder,
          ],
          refetchType: 'all',
        })
      }
    }
  }

  useEffect(() => {
    if (messages.length > 0) {
      scrollToBottom()
    }
  }, [messages.length, scrollToBottom])

  // --------------------------------------------------
  // 5. Send message mutation
  // --------------------------------------------------

  const sendMutation = useMutation({
    mutationFn: async ({
      content,
      idempotencyKey,
      existingConversationId,
      selectedConnectionId,
      userTimezone,
      requestUserId,
    }) => {
      const requestIsCurrent = () =>
        isCurrentUserScope(requestUserId)
      if (!requestIsCurrent()) {
        throw new Error('The active account changed during this request.')
      }

      let activeConversationId = existingConversationId

      // Create a conversation only for the first message.
      if (!activeConversationId) {
        const title =
          content.length > 197
            ? `${content.slice(0, 197)}...`
            : content

        const conversation =
          await conversationService.createConversation(title)

        activeConversationId = conversation.id
      }

      try {
        if (!requestIsCurrent()) {
          throw new Error('The active account changed during this request.')
        }
        const response =
          await conversationService.sendChatMessage(
            activeConversationId,
            content,
            idempotencyKey,
            selectedConnectionId || null,
            userTimezone,
          )

        if (
          !response?.user_message ||
          !response?.assistant_message ||
          response.conversation_id !== activeConversationId
        ) {
          throw new Error(
            'Unexpected response from the AI backend.'
          )
        }

        return {
          response,
          conversationId: activeConversationId,
        }
      } catch (err) {
        // Preserve the created conversation ID for retry.
        err.activeConversationId = activeConversationId
        throw err
      }
    },
  })

  const sending = sendMutation.isPending
    && sendMutation.variables?.requestUserId === userId

  // --------------------------------------------------
  // 6. Start a new chat
  // --------------------------------------------------

  function handleNewChat() {
    if (sendingRef.current || initializing) return

    newChatModeRef.current = true
    hasRestoredRef.current = true

    setSearchParams({}, { replace: true })

    setConversationId(null)
    setOptimisticMessages([])
    setMessage('')
    setError('')
    setProviderInfo(null)

    pendingRequestRef.current = null
  }

  // --------------------------------------------------
  // 7. Submit message
  // --------------------------------------------------

  async function handleSubmit(event) {
    event.preventDefault()

    if (!isCurrentUserScope(userId)) return
    const content = message.trim()

    if (!content || sendingRef.current || initializing) return

    sendingRef.current = true
    setError('')

    const temporaryId = `pending-${crypto.randomUUID()}`
    const userTimezone =
      Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

    // Reuse the same idempotency key for the same retry.
    if (
      !pendingRequestRef.current ||
      pendingRequestRef.current.content !== content ||
      pendingRequestRef.current.connectionId !== selectedGmailConnectionId ||
      pendingRequestRef.current.userTimezone !== userTimezone
    ) {
      pendingRequestRef.current = {
        content,
        key: crypto.randomUUID(),
        connectionId: selectedGmailConnectionId,
        userTimezone,
      }
    }

    const idempotencyKey = pendingRequestRef.current.key

    setOptimisticMessages((current) => [
      ...current,
      {
        id: temporaryId,
        role: 'user',
        content,
        pending: true,
      },
    ])

    setMessage('')
    scrollToBottom()

    try {
      const result = await sendMutation.mutateAsync({
        content,
        idempotencyKey,
        existingConversationId: conversationId,
        selectedConnectionId: selectedGmailConnectionId,
        userTimezone,
        requestUserId: userId,
      })

      if (!isCurrentUserScope(userId)) return

      const {
        response,
        conversationId: activeId,
      } = result

      setConversationId(activeId)
      newChatModeRef.current = false

      // Immediately update the cached messages.
      queryClient.setQueryData(
        ['conversation-messages', userId, activeId],
        (current = []) => [
          ...current.filter((item) => item.id !== temporaryId),
          response.user_message,
          response.assistant_message,
        ]
      )

      setOptimisticMessages([])

      setProviderInfo({
        provider: response.provider,
        model: response.model,
      })

      pendingRequestRef.current = null

      // Refresh the conversation list in the background.
      queryClient.invalidateQueries({
        queryKey: ['conversations', userId],
      })

      scrollToBottom()
    } catch (err) {
      if (!isCurrentUserScope(userId)) return

      if (err.activeConversationId) {
        setConversationId(err.activeConversationId)

        queryClient.invalidateQueries({
          queryKey: ['conversations', userId],
        })
      }

      if (err.status >= 500) {
        pendingRequestRef.current = null
      }

      setOptimisticMessages((current) =>
        current.filter((item) => item.id !== temporaryId)
      )

      // Keep the draft available for retry.
      setMessage(content)

      setError(
        err.message ||
        'Unable to process your message. Please try again.'
      )
    } finally {
      if (isCurrentUserScope(userId)) {
        sendingRef.current = false
      }
    }
  }

  // --------------------------------------------------
  // 8. Suggested prompts
  // --------------------------------------------------

  const suggestions = [
    {
      title: 'Organize my day',
      description: 'Help me plan my schedule.',
      prompt: 'Help me organize my day.',
    },
    {
      title: 'Understand my emails',
      description: 'Summarize important messages.',
      prompt: 'Help me summarize my important emails.',
    },
    {
      title: 'Remember something',
      description: 'Manage my personal information.',
      prompt: 'I want to save an important personal detail.',
    },
    {
      title: 'Explore ideas',
      description: 'Help me think through a problem.',
      prompt: 'Help me explore a new idea.',
    },
  ]

  // --------------------------------------------------
  // 9. UI
  // --------------------------------------------------

  return (
    <div className="mx-auto flex min-h-[calc(100vh-150px)] w-full max-w-5xl flex-col">

      {/* Page heading */}
      <div className="mb-6 flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-bold tracking-[1.5px] text-violet-400">
            <span>✧</span>
            PERSONAL AI ASSISTANT
          </div>

          <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">
            AI Assistant
          </h1>

          <p className={`mt-2 text-sm ${muted}`}>
            Your intelligent workspace starts with a conversation.
          </p>
        </div>

        <button
          type="button"
          onClick={handleNewChat}
          disabled={sending || initializing}
          className={`flex items-center justify-center gap-2 rounded-xl border px-4 py-3 text-sm font-semibold transition hover:border-violet-500/50 disabled:cursor-not-allowed disabled:opacity-50 ${
            isDark
              ? 'border-[#303448] bg-[#171a27] text-slate-300'
              : 'border-slate-200 bg-white text-slate-700'
          }`}
        >
          <span>＋</span>
          New chat
        </button>
      </div>

      {/* Chat container */}
      <section className={`flex min-h-[520px] flex-1 flex-col overflow-hidden rounded-2xl border ${surface}`}>

        {/* Chat header */}
        <div className={`flex items-center gap-3 border-b px-4 py-4 sm:px-6 ${
          isDark ? 'border-[#252838]' : 'border-slate-200'
        }`}>
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 to-indigo-600 text-xl text-white">
            ✧
          </div>

          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold">
              Personal AI
            </h2>

            <p className={`mt-1 text-xs ${muted}`}>
              {initializing
                ? 'Restoring your conversation...'
                : sending
                  ? 'Processing your message...'
                  : providerInfo
                    ? `${providerInfo.provider} · ${providerInfo.model}`
                    : conversationId
                      ? 'Conversation restored'
                      : 'Ready for a new conversation'}
            </p>
          </div>

          <span className="flex items-center gap-2 text-xs text-emerald-400">
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            {initializing
              ? 'Loading...'
              : sending
                ? 'Thinking...'
                : 'Ready'}
          </span>
        </div>

        {/* Messages */}
        <div className="flex flex-1 flex-col gap-6 overflow-y-auto p-4 sm:p-6">

          {initializing ? (
            <div className={`m-auto py-10 text-sm ${muted}`}>
              Loading your previous conversation...
            </div>
          ) : messages.length === 0 ? (
            <div className="m-auto flex w-full max-w-xl flex-col items-center py-10 text-center">
              <div className="mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500/20 to-indigo-500/20 text-4xl text-violet-400">
                ✧
              </div>

              <h2 className="text-xl font-bold sm:text-2xl">
                How can I help you today?
              </h2>

              <p className={`mt-3 max-w-md text-sm leading-6 ${muted}`}>
                Ask questions, organize your thoughts, or explore
                what your personal AI assistant can do.
              </p>

              <div className="mt-8 grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
                {suggestions.map((suggestion) => (
                  <button
                    key={suggestion.title}
                    type="button"
                    onClick={() => setMessage(suggestion.prompt)}
                    className={`rounded-xl border p-4 text-left transition hover:border-violet-500/50 ${
                      isDark
                        ? 'border-[#303448] bg-[#171a27] hover:bg-[#202333]'
                        : 'border-slate-200 bg-slate-50 hover:bg-violet-50'
                    }`}
                  >
                    <div className="text-sm font-semibold">
                      {suggestion.title}
                    </div>

                    <p className={`mt-2 text-xs leading-5 ${muted}`}>
                      {suggestion.description}
                    </p>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((item) => (
              <div
                key={item.id}
                className={`flex ${
                  item.role === 'user'
                    ? 'justify-end'
                    : 'justify-start'
                }`}
              >
                <div
                  className={`min-w-0 w-full max-w-[90%] rounded-2xl px-4 py-3 text-sm leading-7 sm:max-w-[85%] ${
                    item.role === 'user'
                      ? 'rounded-br-sm bg-gradient-to-r from-violet-600 to-indigo-600 text-white'
                      : isDark
                        ? 'rounded-bl-sm bg-[#202333] text-slate-200'
                        : 'rounded-bl-sm bg-slate-100 text-slate-800'
                  }`}
                >
                  {item.role === 'assistant' ? (
                    <AssistantMarkdown content={item.content} />
                  ) : (
                    <div className="whitespace-pre-wrap break-words">
                      {item.content}
                    </div>
                  )}
                  {item.role === 'assistant' &&
                    item.metadata?.response_completion?.status ===
                      'truncated' && (
                      <p
                        role="status"
                        className="mt-3 border-t border-current/15 pt-2 text-xs leading-5 opacity-80"
                      >
                        This response may be incomplete because the AI
                        reached its output limit. Ask me to continue from
                        where it stopped.
                      </p>
                    )}
                  {item.role === 'assistant' &&
                    item.metadata?.email_draft && (
                      <EmailDraftCard
                        message={item}
                        isDark={isDark}
                        connectionId={
                          item.metadata.email_draft_connection_id ||
                          selectedGmailConnectionId
                        }
                        onUpdated={(updatedMessage) => {
                          if (!isCurrentUserScope(userId)) {
                            return
                          }
                          queryClient.setQueryData(
                            [
                              'conversation-messages',
                              userId,
                              item.conversation_id,
                            ],
                            (current = []) =>
                              current.map((currentMessage) =>
                                currentMessage.id === updatedMessage.id
                                  ? updatedMessage
                                  : currentMessage,
                              ),
                          )
                        }}
                      />
                    )}
                  {item.role === 'assistant' &&
                    item.metadata?.action_approval && (
                      <ActionApprovalCard
                        message={item}
                        isDark={isDark}
                        onUpdated={(updatedMessage) =>
                          updateApprovalMessage(item, updatedMessage)
                        }
                      />
                    )}
                </div>
              </div>
            ))
          )}

          {sending && (
            <div className="flex justify-start">
              <div className={`rounded-xl px-4 py-3 text-xs ${muted} ${
                isDark ? 'bg-[#202333]' : 'bg-slate-100'
              }`}>
                AI is thinking...
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Error */}
        {error && (
          <div className="mx-4 mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400 sm:mx-5">
            {error}

            <p className="mt-1 text-xs">
              Your message is still in the input. You can retry it.
            </p>
          </div>
        )}
        {gmailSendNotice && (
          <div
            role="status"
            className="mx-4 mb-3 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-500 sm:mx-5"
          >
            {gmailSendNotice}
          </div>
        )}

        {/* Message composer */}
        <div className={`border-t p-4 sm:p-5 ${
          isDark ? 'border-[#252838]' : 'border-slate-200'
        }`}>
          {selectedGmailConnection && (
            <div className={`mb-3 flex flex-wrap items-center justify-between gap-2 text-xs ${muted}`}>
              <span>
                Gmail ready · {selectedGmailConnection.email || 'Connected account'}
              </span>
              <Link
                to="/dashboard/connectors"
                className="font-semibold text-violet-400 hover:text-violet-300"
              >
                Manage in Connectors
              </Link>
            </div>
          )}
          <form onSubmit={handleSubmit}>
            <div className={`flex items-end gap-2 rounded-2xl border p-2 ${
              isDark
                ? 'border-[#303448] bg-[#171a27]'
                : 'border-slate-200 bg-slate-50'
            }`}>
              <textarea
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                onKeyDown={(event) => {
                  if (
                    event.key === 'Enter' &&
                    !event.shiftKey &&
                    !event.nativeEvent.isComposing
                  ) {
                    event.preventDefault()
                    event.currentTarget.form?.requestSubmit()
                  }
                }}
                placeholder="Message your personal AI..."
                rows={2}
                maxLength={20000}
                disabled={sending || initializing}
                className={`max-h-36 min-h-12 flex-1 resize-none bg-transparent px-3 py-3 text-sm outline-none disabled:opacity-60 ${
                  isDark
                    ? 'text-white placeholder:text-slate-500'
                    : 'text-slate-800 placeholder:text-slate-400'
                }`}
              />

              <button
                type="submit"
                disabled={!message.trim() || sending || initializing}
                className="mb-1 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 text-lg text-white transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
                aria-label="Send message"
              >
                {sending ? '…' : '↑'}
              </button>
            </div>

            <div className={`mt-3 flex flex-col justify-between gap-2 text-[11px] sm:flex-row sm:items-center ${muted}`}>
              <span>Enter to send · Shift + Enter for a new line</span>
              <span>Messages are processed by your AI backend.</span>
            </div>
          </form>
        </div>
      </section>
    </div>
  )
}

export default Assistant
