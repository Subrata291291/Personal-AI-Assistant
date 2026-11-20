import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import TransientStatus from './TransientStatus.jsx'

describe('TransientStatus', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
  })

  it('fades and automatically dismisses a success after about four seconds', () => {
    const onDismiss = vi.fn()
    const { rerender } = render(
      <TransientStatus message="Email sent successfully." onDismiss={onDismiss} />,
    )

    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('Email sent successfully.')
    expect(notice).toHaveClass('transient-status')
    expect(notice).not.toHaveClass('transient-status--error')

    act(() => vi.advanceTimersByTime(4000))
    expect(onDismiss).toHaveBeenCalledTimes(1)

    rerender(<TransientStatus message="" onDismiss={onDismiss} />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('replaces a stale notification and restarts its timer', () => {
    const onDismiss = vi.fn()
    const { rerender } = render(
      <TransientStatus message="First success." onDismiss={onDismiss} />,
    )

    act(() => vi.advanceTimersByTime(3500))
    rerender(
      <TransientStatus message="Second success." onDismiss={onDismiss} />,
    )

    expect(screen.queryByText('First success.')).not.toBeInTheDocument()
    expect(screen.getByText('Second success.')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveClass('transient-status')

    act(() => vi.advanceTimersByTime(3900))
    expect(onDismiss).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(100))
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('keeps an error visible far longer than a success', () => {
    const onDismiss = vi.fn()
    render(
      <TransientStatus
        message="Unable to send email."
        tone="error"
        onDismiss={onDismiss}
      />,
    )

    const alert = screen.getByRole('alert')
    expect(alert).toHaveClass('transient-status--error')

    act(() => vi.advanceTimersByTime(5000))
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(onDismiss).not.toHaveBeenCalled()

    act(() => vi.advanceTimersByTime(5000))
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('clears timers on unmount', () => {
    const onDismiss = vi.fn()
    const { unmount } = render(
      <TransientStatus message="Success." onDismiss={onDismiss} />,
    )

    unmount()
    act(() => vi.advanceTimersByTime(10000))
    expect(onDismiss).not.toHaveBeenCalled()
  })
})
