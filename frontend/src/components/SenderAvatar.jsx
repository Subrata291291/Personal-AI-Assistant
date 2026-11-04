import { useState } from 'react'
import {
  getCompanyLogoUrl,
  getSenderColor,
  getSenderInitials,
  getVerifiedContactPhotoUrl,
  parseSenderIdentity,
} from '../utils/senderAvatar.js'

const failedLogoUrls = new Set()

function SenderAvatar({
  sender,
  contactPhoto,
  isDark = false,
  size = 'h-10 w-10',
  className = '',
}) {
  const contactPhotoUrl = getVerifiedContactPhotoUrl(contactPhoto)
  const companyLogoUrl = getCompanyLogoUrl(sender)
  const imageUrl = contactPhotoUrl ||
    (companyLogoUrl && !failedLogoUrls.has(companyLogoUrl)
      ? companyLogoUrl
      : '')
  const [failedUrl, setFailedUrl] = useState('')
  const initials = getSenderInitials(sender)
  const { name } = parseSenderIdentity(sender)

  return (
    <span
      role="img"
      aria-label={`${name || 'Unknown sender'} avatar${
        contactPhotoUrl
          ? ', verified contact photo'
          : imageUrl
            ? ', company domain favicon'
            : ', initials'
      }`}
      title={
        contactPhotoUrl
          ? 'Verified contact photo'
          : imageUrl
            ? 'Company domain favicon'
            : undefined
      }
      data-testid="sender-avatar"
      className={`relative inline-flex ${size} shrink-0 items-center justify-center overflow-hidden rounded-full ${
        imageUrl && imageUrl !== failedUrl
          ? isDark ? 'bg-slate-800' : 'bg-slate-100'
          : `bg-gradient-to-br ${getSenderColor(sender)}`
      } ${className}`}
    >
      {imageUrl && imageUrl !== failedUrl ? (
        <img
          src={imageUrl}
          alt=""
          aria-hidden="true"
          loading="lazy"
          referrerPolicy="no-referrer"
          className="h-full w-full object-cover"
          onError={() => {
            if (!contactPhotoUrl) failedLogoUrls.add(imageUrl)
            setFailedUrl(imageUrl)
          }}
        />
      ) : (
        <span className="select-none text-sm font-bold tracking-wide text-white">
          {initials}
        </span>
      )}
    </span>
  )
}

export default SenderAvatar
