import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import SenderAvatar from './SenderAvatar.jsx'
import {
  getCompanyLogoUrl,
  getSenderInitials,
  parseSenderIdentity,
} from '../utils/senderAvatar.js'

describe('SenderAvatar', () => {
  it('uses a verified contact photo before a company domain logo', () => {
    const { container } = render(
      <SenderAvatar
        sender={'Ada Lovelace <ada@analytical.example.com>'}
        contactPhoto={{
          url: 'https://lh3.googleusercontent.com/photo-id',
          verified: true,
        }}
      />,
    )

    expect(screen.getByRole('img', { name: /^Ada Lovelace avatar/ }))
      .toBeInTheDocument()
    expect(container.querySelector('img'))
      .toHaveAttribute('src', 'https://lh3.googleusercontent.com/photo-id')
    expect(screen.getByTestId('sender-avatar'))
      .toHaveAttribute('title', 'Verified contact photo')
  })

  it('uses a fixed favicon endpoint for validated company domains', () => {
    const { container } = render(
      <SenderAvatar sender={'Ada Lovelace <ada@analytical.example.com>'} />,
    )

    const image = container.querySelector('img')
    expect(image).toHaveAttribute(
      'src',
      'https://www.google.com/s2/favicons?domain=analytical.example.com&sz=64',
    )
    expect(image).toHaveAttribute('loading', 'lazy')
    expect(image).toHaveAttribute('referrerpolicy', 'no-referrer')
    expect(screen.getByTestId('sender-avatar'))
      .toHaveAttribute('title', 'Company domain favicon')
    expect(getCompanyLogoUrl('Ada <ada@evil.com/path>')).toBe('')
  })

  it('uses deterministic initials instead of a company logo for Gmail senders', () => {
    const { container, rerender } = render(
      <SenderAvatar sender={'Ada Lovelace <ada@gmail.com>'} />,
    )
    const first = screen.getByRole('img', { name: /^Ada Lovelace avatar/ })
    expect(first.querySelector('img')).toBeNull()
    expect(first).toHaveTextContent('AL')
    const firstColor = first.className

    rerender(<SenderAvatar sender={'Ada Lovelace <ada@gmail.com>'} />)
    expect(screen.getByRole('img', { name: /^Ada Lovelace avatar/ }).className)
      .toBe(firstColor)
    expect(container.firstChild).toHaveClass('rounded-full')
  })

  it('uses the email when the display name is missing', () => {
    render(<SenderAvatar sender="mira@gmail.com" />)

    expect(screen.getByRole('img', { name: /^mira@gmail.com avatar/ }))
      .toHaveTextContent('MI')
    expect(getSenderInitials('mira@gmail.com')).toBe('MI')
  })

  it('handles missing and malformed senders without unsafe logo URLs', () => {
    const { rerender } = render(<SenderAvatar />)
    expect(screen.getByRole('img', { name: /^Unknown sender avatar/ }))
      .toHaveTextContent('?')

    rerender(<SenderAvatar sender="Brand <bad@@example.com>" />)
    expect(screen.getByRole('img', { name: /^Brand avatar/ }))
      .toHaveTextContent('BR')
    expect(screen.getByTestId('sender-avatar').querySelector('img'))
      .not.toBeInTheDocument()
    expect(parseSenderIdentity('Brand <bad@@example.com>').domain).toBe('')
  })

  it('falls back to initials when the domain favicon fails to load', () => {
    const { container } = render(
      <SenderAvatar sender={'Acme <updates@acme-corp.test.com>'} />,
    )

    const image = container.querySelector('img')
    fireEvent.error(image)

    expect(screen.getByRole('img', { name: /^Acme avatar/ }))
      .toHaveTextContent('A')
    expect(screen.getByTestId('sender-avatar').querySelector('img'))
      .not.toBeInTheDocument()
  })

  it('ignores unverified or non-Google contact photo URLs', () => {
    render(
      <SenderAvatar
        sender="reader@gmail.com"
        contactPhoto={{
          url: 'https://attacker.example/avatar.png',
          verified: true,
        }}
      />,
    )

    expect(screen.getByRole('img', { name: /^reader@gmail.com avatar/ }))
      .toHaveTextContent('RE')
    expect(screen.getByTestId('sender-avatar').querySelector('img'))
      .not.toBeInTheDocument()
  })

  it('uses the same initials fallback styling in dark and light themes', () => {
    const { rerender } = render(
      <SenderAvatar sender="reader@gmail.com" isDark={false} />,
    )
    const lightAvatar = screen.getByTestId('sender-avatar')
    const colorClass = lightAvatar.className.match(/from-[^\s]+ to-[^\s]+/)?.[0]

    rerender(<SenderAvatar sender="reader@gmail.com" isDark />)
    const darkAvatar = screen.getByTestId('sender-avatar')

    expect(colorClass).toBeTruthy()
    expect(darkAvatar.className).toContain(colorClass)
    expect(darkAvatar).toHaveTextContent('RE')
  })
})
