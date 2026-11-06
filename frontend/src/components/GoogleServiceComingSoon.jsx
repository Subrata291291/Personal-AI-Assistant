import { useOutletContext } from 'react-router-dom'
import GoogleServiceIcon from './GoogleServiceIcon.jsx'
import { isGoogleServiceConnected } from '../lib/googleServiceCapabilities.js'

const serviceDetails = {
  drive: {
    title: 'Google Drive',
    description: 'Drive integration is not implemented yet.',
    planned: [
      'File listing and folder navigation',
      'Search and file details',
      'Upload, download, and file organization',
    ],
  },
  contacts: {
    title: 'Google Contacts',
    description: 'Contacts integration is not implemented yet.',
    planned: [
      'Contact listing and search',
      'Contact details',
      'Contact changes after authorization and backend support',
    ],
  },
}

export default function GoogleServiceComingSoon({ service }) {
  const {
    isDark,
    connections = [],
    connectionsLoading = false,
    connectionsError = false,
  } = useOutletContext()
  const details = serviceDetails[service]
  const connected = isGoogleServiceConnected(connections, service)
  const panel = isDark
    ? 'border-[#252838] bg-[#11131c]'
    : 'border-slate-200 bg-white'
  const text = isDark ? 'text-slate-300' : 'text-slate-600'
  const status = connectionsLoading
    ? 'Checking service connection…'
    : connectionsError
      ? 'Connection status unavailable'
      : connected
        ? 'Google connection detected; service operations are coming soon.'
        : 'Not connected. OAuth connection is not available yet.'

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <header className="flex items-start gap-4">
        <GoogleServiceIcon
          service={service}
          className="h-11 w-11 shrink-0"
        />
        <div>
          <p className="text-xs font-bold uppercase tracking-widest text-violet-400">
            Google Workspace
          </p>
          <h1 className="mt-2 text-2xl font-bold">{details.title}</h1>
          <p className={`mt-2 text-sm ${text}`}>{details.description}</p>
        </div>
      </header>

      <section
        className={`space-y-4 rounded-2xl border p-5 ${panel}`}
        aria-labelledby={`${service}-availability-heading`}
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2
            id={`${service}-availability-heading`}
            className="text-lg font-semibold"
          >
            Workspace
          </h2>
          <span
            className="rounded-full border border-amber-500/30 bg-amber-500/10 px-3 py-1 text-xs font-medium text-amber-400"
            role={connectionsError ? 'alert' : 'status'}
          >
            {connectionsError ? 'Status unavailable' : 'Coming soon'}
          </span>
        </div>
        <p className={`text-sm ${text}`}>{status}</p>
        <div>
          <h3 className="text-sm font-semibold">Planned, not available</h3>
          <ul className={`mt-2 list-inside list-disc space-y-1 text-sm ${text}`}>
            {details.planned.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </div>
        <p className={`text-xs ${text}`}>
          No files, folders, or contacts are being retrieved or changed.
        </p>
      </section>
    </div>
  )
}
