function formatDetail(detail) {
  if (typeof detail === 'string') return detail.trim()

  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === 'string') return item.trim()
        if (!item || typeof item !== 'object') return ''

        const message = typeof item.msg === 'string' ? item.msg.trim() : ''
        const location = Array.isArray(item.loc)
          ? item.loc.filter((part) => typeof part === 'string').join('.')
          : ''
        return location && message
          ? `${location}: ${message}`
          : message || location
      })
      .filter(Boolean)
      .join('\n')
  }

  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string') return detail.message.trim()
    if (typeof detail.detail === 'string' || Array.isArray(detail.detail)) {
      return formatDetail(detail.detail)
    }
    if (typeof detail.msg === 'string') return detail.msg.trim()
  }

  return ''
}

export function formatErrorMessage(error, fallback = 'The request could not be completed.') {
  for (const candidate of [error?.message, error?.detail, error]) {
    const message = formatDetail(candidate)
    if (message) return message
  }
  return fallback
}

export function formatRequestError(
  error,
  fallback = 'The request could not be completed.',
) {
  const message = error?.networkFailure
    ? 'Could not reach the API. Check your connection and approval status before retrying.'
    : formatErrorMessage(error, fallback)
  return error?.requestId
    ? `${message} (Request ID: ${error.requestId})`
    : message
}
