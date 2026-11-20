
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
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'

import MyOrbitLogo from '../components/MyOrbitLogo.jsx'
import { useAuth } from '../context/AuthContext'
import EmailDraftCard from '../components/EmailDraftCard.jsx'
import ActionApprovalCard from '../components/ActionApprovalCard.jsx'
import { resolveAssistantGmailConnection } from '../services/gmailAccountSelection.js'
import { conversationService } from '../services/conversationService.js'
import { api } from '../lib/api.js'
import AssistantMarkdown from '../components/AssistantMarkdown.jsx'
import AssistantTypingIndicator from '../components/AssistantTypingIndicator.jsx'
import TransientStatus from '../components/TransientStatus.jsx'
import {
  requireArrayResponse,
  requireConnectionsResponse,
} from '../lib/responseValidation'

const OUTPUT_LIMIT_NOTICE =
  'The AI reached its output limit. This answer contains only the verified content shown above; narrow the request for a smaller result.'

function completionNotices(completion) {
  if (!completion || typeof completion !== 'object') return []

  const status = String(completion.status || '').toLowerCase()
  const outputLimited = Boolean(completion.output_limited)
  const notices = []

  if (status === 'failed') {
    notices.push(
      'This response could not be completed. No email details were verified.',
    )
  } else if (status === 'cancelled') {
    notices.push(
      'This response was cancelled before email summaries were generated.',
    )
  } else if (status === 'partial') {
    const requested = Number(completion.requested_item_count)
    const processed = Number(completion.processed_item_count)
    const remaining = Number(completion.remaining_item_count)
    const details = []
    if (Number.isFinite(requested) && Number.isFinite(processed)) {
      details.push(`${processed} of ${requested} requested items were processed`)
    }
    if (Number.isFinite(remaining) && remaining > 0) {
      details.push(
        `${remaining} item${remaining === 1 ? '' : 's'} remain${remaining === 1 ? 's' : ''}`,
      )
    }
    notices.push(
      details.length
        ? `This response is partial. ${details.join('; ')}.`
        : 'This response is partial. Some requested items were not included.',
    )
  } else if (status === 'truncated') {
    notices.push(OUTPUT_LIMIT_NOTICE)
  } else if (status === 'unknown') {
    notices.push(
      'This response may be incomplete; its completion status could not be verified.',
    )
  }

  if (outputLimited && status !== 'truncated') {
    notices.push(OUTPUT_LIMIT_NOTICE)
  }

  return notices
}

function CompletionNotice({ completion }) {
  const notices = completionNotices(completion)
  if (notices.length === 0) return null

  return (
    <div className="mt-3 space-y-1 border-t border-current/15 pt-2">
      {notices.map((notice) => (
        <p
          key={notice}
          role="status"
          className="text-xs leading-5 opacity-80"
        >
          {notice}
        </p>
      ))}
    </div>
  )
}

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
  const [errorRestoredInput, setErrorRestoredInput] = useState(false)
  const [gmailSendNotice, setGmailSendNotice] = useState('')
  const [providerInfo, setProviderInfo] = useState(null)
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamStatus, setStreamStatus] = useState('')
  const [postContentStatus, setPostContentStatus] = useState('')

  const messagesEndRef = useRef(null)
  const messagesContainerRef = useRef(null)
  const atBottomRef = useRef(true)
  const textareaRef = useRef(null)
  const pendingRequestRef = useRef(null)
  const notifiedSendProposalRef = useRef(null)
  const sendingRef = useRef(false)
  const abortControllerRef = useRef(null)

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

  const messages = useMemo(() => {
    const combined = [
      ...(messagesQuery.data || []),
      ...optimisticMessages,
    ]
    const seen = new Set()
    return combined.filter((item) => {
      if (!item || !item.id || seen.has(item.id)) return false
      seen.add(item.id)
      return true
    })
  }, [messagesQuery.data, optimisticMessages])

  // Drop optimistic copies once the persisted versions arrive.
  useEffect(() => {
    if (!Array.isArray(messagesQuery.data)) return
    const serverIds = new Set(
      messagesQuery.data.map((item) => item.id).filter(Boolean)
    )
    if (serverIds.size === 0) return
    setOptimisticMessages((current) => {
      const remaining = current.filter(
        (item) => !serverIds.has(item.id)
      )
      return remaining.length === current.length
        ? current
        : remaining
    })
  }, [messagesQuery.data])

  const initializing =
    !userId ||
    conversationsQuery.isPending ||
    (
      Boolean(conversationId) &&
      messagesQuery.isPending &&
      !isStreaming
    )

  // --------------------------------------------------
  // 4. Scroll to latest message
  // --------------------------------------------------

  const scrollToBottom = useCallback((force = false) => {
    if (!force && !atBottomRef.current) return
    requestAnimationFrame(() => {
      if (!isCurrentUserScope(userId)) return
      messagesEndRef.current?.scrollIntoView({
        behavior: 'smooth',
        block: 'end',
      })
      atBottomRef.current = true
    })
  }, [isCurrentUserScope, userId])

  function handleMessagesScroll(event) {
    const element = event.currentTarget
    const distanceFromBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight
    atBottomRef.current = distanceFromBottom <= 120
  }

  // Keep the composer compact while still allowing comfortable multiline input.
  useLayoutEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 144)}px`
  }, [message])

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
  // 5. Streaming send state
  // --------------------------------------------------

  const sending = isStreaming

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
    setErrorRestoredInput(false)
    setProviderInfo(null)
    setStreamStatus('')
    setPostContentStatus('')
    setIsStreaming(false)
    setGmailSendNotice('')

    pendingRequestRef.current = null
  }

  // --------------------------------------------------
  // 7. Submit message
  // --------------------------------------------------

  // A terminally failed request can never be retried with the same key. Clear
  // the retry metadata only when the backend confirms `failed` (either in the
  // error itself or through the scoped status endpoint). Transport failures and
  // uncertain outcomes keep the key so same-key replay stays duplicate-safe.
  async function clearFailedRequestIfConfirmed(
    conversationIdToCheck,
    idempotencyKey,
    explicitlyFailed,
  ) {
    let confirmed = Boolean(explicitlyFailed)
    if (!confirmed && conversationIdToCheck && idempotencyKey) {
      try {
        const status = await conversationService.getChatRequestStatus(
          conversationIdToCheck,
          idempotencyKey,
        )
        confirmed = status?.status === 'failed'
      } catch {
        // Status is unavailable: treat the outcome as uncertain and keep the key.
      }
    }
    if (confirmed && pendingRequestRef.current?.key === idempotencyKey) {
      pendingRequestRef.current = null
    }
    return confirmed
  }

  async function handleSubmit(event) {
    event.preventDefault()

    if (!isCurrentUserScope(userId)) return
    const content = message.trim()

    if (!content || sendingRef.current || initializing) return

    sendingRef.current = true
    setError('')
    setErrorRestoredInput(false)

    const temporaryId = `pending-${crypto.randomUUID()}`
    const temporaryAssistantId = `pending-assistant-${crypto.randomUUID()}`
    const configuredTimezone = user?.user_metadata?.timezone
    let userTimezone = 'Asia/Kolkata'
    if (typeof configuredTimezone === 'string' && configuredTimezone) {
      try {
        new Intl.DateTimeFormat('en', { timeZone: configuredTimezone })
        userTimezone = configuredTimezone
      } catch {
        userTimezone = 'Asia/Kolkata'
      }
    }

    // Reuse the same idempotency key for the same retry. A same-key replay
    // supersedes the previous optimistic pair so a stopped attempt cannot
    // remain visible alongside the retried one.
    const previousRequest = pendingRequestRef.current
    const reuseKey = Boolean(
      previousRequest &&
        previousRequest.content === content &&
        previousRequest.connectionId === selectedGmailConnectionId &&
        previousRequest.userTimezone === userTimezone,
    )
    const supersededIds = reuseKey
      ? [
          previousRequest.userMessageId,
          previousRequest.assistantMessageId,
        ].filter(Boolean)
      : []

    if (!reuseKey) {
      pendingRequestRef.current = {
        content,
        key: crypto.randomUUID(),
        connectionId: selectedGmailConnectionId,
        userTimezone,
      }
    }
    pendingRequestRef.current.userMessageId = temporaryId
    pendingRequestRef.current.assistantMessageId = temporaryAssistantId

    const idempotencyKey = pendingRequestRef.current.key
    const controller = new AbortController()
    abortControllerRef.current = controller

    setOptimisticMessages((current) => [
      ...current.filter((item) => !supersededIds.includes(item.id)),
      {
        id: temporaryId,
        role: 'user',
        content,
        pending: true,
      },
      {
        id: temporaryAssistantId,
        role: 'assistant',
        content: '',
        pending: true,
        streaming: true,
      },
    ])

    setMessage('')
    setIsStreaming(true)
    setStreamStatus('')
    setPostContentStatus('')
    atBottomRef.current = true
    scrollToBottom(true)

    let activeConversationId = conversationId
    let contentStarted = false

    try {
      if (!activeConversationId) {
        const title =
          content.length > 197
            ? `${content.slice(0, 197)}...`
            : content
        const conversation =
          await conversationService.createConversation(title)
        if (!isCurrentUserScope(userId)) return
        activeConversationId = conversation.id
        setConversationId(activeConversationId)
      }

      await conversationService.streamChatMessage(
        activeConversationId,
        content,
        idempotencyKey,
        selectedGmailConnectionId || null,
        userTimezone,
        (evt) => {
          if (!evt || !evt.type) return
          if (!isCurrentUserScope(userId)) return

          if (evt.type === 'status') {
            const statusText = evt.message || ''
            // Before the first token this belongs beside the typing indicator.
            // Afterward it must stay visible as an inline progress line.
            if (contentStarted) setPostContentStatus(statusText)
            else setStreamStatus(statusText)
            return
          }

          if (evt.type === 'token') {
            contentStarted = true
            // Real text is now on screen; the thinking indicator has served
            // its purpose and must not linger next to the streamed answer.
            // Post-content progress stays until a new status or done event.
            setStreamStatus('')
            setOptimisticMessages((current) =>
              current.map((m) =>
                m.id === temporaryAssistantId
                  ? { ...m, content: (m.content || '') + (evt.content || '') }
                  : m
              )
            )
            scrollToBottom()
            return
          }

          if (evt.type === 'replace') {
            contentStarted = true
            setStreamStatus('')
            setOptimisticMessages((current) =>
              current.map((m) =>
                m.id === temporaryAssistantId
                  ? { ...m, content: evt.content || '' }
                  : m
              )
            )
            scrollToBottom()
            return
          }

          if (evt.type === 'done') {
            const doneConvId = evt.conversation_id || activeConversationId
            const doneCompletion =
              evt.completion && typeof evt.completion === 'object'
                ? evt.completion
                : null
            if (doneConvId) {
              activeConversationId = doneConvId
              setConversationId(doneConvId)
            }
            newChatModeRef.current = false
            if (evt.provider || evt.model) {
              setProviderInfo({ provider: evt.provider, model: evt.model })
            }
            pendingRequestRef.current = null
            setOptimisticMessages((current) =>
              current
                .map((m) => {
                  if (
                    m.id === temporaryAssistantId &&
                    !(m.content || '').trim() &&
                    !Object.keys(m.metadata || {}).some(
                      (key) => key !== 'response_completion',
                    )
                  ) {
                    // Done-only replay: no token content was streamed, so wait
                    // for the persisted answer instead of an empty bubble.
                    return null
                  }
                  const next = { ...m, pending: false, streaming: false }
                  if (m.id === temporaryId && evt.user_message_id) {
                    next.id = evt.user_message_id
                  }
                  if (m.id === temporaryAssistantId && evt.message_id) {
                    next.id = evt.message_id
                  }
                  if (m.id === temporaryAssistantId && doneCompletion) {
                    next.metadata = {
                      ...(m.metadata || {}),
                      response_completion: doneCompletion,
                    }
                  }
                  return next
                })
                .filter(Boolean)
            )
            setStreamStatus('')
            setPostContentStatus('')
            setIsStreaming(false)
            queryClient.invalidateQueries({ queryKey: ['conversations', userId] })
            if (doneConvId) {
              queryClient.invalidateQueries({
                queryKey: ['conversation-messages', userId, doneConvId],
              })
            }
            scrollToBottom(true)
          }
        },
        controller.signal,
      )
    } catch (err) {
      if (!isCurrentUserScope(userId)) return

      const aborted = err?.name === 'AbortError'
      const partial = Boolean(err?.partial) || contentStarted
      const message = String(err?.message || '')
      const explicitlyFailed = Boolean(
        (err?.status === 409 || err?.streamError) &&
          /previously failed/i.test(message),
      )

      // Only a confirmed terminal failure invalidates the key. Network and
      // uncertain failures keep it for duplicate-safe same-key replay.
      const terminalFailure = await clearFailedRequestIfConfirmed(
        activeConversationId,
        idempotencyKey,
        explicitlyFailed,
      )
      const failureMessage = terminalFailure
        ? 'The previous request failed. Retry to send this message as a new request.'
        : message

      if (aborted && !contentStarted) {
        // Stopped before any answer arrived: keep the user's message, but do
        // not leave an empty assistant bubble behind.
        setOptimisticMessages((current) =>
          current.filter((item) => item.id !== temporaryAssistantId)
        )
        setError('Response stopped before it started.')
        setErrorRestoredInput(false)
      } else if (partial || aborted) {
        // Keep whatever was already rendered; never duplicate it. A local Stop
        // never reaches `done`, so record the cancellation in the optimistic
        // bubble's completion metadata to surface the same notice.
        const cancelledLocally = aborted && !terminalFailure
        setOptimisticMessages((current) =>
          current.map((item) => {
            if (item.id !== temporaryAssistantId) {
              return { ...item, pending: false }
            }
            return {
              ...item,
              pending: false,
              streaming: false,
              ...(cancelledLocally
                ? {
                    metadata: {
                      ...(item.metadata || {}),
                      response_completion: {
                        ...(item.metadata?.response_completion || {}),
                        status: 'cancelled',
                      },
                    },
                  }
                : {}),
            }
          })
        )
        setError(
          aborted && !terminalFailure
            ? 'Response stopped before it finished.'
            : (failureMessage || 'The response was interrupted.')
        )
        setErrorRestoredInput(false)
      } else {
        setOptimisticMessages((current) =>
          current.filter(
            (item) =>
              item.id !== temporaryId && item.id !== temporaryAssistantId
          )
        )
        setMessage(content)
        setError(
          failureMessage ||
            'Unable to process your message. Please try again.'
        )
        setErrorRestoredInput(true)
      }
    } finally {
      if (isCurrentUserScope(userId)) {
        sendingRef.current = false
        setIsStreaming(false)
        setStreamStatus('')
        setPostContentStatus('')
        abortControllerRef.current = null
      }
    }
  }

  function handleStop() {
    const controller = abortControllerRef.current
    if (controller) {
      controller.abort()
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
    <div className="mx-auto flex min-h-[calc(100dvh-150px)] w-full max-w-5xl flex-col">

      {/* Page heading */}
      <div className="mb-6 flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-bold tracking-[1.5px] text-violet-400">
            <span>✧</span>
            MYORBIT AI
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
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-violet-500/10 ring-1 ring-violet-400/20 text-violet-300">
            <MyOrbitLogo className="h-6 w-6" />
          </div>

          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold">
              MyOrbit AI
            </h2>

            <p className={`mt-1 text-xs ${muted}`}>
              {initializing
                ? 'Restoring your conversation...'
                : sending
                  ? 'Generating response...'
                  : providerInfo
                    ? `${providerInfo.provider} · ${providerInfo.model}`
                    : conversationId
                      ? 'Conversation restored'
                      : 'Ready for a new conversation'}
            </p>
          </div>

          <span className="flex items-center gap-2 text-xs text-emerald-400">
            <span className={`h-1.5 w-1.5 rounded-full ${
              sending ? 'bg-violet-400' : 'bg-emerald-400'
            }`} />
            {initializing
              ? 'Loading...'
              : sending
                ? 'Active'
                : 'Ready'}
          </span>
        </div>

        {/* Messages */}
        <div
          ref={messagesContainerRef}
          onScroll={handleMessagesScroll}
          className="assistant-scrollport flex flex-1 flex-col gap-5 overflow-y-auto overscroll-contain p-4 sm:p-6"
        >

          {initializing ? (
            <div className={`m-auto py-10 text-sm ${muted}`}>
              Loading your previous conversation...
            </div>
          ) : messages.length === 0 ? (
            <div className="m-auto flex w-full max-w-xl flex-col items-center py-10 text-center">
              <div className="mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-violet-500/10 text-4xl text-violet-400">
                ✧
              </div>

              <h2 className="text-xl font-bold sm:text-2xl">
                How can I help you today?
              </h2>

              <p className={`mt-3 max-w-md text-sm leading-6 ${muted}`}>
                Ask questions, organize your thoughts, or explore
                what MyOrbit AI can do.
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
                  className={`min-w-0 rounded-2xl px-4 py-3 text-sm leading-7 shadow-sm ${
                    item.role === 'user'
                      ? 'w-fit max-w-[88%] rounded-br-sm bg-violet-600 text-white sm:max-w-[75%]'
                      : 'w-full max-w-[95%] rounded-bl-sm sm:max-w-[88%] ' + (
                        isDark
                          ? 'bg-[#202333] text-slate-200'
                          : 'bg-slate-100 text-slate-800'
                      )
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
                    item.streaming &&
                    !item.content && (
                      <AssistantTypingIndicator status={streamStatus} />
                    )}
                  {item.role === 'assistant' &&
                    item.streaming &&
                    Boolean(item.content) &&
                    postContentStatus && (
                      <p
                        role="status"
                        className={`mt-2 text-xs ${muted}`}
                      >
                        {postContentStatus}
                      </p>
                    )}
                  {item.role === 'assistant' && (
                    <CompletionNotice
                      completion={item.metadata?.response_completion}
                    />
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

          <div ref={messagesEndRef} />
        </div>

        {/* Error */}
        {error && (
          <div className="mx-4 mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400 sm:mx-5">
            {error}

            {errorRestoredInput && (
              <p className="mt-1 text-xs">
                Your message is still in the input. You can retry it.
              </p>
            )}
          </div>
        )}
        <TransientStatus
          message={gmailSendNotice}
          onDismiss={() => setGmailSendNotice('')}
          className="mx-4 mb-3 px-4 py-3 text-sm sm:mx-5"
        />

        {/* Message composer */}
        <div className={`border-t px-4 pb-4 pt-3 sm:px-6 sm:pb-5 ${
          isDark
            ? 'border-[#252838] bg-[#161926]/60'
            : 'border-slate-200 bg-slate-50/60'
        }`}>
          {selectedGmailConnection && (
            <div className={`mb-2.5 flex flex-wrap items-center justify-between gap-2 text-[11px] ${muted}`}>
              <span className="truncate">
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
            <div className={`flex items-end gap-2 rounded-[1.25rem] border p-2 shadow-sm transition focus-within:border-violet-500/60 focus-within:ring-2 focus-within:ring-violet-500/15 ${
              isDark
                ? 'border-[#303448] bg-[#171a27]'
                : 'border-slate-200 bg-white'
            }`}>
              <textarea
                ref={textareaRef}
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
                rows={1}
                maxLength={20000}
                disabled={sending || initializing}
                className={`max-h-36 min-h-[2.75rem] flex-1 resize-none bg-transparent px-3 py-2.5 text-sm leading-6 outline-none placeholder:text-slate-500 disabled:cursor-not-allowed disabled:opacity-50 ${
                  isDark
                    ? 'text-white'
                    : 'text-slate-800'
                }`}
              />

              {sending && (
                <button
                  type="button"
                  onClick={handleStop}
                  className="mb-0.5 flex h-10 shrink-0 items-center justify-center gap-1.5 rounded-xl border border-red-500/40 px-3 text-xs font-semibold text-red-400 transition hover:bg-red-500/10"
                  aria-label="Stop generating"
                >
                  <span className="h-2 w-2 rounded-[2px] bg-current" aria-hidden="true" />
                  Stop
                </button>
              )}

              <button
                type="submit"
                disabled={!message.trim() || sending || initializing}
                className="mb-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-600 text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-40"
                aria-label="Send message"
              >
                <svg
                  viewBox="0 0 24 24"
                  className="h-4 w-4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M12 19V5" />
                  <path d="m5 12 7-7 7 7" />
                </svg>
              </button>
            </div>

            <div className={`mt-2 flex flex-col gap-1 text-[10px] sm:flex-row sm:items-center sm:justify-between ${muted}`}>
              <span>
                <kbd className="rounded border border-current/20 px-1">Enter</kbd>
                {' '}to send ·{' '}
                <kbd className="rounded border border-current/20 px-1">Shift + Enter</kbd>
                {' '}for a new line
              </span>
              <span className="hidden sm:inline">
                Messages are processed by your AI backend.
              </span>
            </div>
          </form>
        </div>
      </section>
    </div>
  )
}

export default Assistant
