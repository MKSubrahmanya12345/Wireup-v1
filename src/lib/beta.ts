import { apiUrl } from '@/lib/api'

export function recordBetaEvent(name: string, properties: Record<string, unknown> = {}, projectId?: string) {
  void fetch(apiUrl('/api/beta/events'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, project_id: projectId, properties }),
  }).catch(() => {})
}

export function installBrowserMonitoring() {
  const report = (name: string, properties: Record<string, unknown>) => {
    void fetch(apiUrl('/api/monitoring/event'), {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, properties }),
      keepalive: true,
    }).catch(() => {})
  }
  const onError = (event: ErrorEvent) => report('browser.error', { message: event.message, source: event.filename })
  const onRejection = (event: PromiseRejectionEvent) => report('browser.unhandled_rejection', { reason: String(event.reason).slice(0, 500) })
  window.addEventListener('error', onError)
  window.addEventListener('unhandledrejection', onRejection)
  return () => { window.removeEventListener('error', onError); window.removeEventListener('unhandledrejection', onRejection) }
}
