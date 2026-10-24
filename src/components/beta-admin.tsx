import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Activity, ArrowUpRight, MailPlus, Users, Zap } from 'lucide-react'
import { apiUrl } from '@/lib/api'

type Summary = { users: { email: string; role: string; last_seen_at: number | null }[]; events: { name: string; count: number }[] }
type MonitorStatus = { posthog: boolean; langfuse: boolean; sentry: boolean }

async function request(path: string, init?: RequestInit) {
  const response = await fetch(apiUrl(path), { ...init, credentials: 'include', headers: { 'Content-Type': 'application/json', ...init?.headers } })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || 'Request failed.')
  return body
}

export function BetaAdmin() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [monitoring, setMonitoring] = useState<MonitorStatus | null>(null)
  const [email, setEmail] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const load = () => {
    void Promise.all([request('/api/beta/admin/summary'), request('/api/monitoring/status')])
      .then(([nextSummary, nextMonitoring]) => { setSummary(nextSummary); setMonitoring(nextMonitoring) })
      .catch(value => setError(value instanceof Error ? value.message : 'Could not load beta summary.'))
  }
  useEffect(load, [])
  const invite = async (event: FormEvent) => {
    event.preventDefault(); setError(''); setMessage('')
    try { await request('/api/beta/invite', { method: 'POST', body: JSON.stringify({ email }) }); setMessage(`${email} is now invited.`); setEmail(''); load() }
    catch (value) { setError(value instanceof Error ? value.message : 'Could not invite tester.') }
  }
  return <main className="nova-admin min-h-svh px-5 py-8 text-[#12110f] sm:px-10 sm:py-12">
    <div className="mx-auto max-w-6xl">
      <header className="flex flex-wrap items-end justify-between gap-5 border-b-2 border-[#12110f] pb-6">
        <div><p className="font-mono text-xs font-bold uppercase tracking-[0.22em]">NovaBoard / WireUp</p><h1 className="mt-2">BETA CONTROL ROOM</h1><p className="mt-3 max-w-xl text-sm">See who is testing, what they use, and whether the product gets them to a working prototype.</p></div>
        <a href="/" className="nova-admin-button">Open product <ArrowUpRight size={16}/></a>
      </header>
      {error && <p role="alert" className="nova-admin-alert">{error}</p>}
      {message && <p className="nova-admin-success">{message}</p>}
      <section className="mt-8 grid gap-4 md:grid-cols-3">
        <Metric icon={<Users/>} label="Testers" value={summary?.users.length ?? '—'} />
        <Metric icon={<Activity/>} label="Events captured" value={summary ? summary.events.reduce((total, item) => total + item.count, 0) : '—'} />
        <Metric icon={<Zap/>} label="Monitoring" value={monitoring ? Object.values(monitoring).filter(Boolean).length + '/3' : '—'} />
      </section>
      <section className="mt-8 grid gap-6 lg:grid-cols-[1.1fr_.9fr]">
        <div className="nova-admin-card"><div className="flex items-center justify-between"><h2>TESTERS</h2><span className="nova-admin-tag">{summary?.users.length ?? 0} PEOPLE</span></div><div className="mt-5 space-y-3">{summary?.users.map(user => <div key={user.email} className="nova-admin-row"><div><strong>{user.email}</strong><small>{user.last_seen_at ? `Last seen ${new Date(user.last_seen_at * 1000).toLocaleString()}` : 'Not active yet'}</small></div><span>{user.role}</span></div>)}</div><form onSubmit={invite} className="mt-6 flex gap-2 border-t-2 border-[#12110f] pt-5"><input required type="email" value={email} onChange={event => setEmail(event.target.value)} placeholder="tester@email.com" aria-label="Tester email" /><button className="nova-admin-button" type="submit"><MailPlus size={16}/>Invite</button></form></div>
        <div className="space-y-6"><div className="nova-admin-card"><h2>EVENT SIGNALS</h2><div className="mt-5 space-y-3">{summary?.events.map(event => <div key={event.name} className="nova-admin-row"><strong>{event.name}</strong><span className="nova-admin-count">{event.count}</span></div>)}</div></div><div className="nova-admin-card"><h2>OBSERVABILITY</h2><div className="mt-5 space-y-3">{monitoring && Object.entries(monitoring).map(([name, active]) => <div key={name} className="nova-admin-row"><strong>{name}</strong><span className={active ? 'nova-admin-live' : 'nova-admin-off'}>{active ? 'CONNECTED' : 'NOT SET'}</span></div>)}</div><p className="mt-4 text-xs">Sentry catches failures. PostHog shows beta behavior. Langfuse traces model calls. Configure them in the backend environment.</p></div></div>
      </section>
    </div>
  </main>
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value: string | number }) {
  return <div className="nova-admin-card flex items-center gap-4"><div className="nova-admin-icon">{icon}</div><div><small>{label}</small><strong className="block text-3xl">{value}</strong></div></div>
}
