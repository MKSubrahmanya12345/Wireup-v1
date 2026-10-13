import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { openSample } from '@/lib/open-sample'
import { ArrowUpRight, LoaderCircle } from 'lucide-react'
import './sample-projects.css'

type Sample = { id: string; title: string; category: string; boards: string[]; language: string; available: boolean; starter?: boolean; simulation_scope: 'ready' | 'partial' | 'blocked'; thumbnail?: string }
export function SampleProjects() {
  const [samples, setSamples] = useState<Sample[]>([])
  const [loading, setLoading] = useState(true)
  const [opening, setOpening] = useState('')
  const [error, setError] = useState('')
  const [catalogTotal, setCatalogTotal] = useState(0)
  const navigate = useNavigate()
  useEffect(() => {
    let active = true
    fetch('/api/hardware/samples/all').then(async response => {
      if (!response.ok) throw new Error('Starter projects are temporarily unavailable.')
      const result = await response.json()
      if (!Array.isArray(result.examples)) throw new Error('Starter project catalog returned an invalid response.')
      if (active) {
        setSamples(result.examples.filter((example: Sample) => example.available).sort((a: Sample, b: Sample) => Number(!!b.starter) - Number(!!a.starter)))
        setCatalogTotal(result.examples.length)
      }
    }).catch(error => { if (active) setError(error.message) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])
  const open = async (id: string) => {
    if (opening) return
    setOpening(id); setError('')
    try {
      const result = await openSample(id)
      navigate(`/project/${result.id}`, { state: { setupGuidance: result.setup_guidance } })
    } catch (error) { setError(error instanceof Error ? error.message : 'Could not open this sample.'); setOpening('') }
  }
  const sampleCard = (sample: Sample, duplicate = false) => <button key={sample.id} type="button" tabIndex={duplicate ? -1 : undefined} disabled={!!opening} onClick={() => void open(sample.id)} aria-label={`Open sample ${sample.title}`} className="sample-card group overflow-hidden rounded-xl border border-neutral-800 bg-neutral-900/95 text-left hover:border-neutral-500 disabled:opacity-60">
    <img src={sample.thumbnail || '/wireup-logo.png'} alt="" loading="lazy" className="h-36 w-full bg-neutral-950 object-contain" />
    <div className="p-4"><div className="flex items-center justify-between gap-2"><h3 className="text-sm font-medium">{sample.title}</h3>{opening === sample.id ? <LoaderCircle size={16} className="shrink-0 animate-spin" /> : <ArrowUpRight size={16} className="shrink-0 text-neutral-500" />}</div><p className="mt-2 text-xs leading-5 text-neutral-400">{sample.boards.join(', ')} · {sample.category} · {sample.language}</p><p className="mt-3 text-[11px] text-neutral-500">{sample.simulation_scope === 'ready' ? 'Simulation-ready hardware · compile/run unverified' : 'Editable project · partial simulation support'}</p></div>
  </button>
  return <section aria-label="Starter projects" className="sample-projects mt-14">
    <div className="mb-5"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-medium">Start from an example</h2><div className="sample-strip-actions"><Link to="/examples" className="rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-xs text-neutral-200">View all examples</Link></div></div><p className="mt-2 text-sm text-neutral-400">{loading ? 'Explore editable circuits with their original firmware.' : `${samples.length} available projects from the full ${catalogTotal}-example catalog. Each opens your own editable copy; simulation support is shown on every card.`}</p></div>
    {loading && <p role="status" className="text-xs text-neutral-500">Loading starter projects…</p>}
    {error && <p role="alert" className="mb-4 rounded-lg border border-red-400/20 bg-neutral-900 p-3 text-sm text-red-300">{error}</p>}
    {samples.length > 0 && <div className="sample-strip" role="region" aria-label="Scrolling starter examples" tabIndex={0} data-paused={!!opening} data-single={samples.length === 1}>
      <div className="sample-strip-track" style={{ animationDuration: `${Math.max(30, samples.length * 7)}s` }}>
        <div className="sample-strip-group">{samples.map(sample => sampleCard(sample))}</div>
        {samples.length > 1 && <div className="sample-strip-group sample-strip-duplicate" aria-hidden="true">{samples.map(sample => sampleCard(sample, true))}</div>}
      </div>
    </div>}
    {!loading && !error && !samples.length && <p className="text-sm text-neutral-500">No compatible starters are available yet.</p>}
  </section>
}
