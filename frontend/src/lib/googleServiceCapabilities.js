export const GOOGLE_WORKSPACE_SERVICES = [
  {
    id: 'gmail',
    label: 'Gmail',
    path: '/dashboard/gmail',
    capability: 'gmail',
  },
  {
    id: 'calendar',
    label: 'Google Calendar',
    path: '/dashboard/calendar',
    capability: 'calendar_events_read',
  },
  {
    id: 'drive',
    label: 'Google Drive',
    path: '/dashboard/drive',
    capability: 'drive_files_read',
    comingSoon: true,
  },
  {
    id: 'contacts',
    label: 'Google Contacts',
    path: '/dashboard/contacts',
    capability: 'contacts_read',
    comingSoon: true,
  },
]

export function getConnectedGoogleServices(connections = []) {
  const connectedServices = new Set()

  for (const connection of connections) {
    if (
      connection?.provider !== 'google' ||
      connection?.status !== 'connected'
    ) {
      continue
    }

    for (const service of GOOGLE_WORKSPACE_SERVICES) {
      if (connection.capabilities?.[service.capability] === true) {
        connectedServices.add(service.id)
      }
    }
  }

  return connectedServices
}

export function isGoogleServiceConnected(connections, serviceId) {
  return getConnectedGoogleServices(connections).has(serviceId)
}

export function isGoogleServiceEnabled(serviceId, userMetadata = {}) {
  return userMetadata?.[`${serviceId}_enabled`] !== false
}

export function getGoogleServiceStatus(
  connections,
  serviceId,
  userMetadata = {},
) {
  const service = GOOGLE_WORKSPACE_SERVICES.find(
    (item) => item.id === serviceId,
  )
  if (!service) return 'Unavailable'
  if (service.comingSoon) return 'Coming soon'
  if (!isGoogleServiceConnected(connections, serviceId)) return 'Disconnected'
  return isGoogleServiceEnabled(serviceId, userMetadata)
    ? 'Connected · Enabled'
    : 'Connected · Disabled'
}

export function getConnectedGoogleServiceCount(connections) {
  const connectedServices = getConnectedGoogleServices(connections)
  return GOOGLE_WORKSPACE_SERVICES.filter(
    (service) => !service.comingSoon && connectedServices.has(service.id),
  ).length
}
