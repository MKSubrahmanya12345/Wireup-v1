import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowUp, CircuitBoard, LoaderCircle, Plus } from 'lucide-react'
import { SampleProjects } from './sample-projects'
import { HowItWorks } from './how-it-works'
import './platform-home.css'
import { ProjectList } from './project-list'
import { GradientOrb } from './ui/gradient-orb'
import { hardwareApi } from '@/lib/hardware'
import { projectTitle } from '@/lib/project-title'
import { loadWorkspaceModule } from '@/lib/workspace-module'
import type { HardwareProjectSummary } from '@/lib/hardware'
import { recordBetaEvent } from '@/lib/beta'

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
    void loadWorkspaceModule().catch(() => {})
    try {
      recordBetaEvent('project_create_started', { has_prompt: Boolean(description) })
      const project = await hardwareApi.createProject(projectTitle(description))
      recordBetaEvent('project_created', { board: project.board, has_prompt: Boolean(description) }, project.id)
      navigate(`/project/${project.id}`, { state: { initialPrompt: description } })
    } catch (error) { setError(error instanceof Error ? error.message : 'Could not create your workspace.'); setBusy(false) }
  }
  return <div className="nova-home dark relative isolate min-h-svh bg-[#0a0a0a] text-neutral-100">
    <div aria-hidden="true" className="pointer-events-none fixed inset-0 -z-10"><GradientOrb /></div>
    <div className="home-grid-intro">
    <header className="flex h-14 items-center justify-between border-b border-neutral-800/60 bg-neutral-950/60 px-5 backdrop-blur sm:px-10"><Link to="/" className="flex items-center gap-3 text-xl font-semibold"><img src="/wireup-logo.png" alt="" className="size-9 rounded-lg" />WireUp</Link><button onClick={() => void create('')} disabled={busy} className="flex items-center gap-2 rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-xs hover:border-neutral-500 disabled:opacity-50"><Plus size={15} />New prototype</button></header>
    <main className="home-workbench mx-auto max-w-5xl px-5 pt-20 sm:pt-28">
      <div className="home-workbench-art" aria-hidden="true">
        <figure className="home-board home-board-uno"><img src="/home-art/arduino-uno-wireframe.png" alt="" /><figcaption>ARDUINO UNO / ATMEGA328P</figcaption></figure>
        <figure className="home-board home-board-esp"><img src="/home-art/esp32-wireframe.png" alt="" /><figcaption>ESP32 / DEVKIT</figcaption></figure>
        <svg className="home-connections" viewBox="0 0 1200 500" fill="none"><path d="M100 260C230 20 360 30 540 160S930 480 1110 180M110 300C270 490 430 380 630 230S940 10 1090 180M240 100L940 340M190 340L960 100M280 70L1020 280M150 250L910 60"/><path d="M80 300C200 80 320 10 600 100S990 400 1120 180M140 360C320 510 470 390 690 220S930 0 1090 120"/><g><circle cx="240" cy="100" r="4"/><circle cx="940" cy="340" r="4"/><circle cx="960" cy="100" r="4"/><circle cx="190" cy="340" r="4"/></g></svg>
        <svg className="home-schematic" viewBox="0 0 1200 260" fill="none"><path d="M50 150H150V80H250V150H350M850 150H950V70H1050V150H1150M150 150V210H320M900 150V210H1050M450 80H550V180H650V80H750"/><rect x="170" y="60" width="60" height="40"/><rect x="970" y="50" width="60" height="40"/><rect x="530" y="100" width="140" height="60"/><path d="M185 100V130M200 100V130M215 100V130M985 90V120M1000 90V120M1015 90V120M550 160V195M580 160V195M610 160V195M640 160V195"/></svg>
      </div>
      <section className="mx-auto max-w-3xl text-center"><span className="inline-flex items-center gap-2 rounded-full border border-neutral-800 bg-neutral-950/70 px-3 py-1.5 text-xs text-neutral-300"><CircuitBoard size={14} />AI hardware prototyping</span><h1 className="mt-6 text-4xl font-semibold tracking-tight sm:text-5xl">What do you want to build?</h1><p className="mx-auto mt-4 max-w-xl text-sm leading-6 text-neutral-400">Describe your circuit. WireUp helps you place components, connect wires, write firmware, and test it in a real emulator.</p>
        <form onSubmit={event => { event.preventDefault(); if (prompt.trim()) void create() }} className="mt-8 rounded-2xl border border-neutral-700 bg-neutral-900 p-3 text-left shadow-2xl"><textarea aria-label="Describe your hardware prototype" onFocus={() => { void loadWorkspaceModule().catch(() => {}) }} placeholder="Build an Arduino circuit that blinks an LED and prints to the serial monitor…" value={prompt} maxLength={16000} onChange={event => setPrompt(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (prompt.trim()) void create() } }} className="min-h-24 w-full resize-y bg-transparent p-2 text-sm outline-none placeholder:text-neutral-500" /><div className="flex items-center justify-between"><span className="px-2 text-xs text-neutral-500">Circuit · Firmware · Simulation</span><button aria-label="Build prototype" disabled={busy || !prompt.trim()} className="rounded-lg bg-white p-2 text-black disabled:bg-neutral-800 disabled:text-neutral-500">{busy ? <LoaderCircle size={18} className="animate-spin" /> : <ArrowUp size={18} />}</button></div></form>
        {error && <p role="alert" className="mt-3 rounded-lg border border-red-400/20 bg-neutral-950 p-3 text-sm text-red-300">{error}</p>}
        <div className="mt-4 flex flex-wrap justify-center gap-2">{['Blink an LED with Arduino', 'Build a pushbutton-controlled LED', 'Calculate an LED resistor'].map(value => <button key={value} onClick={() => setPrompt(value)} className="rounded-full border border-neutral-800 bg-neutral-900/95 px-3 py-2 text-xs text-neutral-400 hover:text-white">{value}</button>)}</div>
      </section>
    </main>
    </div>
    <SampleProjects />
    <HowItWorks />
    <div className="mx-auto max-w-5xl px-5 pb-12">
      <div className="mt-20"><ProjectList projects={projects} limit={5} onDeleted={ids => setProjects(previous => previous.filter(project => !ids.includes(project.id)))} /></div>
      <footer className="mt-12 text-center text-[11px] text-neutral-500">WireUp · Powered by LangGraph, Groq, and Velxio <a href="/velxio-license.txt" className="ml-2 underline">Open-source licenses</a></footer>
    </div>
  </div>
}
