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

export function getConnectedGoogleServiceCount(connections) {
  return getConnectedGoogleServices(connections).size
}
