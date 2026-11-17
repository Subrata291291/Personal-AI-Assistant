
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
import {
  GOOGLE_CONNECTIONS_STALE_TIME,
  PRIVATE_DATA_GC_TIME,
} from '../lib/queryClient.js'

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
  const featureEnabled = capabilities?.[`calendar_${action}_feature_enabled`]
  const executionAvailable =
    capabilities?.[`calendar_${action}_execution_available`]
  return {
    feature: featureEnabled === true
      ? 'Enabled'
      : featureEnabled === false
        ? 'Disabled'
        : 'Not available',
    execution: executionAvailable === true
      ? 'Available'
      : executionAvailable === false
        ? 'Not available'
        : 'Not available',
  }
}

function capabilityStatus(value, granted = 'Granted', notGranted = 'Not granted') {
  if (value === true) return granted
  if (value === false) return notGranted
  return 'Not available'
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
  const contactsRead = capabilities.some((item) => item.contacts_read)
  const contactsWriteGranted = capabilities.some(
    (item) => item.contacts_write_permission_granted,
  )
  const contactsCreateAvailable = capabilities.some(
    (item) => item.contacts_create_execution_available,
  )
  const contactsUpdateAvailable = capabilities.some(
    (item) => item.contacts_update_execution_available,
  )
  const contactsDeleteAvailable = capabilities.some(
    (item) => item.contacts_delete_execution_available,
  )
  const driveContractsRead = capabilities.some(
    (item) => item.drive_contracts_read,
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
    contacts_capabilities: {
      connected: capabilities.some((item) => item.contacts),
      read: contactsRead,
      write_permission_granted: contactsWriteGranted,
      google_connection_available: googleConnections.length > 0,
      permissions_need_consent:
        googleConnections.length > 0 && !contactsRead,
      create_execution_available: contactsCreateAvailable,
      update_execution_available: contactsUpdateAvailable,
      delete_execution_available: contactsDeleteAvailable,
      write_execution_available:
        contactsCreateAvailable ||
        contactsUpdateAvailable ||
        contactsDeleteAvailable,
    },
    drive_capabilities: {
      connected: capabilities.some((item) => item.drive),
      read: driveContractsRead,
      contracts_read: driveContractsRead,
      google_connection_available: googleConnections.length > 0,
      permissions_need_consent:
        googleConnections.length > 0 && !driveContractsRead,
    },
  }
}

function getCalendarDescription(capabilities) {
  const permission = capabilities.write_permission_granted
  const permissionText = permission === true
    ? 'Write permission is granted'
    : permission === false
      ? 'Write permission is not granted'
      : 'Write permission status is unavailable'
  const featureStates = [
    capabilities.create_feature_enabled,
    capabilities.update_feature_enabled,
    capabilities.delete_feature_enabled,
  ]
  const executionText = featureStates.every((enabled) => enabled === false)
    ? 'event creation, updates and deletion are currently disabled.'
    : featureStates.every((enabled) => enabled === true)
      ? 'event creation, updates and deletion are currently enabled.'
      : `Create is ${capabilityStatus(featureStates[0], 'enabled', 'disabled').toLowerCase()}, update is ${capabilityStatus(featureStates[1], 'enabled', 'disabled').toLowerCase()}, and delete is ${capabilityStatus(featureStates[2], 'enabled', 'disabled').toLowerCase()}.`

  return `Access calendar events and availability. ${permissionText}, but ${executionText}`
}

function getContactsDescription(capabilities) {
  const permission = capabilities.write_permission_granted
  const permissionText = permission === true
    ? 'Google Contacts write permission is currently granted on this account. The Assistant uses read-only Contacts access; to remove write permission, revoke or reauthorize this Google connection.'
    : permission === false
      ? 'Read-only Google Contacts access is requested. Write permission is not granted.'
      : 'Contacts permission status is unavailable.'
  const featureStates = [
    capabilities.create_feature_enabled,
    capabilities.update_feature_enabled,
    capabilities.delete_feature_enabled,
  ]
  const executionText = featureStates.every((enabled) => enabled === false)
    ? 'Contact creation, updates, and deletion are disabled in the Assistant.'
    : featureStates.every((enabled) => enabled === true)
      ? 'Contact creation, updates, and deletion are enabled.'
      : `Create is ${capabilityStatus(featureStates[0], 'enabled', 'disabled').toLowerCase()}, update is ${capabilityStatus(featureStates[1], 'enabled', 'disabled').toLowerCase()}, and delete is ${capabilityStatus(featureStates[2], 'enabled', 'disabled').toLowerCase()}.`

  return `Search and view your Google Contacts. ${permissionText} ${executionText}`
}

function getDriveDescription(capabilities) {
  const permission = capabilities.contracts_read
  const permissionText = permission === true
    ? 'selected-file read access is granted'
    : permission === false
      ? 'selected-file read access is not granted'
      : 'selected-file read access status is unavailable'
  return `Select PDF, DOCX, or Google Docs contracts with Google Picker. Access is read-only; ${permissionText}, and contract text is processed on demand without being stored.`
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

    staleTime: GOOGLE_CONNECTIONS_STALE_TIME,
    gcTime: PRIVATE_DATA_GC_TIME,

    refetchOnMount: true,
    refetchOnWindowFocus: true,
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
  const selectedGmailConnection =
    gmailConnections.find(
      (connection) =>
        connection.id === user?.user_metadata?.default_gmail_connection_id,
    ) || gmailConnections[0] || null
  const gmailAccountLabel =
    selectedGmailConnection?.email ||
    selectedGmailConnection?.account_email ||
    (gmailConnections.length > 0 ? 'Connected Google account' : 'Not connected')
  const gmailCapabilities = selectedGmailConnection?.capabilities || {}
  const gmailReadGranted =
    gmailCapabilities.gmail_read === true ||
    gmailCapabilities.gmail === true
  const gmailModifyGranted =
    gmailCapabilities.gmail_modify === true ||
    gmailCapabilities.gmail_write === true
  const gmailSendGranted =
    gmailCapabilities.gmail_send === true
  const gmailCapabilitiesAvailable = isGoogleServiceEnabled(
    'gmail',
    user?.user_metadata,
  )
  const calendarCapabilities =
    connectionsQuery.data?.calendar_capabilities || {}
  const connectedCalendarCapabilities = connectedGoogleConnections.map(
    (connection) => connection.capabilities || {},
  )
  const aggregateCalendarCapability = (aggregateKey, connectionKey) => {
    if (typeof calendarCapabilities[aggregateKey] === 'boolean') {
      return calendarCapabilities[aggregateKey]
    }
    const values = connectedCalendarCapabilities.map(
      (capability) => capability[connectionKey],
    )
    if (values.some((value) => value === true)) return true
    if (values.length > 0 && values.every((value) => value === false)) {
      return false
    }
    return undefined
  }
  const calendarDescription = getCalendarDescription({
    write_permission_granted: aggregateCalendarCapability(
      'write_permission_granted',
      'calendar_write_permission_granted',
    ),
    create_feature_enabled: aggregateCalendarCapability(
      'create_feature_enabled',
      'calendar_create_feature_enabled',
    ),
    update_feature_enabled: aggregateCalendarCapability(
      'update_feature_enabled',
      'calendar_update_feature_enabled',
    ),
    delete_feature_enabled: aggregateCalendarCapability(
      'delete_feature_enabled',
      'calendar_delete_feature_enabled',
    ),
  })
  const isCalendarEventReadAvailable = aggregateCalendarCapability(
    'events_read',
    'calendar_events_read',
  ) === true
  const isCalendarConnected = aggregateCalendarCapability(
    'connected',
    'calendar',
  )
  const calendarNeedsConsent =
    isCalendarConnected === true && !isCalendarEventReadAvailable
  const configuredDefaultConnectionId =
    user?.user_metadata?.default_calendar_connection_id
  const configuredDefaultContactsConnectionId =
    user?.user_metadata?.default_contacts_connection_id
  const isGmailConnected = gmailConnections.length > 0

  const contactsCapabilities =
    connectionsQuery.data?.contacts_capabilities || {}
  const driveCapabilities =
    connectionsQuery.data?.drive_capabilities || {}
  const aggregateServiceCapability = (
    aggregateCapabilities,
    connectionKey,
    aggregateKey,
  ) => {
    if (typeof aggregateCapabilities[aggregateKey] === 'boolean') {
      return aggregateCapabilities[aggregateKey]
    }
    const values = connectedCalendarCapabilities.map(
      (capability) => capability[connectionKey],
    )
    if (values.some((value) => value === true)) return true
    if (values.length > 0 && values.every((value) => value === false)) {
      return false
    }
    return undefined
  }
  const aggregateContactsCapability = (aggregateKey, connectionKey) =>
    aggregateServiceCapability(
      contactsCapabilities,
      connectionKey,
      aggregateKey,
    )
  const aggregateDriveCapability = (aggregateKey, connectionKey) =>
    aggregateServiceCapability(
      driveCapabilities,
      connectionKey,
      aggregateKey,
    )
  const isContactsReadAvailable =
    aggregateContactsCapability('read', 'contacts_read') === true
  const contactsReadAvailableForAssistant =
    typeof contactsCapabilities.chat_available === 'boolean'
      ? contactsCapabilities.chat_available
      : typeof contactsCapabilities.read_available === 'boolean'
        ? contactsCapabilities.read_available
        : isContactsReadAvailable
  const contactsSelectionRequired =
    contactsCapabilities.selection_required === true
  const contactsDefaultInvalid =
    contactsCapabilities.default_connection_invalid === true
  const isContactsConnected =
    contactsReadAvailableForAssistant ||
    (isContactsReadAvailable &&
      !contactsSelectionRequired &&
      !contactsDefaultInvalid)
  const contactsNeedsConsent =
    contactsCapabilities.permissions_need_consent ??
    (connectedGoogleConnections.length > 0 && !isContactsReadAvailable)
  const _isDriveReadAvailable =
    aggregateDriveCapability('contracts_read', 'drive_contracts_read') === true
  const _isDriveConnected =
    aggregateDriveCapability('connected', 'drive') === true
  const _driveNeedsConsent =
    driveCapabilities.permissions_need_consent ??
    (connectedGoogleConnections.length > 0 && !_isDriveReadAvailable)
  const contactsCandidates = connectedGoogleConnections.filter(
    (connection) =>
      connection.capabilities?.contacts_read === true ||
      connection.capabilities?.contacts_write_permission_granted === true,
  )
  const contactsDescription = (() => {
    if (contactsSelectionRequired) {
      return 'Search and view your Google Contacts. More than one connected Google account has Contacts access; choose which account to use below.'
    }
    if (contactsDefaultInvalid) {
      return 'Search and view your Google Contacts. The selected Contacts account is unavailable; choose a different account below.'
    }
    if (contactsReadAvailableForAssistant || isContactsReadAvailable) {
      return 'Search and view your Google Contacts. Contact information is read-only in this app.'
    }
    return 'Search and view your Google Contacts. Connect a Google account and grant Contacts read access to use this feature.'
  })()
  const _driveDescription = getDriveDescription({
    contracts_read: aggregateDriveCapability(
      'contracts_read',
      'drive_contracts_read',
    ),
  })

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
    if (connectionIds.includes(configuredDefaultContactsConnectionId)) {
      metadata.default_contacts_connection_id = null
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
    if (integration === 'drive') {
      setNotice({
        type: 'info',
        message: 'Google Drive is coming soon and is not available yet.',
      })
      return
    }
    const serviceById = {
      gmail: 'gmail',
      calendar: 'calendar',
      contacts: 'contacts_read',
    }
    const connectedByIntegration = {
      gmail: isGmailConnected,
      calendar: isCalendarEventReadAvailable,
      contacts: isContactsReadAvailable,
    }
    const service = serviceById[integration]
    const connected = connectedByIntegration[integration] === true
    if (enabled && !connected && service) {
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
          ...(connection.capabilities?.contacts ? ['Google Contacts'] : []),
          ...(connection.capabilities?.drive ? ['Google Drive'] : []),
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

    const serviceMap = {
      contacts: 'contacts_read',
      drive: 'drive_file',
    }
    const resolvedService = serviceMap[service] || service

    setNotice(null)
    connectMutation.mutate({ service: resolvedService })
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
              description: calendarDescription,
              available: true,
              connected: isCalendarEventReadAvailable,
              partial: calendarNeedsConsent,
              enabled: isGoogleServiceEnabled('calendar', user?.user_metadata),
            },
            {
              id: 'drive',
              name: 'Google Drive',
              description:
                'Coming soon. Google Drive integration is temporarily unavailable and will be available in a future update.',
              available: false,
              comingSoon: true,
              connected: false,
              partial: false,
              enabled: false,
            },
            {
              id: 'contacts',
              name: 'Google Contacts',
              description: contactsDescription,
              available: true,
              connected: isContactsConnected,
              partial: contactsNeedsConsent || contactsSelectionRequired || contactsDefaultInvalid,
              enabled: isGoogleServiceEnabled('contacts', user?.user_metadata),
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
                className={`rounded-2xl border p-5 transition-colors duration-200 hover:border-slate-400/60 dark:hover:border-slate-600 ${colors.service}`}
              >
                <div className="flex items-start gap-3">
                  <div className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-xl ${
                    isDark ? 'border border-slate-700/70 bg-slate-800/70' : 'border border-slate-200 bg-slate-50'
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
                  {(service.available || service.comingSoon) && (
                    <div className="flex shrink-0 items-center gap-2">
                      <span
                        aria-live="polite"
                        className={`text-xs font-bold tracking-wide ${
                          serviceEnabled && !service.comingSoon
                            ? SEMANTIC_STATUS_STYLES.positive.text
                            : isDark
                              ? 'text-slate-400'
                              : 'text-slate-500'
                        }`}
                      >
                        {service.comingSoon ? 'OFF' : service.enabled ? 'ON' : 'OFF'}
                      </span>
                      <button
                        type="button"
                        role="switch"
                        aria-label={`Enable ${service.name}`}
                        aria-checked={service.comingSoon ? false : serviceEnabled}
                        aria-busy={integrationPreferenceMutation.isPending && !service.comingSoon}
                        title={service.comingSoon ? `${service.name} coming soon` : `${service.name} ${serviceEnabled ? 'ON' : 'OFF'}`}
                        disabled={toggleDisabled || service.comingSoon}
                        onClick={() => handleIntegrationToggle(
                          service.id,
                          !serviceEnabled,
                        )}
                        className={`group relative inline-flex h-7 w-[3.25rem] shrink-0 cursor-pointer items-center rounded-full p-1 transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-400 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 ${
                          isDark
                            ? 'focus-visible:ring-offset-slate-900'
                            : 'focus-visible:ring-offset-white'
                        } ${
                          serviceEnabled && !service.comingSoon
                            ? SEMANTIC_STATUS_STYLES.positive.toggle
                            : isDark
                              ? 'bg-slate-700'
                              : 'bg-slate-300'
                        }`}
                      >
                        <span
                          aria-hidden="true"
                          className={`h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
                            integrationPreferenceMutation.isPending && !service.comingSoon
                              ? 'animate-pulse'
                              : ''
                          } ${
                            serviceEnabled && !service.comingSoon
                              ? 'translate-x-5'
                              : 'translate-x-0'
                          }`}
                        />
                      </button>
                    </div>
                  )}
                </div>

                <div className={`mt-4 border-t pt-4 ${colors.divider}`}>
                  {service.available ? (
                    <>
                      <p className={`text-xs font-medium ${serviceStatusColor}`}>
                        {serviceStatus}
                      </p>

                      {connectedGoogleConnections.length > 0 && !isConnected && (
                        <p className={`mt-2 text-xs ${colors.muted}`}>
                          {service.partial && service.id === 'calendar'
                            ? calendarCapabilities.connected
                              ? 'Calendar is connected with partial access. Grant the missing read-only permissions to enable the requested Calendar operations.'
                              : 'Calendar is not connected. Grant Calendar event-read and, if needed, separate free/busy permission in Connected Apps.'
                            : service.partial && service.id === 'contacts' && contactsSelectionRequired
                              ? 'More than one connected Google account has Contacts access. Choose which account to use below so the Assistant can read contacts.'
                              : service.partial && service.id === 'contacts' && contactsDefaultInvalid
                                ? 'The selected Contacts account is no longer available. Choose a different Contacts account below.'
                                : `A Google account is connected, but ${service.name} permission has not been granted.`}
                        </p>
                      )}

                      {service.id === 'calendar'
                        && connectedGoogleConnections.length > 0 && (
                        <div className={`mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 ${colors.muted}`}>
                          <p>
                            Assistant Calendar context:{' '}
                            {capabilityStatus(aggregateCalendarCapability(
                              'api_available',
                              'calendar_api_available',
                            ), 'Available', 'Not available')}
                          </p>
                          <p>
                            Event-read:{' '}
                            {capabilityStatus(aggregateCalendarCapability(
                              'events_read',
                              'calendar_events_read',
                            ))}
                          </p>
                          <p>
                            FreeBusy:{' '}
                            {capabilityStatus(aggregateCalendarCapability(
                              'freebusy',
                              'calendar_freebusy',
                            ))}
                          </p>
                          <p>
                            Write permission:{' '}
                            {capabilityStatus(aggregateCalendarCapability(
                              'write_permission_granted',
                              'calendar_write_permission_granted',
                            ))}
                          </p>
                          {['create', 'update', 'delete'].map((action) => {
                            const featureEnabled = aggregateCalendarCapability(
                              `${action}_feature_enabled`,
                              `calendar_${action}_feature_enabled`,
                            )
                            const executionAvailable = aggregateCalendarCapability(
                              `${action}_execution_available`,
                              `calendar_${action}_execution_available`,
                            )
                            return (
                              <p key={action}>
                                {action[0].toUpperCase() + action.slice(1)}:{' '}
                                {capabilityStatus(
                                  featureEnabled,
                                  'Enabled',
                                  'Disabled',
                                )}
                                {' · Execution '}
                                {capabilityStatus(
                                  executionAvailable,
                                  'available',
                                  'not available',
                                )}
                              </p>
                            )
                          })}
                        </div>
                      )}

                      {service.id === 'contacts'
                        && connectedGoogleConnections.length > 0 && (
                        <div className={`mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 ${colors.muted}`}>
                          <p>
                            Read permission:{' '}
                            {capabilityStatus(aggregateContactsCapability(
                              'read',
                              'contacts_read',
                            ))}
                          </p>
                          <p>
                            Write permission:{' '}
                            {capabilityStatus(aggregateContactsCapability(
                              'write_permission_granted',
                              'contacts_write_permission_granted',
                            ))}
                          </p>
                          {['create', 'update', 'delete'].map((action) => {
                            const featureEnabled = aggregateContactsCapability(
                              `${action}_feature_enabled`,
                              `contacts_${action}_feature_enabled`,
                            )
                            const executionAvailable = aggregateContactsCapability(
                              `${action}_execution_available`,
                              `contacts_${action}_execution_available`,
                            )
                            return (
                              <p key={action}>
                                {action[0].toUpperCase() + action.slice(1)}:{' '}
                                {capabilityStatus(
                                  featureEnabled,
                                  'Enabled',
                                  'Disabled',
                                )}
                                {' · Execution '}
                                {capabilityStatus(
                                  executionAvailable,
                                  'available',
                                  'not available',
                                )}
                              </p>
                            )
                          })}
                        </div>
                      )}

                      {service.id === 'gmail'
                        && connectedGoogleConnections.length > 0 && (
                        <div className={`mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 ${colors.muted}`}>
                          <p>
                            Connected account:{' '}
                            <span className={isDark ? 'text-slate-200' : 'text-slate-700'}>
                              {gmailAccountLabel || 'Not available'}
                            </span>
                          </p>
                          <p>
                            Read permission:{' '}
                            {capabilityStatus(gmailReadGranted)}
                          </p>
                          <p>
                            Modify/Draft permission:{' '}
                            {capabilityStatus(gmailModifyGranted)}
                          </p>
                          <p>
                            Send permission:{' '}
                            {capabilityStatus(gmailSendGranted)}
                          </p>
                          <p>
                            Email search:{' '}
                            {gmailCapabilitiesAvailable ? 'Available' : gmailReadGranted ? 'Disabled in settings' : 'Not available'}
                          </p>
                          <p>
                            Email summary:{' '}
                            {gmailCapabilitiesAvailable ? 'Available' : gmailReadGranted ? 'Disabled in settings' : 'Not available'}
                          </p>
                          <p className="sm:col-span-2">
                            Draft/reply/compose actions:{' '}
                            {gmailCapabilitiesAvailable && (gmailModifyGranted || gmailSendGranted)
                              ? 'Available (subject to permissions and review flows)'
                              : gmailReadGranted
                                ? 'Limited by granted permissions or settings'
                                : 'Not available'}
                          </p>
                        </div>
                      )}

                      {service.id === 'contacts'
                        && contactsCandidates.length > 1 && (
                        <label className={`mt-3 block text-xs ${colors.muted}`}>
                          Contacts account
                          <select
                            className={`mt-1 block w-full rounded-lg border px-2 py-1.5 text-xs ${
                              isDark
                                ? 'border-slate-700 bg-slate-900 text-slate-200'
                                : 'border-slate-200 bg-white text-slate-700'
                            }`}
                            value={configuredDefaultContactsConnectionId || ''}
                            onChange={(event) => {
                              const value = event.target.value || null
                              updateUserMetadata({
                                default_contacts_connection_id: value,
                              })
                            }}
                          >
                            <option value="">Choose an account</option>
                            {contactsCandidates.map((connection) => (
                              <option key={connection.id} value={connection.id}>
                                {connection.email || 'Google account'}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}

                      {service.id === 'drive'
                        && connectedGoogleConnections.length > 0 && (
                        <div className={`mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 ${colors.muted}`}>
                          <p>
                            Selected-file read access:{' '}
                            {capabilityStatus(aggregateDriveCapability(
                              'contracts_read',
                              'drive_contracts_read',
                            ))}
                          </p>
                          <p>
                            File operations: Read-only (no upload, edit,
                            delete, or sharing)
                          </p>
                        </div>
                      )}

                      <div className="mt-4 flex flex-wrap items-center gap-3">
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
                              ? service.id === 'calendar'
                                ? 'Grant missing Calendar permissions'
                                : `Grant missing ${service.name} permissions`
                              : `Connect ${service.name}`}
                          </button>
                        )}
                      </div>
                    </>
                  ) : (
                    <div className="flex items-center justify-between">
                      <span className={`text-xs font-medium ${SEMANTIC_STATUS_STYLES.warning?.text || 'text-amber-400'}`}>
                        {service.comingSoon ? 'Under development' : 'Coming soon'}
                      </span>
                      <span className={`text-xs ${colors.muted}`}>
                        {service.comingSoon ? 'Unavailable' : 'Not connected'}
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
                    Google Contacts: {connection.capabilities?.contacts ? 'Connected' : 'Not connected'}
                    {' · Read: '}
                    {capabilityStatus(connection.capabilities?.contacts_read)}
                    {' · Write: '}
                    {capabilityStatus(
                      connection.capabilities?.contacts_write_permission_granted,
                    )}
                  </p>
                  <p className={`mt-1 text-xs ${colors.muted}`}>
                    Assistant Calendar context: {capabilityStatus(
                      connection.capabilities?.calendar_api_available,
                      'Available',
                      'Not available',
                    )}
                    {' · '}
                    Event-read: {capabilityStatus(
                      connection.capabilities?.calendar_events_read,
                      'Granted',
                      'Not granted',
                    )}
                    {' · '}
                    FreeBusy: {capabilityStatus(
                      connection.capabilities?.calendar_freebusy,
                      'Granted',
                      'Not granted',
                    )}
                    {' · '}
                    Write permission: {capabilityStatus(
                      connection.capabilities?.calendar_write_permission_granted,
                    )}
                    {['create', 'update', 'delete'].map((action) => {
                      const status = writeCapabilityStatus(
                        connection.capabilities,
                        action,
                      )
                      return (
                        <span key={action}>
                          {' · '}
                          {action[0].toUpperCase() + action.slice(1)}: {status.feature}
                          {' · Execution '}
                          {status.execution}
                        </span>
                      )
                    })}
                  </p>
                  <p className={`mt-1 text-xs ${colors.muted}`}>
                    Connected {formatDate(connection.connected_at)}
                  </p>
                </div>
                {connection.capabilities?.calendar_write_permission_granted === false && (
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
                {connection.capabilities?.calendar_events_read === true &&
                  connection.capabilities?.calendar_list_read === false && (
                  <div className="flex flex-col items-start gap-1">
                    <p className={`max-w-56 text-xs ${colors.muted}`}>
                      Calendar list permission is required to discover and
                      select secondary calendars.
                    </p>
                    <button
                      type="button"
                      onClick={() => connectMutation.mutate({
                        service: 'calendar_list_read',
                        connectionId: connection.id,
                      })}
                      disabled={connectMutation.isPending}
                      className={`shrink-0 rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${
                        isDark
                          ? 'border-slate-700 text-slate-300 hover:bg-slate-800'
                          : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                      }`}
                    >
                      Grant Calendar List Access
                    </button>
                  </div>
                )}
                {connection.capabilities?.contacts_read === false && (
                  <button
                    type="button"
                    onClick={() => connectMutation.mutate({
                      service: 'contacts_read',
                      connectionId: connection.id,
                    })}
                    disabled={connectMutation.isPending}
                    className={`shrink-0 rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${
                      isDark
                        ? 'border-slate-700 text-slate-300 hover:bg-slate-800'
                        : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                    }`}
                  >
                    Grant Contacts read access
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
