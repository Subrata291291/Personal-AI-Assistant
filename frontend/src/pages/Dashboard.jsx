
import { useNavigate, useOutletContext } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const connectors = [
  {
    name: 'Gmail',
    detail: 'Email integration',
    icon: '✉',
    color: 'red',
  },
  {
    name: 'Google Calendar',
    detail: 'Events and schedule',
    icon: '▦',
    color: 'blue',
  },
  {
    name: 'Google Drive',
    detail: 'Files and documents',
    icon: '▰',
    color: 'green',
  },
  {
    name: 'Google Contacts',
    detail: 'People and contacts',
    icon: '♙',
    color: 'yellow',
  },
]

const conversations = [
  { title: 'Plan my weekly schedule', time: 'Today, 10:30 AM', icon: '◷' },
  { title: 'Summarize my recent emails', time: 'Yesterday', icon: '✉' },
  { title: 'Remember my project preferences', time: 'Oct 30, 2026', icon: '◈' },
]

const overviewCards = [
  {
    label: 'AI ACTIVITY',
    value: 'Ready',
    description: 'Your assistant is standing by',
    note: 'System available',
    icon: '✧',
    color: 'text-violet-400',
  },
  {
    label: 'LONG-TERM MEMORY',
    value: '—',
    description: 'Saved personal memories',
    note: 'Memory settings control saving',
    icon: '◈',
    color: 'text-blue-400',
  },
  {
    label: 'CONNECTED APPS',
    value: '0 / 4',
    description: 'Available integrations',
    note: 'Connect your favourite tools',
    icon: '⌘',
    color: 'text-emerald-400',
  },
  {
    label: 'YOUR TASKS',
    value: '—',
    description: 'Personal tasks and reminders',
    note: 'Your productivity space',
    icon: '✓',
    color: 'text-orange-400',
  },
]

function Dashboard() {
  const { user } = useAuth()
  const navigate = useNavigate()

  const { isDark, muted, surface, hoverSurface } =
    useOutletContext()

  const metadata = user?.user_metadata || {}

  const fullName =
    metadata.full_name ||
    metadata.name ||
    user?.email?.split('@')[0] ||
    'User'

  const firstName = fullName.split(' ')[0]

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
            ✦
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
          ✧ Start a conversation <span>→</span>
        </button>
      </section>

      {/* Overview cards */}
      <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {overviewCards.map((card) => (
          <div
            key={card.label}
            className={`min-w-0 rounded-2xl border p-5 transition hover:-translate-y-1 sm:p-6 ${surface}`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className={`text-2xl ${card.color}`}>
                {card.icon}
              </span>
              <span className={`text-xs font-bold tracking-wide sm:tracking-[1.2px] ${muted}`}>
                {card.label}
              </span>
            </div>

            <div className="mt-6 text-2xl font-bold">
              {card.value}
            </div>

            <div className={`mt-2 text-sm leading-6 ${muted}`}>
              {card.description}
            </div>

            <div
              className={`mt-5 border-t pt-4 text-xs leading-5 ${muted} ${
                isDark ? 'border-[#252838]' : 'border-slate-200'
              }`}
            >
              {card.note}
            </div>
          </div>
        ))}
      </section>

      {/* Connectors and conversations */}
      <section className="grid grid-cols-1 gap-5 xl:grid-cols-2">

        {/* Connected apps */}
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
                  className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-lg sm:h-11 sm:w-11 sm:text-xl ${
                    connector.color === 'red'
                      ? 'bg-red-500/10 text-red-400'
                      : connector.color === 'blue'
                        ? 'bg-blue-500/10 text-blue-400'
                        : connector.color === 'green'
                          ? 'bg-emerald-500/10 text-emerald-400'
                          : 'bg-yellow-500/10 text-yellow-400'
                  }`}
                >
                  {connector.icon}
                </div>

                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold">
                    {connector.name}
                  </div>
                  <div className={`mt-1 truncate text-xs ${muted}`}>
                    {connector.detail}
                  </div>
                </div>

                <span
                  className={`shrink-0 rounded-full px-2 py-1 text-[10px] sm:px-3 sm:text-xs ${
                    isDark
                      ? 'bg-[#292d3c] text-slate-400'
                      : 'bg-slate-100 text-slate-500'
                  }`}
                >
                  Coming soon
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Recent conversations */}
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
            {conversations.map((conversation) => (
              <button
                key={conversation.title}
                type="button"
                onClick={() => navigate('/dashboard/assistant')}
                className={`flex w-full min-w-0 items-center gap-3 rounded-xl p-3 text-left transition sm:p-3.5 ${hoverSurface}`}
              >
                <div
                  className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-lg sm:h-11 sm:w-11 ${
                    isDark
                      ? 'bg-[#252838] text-violet-400'
                      : 'bg-violet-50 text-violet-500'
                  }`}
                >
                  {conversation.icon}
                </div>

                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold">
                    {conversation.title}
                  </div>
                  <div className={`mt-1 text-xs ${muted}`}>
                    {conversation.time}
                  </div>
                </div>

                <span className={`shrink-0 ${muted}`}>→</span>
              </button>
            ))}
          </div>
        </div>
      </section>

      {/* Assistant banner */}
      <section className="flex flex-col items-start gap-4 rounded-2xl border border-violet-500/20 bg-gradient-to-r from-violet-600/15 to-indigo-600/10 p-5 sm:flex-row sm:items-center sm:gap-5 sm:p-6">
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-violet-500/20 text-2xl text-violet-400 sm:h-14 sm:w-14">
          ✧
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
          isDark ? 'border-[#252838]' : 'border-slate-200'
        } ${muted}`}
      >
        <span>Personal AI Assistant</span>
        <span>Built around your life. Powered by AI.</span>
      </footer>
    </div>
  )
}

export default Dashboard
