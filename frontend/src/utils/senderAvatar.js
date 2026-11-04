const GENERIC_EMAIL_DOMAINS = new Set([
  'gmail.com',
  'googlemail.com',
  'googlemail.de',
  'outlook.com',
  'hotmail.com',
  'live.com',
  'msn.com',
  'yahoo.com',
  'yahoo.co.uk',
  'yahoo.co.in',
  'yahoo.co.jp',
  'ymail.com',
  'icloud.com',
  'me.com',
  'mac.com',
  'aol.com',
  'proton.me',
  'protonmail.com',
  'pm.me',
  'gmx.com',
  'mail.com',
  'fastmail.com',
  'hey.com',
  'zoho.com',
  'yandex.com',
  'yandex.ru',
  'qq.com',
  '163.com',
  '126.com',
  'rediffmail.com',
  'tuta.com',
  'tutanota.com',
  'hushmail.com',
])

const AVATAR_COLORS = [
  'from-violet-500 to-indigo-600',
  'from-sky-500 to-blue-600',
  'from-emerald-500 to-teal-600',
  'from-rose-500 to-pink-600',
  'from-amber-500 to-orange-600',
  'from-cyan-500 to-teal-600',
  'from-fuchsia-500 to-purple-600',
  'from-indigo-500 to-blue-700',
]

export function parseSenderIdentity(sender) {
  const rawSender = typeof sender === 'string' ? sender.trim() : ''
  const match = rawSender.match(/^\s*(.*?)\s*<\s*([^<>]+)\s*>\s*$/)
  let name = match?.[1]?.replace(/^["']|["']$/g, '').trim() || ''
  let email = match?.[2]?.trim() || ''

  if (!match && rawSender.includes('@')) email = rawSender
  if (!name && email) name = email
  if (!name && rawSender && !rawSender.includes('@')) name = rawSender

  const normalizedEmail = email.toLowerCase()
  const domain = normalizedEmail.split('@').length === 2
    ? normalizedEmail.split('@')[1]
    : ''
  const validDomain = domain.length <= 253 &&
    /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i
      .test(domain)

  return {
    name,
    email: normalizedEmail,
    domain: validDomain ? domain : '',
  }
}

export function getSenderInitials(sender) {
  const { name, email } = parseSenderIdentity(sender)
  const identity = name || email
  const words = identity
    .replace(/[<>"']/g, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean)

  if (!words.length) return '?'
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase()
  return `${words[0][0]}${words[words.length - 1][0]}`.toUpperCase()
}

export function getSenderColor(sender) {
  const { email, name } = parseSenderIdentity(sender)
  const identity = (email || name || '?').toLowerCase()
  let hash = 0
  for (let index = 0; index < identity.length; index += 1) {
    hash = (hash * 31 + identity.charCodeAt(index)) | 0
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length]
}

export function getVerifiedContactPhotoUrl(contactPhoto) {
  if (
    !contactPhoto ||
    contactPhoto.verified !== true ||
    typeof contactPhoto.url !== 'string'
  ) {
    return ''
  }

  try {
    const url = new URL(contactPhoto.url)
    const host = url.hostname.toLowerCase()
    if (
      url.protocol === 'https:' &&
      (host === 'googleusercontent.com' ||
        host.endsWith('.googleusercontent.com'))
    ) {
      return url.href
    }
  } catch {
    return ''
  }
  return ''
}

export function getCompanyLogoUrl(sender) {
  const { domain } = parseSenderIdentity(sender)
  if (!domain || GENERIC_EMAIL_DOMAINS.has(domain)) return ''
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64`
}
