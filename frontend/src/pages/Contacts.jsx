import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useOutletContext } from 'react-router-dom'

import { contactsService } from '../services/contactsService.js'
import { api } from '../lib/api.js'
import { formatErrorMessage, formatRequestError } from '../lib/errorMessage.js'
import { requireConnectionsResponse } from '../lib/responseValidation.js'
import { useAuth } from '../context/AuthContext.jsx'

function initialsFor(name, email) {
  const source = (name || email || '').trim()
  if (!source) return '?'
  const parts = source.split(/\s+/).filter(Boolean)
  if (parts.length >= 2) {
    return `${parts[0][0] || ''}${parts[1][0] || ''}`.toUpperCase()
  }
  return source.slice(0, 2).toUpperCase()
}

function ContactAvatar({ contact, className = 'h-11 w-11', isDark }) {
  const name = contact?.display_name || contact?.primary_email || 'Contact'
  const initials = initialsFor(contact?.display_name, contact?.primary_email)
  if (contact?.photo_url) {
    return (
      <img
        src={contact.photo_url}
        alt={`${name} photo`}
        className={`${className} rounded-full object-cover ring-1 ${isDark ? 'ring-slate-700/80' : 'ring-slate-200'}`}
        referrerPolicy="no-referrer"
      />
    )
  }
  return (
    <div
      aria-label={`${name} avatar`}
      className={`${className} flex items-center justify-center rounded-full bg-gradient-to-br from-violet-500/25 to-indigo-500/20 text-sm font-semibold ${isDark ? 'text-violet-200 ring-1 ring-violet-400/20' : 'text-violet-700 ring-1 ring-violet-200'}`}
    >
      {initials}
    </div>
  )
}

function Field({ label, value, isDark }) {
  if (!value) return null
  return (
    <div className={`rounded-xl border px-3 py-2.5 ${isDark ? 'border-slate-800 bg-slate-900/40' : 'border-slate-200 bg-slate-50/70'}`}>
      <div className={`text-[11px] font-semibold uppercase tracking-wider ${isDark ? 'text-slate-500' : 'text-slate-500'}`}>{label}</div>
      <div className={`mt-1 text-sm break-words ${isDark ? 'text-slate-100' : 'text-slate-800'}`}>{value}</div>
    </div>
  )
}

function formatAddress(address) {
  if (!address) return ''
  if (typeof address === 'string') return address
  return (
    address.formatted ||
    [address.street, address.city, address.region, address.postal_code, address.country]
      .filter(Boolean)
      .join(', ')
  )
}

function ListSkeleton({ isDark }) {
  const row = isDark ? 'bg-slate-800/70' : 'bg-slate-200/70'
  return (
    <div className="space-y-3" aria-hidden="true">
      {Array.from({ length: 6 }).map((_, idx) => (
        <div key={idx} className={`flex items-center gap-3 rounded-xl border px-3 py-3 ${isDark ? 'border-slate-800 bg-slate-900/30' : 'border-slate-200 bg-white'}`}>
          <div className={`h-11 w-11 rounded-full ${row} animate-pulse`} />
          <div className="min-w-0 flex-1 space-y-2">
            <div className={`h-3.5 w-2/5 rounded ${row} animate-pulse`} />
            <div className={`h-3 w-3/5 rounded ${row} animate-pulse`} />
          </div>
        </div>
      ))}
    </div>
  )
}

function DetailSkeleton({ isDark }) {
  const row = isDark ? 'bg-slate-800/70' : 'bg-slate-200/70'
  return (
    <div className="mt-4 space-y-4" aria-hidden="true">
      <div className="flex items-center gap-3">
        <div className={`h-14 w-14 rounded-full ${row} animate-pulse`} />
        <div className="flex-1 space-y-2">
          <div className={`h-4 w-1/2 rounded ${row} animate-pulse`} />
          <div className={`h-3 w-2/3 rounded ${row} animate-pulse`} />
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {Array.from({ length: 4 }).map((_, idx) => (
          <div key={idx} className={`h-16 rounded-xl ${row} animate-pulse`} />
        ))}
      </div>
    </div>
  )
}

export default function Contacts() {
  const { isDark } = useOutletContext()
  const { user, updateUserMetadata } = useAuth()
  const queryClient = useQueryClient()

  const [searchInput, setSearchInput] = useState('')
  const [query, setQuery] = useState('')
  const [pages, setPages] = useState([null])
  const [selectedResourceName, setSelectedResourceName] = useState(null)
  const [uiError, setUiError] = useState('')

  const colors = {
    heading: isDark ? 'text-slate-100' : 'text-slate-900',
    muted: isDark ? 'text-slate-400' : 'text-slate-500',
    card: isDark ? 'border-[#252838] bg-[#11131c]' : 'border-slate-200 bg-white',
    subtleCard: isDark ? 'border-slate-800 bg-slate-900/30' : 'border-slate-200 bg-slate-50/50',
    input: isDark
      ? 'border-slate-700 bg-[#171a27] text-slate-100 placeholder-slate-500 focus:border-violet-500/60 focus:ring-violet-500/20'
      : 'border-slate-200 bg-white text-slate-800 placeholder-slate-400 focus:border-violet-400 focus:ring-violet-200',
    rowHover: isDark ? 'hover:bg-slate-800/50 hover:border-slate-700' : 'hover:bg-slate-50 hover:border-slate-200',
    selected: isDark ? 'bg-violet-500/10 border-violet-500/30 shadow-[0_0_0_1px_rgba(139,92,246,0.15)]' : 'bg-violet-50 border-violet-200 shadow-[0_0_0_1px_rgba(139,92,246,0.12)]',
  }

  const connectionsQuery = useQuery({
    queryKey: ['oauth-connections', user?.id],
    queryFn: async () => requireConnectionsResponse(await api.get('/api/v1/oauth/connections')),
    enabled: Boolean(user?.id),
    staleTime: 30_000,
  })

  const contactsCapabilities = connectionsQuery.data?.contacts_capabilities || {}
  const connections = connectionsQuery.data?.connections || []
  const contactsCandidates = connections.filter(
    (c) =>
      c.provider === 'google' &&
      c.status === 'connected' &&
      (c.capabilities?.contacts_read || c.capabilities?.contacts_write_permission_granted),
  )
  const defaultContactsConnectionId = user?.user_metadata?.default_contacts_connection_id || ''
  const selectionRequired =
    contactsCapabilities.selection_required === true ||
    (contactsCandidates.length > 1 && !defaultContactsConnectionId)
  const readAvailable =
    contactsCapabilities.chat_available === true ||
    contactsCapabilities.read_available === true ||
    contactsCapabilities.read === true

  const currentPageToken = pages[pages.length - 1]

  const listQuery = useQuery({
    queryKey: ['contacts', user?.id, defaultContactsConnectionId || 'auto', query, currentPageToken],
    queryFn: ({ signal }) =>
      contactsService.listContacts({
        pageSize: 25,
        pageToken: currentPageToken || undefined,
        query: query || undefined,
        signal,
      }),
    enabled:
      Boolean(user?.id) &&
      !connectionsQuery.isLoading &&
      !selectionRequired &&
      readAvailable !== false,
    placeholderData: (previousData) => previousData,
    retry: false,
  })

  const contacts = listQuery.data?.contacts || []
  const nextPageToken = listQuery.data?.next_page_token || null
  const selectedContact = useMemo(() => {
    if (!selectedResourceName) return null
    return contacts.find((c) => c.resource_name === selectedResourceName) || null
  }, [contacts, selectedResourceName])

  const detailQuery = useQuery({
    queryKey: ['contact-detail', user?.id, defaultContactsConnectionId || 'auto', selectedResourceName],
    queryFn: () => contactsService.getContact(selectedResourceName),
    enabled: Boolean(user?.id && selectedResourceName),
    retry: false,
  })
  const detailContact = detailQuery.data?.contact || selectedContact

  const selectedAccountEmail =
    listQuery.data?.selected_account_email ||
    contactsCandidates.find((c) => c.id === defaultContactsConnectionId)?.email ||
    (contactsCandidates.length === 1 ? contactsCandidates[0]?.email : '') ||
    ''

  // Accurate count only from backend metadata; do not invent totals.
  const reportedTotal =
    typeof listQuery.data?.total_items === 'number'
      ? listQuery.data.total_items
      : typeof listQuery.data?.total_people === 'number'
        ? listQuery.data.total_people
        : null
  const isSearchMode = Boolean(query)
  const showingCount = contacts.length

  async function chooseAccount(connectionId) {
    setUiError('')
    try {
      await updateUserMetadata({ default_contacts_connection_id: connectionId })
      await queryClient.invalidateQueries({ queryKey: ['oauth-connections', user?.id] })
      setPages([null])
      setSelectedResourceName(null)
      await queryClient.invalidateQueries({ queryKey: ['contacts', user?.id] })
    } catch (err) {
      setUiError(formatRequestError(err, 'Unable to save the selected Contacts account.'))
    }
  }

  function onSearchSubmit(e) {
    e.preventDefault()
    setQuery(searchInput.trim())
    setPages([null])
    setSelectedResourceName(null)
  }

  function onClearSearch() {
    setSearchInput('')
    setQuery('')
    setPages([null])
    setSelectedResourceName(null)
  }

  function goNextPage() {
    if (!nextPageToken) return
    setPages((prev) => [...prev, nextPageToken])
    setSelectedResourceName(null)
  }

  function goPrevPage() {
    setPages((prev) => (prev.length > 1 ? prev.slice(0, -1) : prev))
    setSelectedResourceName(null)
  }

  const error = uiError || (listQuery.error ? formatErrorMessage(listQuery.error) : '')
  const selectionErrorDetail = listQuery.error?.detail
  const requiresSelection =
    selectionRequired || selectionErrorDetail?.code === 'contacts_account_selection_required'
  const permissionDenied =
    listQuery.error?.status === 403 || contactsCapabilities.read_scope_missing === true

  return (
    <div className="mx-auto max-w-7xl space-y-6 pb-10">
      <header className={`overflow-hidden rounded-2xl border ${colors.card}`}>
        <div className={`border-b px-5 py-5 sm:px-6 ${isDark ? 'border-[#252838] bg-gradient-to-r from-violet-500/10 via-transparent to-indigo-500/10' : 'border-slate-200 bg-gradient-to-r from-violet-50 via-white to-indigo-50'}`}>
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div className="min-w-0">
              <div className="mb-2 inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-semibold tracking-wide text-violet-500 border-violet-500/20 bg-violet-500/5">
                GOOGLE CONTACTS
              </div>
              <h1 className={`text-3xl font-semibold tracking-tight ${colors.heading}`}>Contacts</h1>
              <p className={`mt-2 max-w-2xl text-sm ${colors.muted}`}>
                Search and view contacts from your connected Google account. Information is read-only and kept in sync with your Google Contacts access.
              </p>
              <div className={`mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs ${colors.muted}`}>
                <span>
                  Account:{' '}
                  <span className={isDark ? 'text-slate-200' : 'text-slate-700'}>
                    {selectedAccountEmail || (connectionsQuery.isLoading ? 'Checking…' : 'Not selected')}
                  </span>
                </span>
                <span className={isDark ? 'text-slate-600' : 'text-slate-300'} aria-hidden="true">•</span>
                <span>Read-only access</span>
              </div>
            </div>

            <div className={`grid min-w-[220px] grid-cols-2 gap-3 rounded-xl border p-3 sm:min-w-[280px] ${colors.subtleCard}`}>
              <div>
                <div className={`text-[11px] font-semibold uppercase tracking-wider ${colors.muted}`}>Total contacts</div>
                <div className={`mt-1 text-2xl font-semibold ${colors.heading}`}>
                  {connectionsQuery.isLoading || (listQuery.isLoading && !listQuery.data)
                    ? '—'
                    : reportedTotal !== null && !isSearchMode
                      ? reportedTotal.toLocaleString()
                      : reportedTotal !== null && isSearchMode
                        ? reportedTotal.toLocaleString()
                        : '—'}
                </div>
                <div className={`mt-0.5 text-xs ${colors.muted}`}>
                  {reportedTotal !== null
                    ? (isSearchMode ? 'Account total' : 'From Google Contacts')
                    : 'Total unavailable from API'}
                </div>
              </div>
              <div>
                <div className={`text-[11px] font-semibold uppercase tracking-wider ${colors.muted}`}>
                  {isSearchMode ? 'Matches on page' : 'Showing'}
                </div>
                <div className={`mt-1 text-2xl font-semibold ${colors.heading}`}>
                  {listQuery.isLoading && !listQuery.data ? '—' : showingCount.toLocaleString()}
                </div>
                <div className={`mt-0.5 text-xs ${colors.muted}`}>
                  {isSearchMode ? `for “${query}”` : `Page ${pages.length}`}
                </div>
              </div>
            </div>
          </div>
        </div>
      </header>

      {error && !requiresSelection && !(listQuery.isError && contacts.length === 0) && (
        <div className={`rounded-xl border p-4 text-sm ${isDark ? 'border-red-500/20 bg-red-500/10 text-red-300' : 'border-red-200 bg-red-50 text-red-700'}`} role="alert">
          <p className="font-medium">Unable to load contacts</p>
          <p className="mt-1 opacity-90">{error}</p>
          <div className="mt-3 flex flex-wrap gap-3">
            <button
              type="button"
              onClick={() => listQuery.refetch()}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium ${isDark ? 'border-red-400/30 hover:bg-red-500/10' : 'border-red-300 hover:bg-red-100'}`}
            >
              Try again
            </button>
            {permissionDenied && (
              <Link to="/dashboard/connectors" className="inline-flex items-center rounded-lg px-3 py-1.5 text-xs font-medium underline underline-offset-2">
                Review Google Contacts permissions
              </Link>
            )}
            {!permissionDenied && connectionsQuery.data && !readAvailable && contactsCandidates.length === 0 && (
              <Link to="/dashboard/connectors" className="inline-flex items-center rounded-lg px-3 py-1.5 text-xs font-medium underline underline-offset-2">
                Connect Google Contacts
              </Link>
            )}
          </div>
        </div>
      )}

      {requiresSelection && (
        <div className={`rounded-xl border p-5 ${colors.card}`} role="region" aria-label="Select a Contacts account">
          <h2 className={`text-lg font-semibold ${colors.heading}`}>Select a Contacts account</h2>
          <p className={`mt-1 text-sm ${colors.muted}`}>
            More than one connected Google account has Contacts access. Choose which account to use.
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            {contactsCandidates.map((connection) => (
              <button
                key={connection.id}
                type="button"
                onClick={() => chooseAccount(connection.id)}
                className={`rounded-lg border px-3 py-2 text-sm font-medium transition ${isDark ? 'border-slate-700 text-slate-200 hover:bg-slate-800' : 'border-slate-200 text-slate-700 hover:bg-slate-50'}`}
              >
                {connection.email || 'Google account'}
              </button>
            ))}
          </div>
          {selectionErrorDetail?.candidate_emails?.length > 0 && contactsCandidates.length === 0 && (
            <p className={`mt-2 text-xs ${colors.muted}`}>
              Eligible accounts: {selectionErrorDetail.candidate_emails.join(', ')}
            </p>
          )}
        </div>
      )}

      {!requiresSelection && (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1.08fr)_minmax(0,0.92fr)]">
          <section className={`rounded-2xl border p-4 sm:p-5 ${colors.card}`}>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className={`text-base font-semibold ${colors.heading}`}>Directory</h2>
                <p className={`text-xs ${colors.muted}`}>Browse and search by name, email, or phone number.</p>
              </div>
            </div>

            <form onSubmit={onSearchSubmit} className="mt-4 flex flex-col gap-2 sm:flex-row">
              <label className="relative min-w-0 flex-1">
                <span className="sr-only">Search contacts</span>
                <svg aria-hidden="true" className={`pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 ${colors.muted}`} viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M9 3.5a5.5 5.5 0 1 0 3.473 9.772l2.627 2.628a.75.75 0 1 0 1.06-1.06l-2.627-2.628A5.5 5.5 0 0 0 9 3.5ZM5 9a4 4 0 1 1 8 0 4 4 0 0 1-8 0Z" clipRule="evenodd" />
                </svg>
                <input
                  type="search"
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  placeholder="Search by name, email, or phone"
                  className={`w-full rounded-xl border py-2.5 pl-9 pr-3 text-sm outline-none transition focus:ring-2 ${colors.input}`}
                  aria-label="Search contacts"
                />
              </label>
              <div className="flex gap-2">
                <button
                  type="submit"
                  className="rounded-xl bg-violet-600 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-violet-500 focus:outline-none focus:ring-2 focus:ring-violet-400/40"
                >
                  Search
                </button>
                {query && (
                  <button
                    type="button"
                    onClick={onClearSearch}
                    className={`rounded-xl border px-4 py-2.5 text-sm font-medium transition ${isDark ? 'border-slate-700 text-slate-300 hover:bg-slate-800' : 'border-slate-200 text-slate-700 hover:bg-slate-50'}`}
                  >
                    Clear
                  </button>
                )}
              </div>
            </form>

            {listQuery.isError && !listQuery.isLoading && contacts.length > 0 && (
              <div className={`mt-3 rounded-lg border px-3 py-2 text-xs ${isDark ? 'border-amber-500/20 bg-amber-500/10 text-amber-200' : 'border-amber-200 bg-amber-50 text-amber-800'}`} role="status">
                Showing previous results. Latest refresh failed: {formatErrorMessage(listQuery.error)}
              </div>
            )}

            <div className="mt-4">
              {listQuery.isLoading && !listQuery.data ? (
                <ListSkeleton isDark={isDark} />
              ) : listQuery.isError && contacts.length === 0 ? (
                <div className={`rounded-xl border border-dashed p-8 text-center ${isDark ? 'border-slate-700 bg-slate-900/20' : 'border-slate-300 bg-slate-50'}`}>
                  <p className={`text-sm font-medium ${colors.heading}`}>Couldn’t load contacts</p>
                  <p className={`mt-1 text-sm ${colors.muted}`}>{formatErrorMessage(listQuery.error)}</p>
                  <div className="mt-4 flex flex-wrap items-center justify-center gap-3">
                    <button
                      type="button"
                      onClick={() => listQuery.refetch()}
                      className="rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-500"
                    >
                      Retry
                    </button>
                    {permissionDenied && (
                      <Link to="/dashboard/connectors" className="text-sm font-medium text-violet-500 underline underline-offset-2 hover:text-violet-400">
                        Review Google Contacts permissions
                      </Link>
                    )}
                    {!permissionDenied && connectionsQuery.data && !readAvailable && contactsCandidates.length === 0 && (
                      <Link to="/dashboard/connectors" className="text-sm font-medium text-violet-500 underline underline-offset-2 hover:text-violet-400">
                        Connect Google Contacts
                      </Link>
                    )}
                  </div>
                </div>
              ) : contacts.length === 0 ? (
                <div className={`rounded-xl border border-dashed p-8 text-center ${isDark ? 'border-slate-700 bg-slate-900/20' : 'border-slate-300 bg-slate-50'}`}>
                  <div className={`mx-auto flex h-12 w-12 items-center justify-center rounded-full ${isDark ? 'bg-slate-800 text-slate-300' : 'bg-white text-slate-500 border border-slate-200'}`}>
                    <svg aria-hidden="true" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor"><path d="M10 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM3.465 14.493a1.23 1.23 0 0 0 .41 1.412A9.957 9.957 0 0 0 10 18c2.31 0 4.438-.784 6.131-2.1.43-.333.604-.903.408-1.41a7.002 7.002 0 0 0-13.074.003Z"/></svg>
                  </div>
                  <p className={`mt-3 text-sm font-medium ${colors.heading}`}>
                    {query ? 'No contacts matched your search' : 'No contacts found'}
                  </p>
                  <p className={`mt-1 text-sm ${colors.muted}`}>
                    {query ? 'Try a different name, email, or phone number.' : 'This Google account has no visible contacts yet.'}
                  </p>
                  {query && (
                    <button type="button" onClick={onClearSearch} className="mt-4 rounded-lg border px-4 py-2 text-sm font-medium hover:bg-slate-500/5">
                      Clear search
                    </button>
                  )}
                </div>
              ) : (
                <ul className="space-y-2" aria-label="Contacts list">
                  {contacts.map((contact) => {
                    const selected = selectedResourceName === contact.resource_name
                    const title = contact.display_name || contact.primary_email || contact.primary_phone || 'Unnamed contact'
                    const subtitle = contact.primary_email || contact.primary_phone || 'No email or phone'
                    return (
                      <li key={contact.resource_name}>
                        <button
                          type="button"
                          onClick={() => setSelectedResourceName(contact.resource_name)}
                          className={`group flex w-full items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition ${selected ? colors.selected : `border-transparent ${colors.rowHover}`}`}
                          aria-current={selected ? 'true' : undefined}
                        >
                          <ContactAvatar contact={contact} className="h-11 w-11" isDark={isDark} />
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <div className={`truncate text-sm font-semibold ${colors.heading}`}>{title}</div>
                              {selected && (
                                <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${isDark ? 'bg-violet-500/20 text-violet-200' : 'bg-violet-100 text-violet-700'}`}>
                                  Selected
                                </span>
                              )}
                            </div>
                            <div className={`truncate text-xs ${colors.muted}`}>{subtitle}</div>
                          </div>
                          <svg aria-hidden="true" className={`h-4 w-4 shrink-0 opacity-0 transition group-hover:opacity-100 ${selected ? 'opacity-100' : ''} ${colors.muted}`} viewBox="0 0 20 20" fill="currentColor"><path fillRule="evenodd" d="M7.21 14.77a.75.75 0 0 1 .02-1.06L11.168 10 7.23 6.29a.75.75 0 1 1 1.04-1.08l4.5 4.25a.75.75 0 0 1 0 1.08l-4.5 4.25a.75.75 0 0 1-1.06-.02Z" clipRule="evenodd"/></svg>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>

            <div className={`mt-5 flex items-center justify-between gap-3 border-t pt-4 ${isDark ? 'border-[#252838]' : 'border-slate-200'}`}>
              <button
                type="button"
                onClick={goPrevPage}
                disabled={pages.length <= 1 || listQuery.isFetching}
                className={`rounded-lg border px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${isDark ? 'border-slate-700 text-slate-300 hover:bg-slate-800' : 'border-slate-200 text-slate-700 hover:bg-slate-50'}`}
              >
                Previous
              </button>
              <span className={`text-xs ${colors.muted}`} aria-live="polite">
                Page {pages.length}
                {listQuery.isFetching ? ' · Refreshing…' : ''}
                {nextPageToken ? ' · More available' : listQuery.data ? ' · End' : ''}
              </span>
              <button
                type="button"
                onClick={goNextPage}
                disabled={!nextPageToken || listQuery.isFetching}
                className={`rounded-lg border px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${isDark ? 'border-slate-700 text-slate-300 hover:bg-slate-800' : 'border-slate-200 text-slate-700 hover:bg-slate-50'}`}
              >
                Next
              </button>
            </div>
          </section>

          <section className={`rounded-2xl border p-4 sm:p-5 ${colors.card}`}>
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className={`text-base font-semibold ${colors.heading}`}>Contact details</h2>
                <p className={`text-xs ${colors.muted}`}>View information for the selected contact.</p>
              </div>
              {detailContact && (
                <span className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${isDark ? 'bg-slate-800 text-slate-300' : 'bg-slate-100 text-slate-600'}`}>
                  Read-only
                </span>
              )}
            </div>

            {!selectedResourceName ? (
              <div className={`mt-6 rounded-xl border border-dashed p-8 text-center ${isDark ? 'border-slate-700 bg-slate-900/20' : 'border-slate-300 bg-slate-50'}`}>
                <div className={`mx-auto flex h-12 w-12 items-center justify-center rounded-full ${isDark ? 'bg-slate-800 text-slate-300' : 'bg-white text-slate-500 border border-slate-200'}`}>
                  <svg aria-hidden="true" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor"><path fillRule="evenodd" d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-7-4a1 1 0 1 1-2 0 1 1 0 0 1 2 0ZM9 9a.75.75 0 0 0 0 1.5h.253a.25.25 0 0 1 .244.304l-.459 2.066A1.75 1.75 0 0 0 10.747 15H11a.75.75 0 0 0 0-1.5h-.253a.25.25 0 0 1-.244-.304l.459-2.066A1.75 1.75 0 0 0 9.253 9H9Z" clipRule="evenodd"/></svg>
                </div>
                <p className={`mt-3 text-sm font-medium ${colors.heading}`}>No contact selected</p>
                <p className={`mt-1 text-sm ${colors.muted}`}>Choose a contact from the directory to view their details here.</p>
              </div>
            ) : detailQuery.isLoading && !detailContact ? (
              <DetailSkeleton isDark={isDark} />
            ) : detailQuery.error && !detailContact ? (
              <div className={`mt-4 rounded-xl border p-4 ${isDark ? 'border-red-500/20 bg-red-500/10 text-red-300' : 'border-red-200 bg-red-50 text-red-700'}`} role="alert">
                <p className="text-sm font-medium">Couldn’t load contact details</p>
                <p className="mt-1 text-sm opacity-90">{formatErrorMessage(detailQuery.error)}</p>
                <button type="button" onClick={() => detailQuery.refetch()} className="mt-3 rounded-lg border px-3 py-1.5 text-xs font-medium hover:bg-black/5 dark:hover:bg-white/5">
                  Retry
                </button>
              </div>
            ) : detailContact ? (
              <div className="mt-5 space-y-4">
                <div className={`flex items-center gap-4 rounded-xl border p-4 ${colors.subtleCard}`}>
                  <ContactAvatar contact={detailContact} className="h-14 w-14" isDark={isDark} />
                  <div className="min-w-0">
                    <div className={`truncate text-lg font-semibold ${colors.heading}`}>
                      {detailContact.display_name || 'Unnamed contact'}
                    </div>
                    {detailContact.primary_email && (
                      <div className={`truncate text-sm ${colors.muted}`}>{detailContact.primary_email}</div>
                    )}
                    {!detailContact.primary_email && detailContact.primary_phone && (
                      <div className={`truncate text-sm ${colors.muted}`}>{detailContact.primary_phone}</div>
                    )}
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Email" value={detailContact.primary_email} isDark={isDark} />
                  <Field label="Phone" value={detailContact.primary_phone} isDark={isDark} />
                  <Field label="Organization" value={detailContact.organization} isDark={isDark} />
                  <Field label="Job title" value={detailContact.job_title} isDark={isDark} />
                </div>
                <Field label="Address" value={formatAddress(detailContact.primary_address)} isDark={isDark} />

                {Array.isArray(detailContact.emails) && detailContact.emails.length > 1 && (
                  <div className={`rounded-xl border p-3 ${colors.subtleCard}`}>
                    <div className={`text-[11px] font-semibold uppercase tracking-wider ${colors.muted}`}>All emails</div>
                    <ul className={`mt-2 space-y-1.5 text-sm ${isDark ? 'text-slate-200' : 'text-slate-700'}`}>
                      {detailContact.emails.map((item, idx) => (
                        <li key={`${item.value}-${idx}`} className="break-words">{item.value}{item.type ? ` (${item.type})` : ''}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {Array.isArray(detailContact.phones) && detailContact.phones.length > 1 && (
                  <div className={`rounded-xl border p-3 ${colors.subtleCard}`}>
                    <div className={`text-[11px] font-semibold uppercase tracking-wider ${colors.muted}`}>All phone numbers</div>
                    <ul className={`mt-2 space-y-1.5 text-sm ${isDark ? 'text-slate-200' : 'text-slate-700'}`}>
                      {detailContact.phones.map((item, idx) => (
                        <li key={`${item.value}-${idx}`} className="break-words">{item.value}{item.type ? ` (${item.type})` : ''}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            ) : (
              <p className={`mt-4 text-sm ${colors.muted}`}>Contact details are unavailable.</p>
            )}
          </section>
        </div>
      )}

      <p className={`text-xs ${colors.muted}`}>
        Contact editing is not available. To view contact information in chat, ask the Assistant (for example, “Show me the phone number of…” or “Find a contact named…”).
      </p>
    </div>
  )
}
