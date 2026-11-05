
import { useEffect, useState } from 'react'
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import {
  useOutletContext,
  useSearchParams,
} from 'react-router-dom'

import { api } from '../lib/api'
import { useAuth } from '../context/AuthContext'
import GoogleServiceIcon from '../components/GoogleServiceIcon'

const CACHE_TIME = 5 * 60 * 1000
const GC_TIME = 30 * 60 * 1000

const CONNECTIONS_KEY = (userId) => [
  'oauth-connections',
  userId,
]

function formatDate(value) {
  if (!value) return 'Not available'

  const date = new Date(value)

  if (Number.isNaN(date.getTime())) {
    return 'Not available'
  }

  return date.toLocaleString()
}

function Connectors() {
  const { user, updateUserMetadata } = useAuth()
  const { isDark } = useOutletContext()

  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [notice, setNotice] = useState(null)
  const [isUpdatingCalendarDefault, setIsUpdatingCalendarDefault] =
    useState(false)

  const userId = user?.id

  const colors = {
    heading: isDark
      ? 'text-slate-100'
      : 'text-slate-800',

    muted: isDark
      ? 'text-slate-400'
      : 'text-slate-500',

    service: isDark
      ? 'border-[#252838] bg-[#11131c]'
      : 'border-slate-200 bg-white',

    divider: isDark
      ? 'border-[#252838]'
      : 'border-slate-200',
  }

  // Connected accounts query
  const connectionsQuery = useQuery({
    queryKey: CONNECTIONS_KEY(userId),

    queryFn: () =>
      api.get('/api/v1/oauth/connections'),

    enabled: Boolean(userId),

    staleTime: CACHE_TIME,
    gcTime: GC_TIME,

    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: true,
  })

  const connections =
    connectionsQuery.data?.connections || []

  const googleConnections = connections.filter(
    (connection) => connection.provider === 'google',
  )
  const connectedGoogleConnections = googleConnections.filter(
    (connection) => connection.status === 'connected',
  )
  const gmailConnections = connectedGoogleConnections.filter(
    (connection) => connection.capabilities?.gmail === true,
  )
  const calendarCapabilities =
    connectionsQuery.data?.calendar_capabilities || {}
  const isCalendarApiComplete = calendarCapabilities.api_complete === true
  const calendarNeedsConsent =
    calendarCapabilities.permissions_need_consent === true
  const configuredDefaultConnectionId =
    user?.user_metadata?.default_calendar_connection_id
  const isGmailConnected = gmailConnections.length > 0

  async function clearDisconnectedPreferences(connectionIds) {
    const metadata = {}
    if (connectionIds.includes(
      user?.user_metadata?.default_gmail_connection_id,
    )) {
      metadata.default_gmail_connection_id = null
    }
    if (connectionIds.includes(configuredDefaultConnectionId)) {
      metadata.default_calendar_connection_id = null
    }
    if (Object.keys(metadata).length === 0) return false

    try {
      await updateUserMetadata(metadata)
      return false
    } catch {
      return true
    }
  }

  async function handleDefaultCalendarChange(event) {
    const connectionId = event.target.value || null
    setIsUpdatingCalendarDefault(true)
    setNotice(null)
    try {
      await updateUserMetadata({
        default_calendar_connection_id: connectionId,
      })
      setNotice({
        type: 'success',
        message: connectionId
          ? 'Default Calendar account updated.'
          : 'Calendar account selection will be automatic when unambiguous.',
      })
    } catch (error) {
      setNotice({
        type: 'error',
        message: error.message || 'Unable to update the default Calendar account.',
      })
    } finally {
      setIsUpdatingCalendarDefault(false)
    }
  }

  // Connect Google account
  const connectMutation = useMutation({
    mutationFn: (service) =>
      api.post(`/api/v1/oauth/google/connect?service=${service}`),

    onSuccess: (result) => {
      if (!result?.authorization_url) {
        setNotice({
          type: 'error',
          message: 'Google authorization URL was not returned.',
        })
        return
      }

      window.location.assign(result.authorization_url)
    },

    onError: (error) => {
      setNotice({
        type: 'error',
        message: error.message || 'Unable to connect Google service.',
      })
    },
  })

  // Disconnect Google account
  const disconnectMutation = useMutation({
    mutationFn: async (connectionIds) => {
      const disconnectedConnectionIds = []
      const errors = []

      for (const connectionId of connectionIds) {
        try {
          await api.delete(`/api/v1/oauth/connections/${connectionId}`)
          disconnectedConnectionIds.push(connectionId)
        } catch (error) {
          errors.push(error)
        }
      }

      if (errors.length > 0) {
        const error = new Error(
          errors[0].message || 'Unable to disconnect Google.',
        )
        error.disconnectedConnectionIds = disconnectedConnectionIds
        throw error
      }

      return connectionIds
    },

    onSuccess: async (_result, disconnectedConnectionIds) => {
      const defaultPreferenceError =
        await clearDisconnectedPreferences(disconnectedConnectionIds)

      await queryClient.invalidateQueries({
        queryKey: CONNECTIONS_KEY(userId),
      })

      await queryClient.removeQueries({
        queryKey: ['gmail-messages', userId],
      })

      await queryClient.removeQueries({
        queryKey: ['gmail-message', userId],
      })

      setNotice({
        type: defaultPreferenceError ? 'error' : 'success',
        message: defaultPreferenceError
          ? 'The account disconnected, but the Assistant could not update its saved Gmail account preference.'
          : 'Google account disconnected successfully.',
      })
    },

    onError: async (error) => {
      const disconnectedConnectionIds =
        error.disconnectedConnectionIds || []
      const defaultPreferenceError = await clearDisconnectedPreferences(
        disconnectedConnectionIds,
      )

      await queryClient.invalidateQueries({
        queryKey: CONNECTIONS_KEY(userId),
      })
      if (disconnectedConnectionIds.length > 0) {
        await queryClient.removeQueries({
          queryKey: ['gmail-messages', userId],
        })
        await queryClient.removeQueries({
          queryKey: ['gmail-message', userId],
        })
      }

      setNotice({
        type: 'error',
        message: defaultPreferenceError
          ? `${error.message || 'Unable to disconnect Google.'} The Assistant's saved Gmail account preference could not be updated.`
          : error.message || 'Unable to disconnect Google.',
      })
    },
  })

  // Handle OAuth callback
  useEffect(() => {
    const googleStatus = searchParams.get('google')

    if (!googleStatus) return

    if (googleStatus === 'connected') {
      setNotice({
        type: 'success',
        message: 'Google account connected successfully.',
      })

      queryClient.invalidateQueries({
        queryKey: CONNECTIONS_KEY(userId),
      })

    } else if (googleStatus === 'error') {
      setNotice({
        type: 'error',
        message: 'Google connection failed. Please try again.',
      })
    }

    setSearchParams({}, { replace: true })
  }, [
    searchParams,
    setSearchParams,
    queryClient,
    userId,
  ])

  function handleDisconnect(connectionIds) {
    const ids = Array.isArray(connectionIds)
      ? connectionIds
      : [connectionIds]
    if (ids.length === 0) return

    const affectedServices = new Set(
      connectedGoogleConnections
        .filter((connection) => ids.includes(connection.id))
        .flatMap((connection) => [
          ...(connection.capabilities?.gmail ? ['Gmail'] : []),
          ...(connection.capabilities?.calendar ? ['Google Calendar'] : []),
        ]),
    )
    const servicesText = [...affectedServices].join(' and ') || 'Google services'
    const confirmed = window.confirm(
      ids.length === 1
        ? `Disconnect this shared Google account from this app? This removes its stored credentials and disables ${servicesText} for this account. It does not revoke access in your Google Account settings.`
        : `Disconnect these shared Google accounts from this app? This removes their stored credentials and disables ${servicesText} for these accounts. It does not revoke access in your Google Account settings.`,
    )

    if (!confirmed) return

    setNotice(null)
    disconnectMutation.mutate(ids)
  }

  function handleConnect(service) {
    if (connectionsQuery.isLoading || connectionsQuery.isError) return

    setNotice(null)
    connectMutation.mutate(service)
  }

  return (
    <div className="mx-auto max-w-5xl space-y-8 pb-10">

      {/* Header */}
      <div>
        <div className="mb-2 text-sm font-semibold tracking-wider text-violet-500">
          INTEGRATIONS
        </div>

        <h1 className={`text-3xl font-semibold ${colors.heading}`}>
          Connected Apps
        </h1>

        <p className={`mt-2 ${colors.muted}`}>
          Connect your external accounts to your Personal AI Assistant.
        </p>
      </div>

      {/* Notification */}
      {notice && (
        <div
          role={notice.type === 'error' ? 'alert' : 'status'}
          className={`rounded-xl border p-4 text-sm ${
            notice.type === 'success'
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500'
              : 'border-red-500/30 bg-red-500/10 text-red-500'
          }`}
        >
          {notice.message}
        </div>
      )}

      <section aria-labelledby="google-services-heading">
        <h2
          id="google-services-heading"
          className={`mb-4 text-xl font-semibold ${colors.heading}`}
        >
          Google Services
        </h2>

        {connectionsQuery.isError && (
          <div className="mb-4 rounded-lg border border-red-500/20 bg-red-500/10 p-3 text-sm text-red-500">
            {connectionsQuery.error.message}
            <button
              type="button"
              onClick={() => connectionsQuery.refetch()}
              className="ml-2 font-semibold underline"
            >
              Retry
            </button>
          </div>
        )}

        <div className="grid items-stretch gap-4 sm:grid-cols-2">
          {[
            {
              id: 'gmail',
              name: 'Gmail',
              description: 'Connect your account to access Gmail in your workspace.',
              available: true,
              connected: isGmailConnected,
            },
            {
              id: 'calendar',
              name: 'Google Calendar',
              description: 'Use read-only Calendar event search, schedules, event details, and optional free/busy checks in chat.',
              available: true,
              connected: isCalendarApiComplete,
              partial: calendarNeedsConsent,
            },
            {
              id: 'drive',
              name: 'Google Drive',
              description: 'Access and organize your files.',
              available: false,
              connected: false,
            },
            {
              id: 'contacts',
              name: 'Google Contacts',
              description: 'Access and manage your contacts.',
              available: false,
              connected: false,
            },
          ].map((service) => {
            const isConnected = service.connected
            const toggleDisabled = !service.available
              || connectionsQuery.isLoading
              || connectionsQuery.isError
              || connectMutation.isPending
              || disconnectMutation.isPending

            return (
              <article
                key={service.id}
                className={`flex min-h-44 flex-col rounded-2xl border p-5 ${colors.service}`}
              >
                <div className="flex items-start gap-3">
                  <div className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-xl ${
                    isDark ? 'bg-[#1b1e2b]' : 'bg-slate-50'
                  }`}>
                    <GoogleServiceIcon service={service.id} />
                  </div>

                  <div className="min-w-0 flex-1">
                    <h3 className={`font-semibold ${colors.heading}`}>
                      {service.name}
                    </h3>
                    <p className={`mt-1 text-sm leading-5 ${colors.muted}`}>
                      {service.description}
                    </p>
                  </div>

                </div>

                <div className="mt-auto pt-5">
                  {service.available ? (
                    <>
                      <p className={`text-xs font-medium ${
                        isConnected ? 'text-emerald-500' : colors.muted
                      }`}>
                        {connectionsQuery.isLoading
                          ? 'Checking connection...'
                          : connectMutation.isPending
                            ? 'Connecting...'
                            : service.id === 'calendar'
                              && !calendarCapabilities.connected
                              ? 'Not connected'
                            : service.partial
                              ? 'Permissions incomplete'
                            : isConnected
                              ? 'Connected'
                              : 'Not connected'}
                      </p>

                      {connectedGoogleConnections.length > 0 && !isConnected && (
                        <p className={`mt-2 text-xs ${colors.muted}`}>
                          {service.partial
                            ? calendarCapabilities.connected
                              ? 'Calendar is connected with partial access. Grant the missing read-only permissions to enable the requested Calendar operations.'
                              : 'Calendar is not connected. Grant Calendar event-read and, if needed, separate free/busy permission in Connected Apps.'
                            : `A Google account is connected, but ${service.name} permission has not been granted.`}
                        </p>
                      )}

                      {service.id === 'calendar'
                        && calendarCapabilities.google_connection_available && (
                        <p className={`mt-2 text-xs ${colors.muted}`}>
                          Calendar event-read: {calendarCapabilities.events_read ? 'available' : 'not granted'} · FreeBusy: {calendarCapabilities.freebusy ? 'available' : 'not granted'} · Calendar chat: {calendarCapabilities.chat_available ? 'available' : 'not available'} · Calendar writes: not available.
                        </p>
                      )}

                      <button
                        type="button"
                        onClick={() => handleConnect(service.id)}
                        disabled={toggleDisabled || isConnected}
                        className={`mt-3 rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${
                          isDark
                            ? 'border-slate-700 text-slate-300 hover:bg-slate-800'
                            : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                        }`}
                      >
                        {isConnected
                          ? `${service.name} connected`
                          : service.partial
                            ? 'Grant missing Calendar permissions'
                            : `Connect ${service.name}`}
                      </button>
                    </>
                  ) : (
                    <div className="flex items-center justify-between">
                      <span className={`text-xs ${colors.muted}`}>
                        Coming soon
                      </span>
                      <span className={`text-xs ${colors.muted}`}>
                        Not connected
                      </span>
                    </div>
                  )}
                </div>
              </article>
            )
          })}
        </div>
      </section>

      {connectedGoogleConnections.length > 0 && (
        <section aria-labelledby="google-accounts-heading">
          <h2
            id="google-accounts-heading"
            className={`mb-4 text-xl font-semibold ${colors.heading}`}
          >
            Connected Google accounts
          </h2>
          <div className="space-y-3">
            {connectedGoogleConnections.map((connection) => (
              <article
                key={connection.id}
                className={`flex flex-wrap items-center justify-between gap-4 rounded-xl border p-4 ${colors.service}`}
              >
                <div className="min-w-0">
                  <p className={`truncate text-sm font-medium ${colors.heading}`}>
                    {connection.email || 'Google account'}
                  </p>
                  <p className={`mt-1 text-xs ${colors.muted}`}>
                    Gmail: {connection.capabilities?.gmail ? 'Connected' : 'Not connected'}
                    {' · '}
                    Google Calendar: {connection.capabilities?.calendar ? 'Connected' : 'Not connected'}
                  </p>
                  <p className={`mt-1 text-xs ${colors.muted}`}>
                    Calendar API: Events {connection.capabilities?.calendar_events_read ? 'available' : 'not granted'}
                    {' · '}
                    FreeBusy {connection.capabilities?.calendar_freebusy ? 'available' : 'not granted'}
                    {' · '}
                    Calendar chat: {connection.capabilities?.calendar_chat_available ? 'available' : 'not available'}
                    {' · '}
                    Calendar writes: not available
                  </p>
                  <p className={`mt-1 text-xs ${colors.muted}`}>
                    Connected {formatDate(connection.connected_at)}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => handleDisconnect(connection.id)}
                  disabled={disconnectMutation.isPending}
                  className={`shrink-0 rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${
                    isDark
                      ? 'border-slate-700 text-slate-300 hover:bg-slate-800'
                      : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                  }`}
                >
                  Disconnect Google account
                </button>
              </article>
            ))}
          </div>
          <div className={`mt-5 rounded-xl border p-4 ${colors.service}`}>
            <label
              htmlFor="default-calendar-account"
              className={`block text-sm font-medium ${colors.heading}`}
            >
              Default Calendar account
            </label>
            <p className={`mt-1 text-xs ${colors.muted}`}>
              Calendar reads and availability checks use this account when
              selected. If you leave it unset, an account is chosen only when
              the requested permission is unambiguous. Missing permissions on
              your selected account will not fall back to another account.
            </p>
            <select
              id="default-calendar-account"
              value={configuredDefaultConnectionId || ''}
              onChange={handleDefaultCalendarChange}
              disabled={isUpdatingCalendarDefault}
              className={`mt-3 w-full rounded-lg border px-3 py-2 text-sm ${
                isDark
                  ? 'border-slate-700 bg-[#11131c] text-slate-200'
                  : 'border-slate-200 bg-white text-slate-700'
              }`}
            >
              <option value="">
                Automatically select when unambiguous
              </option>
              {connectedGoogleConnections.map((connection) => (
                <option key={connection.id} value={connection.id}>
                  {connection.email || 'Google account'}
                  {connection.capabilities?.calendar_events_read
                    ? ' — event access'
                    : ''}
                  {connection.capabilities?.calendar_freebusy
                    ? ' — free/busy access'
                    : ''}
                </option>
              ))}
            </select>
          </div>
          <p className={`mt-3 text-xs ${colors.muted}`}>
            Disconnect removes the shared credentials from this app, so all enabled Google services for that account stop working. It does not revoke the app’s authorization in Google Account settings.
          </p>
        </section>
      )}

      {/* Background refresh indicator */}
      {connectionsQuery.isFetching &&
        !connectionsQuery.isLoading && (
          <p className={`text-xs ${colors.muted}`}>
            Refreshing connection information...
          </p>
        )}

    </div>
  )
}

export default Connectors
