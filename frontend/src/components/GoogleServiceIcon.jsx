const serviceNames = {
  gmail: 'Gmail',
  calendar: 'Google Calendar',
  drive: 'Google Drive',
  contacts: 'Google Contacts',
}

export default function GoogleServiceIcon({ service, className = 'h-9 w-9' }) {
  return (
    <img
      src={`/icons/google/${service}.svg`}
      alt={`${serviceNames[service]} logo`}
      data-testid={`google-logo-${service}`}
      className={className}
    />
  )
}
