
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

function GoogleIcon() {
  return (
    <svg viewBox="0 0 48 48" className="h-10 w-10">
      <path
        fill="#4285F4"
        d="M43.6 24.5c0-1.4-.1-2.8-.4-4.1H24v7.8h11a9.4 9.4 0 0 1-4.1 6.2v5.1h6.6c3.9-3.6 6.1-8.9 6.1-15z"
      />
      <path
        fill="#34A853"
        d="M24 44c5.5 0 10.1-1.8 13.5-4.9l-6.6-5.1c-1.8 1.2-4.1 2-6.9 2-5.3 0-9.8-3.6-11.4-8.4H5.8v5.3A20 20 0 0 0 24 44z"
      />
      <path
        fill="#FBBC05"
        d="M12.6 27.6a12 12 0 0 1 0-7.2v-5.3H5.8a20 20 0 0 0 0 17.8z"
      />
      <path
        fill="#EA4335"
        d="M24 12c3 0 5.7 1 7.8 3.1l5.8-5.8C34.1 6 29.5 4 24 4A20 20 0 0 0 5.8 15.1l6.8 5.3C14.2 15.6 18.7 12 24 12z"
      />
    </svg>
  )
}

function Connectors() {
  const { user } = useAuth()
  const { isDark } = useOutletContext()

  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [notice, setNotice] = useState(null)

  const userId = user?.id

  const colors = {
    card: isDark
      ? 'border-[#252838] bg-[#11131c]'
      : 'border-slate-200 bg-white',

    heading: isDark
      ? 'text-slate-100'
      : 'text-slate-800',

    text: isDark
      ? 'text-slate-300'
      : 'text-slate-700',

    muted: isDark
      ? 'text-slate-400'
      : 'text-slate-500',

    account: isDark
      ? 'border-[#303448] bg-[#191c29]'
      : 'border-slate-200 bg-slate-50',

    service: isDark
      ? 'border-[#252838] bg-[#11131c]'
      : 'border-slate-200 bg-white',

    hover: isDark
      ? 'hover:bg-[#202333]'
      : 'hover:bg-slate-100',

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

  const isGoogleConnected = googleConnections.some(
    (connection) => connection.status === 'connected',
  )

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
    mutationFn: (connectionId) =>
      api.delete(
        `/api/v1/oauth/connections/${connectionId}`,
      ),

    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: CONNECTIONS_KEY(userId),
      })

      await queryClient.invalidateQueries({
        queryKey: ['gmail-connections', userId],
      })

      await queryClient.removeQueries({
        queryKey: ['gmail-messages', userId],
      })

      await queryClient.removeQueries({
        queryKey: ['gmail-message', userId],
      })

      setNotice({
        type: 'success',
        message: 'Google account disconnected successfully.',
      })
    },

    onError: (error) => {
      setNotice({
        type: 'error',
        message: error.message || 'Unable to disconnect Google.',
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

      queryClient.invalidateQueries({
        queryKey: ['gmail-connections', userId],
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

  function handleDisconnect(connectionId) {
    const confirmed = window.confirm(
      'Disconnect this Google account? Your stored OAuth credentials will be deleted.',
    )

    if (!confirmed) return

    setNotice(null)
    disconnectMutation.mutate(connectionId)
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
          role="status"
          className={`rounded-xl border p-4 text-sm ${
            notice.type === 'success'
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500'
              : 'border-red-500/30 bg-red-500/10 text-red-500'
          }`}
        >
          {notice.message}
        </div>
      )}

      {/* Google Workspace */}
      <section className={`rounded-2xl border p-6 ${colors.card}`}>

        <div className="flex flex-col justify-between gap-5 sm:flex-row sm:items-center">

          <div className="flex items-center gap-4">

            <div className="rounded-xl bg-white p-2 shadow-sm">
              <GoogleIcon />
            </div>

            <div>
              <h2 className={`text-xl font-semibold ${colors.heading}`}>
                Google Workspace
              </h2>

              <p className={`mt-1 text-sm ${colors.muted}`}>
                Connect your Google account to access supported services.
              </p>
            </div>

          </div>

          <div>
            {connectionsQuery.isLoading ? (
              <span className={`text-sm ${colors.muted}`}>
                Checking connection...
              </span>
            ) : isGoogleConnected ? (
              <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-sm font-medium text-emerald-500">
                Connected
              </span>
            ) : (
              <span className={`rounded-full border px-3 py-1.5 text-sm ${
                isDark
                  ? 'border-slate-700 bg-slate-800 text-slate-400'
                  : 'border-slate-200 bg-slate-100 text-slate-500'
              }`}>
                Not connected
              </span>
            )}
          </div>

        </div>

        {/* Connection error */}
        {connectionsQuery.isError && (
          <div className="mt-5 rounded-lg border border-red-500/20 bg-red-500/10 p-3 text-sm text-red-500">
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

        {/* Connected accounts */}
        {googleConnections.map((connection) => (
          <div
            key={connection.id}
            className={`mt-6 flex flex-col justify-between gap-4 rounded-xl border p-4 sm:flex-row sm:items-center ${colors.account}`}
          >
            <div>
              <div className={`font-medium ${colors.heading}`}>
                {connection.email || 'Google account'}
              </div>

              <div className={`mt-1 text-xs ${colors.muted}`}>
                Connected: {formatDate(connection.connected_at)}
              </div>

              <div className="mt-2 text-xs font-medium text-emerald-500">
                Status: {connection.status}
              </div>
            </div>

            <button
              type="button"
              onClick={() => handleDisconnect(connection.id)}
              disabled={disconnectMutation.isPending}
              className="rounded-lg border border-red-500/30 px-4 py-2 text-sm font-medium text-red-500 transition hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {disconnectMutation.isPending
                ? 'Disconnecting...'
                : 'Disconnect'}
            </button>
          </div>
        ))}

        {/* Connect button */}
        <div className="mt-6">
          {!isGoogleConnected && (
            <button
              type="button"
              onClick={() => {
                setNotice(null)
                connectMutation.mutate()
              }}
              disabled={
                connectMutation.isPending ||
                connectionsQuery.isLoading
              }
              className="rounded-xl bg-violet-600 px-6 py-3 font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {connectMutation.isPending
                ? 'Connecting...'
                : 'Connect Google Account'}
            </button>
          )}
        </div>

      </section>

      {/* Google services */}
      <section>

        <h2 className={`mb-4 text-xl font-semibold ${colors.heading}`}>
          Google Services
        </h2>

        <div className="grid gap-4 sm:grid-cols-2">

          {[
            {
              name: 'Gmail',
              icon: '✉',
              description: 'Read and summarize your emails.',
              status: 'Read-only scope configured',
              enabled: true,
            },
            {
              name: 'Google Calendar',
              icon: '▦',
              description: 'Manage your events and schedules.',
              status: 'Scope not configured',
              enabled: false,
            },
            {
              name: 'Google Drive',
              icon: '▱',
              description: 'Search and summarize documents.',
              status: 'Scope not configured',
              enabled: false,
            },
            {
              name: 'Google Contacts',
              icon: '♙',
              description: 'Access your contacts.',
              status: 'Scope not configured',
              enabled: false,
            },
          ].map((service) => (
            <div
              key={service.name}
              className={`rounded-xl border p-5 transition ${colors.service}`}
            >
              <div className="flex items-center gap-3">

                <div className={`flex h-10 w-10 items-center justify-center rounded-xl text-xl ${
                  service.enabled
                    ? 'bg-emerald-500/10 text-emerald-500'
                    : isDark
                      ? 'bg-slate-800 text-slate-400'
                      : 'bg-slate-100 text-slate-500'
                }`}>
                  {service.icon}
                </div>

                <h3 className={`font-semibold ${colors.heading}`}>
                  {service.name}
                </h3>

              </div>

              <p className={`mt-3 text-sm ${colors.muted}`}>
                {service.description}
              </p>

              <div className={`mt-4 text-xs font-medium ${
                service.enabled
                  ? 'text-emerald-500'
                  : colors.muted
              }`}>
                {service.status}
              </div>
            </div>
          ))}

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
