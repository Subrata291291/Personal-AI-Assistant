export function getOAuthRedirectTo(origin = window.location.origin) {
  return new URL('/dashboard', origin).toString()
}
