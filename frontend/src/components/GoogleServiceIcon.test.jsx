import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import GoogleServiceIcon from './GoogleServiceIcon.jsx'

describe('GoogleServiceIcon', () => {
  it('renders the original multicolour Calendar asset without a monochrome filter', () => {
    render(<GoogleServiceIcon service="calendar" className="h-10 w-10" />)

    const icon = screen.getByRole('img', { name: 'Google Calendar logo' })
    expect(icon).toHaveAttribute('src', '/icons/google/calendar.svg')
    expect(icon).toHaveClass('h-10', 'w-10')
    expect(icon.className).not.toContain('brightness-0')
    expect(icon.className).not.toContain('invert')
    expect(icon.className).not.toContain('grayscale')
  })

  it('leaves the Gmail logo untouched', () => {
    render(<GoogleServiceIcon service="gmail" className="h-11 w-11" />)

    const icon = screen.getByRole('img', { name: 'Gmail logo' })
    expect(icon).toHaveAttribute('src', '/icons/google/gmail.svg')
    expect(icon).toHaveClass('h-11', 'w-11')
    expect(icon.className).not.toContain('brightness-0')
    expect(icon.className).not.toContain('invert')
  })
})
