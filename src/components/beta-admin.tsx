import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  Activity, ArrowDownRight, ArrowUpRight, Bot, Boxes, CheckCircle2, ChevronRight,
  CircuitBoard, Clock, Copy, Cpu, Database, Gauge, Inbox, Layers, LoaderCircle, MailPlus,
  RefreshCw, Search, Send, Server, Sparkles, TriangleAlert, Users,
  Wrench, X, XCircle, Zap,
} from 'lucide-react'
import { apiUrl } from '@/lib/api'
import './beta-admin.css'

// ---------------------------------------------------------------------------
// api types

type AdminEvent = {
  id: string; name: string; label: string
  user_id: string | null; workspace_id: string | null; project_id: string | null
  session_id: string | null; run_id: string | null; source: string | null
  occurred_at: number; occurred_at_iso: string | null
  properties: Record<string, unknown>
  email?: string | null; project_name?: string | null
}
type EventName = { name: string; label: string; count: number }
type SeriesPoint = {
  date: string; events: number; active_users: number; new_users: number; runs: number
  tool_calls: number; searches: number; compiles: number; simulations: number
  chats: number; projects: number
}
type Totals = {
  users: number; admins: number; invites: number; accepted_invites: number
  access_requests: number; active_24h: number; active_7d: number; new_users_7d: number
  projects: number; active_projects_7d: number; events: number; events_total: number
  runs: number; run_errors: number; runs_total: number; tool_calls: number
  tool_errors: number; searches: number; unique_searches: number; compiles: number
  compile_failures: number; simulations: number; chats: number; avg_run_ms: number
  avg_tool_ms: number
}
type Overview = {
  generated_at: string | null; range_days: number
  totals: Totals
  previous: { events: number; runs: number; tool_calls: number; searches: number; chats: number; compiles: number }
  series: SeriesPoint[]
  funnel: { stage: string; count: number; hint: string }[]
  top_tools: { name: string; calls: number; errors: number; avg_ms: number; users: number; last_at: string | null }[]
  top_searches: { query: string; scope: string; count: number; users: number; avg_results: number; zero_results: number; last_at: string | null }[]
  providers: { provider: string; model: string; runs: number; errors: number; avg_ms: number; tokens: number; tool_calls: number; success_rate: number }[]
  boards: { board: string; projects: number }[]
  recent_events: AdminEvent[]
  event_names: EventName[]
}
type MonitorStatus = { enabled?: boolean; posthog?: boolean; langfuse?: boolean; sentry?: boolean }

type Tester = {
  id: string; email: string; role: string; workspace_id: string
  created_at: string | null; created_ts: number; last_seen_at: string | null; last_seen_ts: number
  last_event_at: string | null; first_event_at: string | null; invited_at: string | null
  accepted_at: string | null; projects: number; boards: string[]; events: number; runs: number
  tool_calls: number; searches: number; compiles: number; simulations: number; messages: number
  tool_errors: number; run_errors: number
}
type UsersResponse = { users: Tester[]; total: number }
type ProjectShort = {
  id: string; name: string; board: string; revision: number | null; created_at: string | null
  updated_at: string | null; components: number; wires: number; firmware_lines: number
  requirements_status: string; compiler_status: string; component_types: string[]
}
type UserDetail = {
  user: { id: string; email: string; role: string; workspace_id: string; created_at: string | null; last_seen_at: string | null;
    invite: { email: string; created_at: string | null; accepted_at: string | null; invited_by?: string | null } | null }
  projects: ProjectShort[]
  events: AdminEvent[]
  tools: { name: string; calls: number; errors: number; avg_ms: number }[]
  searches: { query: string; count: number }[]
  activity: { date: string; events: number; runs: number; tool_calls: number; searches: number }[]
  auth_sessions: { created_at: string | null; expires_at: string | null; active: boolean }[]
  range_days: number; since: string | null
}
type ProjectRow = ProjectShort & {
  workspace_id: string; firmware_filename: string | null; owner_email: string | null; owner_id: string | null
  events: number; runs: number; tool_calls: number; searches: number; compiles: number
  simulations: number; last_activity_at: string | null; last_activity_ts: number
}
type ProjectsResponse = { projects: ProjectRow[]; total: number; boards: { board: string; projects: number }[] }
type ProjectDetail = ProjectShort & {
  workspace_id: string; firmware_filename: string | null; firmware_source: string; owner_email: string | null
  owner_id: string | null; requirements: Record<string, unknown>; compiler_status: string
  component_types: string[]; events: AdminEvent[]
}
type EventsResponse = { events: AdminEvent[]; total: number; offset: number; limit: number; names: EventName[] }
type RunsResponse = {
  runs: { run_id: string; user_id: string | null; email: string | null; project_id: string | null; project_name: string | null;
    chat_id: string | null; started_at: string | null; completed_at: string | null; status: string; reason: string | null;
    provider: string | null; model: string | null; duration_ms: number; tool_calls: number; model_calls: number;
    tokens: number; prompt_preview: string; prompt_length: number }[]
  total: number
  summary: { total: number; success: number; errors: number; running: number; avg_ms: number; tokens: number }
}
type ToolsResponse = {
  tools: { name: string; calls: number; errors: number; avg_ms: number; users: number; last_at: string | null; sources: Record<string, number>; success_rate: number }[]
  recent: AdminEvent[]; recent_total: number
  manual_commands: { command: string; calls: number; errors: number; avg_ms: number; users: number; last_at: string | null; success_rate: number }[]
  manual_recent: AdminEvent[]; manual_total: number
  sources: { source: string; calls: number }[]
}
type SearchesResponse = {
  queries: { query: string; scope: string; count: number; users: number; avg_results: number; zero_results: number; last_at: string | null }[]
  total: number; recent: AdminEvent[]; scopes: { scope: string; count: number }[]
}
type WaitlistResponse = {
  waitlist: { email: string; requests: number; first_requested_at: string | null; last_requested_at: string | null; delivered: boolean; user_agent: string }[]
  total: number
  invites: { email: string; created_at: string | null; accepted_at: string | null; invited_by?: string | null }[]
  invited_total: number
}

// ---------------------------------------------------------------------------
// helpers

const numberFormat = new Intl.NumberFormat('en-US')
const TABS = [
  { id: 'overview', label: 'Overview', icon: Gauge },
  { id: 'testers', label: 'Testers', icon: Users },
  { id: 'projects', label: 'Projects', icon: Boxes },
  { id: 'activity', label: 'Activity', icon: Activity },
  { id: 'runs', label: 'AI runs', icon: Bot },
  { id: 'tools', label: 'Tools', icon: Wrench },
  { id: 'searches', label: 'Searches', icon: Search },
  { id: 'waitlist', label: 'Waitlist', icon: Inbox },
] as const
type Tab = typeof TABS[number]['id']
type TabIcon = typeof Activity
const METRICS = [
  { key: 'events', label: 'All activity' },
  { key: 'runs', label: 'AI runs' },
  { key: 'tool_calls', label: 'Tool calls' },
  { key: 'searches', label: 'Searches' },
  { key: 'compiles', label: 'Compiles' },
  { key: 'simulations', label: 'Simulations' },
  { key: 'chats', label: 'Messages' },
] as const
type MetricKey = typeof METRICS[number]['key']

async function adminRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  const body: unknown = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body ? (body as { detail?: unknown }).detail : null
    throw new Error(typeof detail === 'string' ? detail : 'The admin API rejected this request.')
  }
  return body as T
}

function useAdminQuery<T>(path: string, refresh: number) {
  const [state, setState] = useState<{ key: string; data: T | null; loading: boolean; error: string }>({ key: `${path}:${refresh}`, data: null, loading: true, error: '' })
  const current = state.key === `${path}:${refresh}` ? state : { key: `${path}:${refresh}`, data: null, loading: true, error: '' }
  useEffect(() => {
    let active = true
    void adminRequest<T>(path)
      .then(result => { if (active) setState(current => current.key === `${path}:${refresh}` ? { key: `${path}:${refresh}`, data: result, loading: false, error: '' } : current) })
      .catch((value: unknown) => { if (active) setState(current => current.key === `${path}:${refresh}` ? { key: `${path}:${refresh}`, data: current.data, loading: false, error: value instanceof Error ? value.message : 'Could not load this view.' } : current) })
      .finally(() => { if (active) setState(current => current.key === `${path}:${refresh}` ? { ...current, loading: false } : current) })
    return () => { active = false }
  }, [path, refresh])
  return { data: current.data, loading: current.loading, error: current.error }
}

const formatNumber = (value: number | null | undefined) => numberFormat.format(Math.round(Number(value) || 0))
const formatPct = (value: number | null | undefined) => `${Math.round(Number(value) || 0)}%`

function formatDuration(value: number | null | undefined) {
  const ms = Number(value) || 0
  if (!ms) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function relative(value: string | null | undefined) {
  if (!value) return '—'
  const stamp = new Date(value).getTime()
  if (Number.isNaN(stamp)) return value
  const diff = Date.now() - stamp
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`
  if (diff < 7 * 86_400_000) return `${Math.round(diff / 86_400_000)}d ago`
  return formatDate(value)
}

function percentChange(current: number, previous: number) {
  return previous ? Math.round(((current - previous) / previous) * 100) : null
}

function eventTone(name: string) {
  if (name.includes('failed') || name.includes('error')) return 'danger'
  if (name.startsWith('compile') || name.startsWith('firmware')) return 'warning'
  if (name.startsWith('simulation')) return 'blue'
  if (name.startsWith('search') || name.startsWith('sample')) return 'violet'
  if (name.startsWith('tool') || name.startsWith('command')) return 'teal'
  if (name.startsWith('project')) return 'green'
  if (name.startsWith('auth')) return 'pink'
  return 'muted'
}

function eventDetail(event: AdminEvent) {
  const properties = event.properties
  const bits: string[] = []
  if (typeof properties.query === 'string' && properties.query) bits.push(`“${properties.query.slice(0, 90)}”`)
  if (typeof properties.tool === 'string' && properties.tool) bits.push(properties.tool)
  if (typeof properties.command === 'string' && properties.command) bits.push(properties.command)
  if (typeof properties.status === 'string' && properties.status) bits.push(properties.status)
  if (typeof properties.result_count === 'number') bits.push(`${properties.result_count} results`)
  if (typeof properties.duration_ms === 'number') bits.push(formatDuration(properties.duration_ms))
  if (typeof properties.error === 'string' && properties.error) bits.push(properties.error.slice(0, 80))
  return bits.join(' · ')
}

function runTone(status: string) {
  if (status === 'success') return 'good'
  if (status === 'running') return 'live'
  if (status === 'cancelled') return 'muted'
  if (status === 'incomplete') return 'warn'
  return 'danger'
}

function initials(email: string) {
  return email.slice(0, 2).toUpperCase()
}

function activityTone(timestamp: string | null) {
  if (!timestamp) return 'idle'
  const age = Date.now() - new Date(timestamp).getTime()
  return age < 86_400_000 ? 'live' : age < 7 * 86_400_000 ? 'warm' : 'idle'
}

// ---------------------------------------------------------------------------
// page

export function BetaAdmin() {
  const [tab, setTab] = useState<Tab>('overview')
  const [days, setDays] = useState(14)
  const [refreshKey, setRefreshKey] = useState(0)
  const overviewKey = `overview:${days}:${refreshKey}`
  const [overviewState, setOverviewState] = useState<{ key: string; data: Overview | null; loading: boolean; error: string }>({ key: overviewKey, data: null, loading: true, error: '' })
  const overview = overviewState.key === overviewKey ? overviewState : { key: overviewKey, data: null, loading: true, error: '' }
  const [monitoring, setMonitoring] = useState<MonitorStatus | null>(null)

  useEffect(() => {
    let active = true
    const key = `overview:${days}:${refreshKey}`
    void adminRequest<Overview>(`/api/beta/admin/overview?days=${days}`)
      .then(result => { if (active) setOverviewState(current => current.key === key ? { key, data: result, loading: false, error: '' } : current) })
      .catch((value: unknown) => { if (active) setOverviewState(current => current.key === key ? { key, data: current.data, loading: false, error: value instanceof Error ? value.message : 'Could not load the admin overview.' } : current) })
      .finally(() => { if (active) setOverviewState(current => current.key === key ? { ...current, loading: false } : current) })
    void adminRequest<MonitorStatus>('/api/monitoring/status')
      .then(result => { if (active) setMonitoring(result) })
      .catch(() => { if (active) setMonitoring(null) })
    return () => { active = false }
  }, [days, refreshKey])

  const loading = overview.loading
  const error = overview.error
  const badges: Partial<Record<Tab, number>> = overview.data ? {
    testers: overview.data.totals.users,
    projects: overview.data.totals.projects,
    activity: overview.data.totals.events,
    runs: overview.data.totals.runs,
    tools: overview.data.totals.tool_calls,
    searches: overview.data.totals.searches,
    waitlist: overview.data.totals.access_requests,
  } : {}

  return <main className="nova-admin adm">
    <div className="adm-shell">
      <header className="adm-masthead">
        <div>
          <p className="adm-eyebrow"><span className="adm-pulse" />NovaBoard / WireUp beta</p>
          <h1>Control room</h1>
          <p className="adm-lede">What testers build, what the agent does, and where the product gets stuck. Every number below comes from the live beta event log joined to canonical project state.</p>
        </div>
        <div className="adm-mast-actions">
          <a className="adm-btn" href="/">Open product <ArrowUpRight size={15} /></a>
          <button className="adm-btn ghost" type="button" onClick={() => setRefreshKey(value => value + 1)} disabled={loading}>
            {loading ? <LoaderCircle className="adm-spin" size={15} /> : <RefreshCw size={15} />} Refresh
          </button>
        </div>
      </header>

      <div className="adm-toolbar">
        <div className="adm-range" role="group" aria-label="Time range">
          <span className="adm-toolbar-label">Range</span>
          {[7, 14, 30, 90].map(value => <button key={value} type="button" aria-pressed={days === value} onClick={() => setDays(value)}>{value}d</button>)}
        </div>
        <div className="adm-status-line">
          <span className="adm-source"><Database size={13} /> beta.sqlite3 + project files</span>
          {monitoring?.enabled === false
            ? <span className="adm-mono">Local admin · monitoring status needs a signed-in session</span>
            : monitoring && [['posthog', 'PostHog'], ['langfuse', 'Langfuse'], ['sentry', 'Sentry']].map(([key, label]) => (
              <span key={key} className={`adm-monitor ${monitoring[key as keyof MonitorStatus] ? 'on' : 'off'}`} title={`${label} is ${monitoring[key as keyof MonitorStatus] ? 'connected' : 'not configured'}`}>{label}</span>
            ))}
        </div>
      </div>

      {error && <div role="alert" className="adm-alert"><TriangleAlert size={16} /><span>{error}</span><button type="button" onClick={() => setRefreshKey(value => value + 1)}>Retry</button></div>}

      <nav className="adm-tabs" aria-label="Admin sections">
        {TABS.map(item => {
          const Icon = item.icon as TabIcon
          return <button key={item.id} type="button" aria-current={tab === item.id ? 'page' : undefined} className={tab === item.id ? 'is-active' : ''} onClick={() => setTab(item.id)}>
            <Icon size={15} /><span>{item.label}</span>{badges[item.id] !== undefined && <b>{formatNumber(badges[item.id])}</b>}
          </button>
        })}
      </nav>

      {tab === 'overview' && (loading && !overview.data
        ? <Loading label="Reading the beta event log…" />
        : overview.data ? <OverviewTab overview={overview.data} monitoring={monitoring} /> : <EmptyState icon={Activity} title="No overview yet" text="The admin API did not return any data. Check the backend and retry." />)}
      {tab === 'testers' && <TestersTab refresh={refreshKey} days={days} />}
      {tab === 'projects' && <ProjectsTab refresh={refreshKey} />}
      {tab === 'activity' && <ActivityTab refresh={refreshKey} days={days} />}
      {tab === 'runs' && <RunsTab refresh={refreshKey} days={days} />}
      {tab === 'tools' && <ToolsTab refresh={refreshKey} days={days} />}
      {tab === 'searches' && <SearchesTab refresh={refreshKey} days={days} />}
      {tab === 'waitlist' && <WaitlistTab refresh={refreshKey} />}

      <footer className="adm-footer">
        <span>{overview.data ? `Generated ${formatDateTime(overview.data.generated_at)} · ${overview.data.range_days} day range` : 'Waiting for the admin API'}</span>
        <span>Tool arguments and prompts are stored as redacted previews only.</span>
      </footer>
    </div>
  </main>
}

// ---------------------------------------------------------------------------
// overview

function OverviewTab({ overview, monitoring }: { overview: Overview; monitoring: MonitorStatus | null }) {
  const t = overview.totals
  const p = overview.previous
  const providers = overview.providers
  return <>
    <section className="adm-kpis">
      <Kpi label="Active testers" value={t.active_7d} detail={`${formatNumber(t.active_24h)} in 24h · ${formatNumber(t.new_users_7d)} new this week`} series={overview.series.map(point => point.active_users)} icon={Users} />
      <Kpi label="Projects" value={t.projects} detail={`${formatNumber(t.active_projects_7d)} touched this week`} series={overview.series.map(point => point.projects)} icon={CircuitBoard} />
      <Kpi label="AI runs" value={t.runs} detail={`${t.runs ? formatPct((t.runs - t.run_errors) / t.runs * 100) : '—'} success · ${formatDuration(t.avg_run_ms)} avg`} series={overview.series.map(point => point.runs)} icon={Bot} delta={percentChange(t.runs, p.runs)} />
      <Kpi label="Tool calls" value={t.tool_calls} detail={`${formatNumber(t.tool_errors)} failed · ${formatDuration(t.avg_tool_ms)} avg`} series={overview.series.map(point => point.tool_calls)} icon={Wrench} delta={percentChange(t.tool_calls, p.tool_calls)} tone={t.tool_errors ? 'warning' : 'default'} />
      <Kpi label="Searches" value={t.searches} detail={`${formatNumber(t.unique_searches)} unique queries`} series={overview.series.map(point => point.searches)} icon={Search} delta={percentChange(t.searches, p.searches)} />
      <Kpi label="Compiles" value={t.compiles} detail={`${formatNumber(t.compile_failures)} failed builds`} series={overview.series.map(point => point.compiles)} icon={Cpu} delta={percentChange(t.compiles, p.compiles)} tone={t.compile_failures ? 'warning' : 'default'} />
      <Kpi label="Simulations" value={t.simulations} detail={`${formatNumber(t.chats)} chat messages · ${formatNumber(t.compile_failures)} failed compiles`} series={overview.series.map(point => point.simulations)} icon={Zap} />
      <Kpi label="Waitlist" value={t.access_requests} detail={`${formatNumber(t.invites)} invited · ${formatNumber(t.accepted_invites)} accepted`} series={overview.series.map(point => point.new_users)} icon={Inbox} />
    </section>

    <section className="adm-grid two">
      <ActivityChart series={overview.series} />
      <Panel title="Tester funnel" hint="Distinct testers reaching each product milestone in this range.">
        <Funnel stages={overview.funnel} />
      </Panel>
    </section>

    <section className="adm-grid two">
      <Panel title="Agent tools" hint="Every tool call the agent made, ranked by volume.">
        <div className="adm-table-wrap">
          <table className="adm-table">
            <thead><tr><th>Tool</th><th>Calls</th><th>Errors</th><th>Avg</th><th>Testers</th></tr></thead>
            <tbody>
              {overview.top_tools.map(tool => <tr key={tool.name}>
                <td><span className="adm-cell-title">{tool.name}</span></td>
                <td className="adm-num">{formatNumber(tool.calls)}</td>
                <td className={tool.errors ? 'adm-num danger' : 'adm-num'}>{formatNumber(tool.errors)}</td>
                <td className="adm-num">{formatDuration(tool.avg_ms)}</td>
                <td className="adm-num">{formatNumber(tool.users)}</td>
              </tr>)}
              {!overview.top_tools.length && <tr><td colSpan={5}><span className="adm-muted">No agent tool calls in this range.</span></td></tr>}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="Models" hint="Run volume, reliability, and token use for each provider/model pair.">
        <div className="adm-table-wrap">
          <table className="adm-table">
            <thead><tr><th>Model</th><th>Runs</th><th>Success</th><th>Avg</th><th>Tokens</th></tr></thead>
            <tbody>
              {providers.map(item => <tr key={`${item.provider}/${item.model}`}>
                <td><span className="adm-cell-title">{item.model}</span><small>{item.provider}</small></td>
                <td className="adm-num">{formatNumber(item.runs)}</td>
                <td className="adm-num"><span className={item.success_rate >= 90 ? 'adm-pill good' : item.success_rate >= 70 ? 'adm-pill warn' : 'adm-pill danger'}>{formatPct(item.success_rate)}</span></td>
                <td className="adm-num">{formatDuration(item.avg_ms)}</td>
                <td className="adm-num">{formatNumber(item.tokens)}</td>
              </tr>)}
              {!providers.length && <tr><td colSpan={5}><span className="adm-muted">No completed AI runs in this range.</span></td></tr>}
            </tbody>
          </table>
        </div>
      </Panel>
    </section>

    <section className="adm-grid two">
      <Panel title="What testers search for" hint="Catalog and example searches, including empty results.">
        <div className="adm-rank-list">
          {overview.top_searches.map((item, index) => <div className="adm-rank" key={`${item.scope}:${item.query}`}>
            <span className="adm-rank-num">{index + 1}</span>
            <div className="adm-rank-body"><strong>{item.query}</strong><small>{item.scope} · {formatNumber(item.users)} testers · {formatNumber(item.count)} searches</small></div>
            <span className={item.zero_results ? 'adm-pill warn' : 'adm-pill muted'}>{item.zero_results ? `${item.zero_results} empty` : `${Math.round(item.avg_results)} avg`}</span>
          </div>)}
          {!overview.top_searches.length && <span className="adm-muted">No searches recorded yet. Catalog searches are captured by the backend.</span>}
        </div>
      </Panel>
      <Panel title="Boards & observability" hint="What hardware testers pick, and whether traces are leaving the building.">
        <div className="adm-board-list">
          {overview.boards.map(item => {
            const max = overview.boards[0]?.projects || 1
            return <div className="adm-board-row" key={item.board}>
              <span>{item.board === 'unselected' ? 'No board yet' : item.board}</span>
              <div className="adm-meter"><i style={{ width: `${Math.max(4, item.projects / max * 100)}%` }} /></div>
              <b>{formatNumber(item.projects)}</b>
            </div>
          })}
          {!overview.boards.length && <span className="adm-muted">No projects saved yet.</span>}
        </div>
        <div className="adm-observability">
          {monitoring?.enabled === false
            ? <p>Sign in as an admin to see observability status.</p>
            : [['posthog', 'PostHog'], ['langfuse', 'Langfuse'], ['sentry', 'Sentry']].map(([key, label]) => <span key={key} className={monitoring?.[key as keyof MonitorStatus] ? 'adm-obs on' : 'adm-obs'}><span />{label} {monitoring?.[key as keyof MonitorStatus] ? 'connected' : 'not set'}</span>)}
        </div>
      </Panel>
    </section>

    <Panel title="Live event stream" hint="The newest 30 events across all testers and sources." action={<span className="adm-mono">{formatNumber(t.events_total)} all-time events</span>}>
      <EventFeed events={overview.recent_events} compact />
    </Panel>
  </>
}

function Kpi({ label, value, detail, series, icon: Icon, delta, tone = 'default', format = 'number' }: {
  label: string; value: number; detail: string; series: number[]; icon: TabIcon; delta?: number | null; tone?: 'default' | 'warning'; format?: 'number' | 'duration'
}) {
  return <article className={`adm-kpi ${tone === 'warning' ? 'is-warning' : ''}`}>
    <header><span className="adm-kpi-icon"><Icon size={16} /></span><span className="adm-kpi-label">{label}</span>{delta !== undefined && delta !== null && <Delta value={delta} />}</header>
    <div className="adm-kpi-value">{format === 'duration' ? formatDuration(value) : formatNumber(value)}</div>
    <p>{detail}</p>
    <Sparkline values={series} />
  </article>
}

function Delta({ value }: { value: number }) {
  const up = value >= 0
  return <span className={`adm-delta ${up ? 'up' : 'down'}`}>{up ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}{Math.abs(value)}%</span>
}

function Sparkline({ values }: { values: number[] }) {
  const points = values.length > 1 ? values : [0, values[0] ?? 0]
  const max = Math.max(...points, 1)
  const width = 132
  const height = 34
  const path = points.map((value, index) => `${index ? 'L' : 'M'}${(index / (points.length - 1)) * width},${height - 3 - (value / max) * (height - 8)}`).join(' ')
  const last = points[points.length - 1]
  return <svg className="adm-spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
    <path d={`${path} L${width},${height} L0,${height} Z`} className="adm-spark-area" />
    <path d={path} className="adm-spark-line" />
    <circle cx={width} cy={height - 3 - (last / max) * (height - 8)} r="2.5" className="adm-spark-dot" />
  </svg>
}

function ActivityChart({ series }: { series: SeriesPoint[] }) {
  const [metric, setMetric] = useState<MetricKey>('events')
  const label = METRICS.find(item => item.key === metric)?.label || 'Activity'
  const width = 860
  const height = 250
  const pad = { left: 38, right: 14, top: 18, bottom: 30 }
  const values = series.map(point => point[metric])
  const max = Math.max(...values, 1)
  const ceiling = niceCeiling(max)
  const points = values.map((value, index) => ({
    x: pad.left + (series.length > 1 ? index / (series.length - 1) : 0.5) * (width - pad.left - pad.right),
    y: pad.top + (1 - value / ceiling) * (height - pad.top - pad.bottom),
    value,
    date: series[index]?.date || '',
  }))
  const line = points.map((point, index) => `${index ? 'L' : 'M'}${point.x},${point.y}`).join(' ')
  const area = points.length ? `${line} L${points[points.length - 1].x},${height - pad.bottom} L${points[0].x},${height - pad.bottom} Z` : ''
  const labelEvery = Math.max(1, Math.ceil(series.length / 7))
  const ticks = [ceiling, ceiling / 2, 0]
  return <section className="adm-panel adm-chart-panel">
    <header className="adm-panel-head">
      <div><h2>Activity trend</h2><p>Daily volume of {label.toLowerCase()} across the selected range.</p></div>
      <div className="adm-seg" role="group" aria-label="Chart metric">{METRICS.map(item => <button key={item.key} type="button" aria-pressed={metric === item.key} onClick={() => setMetric(item.key)}>{item.label}</button>)}</div>
    </header>
    <svg className="adm-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label} by day`}>
      <defs><linearGradient id="adm-chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#9ee56b" stopOpacity=".34" /><stop offset="100%" stopColor="#9ee56b" stopOpacity="0" /></linearGradient></defs>
      {ticks.map(tick => {
        const y = pad.top + (1 - tick / ceiling) * (height - pad.top - pad.bottom)
        return <g key={tick}><line x1={pad.left} x2={width - pad.right} y1={y} y2={y} className="adm-chart-grid" /><text x={pad.left - 9} y={y + 4} textAnchor="end" className="adm-chart-axis">{formatNumber(tick)}</text></g>
      })}
      {area && <path d={area} fill="url(#adm-chart-fill)" />}
      {line && <path d={line} className="adm-chart-line" />}
      {points.map(point => <circle key={point.date} cx={point.x} cy={point.y} r={series.length > 45 ? 1.8 : 2.8} className="adm-chart-dot"><title>{point.date}: {formatNumber(point.value)}</title></circle>)}
      {points.map((point, index) => index % labelEvery === 0 || index === points.length - 1
        ? <text key={`label-${point.date}`} x={point.x} y={height - 8} textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'} className="adm-chart-axis">{point.date.slice(5)}</text>
        : null)}
    </svg>
  </section>
}

function niceCeiling(value: number) {
  if (value <= 5) return 5
  const power = Math.pow(10, Math.floor(Math.log10(value)))
  const scaled = value / power
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10
  return step * power
}

function Funnel({ stages }: { stages: { stage: string; count: number; hint: string }[] }) {
  const max = Math.max(...stages.map(stage => stage.count), 1)
  return <div className="adm-funnel">
    {stages.map((stage, index) => {
      const previous = index ? stages[index - 1].count : 0
      const conversion = index && previous ? Math.round(stage.count / previous * 100) : null
      return <div className="adm-funnel-row" key={stage.stage}>
        <div className="adm-funnel-copy"><span>{stage.stage}</span><small>{stage.hint}</small></div>
        <div className="adm-funnel-track"><i style={{ width: `${Math.max(stage.count ? 3 : 0, stage.count / max * 100)}%` }} /><b>{formatNumber(stage.count)}</b></div>
        <span className="adm-funnel-conv">{conversion === null ? 'entry' : `${conversion}%`}</span>
      </div>
    })}
  </div>
}

// ---------------------------------------------------------------------------
// testers

function TestersTab({ refresh, days }: { refresh: number; days: number }) {
  const { data, loading, error } = useAdminQuery<UsersResponse>('/api/beta/admin/users?limit=500', refresh)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const users = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return (data?.users || []).filter(user => !needle || `${user.email} ${user.role} ${user.boards.join(' ')}`.toLowerCase().includes(needle))
  }, [data, query])
  const active = data?.users.filter(user => activityTone(user.last_seen_at || user.last_event_at) === 'live').length || 0
  return <>
    <div className="adm-section-head"><div><p className="adm-eyebrow">People</p><h2>Testers</h2><p>Every invited account, what they touched, and how recently they showed up.</p></div><span className="adm-mono">{formatNumber(data?.total || 0)} accounts · {formatNumber(active)} active today</span></div>
    {error && <QueryError message={error} />}
    <div className="adm-split">
      <section className="adm-panel adm-split-main">
        <header className="adm-panel-head"><div><h2>Accounts</h2><p>Select a tester for their full timeline.</p></div><label className="adm-field"><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search email, role, board…" aria-label="Search testers" /></label></header>
        {loading && !data ? <Loading label="Loading testers…" /> : <div className="adm-table-wrap">
          <table className="adm-table adm-clickable">
            <thead><tr><th>Tester</th><th>Last seen</th><th>Projects</th><th>Runs</th><th>Tools</th><th>Searches</th><th>Errors</th></tr></thead>
            <tbody>
              {users.map(user => {
                const tone = activityTone(user.last_seen_at || user.last_event_at)
                return <tr key={user.id} className={selected === user.id ? 'is-selected' : ''} tabIndex={0} onClick={() => setSelected(user.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelected(user.id) } }}>
                  <td><div className="adm-person"><span className="adm-avatar">{initials(user.email)}</span><div><span className="adm-cell-title">{user.email}</span><small>{user.role} · joined {formatDate(user.created_at)}</small></div></div></td>
                  <td><span className={`adm-activity ${tone}`}><i />{relative(user.last_seen_at || user.last_event_at)}</span></td>
                  <td className="adm-num">{formatNumber(user.projects)}</td>
                  <td className="adm-num">{formatNumber(user.runs)}</td>
                  <td className="adm-num">{formatNumber(user.tool_calls)}</td>
                  <td className="adm-num">{formatNumber(user.searches)}</td>
                  <td className="adm-num">{user.tool_errors + user.run_errors ? <span className="danger">{formatNumber(user.tool_errors + user.run_errors)}</span> : '0'}</td>
                </tr>
              })}
              {!users.length && <tr><td colSpan={7}><span className="adm-muted">{data ? 'No testers match this search.' : 'No testers yet.'}</span></td></tr>}
            </tbody>
          </table>
        </div>}
      </section>
      {selected ? <TesterDetail key={selected} id={selected} days={days} refresh={refresh} onClose={() => setSelected(null)} /> : <aside className="adm-panel adm-detail-empty"><Users size={28} /><h2>Pick a tester</h2><p>Their projects, agent activity, searches, and sign-in sessions will appear here.</p></aside>}
    </div>
  </>
}

function TesterDetail({ id, days, refresh, onClose }: { id: string; days: number; refresh: number; onClose: () => void }) {
  const { data, loading, error } = useAdminQuery<UserDetail>(`/api/beta/admin/users/${encodeURIComponent(id)}?days=${Math.max(days, 30)}`, refresh)
  return <aside className="adm-panel adm-detail">
    <header className="adm-detail-head">
      <div><p className="adm-eyebrow">Tester detail</p><h2>{data?.user.email || 'Loading tester…'}</h2>{data && <small>{data.user.role} · joined {formatDate(data.user.created_at)} · last seen {relative(data.user.last_seen_at)}</small>}</div>
      <button type="button" className="adm-icon-btn" onClick={onClose} aria-label="Close tester detail"><X size={16} /></button>
    </header>
    {error && <QueryError message={error} />}
    {loading && !data ? <Loading label="Loading timeline…" /> : data && <>
      <div className="adm-mini-grid">
        <MiniStat label="Projects" value={data.projects.length} />
        <MiniStat label="Runs" value={data.events.filter(event => event.name.startsWith('ai.run')).length} />
        <MiniStat label="Tools" value={data.tools.reduce((total, tool) => total + tool.calls, 0)} />
        <MiniStat label="Searches" value={data.searches.reduce((total, item) => total + item.count, 0)} />
      </div>
      <DetailBlock title="Daily activity">
        <ActivityBars data={data.activity} />
      </DetailBlock>
      <DetailBlock title="Projects" count={data.projects.length}>
        {data.projects.length ? data.projects.map(project => <div className="adm-line" key={project.id}><div><strong>{project.name}</strong><small>{project.board} · {formatNumber(project.components)} components · {formatNumber(project.wires)} wires</small></div><span className="adm-pill muted">{project.compiler_status}</span></div>) : <span className="adm-muted">No projects yet.</span>}
      </DetailBlock>
      <DetailBlock title="Agent tools" count={data.tools.length}>
        {data.tools.length ? data.tools.slice(0, 8).map(tool => <div className="adm-line" key={tool.name}><div><strong>{tool.name}</strong><small>{tool.errors ? `${tool.errors} failed` : 'No errors'} · {formatDuration(tool.avg_ms)} avg</small></div><b>{formatNumber(tool.calls)}</b></div>) : <span className="adm-muted">No tool calls yet.</span>}
      </DetailBlock>
      <DetailBlock title="Searches" count={data.searches.length}>
        {data.searches.length ? data.searches.slice(0, 8).map(item => <div className="adm-line" key={item.query}><div><strong>{item.query}</strong></div><b>{formatNumber(item.count)}</b></div>) : <span className="adm-muted">No searches yet.</span>}
      </DetailBlock>
      <DetailBlock title="Recent events" count={data.events.length}>
        <EventFeed events={data.events.slice(0, 14)} compact />
      </DetailBlock>
      <DetailBlock title="Sign-in sessions" count={data.auth_sessions.length}>
        {data.auth_sessions.length ? data.auth_sessions.slice(0, 6).map((session, index) => <div className="adm-line" key={`${session.created_at}-${index}`}><div><strong>{formatDateTime(session.created_at)}</strong><small>Expires {formatDateTime(session.expires_at)}</small></div><span className={session.active ? 'adm-pill good' : 'adm-pill muted'}>{session.active ? 'active' : 'expired'}</span></div>) : <span className="adm-muted">No sessions recorded.</span>}
      </DetailBlock>
    </>}
  </aside>
}

function MiniStat({ label, value }: { label: string; value: number }) {
  return <div className="adm-mini"><small>{label}</small><strong>{formatNumber(value)}</strong></div>
}

function ActivityBars({ data }: { data: { date: string; events: number }[] }) {
  const max = Math.max(...data.map(item => item.events), 1)
  if (!data.length) return <span className="adm-muted">No activity yet.</span>
  return <div className="adm-bars" aria-label="Daily event volume">{data.map(item => <span key={item.date} title={`${item.date}: ${formatNumber(item.events)} events`} style={{ height: `${Math.max(4, item.events / max * 100)}%` }} />)}</div>
}

function DetailBlock({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return <section className="adm-detail-block"><header><h3>{title}</h3>{count !== undefined && <span>{formatNumber(count)}</span>}</header>{children}</section>
}

// ---------------------------------------------------------------------------
// projects

function ProjectsTab({ refresh }: { refresh: number }) {
  const { data, loading, error } = useAdminQuery<ProjectsResponse>('/api/beta/admin/projects?limit=1000', refresh)
  const [query, setQuery] = useState('')
  const [board, setBoard] = useState('all')
  const [selected, setSelected] = useState<string | null>(null)
  const projects = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return (data?.projects || []).filter(project => (board === 'all' || project.board === board) && (!needle || `${project.name} ${project.id} ${project.board} ${project.owner_email || ''}`.toLowerCase().includes(needle)))
  }, [data, query, board])
  return <>
    <div className="adm-section-head"><div><p className="adm-eyebrow">Hardware</p><h2>Projects</h2><p>Canonical project files joined to the behaviour they generated.</p></div><span className="adm-mono">{formatNumber(data?.total || 0)} projects · {formatNumber(data?.boards.length || 0)} boards</span></div>
    {error && <QueryError message={error} />}
    <div className="adm-split">
      <section className="adm-panel adm-split-main">
        <header className="adm-panel-head adm-wrap-head">
          <div><h2>Project files</h2><p>Newest activity first.</p></div>
          <div className="adm-filter-row">
            <label className="adm-field"><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search name, owner, id…" aria-label="Search projects" /></label>
            <select className="adm-select" value={board} onChange={event => setBoard(event.target.value)} aria-label="Filter projects by board"><option value="all">All boards</option>{data?.boards.filter(item => item.board).map(item => <option key={item.board} value={item.board}>{item.board}</option>)}</select>
          </div>
        </header>
        {loading && !data ? <Loading label="Loading projects…" /> : <div className="adm-project-grid">
          {projects.map(project => <button type="button" key={project.id} className={`adm-project ${selected === project.id ? 'is-selected' : ''}`} onClick={() => setSelected(project.id)}>
            <div className="adm-project-top"><span className="adm-board">{project.board === 'unselected' ? 'No board' : project.board}</span><span className={`adm-pill ${project.compiler_status === 'simulation_ready' ? 'good' : project.compiler_status === 'error' ? 'danger' : 'muted'}`}>{project.compiler_status.replaceAll('_', ' ')}</span></div>
            <strong className="adm-project-name">{project.name}</strong>
            <span className="adm-project-owner">{project.owner_email || 'Unowned workspace'}</span>
            <div className="adm-project-stats"><span><b>{formatNumber(project.components)}</b> parts</span><span><b>{formatNumber(project.wires)}</b> wires</span><span><b>{formatNumber(project.firmware_lines)}</b> lines</span></div>
            <div className="adm-project-foot"><span>{formatNumber(project.runs)} runs · {formatNumber(project.tool_calls)} tools · {formatNumber(project.compiles)} compiles</span><span>{relative(project.last_activity_at || project.updated_at)}</span></div>
          </button>)}
          {!projects.length && <span className="adm-muted">{data ? 'No projects match these filters.' : 'No projects yet.'}</span>}
        </div>}
      </section>
      {selected ? <ProjectDetailPanel key={selected} id={selected} refresh={refresh} onClose={() => setSelected(null)} /> : <aside className="adm-panel adm-detail-empty"><CircuitBoard size={28} /><h2>Pick a project</h2><p>See its firmware, components, and the exact events the agent produced.</p></aside>}
    </div>
  </>
}

function ProjectDetailPanel({ id, refresh, onClose }: { id: string; refresh: number; onClose: () => void }) {
  const { data, loading, error } = useAdminQuery<ProjectDetail>(`/api/beta/admin/projects/${encodeURIComponent(id)}`, refresh)
  const [copied, setCopied] = useState(false)
  const copy = () => {
    if (!data?.firmware_source) return
    void navigator.clipboard?.writeText(data.firmware_source).then(() => setCopied(true)).catch(() => {})
  }
  return <aside className="adm-panel adm-detail">
    <header className="adm-detail-head">
      <div><p className="adm-eyebrow">Project detail</p><h2>{data?.name || 'Loading project…'}</h2>{data && <small>{data.owner_email || 'Unowned'} · {data.board} · revision {data.revision ?? '—'}</small>}</div>
      <button type="button" className="adm-icon-btn" onClick={onClose} aria-label="Close project detail"><X size={16} /></button>
    </header>
    {error && <QueryError message={error} />}
    {loading && !data ? <Loading label="Loading project…" /> : data && <>
      <div className="adm-mini-grid">
        <MiniStat label="Parts" value={data.components} />
        <MiniStat label="Wires" value={data.wires} />
        <MiniStat label="Firmware" value={data.firmware_lines} />
        <MiniStat label="Events" value={data.events.length} />
      </div>
      <DetailBlock title="Build state">
        <div className="adm-chip-row">{data.component_types.length ? data.component_types.map(type => <span className="adm-chip" key={type}>{type}</span>) : <span className="adm-muted">No components yet.</span>}</div>
        <div className="adm-line"><div><strong>Requirements</strong><small>Project review state</small></div><span className="adm-pill muted">{data.requirements_status}</span></div>
        <div className="adm-line"><div><strong>Compiler</strong><small>{data.firmware_filename || 'No firmware file'}</small></div><span className={`adm-pill ${data.compiler_status === 'simulation_ready' ? 'good' : data.compiler_status === 'error' ? 'danger' : 'muted'}`}>{data.compiler_status.replaceAll('_', ' ')}</span></div>
        <div className="adm-line"><div><strong>Created</strong><small>{formatDate(data.created_at)}</small></div><span className="adm-mono">updated {relative(data.updated_at)}</span></div>
      </DetailBlock>
      <DetailBlock title="Firmware" count={data.firmware_lines}>
        {data.firmware_source ? <>
          <div className="adm-code-head"><span>{data.firmware_filename || 'sketch.ino'}</span><button type="button" className="adm-btn tiny" onClick={copy}>{copied ? <CheckCircle2 size={13} /> : <Copy size={13} />}{copied ? 'Copied' : 'Copy'}</button></div>
          <pre className="adm-code">{data.firmware_source.slice(0, 12_000)}</pre>
        </> : <span className="adm-muted">No firmware saved for this project.</span>}
      </DetailBlock>
      <DetailBlock title="Project events" count={data.events.length}>
        <EventFeed events={data.events.slice(0, 20)} compact />
      </DetailBlock>
    </>}
  </aside>
}

// ---------------------------------------------------------------------------
// activity

function ActivityTab({ refresh, days }: { refresh: number; days: number }) {
  const [name, setName] = useState('')
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [limit, setLimit] = useState(60)
  useEffect(() => { const timer = setTimeout(() => setLimit(60), 0); return () => clearTimeout(timer) }, [name, days])
  useEffect(() => { const timer = setTimeout(() => { setDebounced(query.trim()); setLimit(60) }, 350); return () => clearTimeout(timer) }, [query])
  const path = `/api/beta/admin/events?days=${days}&limit=${limit}${name ? `&name=${encodeURIComponent(name)}` : ''}${debounced ? `&q=${encodeURIComponent(debounced)}` : ''}`
  const { data, loading, error } = useAdminQuery<EventsResponse>(path, refresh)
  const names = data?.names || []
  return <>
    <div className="adm-section-head"><div><p className="adm-eyebrow">Raw signal</p><h2>Activity stream</h2><p>Every interaction, newest first. Expand any event to see the redacted properties we store.</p></div><span className="adm-mono">{formatNumber(data?.total || 0)} matching events</span></div>
    {error && <QueryError message={error} />}
    <div className="adm-filterbar">
      <label className="adm-field grow"><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search event names or property values…" aria-label="Search events" /></label>
      <button type="button" className={`adm-chip ${!name ? 'is-active' : ''}`} onClick={() => { setName(''); setLimit(60) }}>All signals</button>
      {names.slice(0, 9).map(item => <button type="button" key={item.name} className={`adm-chip ${name === item.name ? 'is-active' : ''}`} onClick={() => { setName(name === item.name ? '' : item.name); setLimit(60) }}>{item.label}<b>{formatNumber(item.count)}</b></button>)}
    </div>
    <Panel title={name ? `${names.find(item => item.name === name)?.label || name}` : 'Events'} hint={loading ? 'Refreshing…' : 'Redacted properties only. Prompt and tool payloads are summarized.'}>
      {loading && !data ? <Loading label="Reading events…" /> : <EventFeed events={data?.events || []} />}
      {data && data.total > data.events.length && <div className="adm-load-more"><button type="button" className="adm-btn" onClick={() => setLimit(value => value + 80)} disabled={loading}>{loading ? <LoaderCircle className="adm-spin" size={14} /> : <Layers size={14} />}Load more events</button><span className="adm-mono">Showing {formatNumber(data.events.length)} of {formatNumber(data.total)}</span></div>}
    </Panel>
  </>
}

// ---------------------------------------------------------------------------
// runs

const RUN_STATUSES = ['', 'success', 'error', 'cancelled', 'running', 'incomplete'] as const

function RunsTab({ refresh, days }: { refresh: number; days: number }) {
  const { data, loading, error } = useAdminQuery<RunsResponse>(`/api/beta/admin/runs?days=${days}&limit=500`, refresh)
  const [status, setStatus] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const runs = (data?.runs || []).filter(run => !status || run.status === status)
  const summary = data?.summary
  return <>
    <div className="adm-section-head"><div><p className="adm-eyebrow">Agent</p><h2>AI runs</h2><p>One row per user message that reached the agent, with duration, tokens, and tool work.</p></div><span className="adm-mono">{formatNumber(summary?.total || 0)} runs in range</span></div>
    {error && <QueryError message={error} />}
    <div className="adm-kpis compact">
      <Kpi label="Completed" value={summary?.success || 0} detail="Successful terminal runs" series={[]} icon={CheckCircle2} />
      <Kpi label="Failed" value={summary?.errors || 0} detail="Errors or timeouts" series={[]} icon={XCircle} tone={summary?.errors ? 'warning' : 'default'} />
      <Kpi label="Running" value={summary?.running || 0} detail="Started under 15 minutes ago" series={[]} icon={LoaderCircle} />
      <Kpi label="Avg duration" value={summary?.avg_ms || 0} detail="Wall time per run" series={[]} icon={Clock} format="duration" />
      <Kpi label="Tokens" value={summary?.tokens || 0} detail="Prompt + completion tokens" series={[]} icon={Sparkles} />
    </div>
    <div className="adm-filterbar">
      {RUN_STATUSES.map(value => <button type="button" key={value || 'all'} className={`adm-chip ${status === value ? 'is-active' : ''}`} onClick={() => setStatus(value)}>{value ? value[0].toUpperCase() + value.slice(1) : 'All runs'}</button>)}
    </div>
    <Panel title="Run log" hint="Click a row to inspect prompt preview, model calls, and failures.">
      {loading && !data ? <Loading label="Loading runs…" /> : <div className="adm-table-wrap">
        <table className="adm-table adm-clickable">
          <thead><tr><th>Status</th><th>Started</th><th>Tester</th><th>Project</th><th>Model</th><th>Duration</th><th>Tools</th><th>Tokens</th></tr></thead>
          <tbody>
            {runs.map(run => <RunRows key={run.run_id} run={run} expanded={expanded === run.run_id} onToggle={() => setExpanded(expanded === run.run_id ? null : run.run_id)} />)}
            {!runs.length && <tr><td colSpan={8}><span className="adm-muted">{data ? 'No runs match this filter.' : 'No runs yet.'}</span></td></tr>}
          </tbody>
        </table>
      </div>}
    </Panel>
  </>
}

function RunRows({ run, expanded, onToggle }: { run: RunsResponse['runs'][number]; expanded: boolean; onToggle: () => void }) {
  return <>
    <tr className={expanded ? 'is-selected' : ''} tabIndex={0} onClick={onToggle} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onToggle() } }}>
      <td><span className={`adm-pill ${runTone(run.status)}`}>{run.status}</span></td>
      <td><span className="adm-cell-title">{relative(run.started_at)}</span><small>{formatDateTime(run.started_at)}</small></td>
      <td>{run.email || <span className="adm-muted">anonymous</span>}</td>
      <td>{run.project_name || <span className="adm-muted">no project</span>}</td>
      <td><span className="adm-cell-title">{run.model || '—'}</span><small>{run.provider || ''}</small></td>
      <td className="adm-num">{formatDuration(run.duration_ms)}</td>
      <td className="adm-num">{formatNumber(run.tool_calls)}</td>
      <td className="adm-num">{formatNumber(run.tokens)}</td>
    </tr>
    {expanded && <tr className="adm-detail-row"><td colSpan={8}>
      <div className="adm-run-detail">
        <div className="adm-run-meta">
          <span>Run <code>{run.run_id}</code></span>
          <span>Reason <code>{run.reason || 'none'}</code></span>
          <span>Model calls <code>{formatNumber(run.model_calls)}</code></span>
          <span>Prompt <code>{formatNumber(run.prompt_length)} chars</code></span>
          <span>Started <code>{formatDateTime(run.started_at)}</code></span>
          <span>Finished <code>{formatDateTime(run.completed_at)}</code></span>
        </div>
        {run.prompt_preview && <div><p className="adm-label">Prompt preview</p><pre className="adm-code">{run.prompt_preview}</pre></div>}
      </div>
    </td></tr>}
  </>
}

// ---------------------------------------------------------------------------
// tools

function ToolsTab({ refresh, days }: { refresh: number; days: number }) {
  const [source, setSource] = useState('')
  const { data, loading, error } = useAdminQuery<ToolsResponse>(`/api/beta/admin/tools?days=${days}${source ? `&source=${encodeURIComponent(source)}` : ''}`, refresh)
  const toolCalls = data?.tools.reduce((total, tool) => total + tool.calls, 0) || 0
  const toolErrors = data?.tools.reduce((total, tool) => total + tool.errors, 0) || 0
  return <>
    <div className="adm-section-head"><div><p className="adm-eyebrow">Agent capability</p><h2>Tools & commands</h2><p>Which agent tools get used, which fail, and what testers do by hand in the workspace.</p></div><span className="adm-mono">{formatNumber(toolCalls)} tool calls · {formatNumber(toolErrors)} failed</span></div>
    {error && <QueryError message={error} />}
    <div className="adm-filterbar">
      <button type="button" className={`adm-chip ${!source ? 'is-active' : ''}`} onClick={() => setSource('')}>All sources</button>
      {data?.sources.map(item => <button type="button" key={item.source} className={`adm-chip ${source === item.source ? 'is-active' : ''}`} onClick={() => setSource(source === item.source ? '' : item.source)}>{item.source}<b>{formatNumber(item.calls)}</b></button>)}
      <span className="adm-toolbar-label">Agent calls arrive from the chat backend; web/manual rows come from workspace interaction.</span>
    </div>
    <section className="adm-grid two">
      <Panel title="Tool catalogue" hint="Calls, failure rate, latency, and distinct testers.">
        {loading && !data ? <Loading label="Loading tools…" /> : <div className="adm-table-wrap">
          <table className="adm-table">
            <thead><tr><th>Tool</th><th>Calls</th><th>Success</th><th>Avg</th><th>Testers</th><th>Last used</th></tr></thead>
            <tbody>
              {data?.tools.map(tool => <tr key={tool.name}>
                <td><span className="adm-cell-title">{tool.name}</span><small>{Object.entries(tool.sources).map(([key, value]) => `${key} ${value}`).join(' · ')}</small></td>
                <td className="adm-num">{formatNumber(tool.calls)}</td>
                <td className="adm-num"><span className={tool.success_rate >= 90 ? 'adm-pill good' : tool.success_rate >= 70 ? 'adm-pill warn' : 'adm-pill danger'}>{formatPct(tool.success_rate)}</span></td>
                <td className="adm-num">{formatDuration(tool.avg_ms)}</td>
                <td className="adm-num">{formatNumber(tool.users)}</td>
                <td className="adm-num">{relative(tool.last_at)}</td>
              </tr>)}
              {!data?.tools.length && <tr><td colSpan={6}><span className="adm-muted">No tool calls in this range.</span></td></tr>}
            </tbody>
          </table>
        </div>}
      </Panel>
      <Panel title="Manual workspace commands" hint="Direct hardware commands, separate from agent tool calls.">
        <div className="adm-table-wrap">
          <table className="adm-table">
            <thead><tr><th>Command</th><th>Calls</th><th>Success</th><th>Avg</th><th>Testers</th></tr></thead>
            <tbody>
              {data?.manual_commands.map(command => <tr key={command.command}>
                <td><span className="adm-cell-title">{command.command}</span></td>
                <td className="adm-num">{formatNumber(command.calls)}</td>
                <td className="adm-num"><span className={command.success_rate >= 90 ? 'adm-pill good' : command.success_rate >= 70 ? 'adm-pill warn' : 'adm-pill danger'}>{formatPct(command.success_rate)}</span></td>
                <td className="adm-num">{formatDuration(command.avg_ms)}</td>
                <td className="adm-num">{formatNumber(command.users)}</td>
              </tr>)}
              {!data?.manual_commands.length && <tr><td colSpan={5}><span className="adm-muted">No manual commands in this range.</span></td></tr>}
            </tbody>
          </table>
        </div>
      </Panel>
    </section>
    <Panel title="Recent agent calls" hint="Newest tool completions with arguments summarized by the backend.">
      <EventFeed events={data?.recent || []} />
    </Panel>
  </>
}

// ---------------------------------------------------------------------------
// searches

function SearchesTab({ refresh, days }: { refresh: number; days: number }) {
  const [scope, setScope] = useState('')
  const { data, loading, error } = useAdminQuery<SearchesResponse>(`/api/beta/admin/searches?days=${days}${scope ? `&scope=${encodeURIComponent(scope)}` : ''}`, refresh)
  const searches = data?.queries.reduce((total, item) => total + item.count, 0) || 0
  const empty = data?.queries.reduce((total, item) => total + item.zero_results, 0) || 0
  return <>
    <div className="adm-section-head"><div><p className="adm-eyebrow">Intent</p><h2>Search queries</h2><p>What testers look for in the component catalog and example explorer, including dead ends.</p></div><span className="adm-mono">{formatNumber(data?.total || 0)} unique · {formatNumber(searches)} searches · {formatNumber(empty)} empty</span></div>
    {error && <QueryError message={error} />}
    <div className="adm-filterbar">
      <button type="button" className={`adm-chip ${!scope ? 'is-active' : ''}`} onClick={() => setScope('')}>All scopes</button>
      {data?.scopes.map(item => <button type="button" key={item.scope} className={`adm-chip ${scope === item.scope ? 'is-active' : ''}`} onClick={() => setScope(scope === item.scope ? '' : item.scope)}>{item.scope}<b>{formatNumber(item.count)}</b></button>)}
    </div>
    <section className="adm-grid two">
      <Panel title="Top queries" hint="Normalized only by case; repeated queries are grouped.">
        {loading && !data ? <Loading label="Loading searches…" /> : <div className="adm-table-wrap">
          <table className="adm-table">
            <thead><tr><th>Query</th><th>Scope</th><th>Searches</th><th>Testers</th><th>Avg results</th><th>Last</th></tr></thead>
            <tbody>
              {data?.queries.map(item => <tr key={`${item.scope}:${item.query}`}>
                <td><span className="adm-cell-title">{item.query}</span>{item.zero_results > 0 && <small className="danger">{item.zero_results} searches returned nothing</small>}</td>
                <td><span className="adm-chip">{item.scope}</span></td>
                <td className="adm-num">{formatNumber(item.count)}</td>
                <td className="adm-num">{formatNumber(item.users)}</td>
                <td className="adm-num">{Math.round(item.avg_results)}</td>
                <td className="adm-num">{relative(item.last_at)}</td>
              </tr>)}
              {!data?.queries.length && <tr><td colSpan={6}><span className="adm-muted">No searches in this range.</span></td></tr>}
            </tbody>
          </table>
        </div>}
      </Panel>
      <Panel title="Recent searches" hint="Newest first, with result counts.">
        <EventFeed events={data?.recent || []} compact />
      </Panel>
    </section>
  </>
}

// ---------------------------------------------------------------------------
// waitlist

function WaitlistTab({ refresh }: { refresh: number }) {
  const [version, setVersion] = useState(0)
  const { data, loading, error } = useAdminQuery<WaitlistResponse>('/api/beta/admin/waitlist', refresh + version)
  const [email, setEmail] = useState('')
  const [inviting, setInviting] = useState('')
  const [message, setMessage] = useState('')
  const [inviteError, setInviteError] = useState('')
  const [link, setLink] = useState('')

  const invite = async (target: string) => {
    const address = target.trim()
    if (!address || inviting) return
    setInviting(address)
    setInviteError('')
    setMessage('')
    setLink('')
    try {
      const result = await adminRequest<{ link?: string; sent?: boolean }>('/api/beta/invite', { method: 'POST', body: JSON.stringify({ email: address }) })
      setMessage(`${address} invited.${result.link ? ' Copy the one-time link below and send it directly.' : result.sent ? ' An email is on its way.' : ''}`)
      setLink(result.link || '')
      setEmail('')
      setVersion(value => value + 1)
    } catch (value) {
      setInviteError(value instanceof Error ? value.message : 'Could not invite this tester.')
    } finally {
      setInviting('')
    }
  }

  return <>
    <div className="adm-section-head"><div><p className="adm-eyebrow">Growth</p><h2>Waitlist & invites</h2><p>Access requests that have not been invited yet, plus the one-time links already issued.</p></div><span className="adm-mono">{formatNumber(data?.total || 0)} waiting · {formatNumber(data?.invited_total || 0)} invited</span></div>
    {(error || inviteError) && <QueryError message={error || inviteError} />}
    {message && <div className="adm-success" role="status"><CheckCircle2 size={16} /><span>{message}</span></div>}
    {link && <div className="adm-invite-link"><p className="adm-label">One-time sign-in link · 7 days</p><code>{link}</code><div><CopyButton value={link} /><a className="adm-btn tiny" href={link}>Open link <ChevronRight size={13} /></a></div></div>}

    <section className="adm-grid two">
      <Panel title="Invite a tester" hint="Invites are single-use and last seven days. Email delivery is optional.">
        <form className="adm-invite-form" onSubmit={event => { event.preventDefault(); void invite(email) }}>
          <label className="adm-field grow"><MailPlus size={15} /><input type="email" required value={email} onChange={event => setEmail(event.target.value)} placeholder="tester@email.com" aria-label="Tester email to invite" /></label>
          <button className="adm-btn" type="submit" disabled={!!inviting || !email.trim()}>{inviting ? <LoaderCircle className="adm-spin" size={15} /> : <Send size={15} />}{inviting ? 'Inviting…' : 'Send invite'}</button>
        </form>
        <div className="adm-callout"><Server size={16} /><p>If the backend has no email provider configured, the API returns a copyable one-time link instead of sending mail.</p></div>
      </Panel>

      <Panel title="Waiting for access" hint="People who asked to join but are not invited yet.">
        {loading && !data ? <Loading label="Loading waitlist…" /> : <div className="adm-table-wrap">
          <table className="adm-table">
            <thead><tr><th>Email</th><th>Requests</th><th>Last request</th><th>Email delivered</th><th /></tr></thead>
            <tbody>
              {data?.waitlist.map(lead => <tr key={lead.email}>
                <td><span className="adm-cell-title">{lead.email}</span><small>{lead.user_agent.slice(0, 80) || 'No agent'}</small></td>
                <td className="adm-num">{formatNumber(lead.requests)}</td>
                <td><span className="adm-cell-title">{relative(lead.last_requested_at)}</span><small>{formatDateTime(lead.last_requested_at)}</small></td>
                <td><span className={lead.delivered ? 'adm-pill good' : 'adm-pill muted'}>{lead.delivered ? 'sent' : 'no'}</span></td>
                <td><button type="button" className="adm-btn tiny" onClick={() => void invite(lead.email)} disabled={!!inviting}>{inviting === lead.email ? <LoaderCircle className="adm-spin" size={13} /> : <MailPlus size={13} />}Invite</button></td>
              </tr>)}
              {!data?.waitlist.length && <tr><td colSpan={5}><span className="adm-muted">No uninvited access requests.</span></td></tr>}
            </tbody>
          </table>
        </div>}
      </Panel>
    </section>

    <Panel title="Invites issued" hint="Everyone who received a one-time link, and whether they used it." action={<span className="adm-mono">{formatNumber(data?.invited_total || 0)} total</span>}>
      {loading && !data ? <Loading label="Loading invites…" /> : <div className="adm-table-wrap">
        <table className="adm-table">
          <thead><tr><th>Email</th><th>Invited</th><th>Accepted</th><th>Status</th><th /></tr></thead>
          <tbody>
            {data?.invites.map(record => <tr key={record.email}>
              <td><span className="adm-cell-title">{record.email}</span></td>
              <td>{formatDateTime(record.created_at)}</td>
              <td>{record.accepted_at ? formatDateTime(record.accepted_at) : <span className="adm-muted">Not yet</span>}</td>
              <td><span className={record.accepted_at ? 'adm-pill good' : 'adm-pill warn'}>{record.accepted_at ? 'accepted' : 'pending'}</span></td>
              <td><button type="button" className="adm-btn tiny ghost" onClick={() => void invite(record.email)} disabled={!!inviting}>{inviting === record.email ? <LoaderCircle className="adm-spin" size={13} /> : <RefreshCw size={13} />}New link</button></td>
            </tr>)}
            {!data?.invites.length && <tr><td colSpan={5}><span className="adm-muted">No invites issued yet.</span></td></tr>}
          </tbody>
        </table>
      </div>}
    </Panel>
  </>
}

// ---------------------------------------------------------------------------
// shared pieces

function Panel({ title, hint, action, children }: { title: string; hint?: string; action?: ReactNode; children: ReactNode }) {
  return <section className="adm-panel">
    <header className="adm-panel-head"><div><h2>{title}</h2>{hint && <p>{hint}</p>}</div>{action}</header>
    {children}
  </section>
}

function EventFeed({ events, compact = false }: { events: AdminEvent[]; compact?: boolean }) {
  if (!events.length) return <EmptyState icon={Activity} title="Nothing here yet" text="Events will appear as testers use the beta." />
  return <ol className={`adm-feed ${compact ? 'is-compact' : ''}`}>
    {events.map(event => <li className="adm-feed-item" key={event.id}>
      <span className="adm-feed-dot" data-tone={eventTone(event.name)} />
      <div className="adm-feed-main">
        <div className="adm-feed-title">
          <strong>{event.label || event.name}</strong>
          {event.email && <span>{event.email}</span>}
          {event.project_name && <span className="adm-feed-project">{event.project_name}</span>}
        </div>
        <div className="adm-feed-meta">
          {eventDetail(event) && <span>{eventDetail(event)}</span>}
          {event.source && <span className="adm-source-tag">{event.source}</span>}
          <time dateTime={event.occurred_at_iso || undefined} title={formatDateTime(event.occurred_at_iso)}>{relative(event.occurred_at_iso)}</time>
        </div>
        {!compact && Object.keys(event.properties).length > 0 && <details className="adm-props"><summary>Properties</summary><pre>{JSON.stringify(event.properties, null, 2)}</pre></details>}
      </div>
    </li>)}
  </ol>
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return <button type="button" className="adm-btn tiny" onClick={() => { void navigator.clipboard?.writeText(value).then(() => setCopied(true)).catch(() => {}) }}>{copied ? <CheckCircle2 size={13} /> : <Copy size={13} />}{copied ? 'Copied' : 'Copy link'}</button>
}

function EmptyState({ icon: Icon, title, text }: { icon: TabIcon; title: string; text: string }) {
  return <div className="adm-empty"><Icon size={24} /><h3>{title}</h3><p>{text}</p></div>
}

function Loading({ label }: { label: string }) {
  return <div className="adm-loading" role="status"><LoaderCircle className="adm-spin" size={18} />{label}</div>
}

function QueryError({ message }: { message: string }) {
  return <div className="adm-inline-error" role="alert"><TriangleAlert size={15} /><span>{message}</span></div>
}
