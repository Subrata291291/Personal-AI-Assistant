function parseGmailTimestamp(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? null : date
  }

  if (typeof value !== 'string' || !value.trim()) return null

  const normalized = value.trim()
  const milliseconds = /^\d+$/.test(normalized)
    ? Number(normalized)
    : Date.parse(normalized)
  if (!Number.isFinite(milliseconds)) return null

  const date = new Date(milliseconds)
  return Number.isNaN(date.getTime()) ? null : date
}

function sameLocalDate(left, right) {
  return left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
}

export function formatGmailTimestamp(value, now = new Date()) {
  const date = parseGmailTimestamp(value)
  if (!date) return { label: '', title: '' }

  const yesterday = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() - 1,
  )

  let label
  if (sameLocalDate(date, now)) {
    label = date.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
    })
  } else if (sameLocalDate(date, yesterday)) {
    label = 'Yesterday'
  } else {
    label = date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      ...(date.getFullYear() === now.getFullYear()
        ? {}
        : { year: 'numeric' }),
    })
  }

  return {
    label,
    title: date.toLocaleString(undefined, {
      month: 'long',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    }),
  }
}

export function sortGmailMessagesByReceivedTime(messages) {
  return messages
    .map((message, index) => ({
      message,
      index,
      timestamp: parseGmailTimestamp(message.internal_date)?.getTime() ?? null,
    }))
    .sort((left, right) => {
      if (left.timestamp === null) return right.timestamp === null
        ? left.index - right.index
        : 1
      if (right.timestamp === null) return -1
      return right.timestamp - left.timestamp || left.index - right.index
    })
    .map(({ message }) => message)
}
