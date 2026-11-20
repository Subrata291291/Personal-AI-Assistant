import { useEffect, useRef } from 'react'

import { SEMANTIC_STATUS_STYLES } from '../lib/semanticStatusStyles.js'

const SUCCESS_DURATION_MS = 4000
const ERROR_DURATION_MS = 10000

/**
 * Temporary action confirmation. Successes fade through CSS and disappear
 * automatically; errors stay long enough to read. Timers are cleared on
 * change/unmount so stale notices never reappear.
 */
export default function TransientStatus({
  message,
  tone = 'success',
  onDismiss,
  className = '',
}) {
  const onDismissRef = useRef(onDismiss)

  useEffect(() => {
    onDismissRef.current = onDismiss
  }, [onDismiss])

  useEffect(() => {
    if (!message) return undefined

    const duration = tone === 'error' ? ERROR_DURATION_MS : SUCCESS_DURATION_MS
    const removeTimer = window.setTimeout(
      () => onDismissRef.current?.(),
      duration,
    )

    return () => window.clearTimeout(removeTimer)
  }, [message, tone])

  if (!message) return null

  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={`transient-status ${
        tone === 'error' ? 'transient-status--error' : ''
      } ${
        tone === 'error'
          ? SEMANTIC_STATUS_STYLES.error.badge
          : SEMANTIC_STATUS_STYLES.positive.badge
      } ${className}`}
    >
      {message}
    </div>
  )
}
