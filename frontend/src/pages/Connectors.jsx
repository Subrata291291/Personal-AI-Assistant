
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
  const configuredDefaultConnectionId =
    user?.user_metadata?.default_gmail_connection_id
  const isGmailConnected = gmailConnections.length > 0

  async function clearDefaultGmailPreference(connectionIds) {
    if (!connectionIds.includes(configuredDefaultConnectionId)) return false

    try {
      await updateUserMetadata({
        default_gmail_connection_id: null,
      })
      return false
    } catch {
      return true
    }
  }

  // Connect Google account
  const connectMutation = useMutation({
    mutationFn: () =>
      api.post('/api/v1/oauth/google/connect'),

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
        message: error.message || 'Unable to connect Google.',
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
        await clearDefaultGmailPreference(disconnectedConnectionIds)

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
      const defaultPreferenceError = await clearDefaultGmailPreference(
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

    const confirmed = window.confirm(
      ids.length === 1
        ? 'Disconnect this Google account? Its stored OAuth credentials will be removed from this app.'
        : `Disconnect Gmail from all ${ids.length} connected Google accounts? Their stored OAuth credentials will be removed from this app.`,
    )

    if (!confirmed) return

    setNotice(null)
    disconnectMutation.mutate(ids)
  }

  function handleGmailToggle() {
    if (connectionsQuery.isLoading || connectionsQuery.isError) return

    if (isGmailConnected) {
      handleDisconnect(gmailConnections.map((connection) => connection.id))
      return
    }

    setNotice(null)
    connectMutation.mutate()
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
            },
            {
              id: 'calendar',
              name: 'Google Calendar',
              description: 'Manage your events and schedule.',
              available: false,
            },
            {
              id: 'drive',
              name: 'Google Drive',
              description: 'Access and organize your files.',
              available: false,
            },
            {
              id: 'contacts',
              name: 'Google Contacts',
              description: 'Access and manage your contacts.',
              available: false,
            },
          ].map((service) => {
            const isGmail = service.id === 'gmail'
            const isConnected = isGmail && isGmailConnected
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

                  <button
                    type="button"
                    role="switch"
                    aria-label={`${service.name} connection`}
                    aria-checked={Boolean(isConnected)}
                    aria-disabled={toggleDisabled}
                    disabled={toggleDisabled}
                    onClick={() => {
                      if (isGmail) handleGmailToggle()
                    }}
                    className={`relative mt-1 inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 focus-visible:ring-offset-2 ${
                      isConnected
                        ? 'bg-violet-600'
                        : isDark
                          ? 'bg-slate-700'
                          : 'bg-slate-300'
                    } ${toggleDisabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'}`}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                        isConnected ? 'translate-x-6' : 'translate-x-1'
                      }`}
                    />
                  </button>
                </div>

                <div className="mt-auto pt-5">
                  {isGmail ? (
                    <>
                      <p className={`text-xs font-medium ${
                        isConnected ? 'text-emerald-500' : colors.muted
                      }`}>
                        {connectionsQuery.isLoading
                          ? 'Checking connection...'
                          : connectMutation.isPending
                            ? 'Connecting...'
                            : disconnectMutation.isPending
                              ? 'Disconnecting...'
                              : isConnected
                                ? 'Connected'
                                : 'Not connected'}
                      </p>

                      {connectedGoogleConnections.some(
                        (connection) => connection.capabilities?.gmail !== true,
                      ) && !isConnected && (
                        <p className={`mt-2 text-xs ${colors.muted}`}>
                          Gmail access is not enabled for this account.
                        </p>
                      )}

                      {connectedGoogleConnections.map((connection) => (
                        <div
                          key={connection.id}
                          className={`mt-3 flex items-center justify-between gap-3 border-t pt-3 ${colors.divider}`}
                        >
                          <div className="min-w-0">
                            <p className={`truncate text-sm font-medium ${colors.heading}`}>
                              {connection.email || 'Google account'}
                            </p>
                            <p className={`mt-1 text-xs ${colors.muted}`}>
                              {connection.capabilities?.gmail === true
                                ? `Connected ${formatDate(connection.connected_at)}`
                                : 'Gmail access unavailable'}
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
                            Disconnect
                          </button>
                        </div>
                      ))}
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
