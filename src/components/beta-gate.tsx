import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { apiUrl } from '@/lib/api'

type BetaUser = { id: string; email: string; role: string; workspace_id: string }
type Session = { enabled: boolean; user: BetaUser | null }

async function betaRequest(path: string, init?: RequestInit) {
  const response = await fetch(apiUrl(path), { ...init, credentials: 'include', headers: { 'Content-Type': 'application/json', ...init?.headers } })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Beta request failed.')
  return body
}

export function BetaGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get('beta_token')
    const login = token
      ? betaRequest('/api/beta/verify', { method: 'POST', body: JSON.stringify({ token }) }).then(result => {
          window.history.replaceState({}, '', window.location.pathname)
          return { enabled: true, user: result.user } as Session
        })
      : betaRequest('/api/beta/session')
    void login.then(setSession).catch(error => setError(error.message))
  }, [])
  if (error) return <BetaLogin error={error} onLogin={user => setSession({ enabled: true, user })} />
  if (!session) return <div className="flex min-h-svh items-center justify-center bg-neutral-950 text-neutral-400">Checking beta access…</div>
  if (session.enabled && !session.user) return <BetaLogin onLogin={user => setSession({ enabled: true, user })} />
  return <>{children}</>
}

function BetaLogin({ error: initialError, onLogin }: { error?: string; onLogin: (user: BetaUser) => void }) {
  const [email, setEmail] = useState('')
  const [error, setError] = useState(initialError || '')
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const access = await betaRequest('/api/beta/request-access', { method: 'POST', body: JSON.stringify({ email }) })
      if (access.sent) {
        setError('Check your email for the secure beta sign-in link.')
      } else if (access.message) {
        const result = await betaRequest('/api/beta/dev-login', { method: 'POST', body: JSON.stringify({ email }) })
        onLogin(result.user)
      }
    } catch (value) { setError(value instanceof Error ? value.message : 'Could not sign in.') }
    finally { setBusy(false) }
  }
  return <main className="flex min-h-svh items-center justify-center bg-neutral-950 px-5 text-neutral-100">
    <form onSubmit={submit} className="w-full max-w-md rounded-2xl border border-neutral-800 bg-neutral-900 p-7 shadow-2xl">
      <p className="text-xs uppercase tracking-[0.2em] text-neutral-500">WireUp beta</p>
      <h1 className="mt-3 text-2xl font-semibold">Sign in to your workspace</h1>
      <p className="mt-3 text-sm leading-6 text-neutral-400">Use the email address selected for testing. We’ll send a secure sign-in link if email delivery is configured.</p>
      <label className="mt-6 block text-sm text-neutral-300">Email<input required type="email" value={email} onChange={event => setEmail(event.target.value)} className="mt-2 w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 outline-none focus:border-neutral-400" /></label>
      {error && <p role="alert" className="mt-4 rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-300">{error}</p>}
      <button disabled={busy} className="mt-6 w-full rounded-lg bg-white px-4 py-2.5 text-sm font-medium text-black disabled:opacity-50">{busy ? 'Signing in…' : 'Continue'}</button>
    </form>
  </main>
}
