
import { useCallback, useEffect, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { conversationService } from '../services/conversationService.js'

function Assistant() {
  const { isDark, muted, surface } = useOutletContext()

  const [message, setMessage] = useState('')
  const [messages, setMessages] = useState([])
  const [conversationId, setConversationId] = useState(null)

  const [sending, setSending] = useState(false)
  const [initializing, setInitializing] = useState(true)
  const [error, setError] = useState('')
  const [providerInfo, setProviderInfo] = useState(null)

  const messagesEndRef = useRef(null)
  const pendingRequestRef = useRef(null)
  const sendingRef = useRef(false)

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      messagesEndRef.current?.scrollIntoView({
        behavior: 'smooth',
        block: 'end',
      })
    })
  }, [])

  // Restore the latest active conversation from the backend.
  useEffect(() => {
    let active = true

    async function restoreLatestConversation() {
      setInitializing(true)
      setError('')

      try {
        const conversations =
          await conversationService.getConversations()

        if (!Array.isArray(conversations)) {
          throw new Error('Invalid conversations response.')
        }

        const activeConversations = conversations
          .filter((item) => !item.is_archived)
          .sort(
            (a, b) =>
              new Date(b.updated_at).getTime() -
              new Date(a.updated_at).getTime()
          )

        if (activeConversations.length === 0) {
          if (active) {
            setConversationId(null)
            setMessages([])
          }
          return
        }

        const latestConversation = activeConversations[0]

        const conversationMessages =
          await conversationService.getMessages(
            latestConversation.id
          )

        if (!active) return

        setConversationId(latestConversation.id)
        setMessages(conversationMessages)
        setProviderInfo(null)

        requestAnimationFrame(() => {
          messagesEndRef.current?.scrollIntoView({
            behavior: 'auto',
            block: 'end',
          })
        })
      } catch (err) {
        if (active) {
          setError(
            err.message ||
            'Unable to restore your previous conversation.'
          )
        }
      } finally {
        if (active) {
          setInitializing(false)
        }
      }
    }

    restoreLatestConversation()

    return () => {
      active = false
    }
  }, [])

  function handleNewChat() {
    if (sendingRef.current || initializing) return

    setConversationId(null)
    setMessages([])
    setMessage('')
    setError('')
    setProviderInfo(null)

    pendingRequestRef.current = null
  }

  async function handleSubmit(event) {
    event.preventDefault()

    const content = message.trim()

    if (!content || sendingRef.current || initializing) return

    sendingRef.current = true
    setSending(true)
    setError('')

    const temporaryId = `pending-${crypto.randomUUID()}`

    // Reuse the same idempotency key when retrying
    // the same request after an uncertain result.
    if (
      !pendingRequestRef.current ||
      pendingRequestRef.current.content !== content
    ) {
      pendingRequestRef.current = {
        content,
        key: crypto.randomUUID(),
      }
    }

    const idempotencyKey = pendingRequestRef.current.key

    setMessages((current) => [
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
      let activeConversationId = conversationId

      // Create a conversation only when its first message is sent.
      if (!activeConversationId) {
        const title =
          content.length > 197
            ? `${content.slice(0, 197)}...`
            : content

        const conversation =
          await conversationService.createConversation(title)

        activeConversationId = conversation.id

        setConversationId(activeConversationId)
      }

      // Send the message through the authenticated LangGraph API.
      const response =
        await conversationService.sendChatMessage(
          activeConversationId,
          content,
          idempotencyKey
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

      // Replace the temporary user message with persisted records.
      setMessages((current) => [
        ...current.filter((item) => item.id !== temporaryId),
        response.user_message,
        response.assistant_message,
      ])

      setProviderInfo({
        provider: response.provider,
        model: response.model,
      })

      pendingRequestRef.current = null

      scrollToBottom()
    } catch (err) {
      setMessages((current) =>
        current.filter((item) => item.id !== temporaryId)
      )

      // Keep the draft available for retry.
      setMessage(content)

      setError(
        err.message ||
        'Unable to process your message. Please try again.'
      )
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

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
                  className={`max-w-[90%] rounded-2xl px-4 py-3 text-sm leading-7 sm:max-w-[75%] ${
                    item.role === 'user'
                      ? 'rounded-br-sm bg-gradient-to-r from-violet-600 to-indigo-600 text-white'
                      : isDark
                        ? 'rounded-bl-sm bg-[#202333] text-slate-200'
                        : 'rounded-bl-sm bg-slate-100 text-slate-800'
                  }`}
                >
                  <div className="whitespace-pre-wrap break-words">
                    {item.content}
                  </div>
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

        {/* Message composer */}
        <div className={`border-t p-4 sm:p-5 ${
          isDark ? 'border-[#252838]' : 'border-slate-200'
        }`}>
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
