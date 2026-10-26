const configuredBase = (import.meta.env.VITE_API_BASE_URL || '').trim().replace(/\/+$/, '')

export function apiUrl(path: string): string {
  return `${configuredBase}${path}`
}

export function apiWebSocketUrl(path: string): string {
  const url = new URL(apiUrl(path), window.location.href)
  if (url.protocol === 'http:') url.protocol = 'ws:'
  if (url.protocol === 'https:') url.protocol = 'wss:'
  return url.toString()
}
