import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowUp, CircuitBoard, LoaderCircle, Plus } from 'lucide-react'
import { SampleProjects } from './sample-projects'
import { ProjectList } from './project-list'
import { GradientOrb } from './ui/gradient-orb'
import { hardwareApi } from '@/lib/hardware'
import { projectTitle } from '@/lib/project-title'
import type { HardwareProjectSummary } from '@/lib/hardware'

export function PlatformHome() {
  const [prompt, setPrompt] = useState('')
  const [projects, setProjects] = useState<HardwareProjectSummary[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const navigate = useNavigate()
  useEffect(() => { hardwareApi.listProjects().then(result => setProjects(result.projects)).catch(() => setError('The hardware service is unavailable. Run npm run dev and refresh.')) }, [])
  const create = async (description = prompt.trim()) => {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const project = await hardwareApi.createProject(projectTitle(description))
      navigate(`/project/${project.id}`, { state: { initialPrompt: description } })
    } catch (error) { setError(error instanceof Error ? error.message : 'Could not create your workspace.'); setBusy(false) }
  }
  return <div className="nova-home dark relative isolate min-h-svh bg-[#0a0a0a] text-neutral-100">
    <div aria-hidden="true" className="pointer-events-none fixed inset-0 -z-10"><GradientOrb /></div>
    <header className="flex h-20 items-center justify-between border-b border-neutral-800/60 bg-neutral-950/60 px-5 backdrop-blur sm:px-10"><Link to="/" className="flex items-center gap-3 text-xl font-semibold"><img src="/wireup-logo.png" alt="" className="size-9 rounded-lg" />WireUp</Link><button onClick={() => void create('')} disabled={busy} className="flex items-center gap-2 rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-xs hover:border-neutral-500 disabled:opacity-50"><Plus size={15} />New prototype</button></header>
    <main className="mx-auto max-w-5xl px-5 pb-12 pt-20 sm:pt-28">
      <section className="mx-auto max-w-3xl text-center"><span className="inline-flex items-center gap-2 rounded-full border border-neutral-800 bg-neutral-950/70 px-3 py-1.5 text-xs text-neutral-300"><CircuitBoard size={14} />AI hardware prototyping</span><h1 className="mt-6 text-4xl font-semibold tracking-tight sm:text-5xl">What do you want to build?</h1><p className="mx-auto mt-4 max-w-xl text-sm leading-6 text-neutral-400">Describe your circuit. WireUp helps you place components, connect wires, write firmware, and test it in a real emulator.</p>
        <form onSubmit={event => { event.preventDefault(); if (prompt.trim()) void create() }} className="mt-8 rounded-2xl border border-neutral-700 bg-neutral-900 p-3 text-left shadow-2xl"><textarea aria-label="Describe your hardware prototype" placeholder="Build an Arduino circuit that blinks an LED and prints to the serial monitor…" value={prompt} maxLength={16000} onChange={event => setPrompt(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (prompt.trim()) void create() } }} className="min-h-24 w-full resize-y bg-transparent p-2 text-sm outline-none placeholder:text-neutral-500" /><div className="flex items-center justify-between"><span className="px-2 text-xs text-neutral-500">Circuit · Firmware · Simulation</span><button aria-label="Build prototype" disabled={busy || !prompt.trim()} className="rounded-lg bg-white p-2 text-black disabled:bg-neutral-800 disabled:text-neutral-500">{busy ? <LoaderCircle size={18} className="animate-spin" /> : <ArrowUp size={18} />}</button></div></form>
        {error && <p role="alert" className="mt-3 rounded-lg border border-red-400/20 bg-neutral-950 p-3 text-sm text-red-300">{error}</p>}
        <div className="mt-4 flex flex-wrap justify-center gap-2">{['Blink an LED with Arduino', 'Build a pushbutton-controlled LED', 'Calculate an LED resistor'].map(value => <button key={value} onClick={() => setPrompt(value)} className="rounded-full border border-neutral-800 bg-neutral-900/95 px-3 py-2 text-xs text-neutral-400 hover:text-white">{value}</button>)}</div>
      </section>
      <SampleProjects />
      <div className="mt-20"><ProjectList projects={projects} limit={5} onDeleted={ids => setProjects(previous => previous.filter(project => !ids.includes(project.id)))} /></div>
      <footer className="mt-12 text-center text-[11px] text-neutral-500">WireUp · Powered by LangGraph, Groq, and Velxio <a href="/velxio-license.txt" className="ml-2 underline">Open-source licenses</a></footer>
    </main>
  </div>
}
