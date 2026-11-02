
import { useState } from 'react'
import {
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from 'react-router-dom'
import { useAuth } from '../../context/AuthContext'

const navigation = [
  {
    section: 'WORKSPACE',
    items: [
      { name: 'Dashboard', path: '/dashboard', icon: '▦', end: true },
      { name: 'AI Assistant', path: '/dashboard/assistant', icon: '✧' },
      { name: 'Conversations', path: '/dashboard/conversations', icon: '☷' },
    ],
  },
  {
    section: 'PERSONAL',
    items: [
      { name: 'Memory Center', path: '/dashboard/memory', icon: '◈' },
      { name: 'Tasks & Reminders', path: '/dashboard/tasks', icon: '✓' },
    ],
  },
  {
    section: 'INTEGRATIONS',
    items: [
      { name: 'Connectors', path: '/dashboard/connectors', icon: '⌘' },
    ],
  },
]

function DashboardLayout() {
  const { user, signOut } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [theme, setTheme] = useState('dark')
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [logoutLoading, setLogoutLoading] = useState(false)
  const [profileError, setProfileError] = useState('')

  const isDark = theme === 'dark'

  const metadata = user?.user_metadata || {}

  const fullName =
    metadata.full_name ||
    metadata.name ||
    user?.email?.split('@')[0] ||
    'User'

  const email = user?.email || ''

  const avatarUrl = metadata.avatar_url || metadata.picture || ''

  const initials = fullName
    .split(' ')
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join('')

  const currentPage =
    navigation
      .flatMap((group) => group.items)
      .find((item) =>
        item.end
          ? location.pathname === item.path
          : location.pathname.startsWith(item.path)
      )?.name || 'Settings'

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

  function closeMenus() {
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

  function navigateTo(path) {
    closeMenus()
    navigate(path)
  }

  return (
    <div
      className={`min-h-screen overflow-x-hidden ${
        isDark
          ? 'bg-[#0b0d14] text-slate-100'
          : 'bg-[#f5f6fa] text-slate-800'
      }`}
    >
      {/* Mobile overlay */}
      {mobileMenuOpen && (
        <button
          type="button"
          aria-label="Close navigation menu"
          onClick={() => setMobileMenuOpen(false)}
          className="fixed inset-0 z-40 bg-black/60 backdrop-blur-[2px] md:hidden"
        />
      )}

      {/* Sidebar */}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-[280px] flex-col border-r transition-transform duration-300 ease-in-out md:w-64 md:translate-x-0 ${
          surface
        } ${
          mobileMenuOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        {/* Brand */}
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

        {/* New conversation */}
        <div className="shrink-0 p-4">
          <button
            type="button"
            onClick={() => navigateTo('/dashboard/assistant')}
            className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-3 py-3.5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/10 transition hover:brightness-110"
          >
            <span className="text-xl">＋</span>
            New conversation
          </button>
        </div>

        {/* Navigation */}
        <nav className="min-h-0 flex-1 space-y-7 overflow-y-auto overflow-x-hidden px-4 pb-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {navigation.map((group) => (
            <div key={group.section}>
              <div className={`mb-3 px-3 text-xs font-bold tracking-[1.5px] ${muted}`}>
                {group.section}
              </div>

              <div className="space-y-1">
                {group.items.map((item) => (
                  <NavLink
                    key={item.name}
                    to={item.path}
                    end={item.end}
                    onClick={closeMenus}
                    className={({ isActive }) =>
                      `flex w-full items-center gap-3 rounded-xl px-3 py-3.5 text-left text-sm transition ${
                        isActive
                          ? 'bg-violet-500/15 font-semibold text-violet-400'
                          : `${muted} ${hoverSurface}`
                      }`
                    }
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
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        {/* Sidebar bottom */}
        <div
          className={`relative shrink-0 border-t p-4 ${
            isDark ? 'border-[#252838]' : 'border-slate-200'
          }`}
        >
          <button
            type="button"
            onClick={() => navigateTo('/dashboard/settings')}
            className={`flex w-full items-center gap-3 rounded-xl px-3 py-3.5 text-sm ${hoverSurface} ${muted}`}
          >
            <span className="w-5 text-center text-xl">⚙</span>
            Settings
          </button>

          {/* Profile */}
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
                  type="button"
                  onClick={() => navigateTo('/dashboard/settings')}
                  className={`flex w-full items-center gap-3 rounded-lg px-3 py-3 text-sm ${muted} ${hoverSurface}`}
                >
                  <span>♙</span>
                  My Profile
                </button>

                <button
                  type="button"
                  onClick={() => navigateTo('/dashboard/settings')}
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
                  type="button"
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

      {/* Main */}
      <main className="min-h-screen min-w-0 md:ml-64">
        {/* Header */}
        <header
          className={`sticky top-0 z-30 flex min-h-[68px] items-center justify-between gap-3 border-b px-4 py-3 sm:px-6 lg:min-h-[76px] lg:px-8 ${
            isDark
              ? 'border-[#252838] bg-[#0b0d14]/95'
              : 'border-slate-200 bg-white/95'
          } backdrop-blur-xl`}
        >
          <div className="flex min-w-0 items-center gap-3">
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
              <span className="font-semibold">{currentPage}</span>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2 sm:gap-3">
            {/* Search */}
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

            {/* Theme */}
            <button
              type="button"
              onClick={() => setTheme(isDark ? 'light' : 'dark')}
              title="Toggle theme"
              aria-label="Toggle theme"
              className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border text-lg sm:h-11 sm:w-11 ${surface} ${hoverSurface}`}
            >
              {isDark ? '☀' : '☾'}
            </button>

            {/* Notifications placeholder */}
            <button
              type="button"
              aria-label="Notifications"
              title="Notifications"
              className={`relative flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border text-lg sm:h-11 sm:w-11 ${surface} ${hoverSurface}`}
            >
              ♧
            </button>
          </div>
        </header>

        {/* Nested page content */}
        <div className="mx-auto w-full max-w-[1500px] p-4 sm:p-6 lg:p-8">
          <Outlet
            context={{
              theme,
              isDark,
              setTheme,
              muted,
              surface,
              hoverSurface,
              search,
            }}
          />
        </div>
      </main>
    </div>
  )
}

export default DashboardLayout
