const BLOCKED_ELEMENTS = [
  'audio',
  'base',
  'button',
  'embed',
  'form',
  'frame',
  'frameset',
  'iframe',
  'input',
  'link',
  'math',
  'meta',
  'noscript',
  'object',
  'option',
  'script',
  'select',
  'source',
  'svg',
  'textarea',
  'track',
  'video',
]

const ENCODED_CONTROL_CHARACTERS = /%(?:0[0-9a-f]|1[0-9a-f]|7f)/i
const SAFE_DATA_IMAGE =
  /^data:image\/(?:bmp|gif|jpe?g|png|webp|avif);base64,[a-z0-9+/=_-]*$/i

function containsControlCharacters(value) {
  return [...value].some((character) => {
    const code = character.charCodeAt(0)
    return code < 32 || code === 127
  })
}

function getSafeLink(value) {
  const href = value.trim()
  if (
    !href ||
    containsControlCharacters(href) ||
    ENCODED_CONTROL_CHARACTERS.test(href)
  ) {
    return null
  }

  let url
  try {
    url = new URL(href)
  } catch {
    return null
  }

  if (
    (url.protocol === 'http:' || url.protocol === 'https:') &&
    url.hostname &&
    !url.username &&
    !url.password
  ) {
    return url.href
  }

  if (url.protocol === 'mailto:') {
    let recipient
    try {
      recipient = decodeURIComponent(url.pathname)
    } catch {
      return null
    }

    if (recipient && !containsControlCharacters(recipient)) {
      return url.href
    }
  }

  return null
}

function getSafeImageSource(value) {
  const source = value.trim()
  if (containsControlCharacters(source)) return null
  if (SAFE_DATA_IMAGE.test(source)) return source
  if (/^cid:[a-z0-9._@%+-]+$/i.test(source)) return source

  try {
    const url = new URL(source)
    if (
      url.protocol === 'https:' &&
      url.hostname &&
      !url.username &&
      !url.password
    ) {
      return url.href
    }
  } catch {
    return null
  }

  return null
}

export function sanitizeEmailHtml(html) {
  if (typeof html !== 'string' || !html) return ''

  const parsed = new DOMParser().parseFromString(html, 'text/html')
  parsed.querySelectorAll(BLOCKED_ELEMENTS.join(',')).forEach((element) => {
    element.remove()
  })

  parsed.querySelectorAll('*').forEach((element) => {
    const isAnchor = element.tagName === 'A'
    const isImage = element.tagName === 'IMG'

    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase()

      if (
        name.startsWith('on') ||
        name === 'srcdoc' ||
        name === 'action' ||
        name === 'formaction' ||
        name === 'ping' ||
        name === 'srcset' ||
        name === 'xlink:href'
      ) {
        element.removeAttribute(attribute.name)
      } else if (name === 'href') {
        if (!isAnchor) {
          element.removeAttribute(attribute.name)
          continue
        }

        const safeLink = getSafeLink(attribute.value)
        if (safeLink) {
          element.setAttribute('href', safeLink)
        } else {
          element.removeAttribute(attribute.name)
        }
      } else if (name === 'src') {
        const safeSource = isImage
          ? getSafeImageSource(attribute.value)
          : null
        if (safeSource) {
          element.setAttribute('src', safeSource)
        } else {
          element.removeAttribute(attribute.name)
        }
      } else if (name === 'background') {
        const safeSource = getSafeImageSource(attribute.value)
        if (safeSource) {
          element.setAttribute('background', safeSource)
        } else {
          element.removeAttribute(attribute.name)
        }
      }
    }

    if (isAnchor && element.hasAttribute('href')) {
      element.setAttribute('target', '_blank')
      element.setAttribute('rel', 'noopener noreferrer')
    } else {
      element.removeAttribute('target')
      element.removeAttribute('rel')
    }
  })

  const styles = [...parsed.head.querySelectorAll('style')]
    .map((style) => style.outerHTML)
    .join('')

  return `${styles}${parsed.body.innerHTML}`
}
