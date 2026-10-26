import { apiUrl } from '@/lib/api'

export type BetaEventContext = { runId?: string; chatId?: string }

let cachedSessionId = ''

export function clientSessionId() {
  if (cachedSessionId) return cachedSessionId
  try {
    cachedSessionId = sessionStorage.getItem('wireup.telemetry.session') || ''
    if (!cachedSessionId) {
      cachedSessionId = crypto.randomUUID()
      sessionStorage.setItem('wireup.telemetry.session', cachedSessionId)
    }
  } catch {
    cachedSessionId = crypto.randomUUID()
  }
  return cachedSessionId
}

export function newRunId() {
  return crypto.randomUUID()
}

export function recordBetaEvent(
  name: string,
  properties: Record<string, unknown> = {},
  projectId?: string,
  context: BetaEventContext = {},
) {
  void fetch(apiUrl('/api/beta/events'), {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name,
      project_id: projectId,
      run_id: context.runId,
      session_id: context.chatId,
      properties: { ...properties, client_session_id: clientSessionId() },
    }),
    keepalive: true,
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
