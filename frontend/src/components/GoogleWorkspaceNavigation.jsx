import { Link } from 'react-router-dom'
import GoogleServiceIcon from './GoogleServiceIcon.jsx'

const services = [
  { id: 'gmail', label: 'Gmail', path: '/dashboard/gmail' },
  { id: 'calendar', label: 'Google Calendar', path: '/dashboard/calendar' },
  { id: 'drive', label: 'Google Drive' },
  { id: 'contacts', label: 'Google Contacts' },
]

export default function GoogleWorkspaceNavigation({
  connections = [],
  loading = false,
  failed = false,
  activeService,
  isDark,
}) {
  const activeGoogleConnections = connections.filter(
    (connection) =>
      connection.provider === 'google' &&
      connection.status === 'connected',
  )
  const gmailConnected = activeGoogleConnections.some(
    (connection) => connection.capabilities?.gmail === true,
  )
  const calendarEventRead = activeGoogleConnections.some(
    (connection) => connection.capabilities?.calendar_events_read === true,
  )
  const calendarFreeBusy = activeGoogleConnections.some(
    (connection) => connection.capabilities?.calendar_freebusy === true,
  )

  const surface = isDark
    ? 'border-[#252838] bg-[#11131c]'
    : 'border-slate-200 bg-white'
  const muted = isDark ? 'text-slate-400' : 'text-slate-500'

  function serviceState(service) {
    if (service.id === 'drive' || service.id === 'contacts') {
      return 'Coming soon'
    }
    if (loading) return 'Checking connection'
    if (failed) return 'Status unavailable'
    if (service.id === 'gmail') {
      return gmailConnected ? 'Connected' : 'Not connected'
    }
    if (!calendarEventRead) return 'Event access not connected'
    return calendarFreeBusy
      ? 'Connected · Free/Busy available'
      : 'Connected · Free/Busy not granted'
  }

  return (
    <nav
      aria-label="Google Workspace services"
      className={`mb-6 rounded-2xl border p-3 ${surface}`}
    >
      <div className={`mb-2 px-2 text-xs font-semibold uppercase tracking-wider ${muted}`}>
        Google Workspace
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {services.map((service) => {
          const isComingSoon = service.id === 'drive' || service.id === 'contacts'
          const available = service.id === 'gmail'
            ? gmailConnected
            : service.id === 'calendar' && calendarEventRead
          const path = available ? service.path : '/dashboard/connectors'
          const active = activeService === service.id

          return (
            <Link
              key={service.id}
              to={path}
              aria-current={active ? 'page' : undefined}
              aria-disabled={isComingSoon || undefined}
              onClick={(event) => {
                if (isComingSoon) event.preventDefault()
              }}
              className={`flex min-h-16 items-center gap-2 rounded-xl border px-3 py-2 transition ${
                active
                  ? 'border-violet-500/50 bg-violet-500/10 text-violet-400'
                  : isDark
                    ? 'border-[#252838] text-slate-200 hover:bg-[#191c29]'
                    : 'border-slate-200 text-slate-700 hover:bg-slate-50'
              } ${isComingSoon ? 'cursor-not-allowed opacity-70' : ''}`}
            >
              <GoogleServiceIcon service={service.id} />
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium">
                  {service.label}
                </span>
                <span className={`block truncate text-[11px] ${muted}`}>
                  {serviceState(service)}
                </span>
              </span>
            </Link>
          )
        })}
      </div>
      {!loading && !failed && calendarEventRead && !calendarFreeBusy && (
        <p className={`mt-2 px-2 text-xs ${muted}`}>
          Calendar events are available. Free/Busy checks require an additional
          Google Calendar permission.
        </p>
      )}
    </nav>
  )
}
