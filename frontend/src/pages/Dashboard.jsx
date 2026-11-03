
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api.js'
import { conversationService } from '../services/conversationService.js'
import AppIcon from '../components/AppIcon'

const connectors = [
  {
    name: 'Gmail',
    detail: 'Email integration',
    icon: 'gmail',
    color: 'red',
  },
  {
    name: 'Google Calendar',
    detail: 'Events and schedule',
    icon: 'calendar',
    color: 'blue',
  },
  {
    name: 'Google Drive',
    detail: 'Files and documents',
    icon: 'drive',
    color: 'green',
  },
  {
    name: 'Google Contacts',
    detail: 'People and contacts',
    icon: 'contacts',
    color: 'yellow',
  },
]

const overviewCards = [
  {
    label: 'AI ACTIVITY',
    value: 'Ready',
    description: 'Your assistant is standing by',
    note: 'System available',
    icon: 'assistant',
    color: 'text-violet-400',
  },
  {
    label: 'LONG-TERM MEMORY',
    description: 'Saved personal memories',
    note: 'Memory settings control saving',
    icon: 'memory',
    color: 'text-blue-400',
    dynamic: 'memories',
  },
  {
    label: 'CONNECTED APPS',
    value: '0 / 4',
    description: 'Available integrations',
    note: 'Connect your favourite tools',
    icon: 'connectors',
    color: 'text-emerald-400',
  },
  {
    label: 'YOUR TASKS',
    value: '—',
    description: 'Personal tasks and reminders',
    note: 'Your productivity space',
    icon: 'tasks',
    color: 'text-orange-400',
  },
]

function formatConversationDate(dateValue) {
  if (!dateValue) return ''

  const date = new Date(dateValue)

  if (Number.isNaN(date.getTime())) return ''

  const now = new Date()

  const today = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate()
  )

  const yesterday = new Date(today)
  yesterday.setDate(yesterday.getDate() - 1)

  if (date >= today) {
    return `Today, ${date.toLocaleTimeString([], {
      hour: 'numeric',
      minute: '2-digit',
    })}`
  }

  if (date >= yesterday) {
    return 'Yesterday'
  }

  return date.toLocaleDateString([], {
    month: 'short',
    day: 'numeric',
    year:
      date.getFullYear() !== now.getFullYear()
        ? 'numeric'
        : undefined,
  })
}

function Dashboard() {
  const { user } = useAuth()
  const navigate = useNavigate()

  const { isDark, muted, surface, hoverSurface } =
    useOutletContext()

  const userId = user?.id

  const { data: connectionsData, isLoading: connectionsLoading, isError: connectionsError } = useQuery({
    queryKey: ['oauth-connections', userId],
    queryFn: () => api.get('/api/v1/oauth/connections'),
    enabled: Boolean(userId),
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
    refetchOnWindowFocus: false,
  })

  const googleConnections = (connectionsData?.connections || []).filter(
    (connection) =>
      connection.provider === 'google' &&
      connection.status === 'connected',
  )

  const isGmailConnected = googleConnections.length > 0

  const metadata = user?.user_metadata || {}

  const fullName =
    metadata.full_name ||
    metadata.name ||
    user?.email?.split('@')[0] ||
    'User'

  const firstName = fullName.split(' ')[0]

  // Pending memory reviews
  const {
    data: pendingReviews = [],
    isLoading: reviewCountLoading,
    isError: reviewCountError,
  } = useQuery({
    queryKey: ['memory-reviews', userId, 'pending'],

    queryFn: () =>
      api.get(
        '/api/v1/memory-reviews?status_filter=pending&limit=100&offset=0'
      ),

    enabled: Boolean(userId),

    select: (data) =>
      Array.isArray(data) ? data : [],
  })

  // Long-term memories
  const {
    data: longTermMemories = [],
    isLoading: memoryLoading,
    isError: memoryError,
  } = useQuery({
    queryKey: ['memories', userId, 'long_term'],

    queryFn: () =>
      api.get(
        '/api/v1/memories?memory_type=long_term&limit=100&offset=0'
      ),

    enabled: Boolean(userId),

    select: (data) =>
      Array.isArray(data) ? data : [],
  })

  // Recent conversations
  const {
    data: recentConversations = [],
    isLoading: conversationsLoading,
    isError: conversationsError,
  } = useQuery({
    queryKey: ['conversations', userId],

    queryFn: () =>
      conversationService.getConversations(),

    enabled: Boolean(userId),

    select: (data) => {
      if (!Array.isArray(data)) return []

      return data
        .filter((item) => !item.is_archived)
        .sort((a, b) => {
          const dateA = new Date(
            a.updated_at || a.created_at || 0
          ).getTime()

          const dateB = new Date(
            b.updated_at || b.created_at || 0
          ).getTime()

          return dateB - dateA
        })
        .slice(0, 3)
    },
  })

  const pendingReviewCount = pendingReviews.length

  const longTermMemoryCount = longTermMemories.length

  function openConversation(conversationId) {
    navigate(
      `/dashboard/assistant?conversationId=${encodeURIComponent(
        conversationId
      )}`
    )
  }

  return (
    <div className="space-y-6 sm:space-y-8">

      {/* Welcome */}
      <section className="flex flex-col items-start justify-between gap-5 sm:flex-row sm:items-center sm:gap-6">

        <div className="min-w-0">

          <div className="mb-3 flex items-center gap-2 text-xs font-bold tracking-[1.5px] text-violet-400 sm:tracking-[1.8px]">
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            YOUR PERSONAL WORKSPACE
          </div>

          <h1 className="text-2xl font-bold tracking-tight sm:text-3xl lg:text-4xl">

            Good day,{' '}

            <span className="bg-gradient-to-r from-violet-400 to-indigo-400 bg-clip-text text-transparent">
              {firstName}.
            </span>{' '}

            <AppIcon name="assistant" className="inline h-5 w-5 align-[-3px] text-violet-400" />

          </h1>

          <p className={`mt-3 max-w-xl text-sm leading-6 sm:text-base sm:leading-7 ${muted}`}>
            Your personal AI is ready to help you think, organize and get things done.
          </p>

        </div>

        <button
          type="button"
          onClick={() => navigate('/dashboard/assistant')}
          className="flex w-full items-center justify-center gap-3 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-5 py-3.5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/20 transition hover:brightness-110 sm:w-auto"
        >
          <AppIcon name="assistant" className="h-5 w-5" />
          Start a conversation
          <AppIcon name="arrow-right" className="h-4 w-4" />
        </button>

      </section>

      {/* Overview Cards */}
      <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">

        {overviewCards.map((card) => {

          let value = card.value

          if (card.dynamic === 'memories') {
            value = memoryLoading
              ? '...'
              : memoryError
                ? '—'
                : longTermMemoryCount
          }

          if (card.label === 'CONNECTED APPS') {
            value = connectionsLoading
              ? '...'
              : connectionsError
                ? '—'
                : `${isGmailConnected ? 1 : 0} / ${connectors.length}`
          }

          return (
            <div
              key={card.label}
              className={`min-w-0 rounded-2xl border p-5 transition hover:-translate-y-1 sm:p-6 ${surface}`}
            >

              <div className="flex items-center justify-between gap-2">

                <span className={card.color}>
                  <AppIcon name={card.icon} className="h-6 w-6" />
                </span>

                <span className={`text-xs font-bold tracking-wide sm:tracking-[1.2px] ${muted}`}>
                  {card.label}
                </span>

              </div>

              <div className="mt-6 text-2xl font-bold">
                {value}
              </div>

              <div className={`mt-2 text-sm leading-6 ${muted}`}>
                {card.description}
              </div>

              <div
                className={`mt-5 border-t pt-4 text-xs leading-5 ${muted} ${
                  isDark
                    ? 'border-[#252838]'
                    : 'border-slate-200'
                }`}
              >
                {card.note}
              </div>

            </div>
          )
        })}

      </section>

      {/* Pending Memory Reviews */}
      <section>

        <button
          type="button"
          onClick={() => navigate('/dashboard/memory')}
          className={`w-full rounded-2xl border p-5 text-left transition hover:-translate-y-1 sm:p-6 ${surface}`}
        >

          <div className="flex items-center justify-between gap-3">

            <span className="text-amber-400">
              <AppIcon name="memory" className="h-6 w-6" />
            </span>

            <span className={`text-xs font-bold tracking-wide ${muted}`}>
              MEMORY REVIEWS
            </span>

          </div>

          <div className="mt-6 flex flex-wrap items-center gap-3">

            <span className="text-2xl font-bold">
              {reviewCountLoading
                ? '...'
                : reviewCountError
                  ? '—'
                  : pendingReviewCount}
            </span>

            {pendingReviewCount > 0 && !reviewCountLoading && (
              <span className="rounded-full bg-amber-500/15 px-2.5 py-1 text-xs font-semibold text-amber-500">
                Action required
              </span>
            )}

          </div>

          <div className={`mt-2 text-sm ${muted}`}>
            Pending AI memory suggestions
          </div>

          <div
            className={`mt-5 border-t pt-4 text-xs ${
              isDark
                ? 'border-[#252838]'
                : 'border-slate-200'
            } ${muted}`}
          >
            Review and manage suggestions →
          </div>

        </button>

      </section>

      {/* Connectors and Conversations */}
      <section className="grid grid-cols-1 gap-5 xl:grid-cols-2">

        {/* Connected Apps */}
        <div className={`min-w-0 rounded-2xl border p-4 sm:p-6 ${surface}`}>

          <div className="flex items-center justify-between gap-3">

            <div className="min-w-0">

              <h2 className="text-base font-bold sm:text-lg">
                Your connected apps
              </h2>

              <p className={`mt-2 text-sm ${muted}`}>
                Bring your favourite tools together.
              </p>

            </div>

            <button
              type="button"
              onClick={() => navigate('/dashboard/connectors')}
              className="shrink-0 text-xs font-semibold text-violet-400 hover:text-violet-300 sm:text-sm"
            >
              Explore all →
            </button>

          </div>

          <div className="mt-5 space-y-2 sm:mt-6">

            {connectors.map((connector) => (

              <div
                key={connector.name}
                className={`flex min-w-0 items-center gap-3 rounded-xl p-3 transition sm:p-3.5 ${hoverSurface}`}
              >

                <div
                  className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl sm:h-11 sm:w-11 ${
                    connector.color === 'red'
                      ? 'bg-red-500/10 text-red-400'
                      : connector.color === 'blue'
                        ? 'bg-blue-500/10 text-blue-400'
                        : connector.color === 'green'
                          ? 'bg-emerald-500/10 text-emerald-400'
                          : 'bg-yellow-500/10 text-yellow-400'
                  }`}
                >
                  <AppIcon name={connector.icon} className="h-5 w-5" />
                </div>

                <div className="min-w-0 flex-1">

                  <div className="truncate text-sm font-semibold">
                    {connector.name}
                  </div>

                  <div className={`mt-1 truncate text-xs ${muted}`}>
                    {connector.name === 'Gmail' && isGmailConnected
                      ? `${googleConnections.length} Google account${googleConnections.length === 1 ? '' : 's'} connected`
                      : connector.detail}
                  </div>

                </div>

                <span
                  className={`shrink-0 rounded-full px-2 py-1 text-[10px] sm:px-3 sm:text-xs ${
                    connector.name === 'Gmail' && isGmailConnected
                      ? 'bg-emerald-500/10 text-emerald-500'
                      : isDark
                        ? 'bg-[#292d3c] text-slate-400'
                        : 'bg-slate-100 text-slate-500'
                  }`}
                >
                  {connector.name === 'Gmail'
                    ? connectionsLoading
                      ? 'Checking...'
                      : connectionsError
                        ? 'Unavailable'
                        : isGmailConnected
                          ? 'Connected'
                          : 'Not connected'
                    : 'Coming soon'}
                </span>

              </div>

            ))}

          </div>

        </div>

        {/* Recent Conversations */}
        <div className={`min-w-0 rounded-2xl border p-4 sm:p-6 ${surface}`}>

          <div className="flex items-center justify-between gap-3">

            <div className="min-w-0">

              <h2 className="text-base font-bold sm:text-lg">
                Recent conversations
              </h2>

              <p className={`mt-2 text-sm ${muted}`}>
                Pick up where you left off.
              </p>

            </div>

            <button
              type="button"
              onClick={() => navigate('/dashboard/conversations')}
              className="shrink-0 text-xs font-semibold text-violet-400 hover:text-violet-300 sm:text-sm"
            >
              View all →
            </button>

          </div>

          <div className="mt-5 space-y-2 sm:mt-6">

            {conversationsLoading ? (

              <div className={`py-8 text-center text-sm ${muted}`}>
                Loading recent conversations...
              </div>

            ) : conversationsError ? (

              <div className="py-8 text-center text-sm text-red-400">
                Unable to load recent conversations.
              </div>

            ) : recentConversations.length === 0 ? (

              <div className={`py-8 text-center text-sm ${muted}`}>
                No conversations yet. Start your first conversation!
              </div>

            ) : (

              recentConversations.map((conversation) => (

                <button
                  key={conversation.id}
                  type="button"
                  onClick={() => openConversation(conversation.id)}
                  className={`flex w-full min-w-0 items-center gap-3 rounded-xl p-3 text-left transition sm:p-3.5 ${hoverSurface}`}
                >

                  <div
                    className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-lg sm:h-11 sm:w-11 ${
                      isDark
                        ? 'bg-[#252838] text-violet-400'
                        : 'bg-violet-50 text-violet-500'
                    }`}
                  >
                    ◷
                  </div>

                  <div className="min-w-0 flex-1">

                    <div className="truncate text-sm font-semibold">
                      {conversation.title || 'Untitled conversation'}
                    </div>

                    <div className={`mt-1 text-xs ${muted}`}>
                      {formatConversationDate(
                        conversation.updated_at ||
                        conversation.created_at
                      )}
                    </div>

                  </div>

                  <span className={`shrink-0 ${muted}`}>
                    →
                  </span>

                </button>

              ))

            )}

          </div>

        </div>

      </section>

      {/* Assistant Banner */}
      <section className="flex flex-col items-start gap-4 rounded-2xl border border-violet-500/20 bg-gradient-to-r from-violet-600/15 to-indigo-600/10 p-5 sm:flex-row sm:items-center sm:gap-5 sm:p-6">

        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-violet-500/20 text-violet-400 sm:h-14 sm:w-14">
          <AppIcon name="assistant" className="h-6 w-6" />
        </div>

        <div className="min-w-0 flex-1">

          <h2 className="text-base font-bold">
            A little help goes a long way.
          </h2>

          <p className={`mt-2 text-sm leading-6 ${muted}`}>
            Ask questions, organize your thoughts, or let your assistant help manage your day.
          </p>

        </div>

        <button
          type="button"
          onClick={() => navigate('/dashboard/assistant')}
          className="w-full shrink-0 rounded-xl bg-violet-600 px-5 py-3.5 text-sm font-semibold text-white transition hover:bg-violet-500 sm:w-auto"
        >
          Talk to your AI →
        </button>

      </section>

      {/* Footer */}
      <footer
        className={`flex flex-col gap-2 border-t pt-5 text-xs sm:flex-row sm:justify-between sm:gap-3 ${
          isDark
            ? 'border-[#252838]'
            : 'border-slate-200'
        } ${muted}`}
      >

        <span>Personal AI Assistant</span>

        <span>Built around your life. Powered by AI.</span>

      </footer>

    </div>
  )
}

export default Dashboard
