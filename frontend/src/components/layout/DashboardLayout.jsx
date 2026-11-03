import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import AppIcon from '../AppIcon'
import { api } from '../../lib/api.js'
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
      { name: 'Dashboard', path: '/dashboard', icon: 'dashboard', end: true },
      { name: 'AI Assistant', path: '/dashboard/assistant', icon: 'assistant' },
      { name: 'Conversations', path: '/dashboard/conversations', icon: 'conversations' },
    ],
  },
  {
    section: 'PERSONAL',
    items: [
      { name: 'Memory Center', path: '/dashboard/memory', icon: 'memory' },
      { name: 'Tasks & Reminders', path: '/dashboard/tasks', icon: 'tasks' },
    ],
  },
  {
    section: 'INTEGRATIONS',
    items: [
      { name: 'Connectors', path: '/dashboard/connectors', icon: 'connectors' },
      { name: 'Gmail', path: '/dashboard/gmail', icon: 'gmail' },
    ],
  },
]

function DashboardLayout() {
  const [pendingReviewCount, setPendingReviewCount] = useState(0)
  const [notificationOpen, setNotificationOpen] = useState(false)
  const [notificationLoading, setNotificationLoading] = useState(true)
  const { user, signOut } = useAuth()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()

  const [theme, setTheme] = useState(() =>
    localStorage.getItem('dashboard-theme') === 'light' ? 'light' : 'dark'
  )
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [logoutLoading, setLogoutLoading] = useState(false)
  const [profileError, setProfileError] = useState('')

  const isDark = theme === 'dark'
  const connectionsQuery = useQuery({
    queryKey: ['oauth-connections', user?.id],
    queryFn: () => api.get('/api/v1/oauth/connections'),
    enabled: Boolean(user?.id),
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
    refetchOnWindowFocus: false,
  })

  const connectedAccountCount = (
    connectionsQuery.data?.connections || []
  ).filter((connection) => connection.status === 'connected').length

  useEffect(() => {
    localStorage.setItem('dashboard-theme', theme)
  }, [theme])

  useEffect(() => {
    let active = true

    async function loadNotifications() {
      try {
        const reviews = await api.get(
          '/api/v1/memory-reviews?status_filter=pending&limit=100&offset=0'
        )

        if (active) {
          setPendingReviewCount(
            Array.isArray(reviews) ? reviews.length : 0
          )
        }
      } catch (error) {
        console.error('Unable to load notifications:', error)
      } finally {
        if (active) {
          setNotificationLoading(false)
        }
      }
    }

    loadNotifications()

    return () => {
      active = false
    }
  }, [])

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

      // Clear all cached user data after successful logout.
      queryClient.clear()

      // Clear user-specific Gmail account selection.
      if (user?.id) {
        sessionStorage.removeItem(
          `gmail-selected-account-${user.id}`,
        )
      }

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

  function openMemoryNotifications() {
    setNotificationOpen(false)
    closeMenus()
    navigate('/dashboard/memory')
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
              <AppIcon name="assistant" className="h-5 w-5" />
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
            className={`rounded-lg p-2 md:hidden ${hoverSurface}`}
          >
            <AppIcon name="close" className="h-5 w-5" />
          </button>
        </div>

        {/* New conversation */}
        <div className="shrink-0 p-4">
          <button
            type="button"
            onClick={() => navigateTo('/dashboard/assistant')}
            className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-3 py-3.5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/10 transition hover:brightness-110"
          >
            <AppIcon name="plus" className="h-5 w-5" />
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
                    <span className="flex w-5 shrink-0 justify-center">
                      <AppIcon name={item.icon} className="h-5 w-5" />
                    </span>
                    <span className="flex-1">{item.name}</span>

                    {item.name === 'Connectors' && !connectionsQuery.isError && (
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${
                          isDark ? 'bg-[#292d3c]' : 'bg-slate-200'
                        }`}
                      >
                        {connectionsQuery.isLoading ? '…' : connectedAccountCount}
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
            <AppIcon name="settings" className="h-5 w-5" />
            Settings
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="min-h-screen min-w-0 pt-[68px] md:ml-64 lg:pt-[76px]">
        {/* Header */}
        <header
          className={`fixed left-0 right-0 top-0 z-30 flex min-h-[68px] items-center justify-between gap-3 border-b px-4 py-3 sm:px-6 md:left-64 lg:min-h-[76px] lg:px-8 ${
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
              <AppIcon name="conversations" className="h-5 w-5" />
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
              <span className={muted}>
                <AppIcon name="search" className="h-4 w-4" />
              </span>
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
              <AppIcon name={isDark ? 'sun' : 'moon'} className="h-5 w-5" />
            </button>

            {/* Notifications */}
            <div className="relative">
              <button
                type="button"
                onClick={() => setNotificationOpen((current) => !current)}
                aria-label="Notifications"
                aria-expanded={notificationOpen}
                title="Notifications"
                className={`relative flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border text-lg sm:h-11 sm:w-11 ${surface} ${hoverSurface}`}
              >
                <AppIcon name="bell" className="h-5 w-5" />

                {!notificationLoading && pendingReviewCount > 0 && (
                  <span className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold text-white">
                    {pendingReviewCount > 99 ? '99+' : pendingReviewCount}
                  </span>
                )}
              </button>

              {notificationOpen && (
                <>
                  <button
                    type="button"
                    aria-label="Close notifications"
                    onClick={() => setNotificationOpen(false)}
                    className="fixed inset-0 z-40 cursor-default"
                  />

                  <div
                    className={`absolute right-0 top-full z-50 mt-3 w-[min(340px,calc(100vw-2rem))] overflow-hidden rounded-2xl border shadow-2xl ${
                      isDark
                        ? 'border-[#303448] bg-[#191c29]'
                        : 'border-slate-200 bg-white'
                    }`}
                  >
                    <div className="flex items-center justify-between border-b border-inherit p-4">
                      <h3 className="font-semibold">
                        Notifications
                      </h3>

                      <span className="rounded-full bg-amber-500/15 px-2.5 py-1 text-xs font-semibold text-amber-500">
                        {pendingReviewCount} Pending
                      </span>
                    </div>

                    <div className="p-4">
                      {notificationLoading ? (
                        <p className={`text-sm ${muted}`}>
                          Loading notifications...
                        </p>
                      ) : pendingReviewCount > 0 ? (
                        <button
                          type="button"
                          onClick={openMemoryNotifications}
                          className={`flex w-full items-start gap-3 rounded-xl p-3 text-left transition ${hoverSurface}`}
                        >
                          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-amber-500/15 text-xl text-amber-500">
                            <AppIcon name="memory" className="h-5 w-5" />
                          </span>

                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-semibold">
                              Memory reviews require attention
                            </span>

                            <span className={`mt-1 block text-xs leading-5 ${muted}`}>
                              You have {pendingReviewCount} pending AI memory suggestions.
                            </span>

                            <span className="mt-2 block text-xs font-semibold text-violet-400">
                              Review now →
                            </span>
                          </span>
                        </button>
                      ) : (
                        <div className="py-5 text-center">
                          <span className="text-3xl text-emerald-500">
                            <AppIcon name="check" className="h-7 w-7" />
                          </span>

                          <p className="mt-2 text-sm font-semibold">
                            You're all caught up!
                          </p>

                          <p className={`mt-1 text-xs ${muted}`}>
                            No pending notifications right now.
                          </p>
                        </div>
                      )}
                    </div>

                    {pendingReviewCount > 0 && (
                      <button
                        type="button"
                        onClick={openMemoryNotifications}
                        className={`w-full border-t border-inherit p-3 text-center text-sm font-semibold text-violet-400 ${hoverSurface}`}
                      >
                        View all memory reviews →
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>

            {/* Profile */}
            <div className="relative">
              <button
                type="button"
                onClick={() => setProfileOpen((current) => !current)}
                aria-expanded={profileOpen}
                aria-label="Open profile menu"
                className={`flex h-10 items-center gap-2 rounded-xl border px-2 transition sm:h-11 sm:gap-3 sm:px-3 ${surface} ${hoverSurface}`}
              >
                {avatarUrl ? (
                  <img
                    src={avatarUrl}
                    alt={fullName}
                    referrerPolicy="no-referrer"
                    className="h-8 w-8 shrink-0 rounded-full object-cover"
                  />
                ) : (
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 text-xs font-bold text-white">
                    {initials || 'U'}
                  </div>
                )}
                <span className="hidden max-w-32 truncate text-sm font-semibold sm:block">
                  {fullName}
                </span>
                <span className={`ml-1 ${muted}`}>
                  <AppIcon
                    name={profileOpen ? 'chevron-up' : 'chevron-down'}
                    className="h-4 w-4"
                  />
                </span>
              </button>

              {profileOpen && (
                <>
                  <button
                    type="button"
                    aria-label="Close profile menu"
                    onClick={() => setProfileOpen(false)}
                    className="fixed inset-0 z-40 cursor-default"
                  />
                  <div
                    className={`absolute right-0 top-full z-50 mt-3 w-[min(280px,calc(100vw-2rem))] rounded-xl border p-2 shadow-2xl ${
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
                      <AppIcon name="settings" className="h-4 w-4" />
                      Settings
                    </button>
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
                      <AppIcon name="logout" className="h-4 w-4" />
                      {logoutLoading ? 'Signing out...' : 'Sign out'}
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </header>

        {/* Nested page content */}
        <div className="mx-auto w-full max-w-[1500px] p-4 sm:p-6 lg:p-8">
          <Outlet
            context={{
              theme,
              isDark,
              setTheme,
              user,
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
