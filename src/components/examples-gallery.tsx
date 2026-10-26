import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { openSample } from '@/lib/open-sample'
import { apiUrl } from '@/lib/api'
import { recordBetaEvent } from '@/lib/beta'

type Example = { id: string; title: string; category: string; boards: string[]; language: string; available: boolean; starter?: boolean; simulation_scope: 'ready' | 'partial' | 'blocked'; thumbnail?: string; blockers: unknown[]; dependencies?: unknown; part_scopes?: unknown[] }
export function ExamplesGallery() {
  const [examples, setExamples] = useState<Example[]>([])
  const [query, setQuery] = useState('')
  const [board, setBoard] = useState('all')
  const [category, setCategory] = useState('all')
  const [scope, setScope] = useState('all')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [opening, setOpening] = useState('')
  const navigate = useNavigate()
  useEffect(() => {
    fetch(apiUrl('/api/hardware/samples/all'), { credentials: 'include' }).then(async response => {
      if (!response.ok) throw new Error('Could not load examples.')
      setExamples((await response.json()).examples)
    }).catch(error => setError(error.message)).finally(() => setLoading(false))
  }, [])
  const visible = examples.filter(example => `${example.title} ${example.id} ${example.category} ${example.boards.join(' ')}`.toLowerCase().includes(query.toLowerCase()) && (board === 'all' || example.boards.includes(board)) && (category === 'all' || example.category === category) && (scope === 'all' || example.simulation_scope === scope))
  useEffect(() => {
    const text = query.trim()
    if (!text) return
    const timer = setTimeout(() => {
      const matches = examples.filter(example => `${example.title} ${example.id} ${example.category} ${example.boards.join(' ')}`.toLowerCase().includes(text.toLowerCase())).length
      recordBetaEvent('search.performed', { query: text.slice(0, 200), scope: 'examples_explorer', result_count: matches, total_examples: examples.length })
    }, 600)
    return () => clearTimeout(timer)
  }, [query, examples])
  const open = async (id: string) => {
    if (opening) return
    setOpening(id); setError('')
    const sample = examples.find(example => example.id === id)
    try {
      const project = await openSample(id)
      recordBetaEvent('sample.opened', { sample_id: id, title: sample?.title, category: sample?.category, boards: sample?.boards, simulation_scope: sample?.simulation_scope }, project.id)
      navigate(`/project/${project.id}`, { state: { setupGuidance: project.setup_guidance } })
    }
    catch (error) { setError(error instanceof Error ? error.message : 'Could not open example.'); setOpening('') }
  }
  const selectClass = 'max-w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm'
  return <div className="dark min-h-svh bg-[#0a0a0a] text-neutral-100">
    <header className="flex h-20 items-center justify-between border-b border-neutral-800 px-5 sm:px-10"><Link to="/" className="flex items-center gap-3 text-xl font-semibold"><img src="/wireup-logo.png" alt="" className="size-9 rounded-lg" />WireUp</Link><Link to="/" className="rounded-lg border border-neutral-700 px-3 py-2 text-xs">Back to home</Link></header>
    <main className="mx-auto max-w-6xl px-5 py-10">
      <h1 className="text-3xl font-semibold">Example projects</h1>
      <p className="mt-3 text-sm text-neutral-400">Explore all {examples.length || 321} Velxio examples. Open editable copies with the original firmware in the same project runtime. Simulation-ready means all listed parts have board-specific models, not verified compilation or execution. Partial examples retain unsupported parts and dependencies.</p>
      <div className="my-6 flex flex-wrap gap-3">
        <input aria-label="Search examples" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search boards, circuits, sensors…" className="min-w-0 flex-1 rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm" />
        <select aria-label="Filter example board" value={board} onChange={event => setBoard(event.target.value)} className={selectClass}><option value="all">All boards</option>{[...new Set(examples.flatMap(example => example.boards))].sort().map(value => <option key={value}>{value}</option>)}</select>
        <select aria-label="Filter example category" value={category} onChange={event => setCategory(event.target.value)} className={selectClass}><option value="all">All categories</option>{[...new Set(examples.map(example => example.category))].sort().map(value => <option key={value}>{value}</option>)}</select>
        <select aria-label="Filter simulation scope" value={scope} onChange={event => setScope(event.target.value)} className={selectClass}><option value="all">All simulation scopes</option><option value="ready">Simulation-ready hardware</option><option value="partial">Partial simulation support</option><option value="blocked">Requirements only</option></select>
      </div>
      {error && <p role="alert" className="mb-4 text-sm text-red-300">{error}</p>}
      {loading ? <p role="status">Loading examples…</p> : <>
        <p className="mb-4 text-xs text-neutral-500">{visible.length} examples</p>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{visible.map(example => <article key={example.id} className="overflow-hidden rounded-xl border border-neutral-800 bg-neutral-900">
          <img src={example.thumbnail || '/wireup-logo.png'} alt="" loading="lazy" className="h-36 w-full bg-neutral-950 object-contain" />
          <div className="p-4">
            <h2 className="text-sm font-medium">{example.title}</h2>
            <p className="mt-2 break-words text-xs text-neutral-500">{example.boards.join(', ')} · {example.language} · {example.category}</p>
            <p className="mt-2 text-xs text-neutral-300">{example.simulation_scope === 'ready' ? 'Simulation-ready hardware · compile/run unverified' : example.available ? 'Partial simulation support' : 'Requirements only · cannot open safely'}</p>
            {example.available && <button disabled={!!opening} onClick={() => void open(example.id)} className="mt-4 rounded-lg bg-white px-3 py-2 text-xs text-black disabled:opacity-50">{opening === example.id ? 'Opening…' : example.starter ? 'Open starter' : 'Open example'}</button>}
            <details className="mt-4 text-xs text-neutral-400"><summary className="cursor-pointer">Requirements / simulation scope</summary><p className="mt-2">Compile the actual source with its required board core and libraries. Models cover only their documented scope; opening is not proof of a successful simulation.</p><pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words text-[11px]">{JSON.stringify({ dependencies: example.dependencies, missingCapabilities: example.blockers, partScopes: example.part_scopes }, null, 2)}</pre></details>
          </div>
        </article>)}</div>
        {!visible.length && <p className="py-10 text-sm text-neutral-500">No examples match these filters.</p>}
      </>}
    </main>
  </div>
}
