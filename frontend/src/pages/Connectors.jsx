
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
import { requireConnectionsResponse } from '../lib/responseValidation'
import { SEMANTIC_STATUS_STYLES } from '../lib/semanticStatusStyles.js'
import { isGoogleServiceEnabled } from '../lib/googleServiceCapabilities.js'

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

function writeCapabilityStatus(capabilities, action) {
  if (capabilities?.[`calendar_${action}_execution_available`]) {
    return 'available'
  }
  if (capabilities?.[`calendar_${action}_feature_enabled`]) {
    return 'permission missing'
  }
  return 'disabled'
}

function removeDisconnectedConnections(data, connectionIds) {
  if (!data) return data

  const connections = (data.connections || []).filter(
    (connection) => !connectionIds.includes(connection.id),
  )
  const googleConnections = connections.filter(
    (connection) =>
      connection.provider === 'google' &&
      connection.status === 'connected',
  )
  const capabilities = googleConnections.map(
    (connection) => connection.capabilities || {},
  )
  const eventsRead = capabilities.some((item) => item.calendar_events_read)
  const freebusy = capabilities.some((item) => item.calendar_freebusy)
  const writePermissionGranted = capabilities.some(
    (item) => item.calendar_write_permission_granted,
  )
  const createAvailable = capabilities.some(
    (item) => item.calendar_create_execution_available,
  )
  const updateAvailable = capabilities.some(
    (item) => item.calendar_update_execution_available,
  )
  const deleteAvailable = capabilities.some(
    (item) => item.calendar_delete_execution_available,
  )
  const createFeatureEnabled = capabilities.some(
    (item) => item.calendar_create_feature_enabled,
  )
  const updateFeatureEnabled = capabilities.some(
    (item) => item.calendar_update_feature_enabled,
  )
  const deleteFeatureEnabled = capabilities.some(
    (item) => item.calendar_delete_feature_enabled,
  )

  return {
    ...data,
    connections,
    total: connections.length,
    calendar_capabilities: {
      connected: capabilities.some((item) => item.calendar),
      events_read: eventsRead,
      freebusy,
      api_available: eventsRead || freebusy,
      api_complete: eventsRead && freebusy,
      chat_available: eventsRead || freebusy,
      google_connection_available: googleConnections.length > 0,
      permissions_need_consent: googleConnections.length > 0 &&
        !(eventsRead && freebusy),
      write_permission_granted: writePermissionGranted,
      create_execution_available: createAvailable,
      update_execution_available: updateAvailable,
      delete_execution_available: deleteAvailable,
      create_feature_enabled: createFeatureEnabled,
      update_feature_enabled: updateFeatureEnabled,
      delete_feature_enabled: deleteFeatureEnabled,
      write_execution_available:
        createAvailable || updateAvailable || deleteAvailable,
      write_available:
        createAvailable || updateAvailable || deleteAvailable,
    },
  }
}

function Connectors() {
  const { user, updateUserMetadata } = useAuth()
  const { isDark } = useOutletContext()

  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [notice, setNotice] = useState(null)

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

    queryFn: async () =>
      requireConnectionsResponse(
        await api.get('/api/v1/oauth/connections'),
      ),

    enabled: Boolean(userId),

    staleTime: 0,
    gcTime: GC_TIME,

    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always',
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
  const isCalendarEventReadAvailable = calendarCapabilities.events_read === true
  const calendarNeedsConsent =
    calendarCapabilities.connected === true && !isCalendarEventReadAvailable
  const configuredDefaultConnectionId =
    user?.user_metadata?.default_calendar_connection_id
  const isGmailConnected = gmailConnections.length > 0

  const integrationPreferenceMutation = useMutation({
    mutationFn: ({ preference, enabled }) =>
      updateUserMetadata({ [preference]: enabled }),
    onSuccess: (_user, { integration, enabled }) => {
      setNotice({
        type: 'success',
        message: `${integration} turned ${enabled ? 'on' : 'off'}. ${
          enabled
            ? 'Its Google account connection was not changed.'
            : 'Your Google account remains connected.'
        }`,
      })
    },
    onError: (error) => {
      setNotice({
        type: 'error',
        message: error.message || 'Unable to update integration preference.',
      })
    },
  })

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

  function updateDisconnectedConnectionCache(connectionIds) {
    if (connectionIds.length === 0) return
    queryClient.setQueryData(
      CONNECTIONS_KEY(userId),
      (data) => removeDisconnectedConnections(data, connectionIds),
    )
  }

  function handleIntegrationToggle(integration, enabled) {
    setNotice(null)
    const service = integration === 'gmail'
      ? 'gmail'
      : 'calendar'
    const connected = service === 'gmail'
      ? isGmailConnected
      : isCalendarEventReadAvailable
    if (enabled && !connected) {
      integrationPreferenceMutation.mutate({
        integration,
        enabled: true,
        preference: `${integration}_enabled`,
      }, {
        onSuccess: () => connectMutation.mutate({ service }),
      })
      return
    }
    integrationPreferenceMutation.mutate({
      integration,
      enabled,
      preference: `${integration}_enabled`,
    })
  }

  // Connect Google account
  const connectMutation = useMutation({
    mutationFn: ({ service, connectionId }) => {
      const params = new URLSearchParams({ service })
      if (connectionId) params.set('connection_id', connectionId)
      return api.post(`/api/v1/oauth/google/connect?${params.toString()}`)
    },

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
      updateDisconnectedConnectionCache(disconnectedConnectionIds)
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
      updateDisconnectedConnectionCache(disconnectedConnectionIds)
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
    connectMutation.mutate({ service })
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
              ? SEMANTIC_STATUS_STYLES.positive.badge
              : SEMANTIC_STATUS_STYLES.error.badge
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
              enabled: isGoogleServiceEnabled('gmail', user?.user_metadata),
            },
            {
              id: 'calendar',
              name: 'Google Calendar',
              description: 'Use read-only Calendar event search, schedules, event details, and optional free/busy checks.',
              available: true,
              connected: isCalendarEventReadAvailable,
              partial: calendarNeedsConsent,
              enabled: isGoogleServiceEnabled('calendar', user?.user_metadata),
            },
            {
              id: 'drive',
              name: 'Google Drive',
              description: 'Access and organize your files.',
              available: false,
              connected: false,
              enabled: false,
            },
            {
              id: 'contacts',
              name: 'Google Contacts',
              description: 'Access and manage your contacts.',
              available: false,
              connected: false,
              enabled: false,
            },
          ].map((service) => {
            const isConnected = service.connected
            const serviceEnabled = service.enabled
            const serviceStatusColor = service.partial
              ? SEMANTIC_STATUS_STYLES.warning.text
              : isConnected && serviceEnabled
                ? SEMANTIC_STATUS_STYLES.positive.text
                : SEMANTIC_STATUS_STYLES.neutral.text
            const serviceStatus = connectionsQuery.isLoading
              ? 'Checking connection...'
              : connectMutation.isPending
                ? 'Connecting...'
                : service.partial
                  ? 'Permissions incomplete'
                  : !isConnected
                    ? 'Disconnected'
                    : serviceEnabled
                      ? 'Connected · Enabled'
                      : 'Connected · Disabled'
            const toggleDisabled = !service.available
              || connectionsQuery.isLoading
              || connectionsQuery.isError
              || connectMutation.isPending
              || disconnectMutation.isPending
              || integrationPreferenceMutation.isPending

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
                      <p className={`text-xs font-medium ${serviceStatusColor}`}>
                        {serviceStatus}
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

                      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                        {!isConnected && (
                          <button
                            type="button"
                            onClick={() => handleConnect(service.id)}
                            disabled={toggleDisabled}
                            className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${
                              isDark
                                ? 'border-slate-700 text-slate-300 hover:bg-slate-800'
                                : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                            }`}
                          >
                            {service.partial
                              ? 'Grant missing Calendar permissions'
                              : `Connect ${service.name}`}
                          </button>
                        )}
                        <div className="flex shrink-0 items-center gap-3">
                          <span
                            aria-live="polite"
                            className={`min-w-8 text-right text-xs font-bold tracking-wide ${
                              serviceEnabled
                                ? SEMANTIC_STATUS_STYLES.positive.text
                                : isDark
                                  ? 'text-slate-400'
                                  : 'text-slate-500'
                            }`}
                          >
                            {service.enabled ? 'ON' : 'OFF'}
                          </span>
                          <button
                            type="button"
                            role="switch"
                            aria-label={`Enable ${service.name}`}
                            aria-checked={serviceEnabled}
                            aria-busy={integrationPreferenceMutation.isPending}
                            title={`${service.name} ${serviceEnabled ? 'ON' : 'OFF'}`}
                            disabled={toggleDisabled}
                            onClick={() => handleIntegrationToggle(
                              service.id,
                              !serviceEnabled,
                            )}
                            className={`group relative inline-flex h-7 w-[3.25rem] shrink-0 cursor-pointer items-center rounded-full p-1 transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-400 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-60 ${
                              isDark
                                ? 'focus-visible:ring-offset-slate-900'
                                : 'focus-visible:ring-offset-white'
                            } ${
                              serviceEnabled
                                ? SEMANTIC_STATUS_STYLES.positive.toggle
                                : isDark
                                  ? 'bg-slate-700 hover:bg-slate-600'
                                  : 'bg-slate-300 hover:bg-slate-400'
                            }`}
                          >
                            <span
                              aria-hidden="true"
                              className={`h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                                integrationPreferenceMutation.isPending
                                  ? 'animate-pulse'
                                  : ''
                              } ${
                                serviceEnabled
                                  ? 'translate-x-5'
                                  : 'translate-x-0'
                              }`}
                            />
                          </button>
                        </div>
                      </div>
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
                    Calendar writes: {connection.capabilities?.calendar_write_permission_granted
                      ? 'permission granted'
                      : 'permission not granted'}
                    {' · '}
                    Create {writeCapabilityStatus(connection.capabilities, 'create')}
                    {' · '}
                    Update {writeCapabilityStatus(connection.capabilities, 'update')}
                    {' · '}
                    Delete {writeCapabilityStatus(connection.capabilities, 'delete')}
                  </p>
                  <p className={`mt-1 text-xs ${colors.muted}`}>
                    Connected {formatDate(connection.connected_at)}
                  </p>
                </div>
                {!connection.capabilities?.calendar_write_permission_granted && (
                  <button
                    type="button"
                    onClick={() => connectMutation.mutate({
                      service: 'calendar_write',
                      connectionId: connection.id,
                    })}
                    disabled={connectMutation.isPending}
                    className={`shrink-0 rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${
                      isDark
                        ? 'border-slate-700 text-slate-300 hover:bg-slate-800'
                        : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                    }`}
                  >
                    Grant Calendar write access
                  </button>
                )}
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
