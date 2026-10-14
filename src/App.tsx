import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { CircuitBoard } from 'lucide-react'
import { hardwareApi, isHardwareProject } from '@/lib/hardware'
import { loadWorkspaceModule } from '@/lib/workspace-module'
import type { CatalogComponent, HardwareProject } from '@/lib/hardware'
import type { HardwareRuntime, RuntimeResults } from '@/hardware/runtime'
import type { AnalogSolveResult } from '@/hardware/spice'
import { BrowserRouter, Route, Routes, useLocation } from 'react-router-dom'
import { ChatApp } from '@/components/chat-app'
import { AllProjects } from '@/components/all-projects'
import { ExamplesGallery } from '@/components/examples-gallery'
import { PlatformHome } from '@/components/platform-home'
import { WorkspaceLoading } from '@/components/ui/workspace-loading'
import './ui-theme.css'

const Preview = lazy(() => import('@/components/demo'))
const SchematicStudio = lazy(() => import('@/components/schematic-studio').then(module => ({ default: module.SchematicStudio })))
const HardwareInstruments = lazy(() => import('@/components/hardware-instruments').then(module => ({ default: module.HardwareInstruments })))
const HardwareWorkspace = lazy(() => loadWorkspaceModule().then(module => ({ default: module.HardwareWorkspace })))

function ProjectPage() {
  const [project, setProject] = useState<HardwareProject | null>(null)
  const location = useLocation()
  const initialPrompt = (location.state as { initialPrompt?: string } | null)?.initialPrompt
  const setupGuidance = (location.state as { setupGuidance?: string } | null)?.setupGuidance
  const onProjectChange = useCallback((next: HardwareProject | null) => setProject(next), [])
  const [runtime, setRuntime] = useState<HardwareRuntime | null>(null)
  const [results, setResults] = useState<RuntimeResults | null>(null)
  const [view, setView] = useState<'circuit' | 'schematic'>('circuit')
  const [analog, setAnalog] = useState<AnalogSolveResult | null>(null)
  const [solving, setSolving] = useState(false)
  const [solveError, setSolveError] = useState('')
  const [catalog, setCatalog] = useState<CatalogComponent[]>([])
  const [selected, setSelected] = useState<string>()
  useEffect(() => { hardwareApi.catalog('', 200).then(result => setCatalog(result.components)).catch(() => {}) }, [])
  const solve = async (analysis: 'dc' | 'transient') => {
    if (!project || solving) return
    setSolving(true); setSolveError(''); setView('schematic')
    try {
      const { solveAnalog } = await import('@/hardware/spice')
      setAnalog(await solveAnalog(project, { analysis }))
    } catch (error) { setSolveError(error instanceof Error ? error.message : 'Analog solve failed.') }
    finally { setSolving(false) }
  }
  const mutate = async (name: 'modify_component' | 'connect_wire' | 'add_component', args: Record<string, unknown>) => {
    if (!project || results?.running) return
    await hardwareApi.command(project, name, { ...args, expected_revision: project.revision })
    setProject(await hardwareApi.getProject(project.id))
    window.dispatchEvent(new CustomEvent('wireup-project-updated', { detail: project.id }))
  }
  const undo = async () => {
    if (!project || results?.running) return
    const next = await hardwareApi.command<HardwareProject>(project, 'undo', { expected_revision: project.revision })
    setProject(isHardwareProject(next) ? next : await hardwareApi.getProject(project.id))
    window.dispatchEvent(new CustomEvent('wireup-project-updated', { detail: project.id }))
  }
  const addComponent = async (type: string) => {
    const count = project?.components.length ?? 0
    await mutate('add_component', { type, x: 420 + (count % 4) * 300, y: 140 + Math.floor(count / 4) * 230 })
  }
  const reload = async () => {
    if (!project) return
    setProject(await hardwareApi.getProject(project.id))
  }
  return <Suspense fallback={<div className="flex h-svh"><WorkspaceLoading/></div>}><HardwareWorkspace onProjectChange={onProjectChange} onRuntimeChange={setRuntime} onResultsChange={setResults} view={view} onViewChange={setView}
    analysisSlot={<><button disabled={solving || !project} onClick={() => void solve('dc')}>{solving ? 'Solving…' : 'DC Bias'}</button><button disabled={solving || !project} onClick={() => void solve('transient')}>Transient</button></>}
    schematicSlot={project ? <Suspense fallback={<div className="hw-schematic-empty"><CircuitBoard size={30} /><h2>Schematic view</h2><p>Loading the schematic studio…</p></div>}><SchematicStudio project={project} catalog={catalog} analog={analog} solving={solving} onSolve={solve}
      onMove={(id, x, y) => mutate('modify_component', { id, x, y })} onAddComponent={addComponent} onTune={(id, properties) => mutate('modify_component', { id, properties })}
      onUndo={undo} onReload={() => void reload()} selectedId={selected} onSelect={setSelected} onConnect={(from, to) => mutate('connect_wire', { from, to })}
      runtime={runtime} results={results} solveError={solveError} /></Suspense> : null}
    oscilloscopeSlot={<Suspense fallback={null}><HardwareInstruments runtime={runtime} results={results} analogResult={analog?.projectRevision === project?.revision ? analog : null} /></Suspense>}
    chatSlot={<ChatApp key={project?.id || 'none'} embedded initialPrompt={initialPrompt} setupGuidance={setupGuidance} projectId={project?.id} runtimeToken={project?.runtime_token} />} /></Suspense>
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/orb" element={<Suspense fallback={<div className="h-svh bg-[#0a0a0a]" />}><Preview /></Suspense>} />
        <Route path="/" element={<PlatformHome />} />
        <Route path="/chat/:id" element={<ChatApp />} />
        <Route path="/projects" element={<AllProjects />} />
        <Route path="/examples" element={<ExamplesGallery />} />
        <Route path="/assistant" element={<ChatApp />} />
        <Route path="/project/:projectId" element={<ProjectPage />} />
        <Route path="*" element={<div className="flex h-svh items-center justify-center bg-neutral-950 text-white"><a href="/">Page not found. Return to WireUp.</a></div>} />
      </Routes>
    </BrowserRouter>
  )
}
