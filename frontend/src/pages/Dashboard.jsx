
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const navigation = [
  {
    section: 'WORKSPACE',
    items: [
      { name: 'Dashboard', icon: '▦' },
      { name: 'AI Assistant', icon: '✧' },
      { name: 'Conversations', icon: '☷' },
    ],
  },
  {
    section: 'PERSONAL',
    items: [
      { name: 'Memory Center', icon: '◈' },
      { name: 'Tasks & Reminders', icon: '✓' },
    ],
  },
  {
    section: 'INTEGRATIONS',
    items: [{ name: 'Connectors', icon: '⌘' }],
  },
]

const connectors = [
  { name: 'Gmail', detail: 'Email integration', icon: '✉', color: 'red' },
  { name: 'Google Calendar', detail: 'Events and schedule', icon: '▦', color: 'blue' },
  { name: 'Google Drive', detail: 'Files and documents', icon: '▰', color: 'green' },
  { name: 'Google Contacts', detail: 'People and contacts', icon: '♙', color: 'yellow' },
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
  const { user, signOut } = useAuth()
  const navigate = useNavigate()

  const [theme, setTheme] = useState('dark')
  const [activePage, setActivePage] = useState('Dashboard')
  const [search, setSearch] = useState('')
  const [profileOpen, setProfileOpen] = useState(false)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [logoutLoading, setLogoutLoading] = useState(false)
  const [profileError, setProfileError] = useState('')

  const isDark = theme === 'dark'

  const metadata = user?.user_metadata || {}

  const fullName =
    metadata.full_name ||
    metadata.name ||
    user?.email?.split('@')[0] ||
    'User'

  const firstName = fullName.split(' ')[0]
  const email = user?.email || ''

  const avatarUrl =
    metadata.avatar_url ||
    metadata.picture ||
    ''

  const initials = fullName
    .split(' ')
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join('')

  const surface = isDark
    ? 'bg-[#11131c] border-[#252838] text-slate-100'
    : 'bg-white border-slate-200 text-slate-800'

  const muted = isDark ? 'text-slate-400' : 'text-slate-500'

  const hoverSurface = isDark
    ? 'hover:bg-[#202333]'
    : 'hover:bg-slate-100'

  const inputSurface = isDark
    ? 'bg-[#171a27] border-[#2b3042] text-slate-200 placeholder:text-slate-500'
    : 'bg-slate-50 border-slate-200 text-slate-700 placeholder:text-slate-400'

  function navigateTo(page) {
    setActivePage(page)
    setMobileMenuOpen(false)
    setProfileOpen(false)
  }

  async function handleLogout() {
    setLogoutLoading(true)
    setProfileError('')

    try {
      await signOut()
      navigate('/login', { replace: true })
    } catch (error) {
      setProfileError(error.message || 'Unable to log out.')
      setLogoutLoading(false)
    }
  }

  return (
    <div
      className={`min-h-screen overflow-x-hidden ${
        isDark
          ? 'bg-[#0b0d14] text-slate-100'
          : 'bg-[#f5f6fa] text-slate-800'
      }`}
    >

      {/* MOBILE SIDEBAR OVERLAY */}
      {mobileMenuOpen && (
        <button
          type="button"
          aria-label="Close navigation menu"
          onClick={() => setMobileMenuOpen(false)}
          className="fixed inset-0 z-40 bg-black/60 backdrop-blur-[2px] md:hidden"
        />
      )}

      {/* SIDEBAR */}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-[280px] flex-col border-r transition-transform duration-300 ease-in-out md:w-64 md:translate-x-0 ${
          surface
        } ${
          mobileMenuOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >

        {/* BRAND */}
        <div className="flex h-[76px] shrink-0 items-center justify-between gap-3 border-b border-inherit px-5">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 to-indigo-600 text-xl text-white shadow-lg shadow-indigo-500/20">
              ✧
            </div>

            <div className="min-w-0">
              <div className="text-base font-bold tracking-wide">
                Personal AI
              </div>
              <div className={`mt-1 text-xs font-semibold tracking-[1.5px] ${muted}`}>
                INTELLIGENCE HUB
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={() => setMobileMenuOpen(false)}
            aria-label="Close sidebar"
            className={`rounded-lg p-2 text-lg md:hidden ${hoverSurface}`}
          >
            ✕
          </button>
        </div>

        {/* NEW CONVERSATION */}
        <div className="shrink-0 p-4">
          <button
            onClick={() => navigateTo('AI Assistant')}
            className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-3 py-3.5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/10 transition hover:brightness-110"
          >
            <span className="text-xl">＋</span>
            New conversation
          </button>
        </div>

        {/* NAVIGATION */}
        <nav className="min-h-0 flex-1 space-y-7 overflow-y-auto overflow-x-hidden px-4 pb-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">

          {navigation.map((group) => (
            <div key={group.section}>
              <div className={`mb-3 px-3 text-xs font-bold tracking-[1.5px] ${muted}`}>
                {group.section}
              </div>

              <div className="space-y-1">
                {group.items.map((item) => {
                  const active = activePage === item.name

                  return (
                    <button
                      key={item.name}
                      onClick={() => navigateTo(item.name)}
                      className={`flex w-full items-center gap-3 rounded-xl px-3 py-3.5 text-left text-sm transition ${
                        active
                          ? 'bg-violet-500/15 font-semibold text-violet-400'
                          : `${muted} ${hoverSurface}`
                      }`}
                    >
                      <span className="w-5 shrink-0 text-center text-xl">
                        {item.icon}
                      </span>

                      <span className="flex-1">{item.name}</span>

                      {item.name === 'Connectors' && (
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs ${
                            isDark ? 'bg-[#292d3c]' : 'bg-slate-200'
                          }`}
                        >
                          4
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </nav>

        {/* SIDEBAR BOTTOM */}
        <div
          className={`relative shrink-0 border-t p-4 ${
            isDark ? 'border-[#252838]' : 'border-slate-200'
          }`}
        >
          <button
            onClick={() => navigateTo('Settings')}
            className={`flex w-full items-center gap-3 rounded-xl px-3 py-3.5 text-sm ${hoverSurface} ${muted}`}
          >
            <span className="w-5 text-center text-xl">⚙</span>
            Settings
          </button>

          {/* PROFILE */}
          <div className="relative mt-3">
            <button
              type="button"
              onClick={() => setProfileOpen((current) => !current)}
              aria-expanded={profileOpen}
              aria-label="Open profile menu"
              className={`flex w-full items-center gap-3 rounded-xl border border-transparent p-2.5 text-left transition ${hoverSurface} ${
                profileOpen ? 'border-violet-500/30' : ''
              }`}
            >
              {avatarUrl ? (
                <img
                  src={avatarUrl}
                  alt={fullName}
                  referrerPolicy="no-referrer"
                  className="h-11 w-11 shrink-0 rounded-full object-cover"
                />
              ) : (
                <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 text-base font-bold text-white">
                  {initials || 'U'}
                </div>
              )}

              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold">
                  {fullName}
                </div>
                <div className={`mt-1 truncate text-xs ${muted}`}>
                  {email}
                </div>
              </div>

              <span className={`text-base ${muted}`}>
                {profileOpen ? '⌃' : '⌄'}
              </span>
            </button>

            {profileOpen && (
              <div
                className={`absolute bottom-full left-0 z-50 mb-3 w-full min-w-[235px] rounded-xl border p-2 shadow-2xl ${
                  isDark
                    ? 'border-[#303448] bg-[#191c29]'
                    : 'border-slate-200 bg-white'
                }`}
              >
                <div className="px-3 py-3">
                  <div className="truncate text-sm font-semibold">
                    {fullName}
                  </div>
                  <div className={`mt-1 break-all text-xs ${muted}`}>
                    {email}
                  </div>
                </div>

                <div className={`my-1 border-t ${isDark ? 'border-[#303448]' : 'border-slate-200'}`} />

                <button
                  onClick={() => navigateTo('Settings')}
                  className={`flex w-full items-center gap-3 rounded-lg px-3 py-3 text-sm ${muted} ${hoverSurface}`}
                >
                  <span>♙</span>
                  My Profile
                </button>

                <button
                  onClick={() => navigateTo('Settings')}
                  className={`flex w-full items-center gap-3 rounded-lg px-3 py-3 text-sm ${muted} ${hoverSurface}`}
                >
                  <span>⚙</span>
                  Account Settings
                </button>

                <div className={`my-1 border-t ${isDark ? 'border-[#303448]' : 'border-slate-200'}`} />

                {profileError && (
                  <div className="px-3 py-2 text-sm text-rose-500">
                    {profileError}
                  </div>
                )}

                <button
                  onClick={handleLogout}
                  disabled={logoutLoading}
                  className="flex w-full items-center gap-3 rounded-lg px-3 py-3 text-sm text-rose-500 transition hover:bg-rose-500/10 disabled:opacity-50"
                >
                  <span>↪</span>
                  {logoutLoading ? 'Signing out...' : 'Sign out'}
                </button>
              </div>
            )}
          </div>
        </div>
      </aside>

      {/* MAIN CONTENT */}
      <main className="min-h-screen min-w-0 md:ml-64">

        {/* TOPBAR */}
        <header
          className={`sticky top-0 z-30 flex min-h-[68px] items-center justify-between gap-3 border-b px-4 py-3 sm:px-6 lg:min-h-[76px] lg:px-8 ${
            isDark
              ? 'border-[#252838] bg-[#0b0d14]/95'
              : 'border-slate-200 bg-white/95'
          } backdrop-blur-xl`}
        >
          <div className="flex min-w-0 items-center gap-3">

            {/* MOBILE MENU BUTTON */}
            <button
              type="button"
              onClick={() => setMobileMenuOpen(true)}
              aria-label="Open navigation menu"
              className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border text-lg md:hidden ${surface} ${hoverSurface}`}
            >
              ☰
            </button>

            <div className="min-w-0 truncate text-xs sm:text-sm">
              <span className={muted}>Workspace</span>
              <span className={`mx-1 sm:mx-2 ${muted}`}>/</span>
              <span className="font-semibold">{activePage}</span>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2 sm:gap-3">

            {/* SEARCH */}
            <label
              className={`hidden w-56 items-center gap-2 rounded-xl border px-3 py-2.5 lg:flex xl:w-72 ${inputSurface}`}
            >
              <span className={muted}>⌕</span>
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search anything..."
                className="w-full min-w-0 bg-transparent text-sm outline-none"
              />
              <kbd className={`shrink-0 rounded px-2 py-1 text-xs ${isDark ? 'bg-[#252838]' : 'bg-slate-200'}`}>
                ⌘ K
              </kbd>
            </label>

            {/* THEME */}
            <button
              onClick={() => setTheme(isDark ? 'light' : 'dark')}
              title="Toggle theme"
              aria-label="Toggle theme"
              className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border text-lg sm:h-11 sm:w-11 ${surface} ${hoverSurface}`}
            >
              {isDark ? '☀' : '☾'}
            </button>

            {/* NOTIFICATIONS */}
            <button
              aria-label="Notifications"
              className={`relative flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border text-lg sm:h-11 sm:w-11 ${surface} ${hoverSurface}`}
            >
              ♧
              <span className="absolute right-2 top-2 h-2 w-2 rounded-full bg-violet-500" />
            </button>
          </div>
        </header>

        {/* DASHBOARD CONTENT */}
        <div className="mx-auto w-full max-w-[1500px] space-y-6 p-4 sm:space-y-8 sm:p-6 lg:p-8">

          {/* WELCOME */}
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
              onClick={() => navigateTo('AI Assistant')}
              className="flex w-full items-center justify-center gap-3 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-5 py-3.5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/20 transition hover:brightness-110 sm:w-auto"
            >
              ✧ Start a conversation <span>→</span>
            </button>
          </section>

          {/* OVERVIEW CARDS */}
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

                <div className={`mt-5 border-t pt-4 text-xs leading-5 ${
                  isDark ? 'border-[#252838]' : 'border-slate-200'
                } ${muted}`}>
                  {card.note}
                </div>
              </div>
            ))}
          </section>

          {/* CONNECTORS AND CONVERSATIONS */}
          <section className="grid grid-cols-1 gap-5 xl:grid-cols-2">

            {/* CONNECTED APPS */}
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
                  onClick={() => navigateTo('Connectors')}
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

                    <span className={`shrink-0 rounded-full px-2 py-1 text-[10px] sm:px-3 sm:text-xs ${
                      isDark
                        ? 'bg-[#292d3c] text-slate-400'
                        : 'bg-slate-100 text-slate-500'
                    }`}>
                      Coming soon
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {/* RECENT CONVERSATIONS */}
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
                  onClick={() => navigateTo('Conversations')}
                  className="shrink-0 text-xs font-semibold text-violet-400 hover:text-violet-300 sm:text-sm"
                >
                  View all →
                </button>
              </div>

              <div className="mt-5 space-y-2 sm:mt-6">
                {conversations.map((conversation) => (
                  <button
                    key={conversation.title}
                    onClick={() => navigateTo('AI Assistant')}
                    className={`flex w-full min-w-0 items-center gap-3 rounded-xl p-3 text-left transition sm:p-3.5 ${hoverSurface}`}
                  >
                    <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-lg sm:h-11 sm:w-11 ${
                      isDark
                        ? 'bg-[#252838] text-violet-400'
                        : 'bg-violet-50 text-violet-500'
                    }`}>
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

          {/* ASSISTANT BANNER */}
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
              onClick={() => navigateTo('AI Assistant')}
              className="w-full shrink-0 rounded-xl bg-violet-600 px-5 py-3.5 text-sm font-semibold text-white transition hover:bg-violet-500 sm:w-auto"
            >
              Talk to your AI →
            </button>
          </section>

          {/* FOOTER */}
          <footer
            className={`flex flex-col gap-2 border-t pt-5 text-xs sm:flex-row sm:justify-between sm:gap-3 ${
              isDark ? 'border-[#252838]' : 'border-slate-200'
            } ${muted}`}
          >
            <span>Personal AI Assistant</span>
            <span>Built around your life. Powered by AI.</span>
          </footer>
        </div>
      </main>
    </div>
  )
}

export default Dashboard
