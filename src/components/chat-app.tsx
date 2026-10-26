import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { Menu, MessageSquare, Pencil, Plus, Trash2, X } from 'lucide-react'
import { GradientOrb } from './ui/gradient-orb'
import type { FeasibilityReport } from './ui/feasibility-card'
import { ProjectSetupCard } from './ui/project-setup-card'
import type { ProjectQuestions } from './ui/project-questions'
import { ChatComposer } from './ui/chat-composer'
import { AssistantThread } from './assistant-thread'
import { appendActivity } from './ui/agent-activity-feed'
import type { ActivityEvent } from './ui/agent-activity-feed'
import { isFeasibility, isQuestions, readChats, storageKey, streamReply } from '@/lib/chat'
import type { Conversation, Message } from '@/lib/chat'
import { projectTitle } from '@/lib/project-title'
import { defaultModels, modelOptionKey, readModelSelection, readSelectedModel } from '@/lib/model-selection'
import type { ModelOption, ModelProvider } from '@/lib/model-selection'
import { newRunId, recordBetaEvent } from '@/lib/beta'
import { apiUrl } from '@/lib/api'

export function ChatApp({ projectId, runtimeToken, embedded = false, initialPrompt, setupGuidance }: { projectId?: string; runtimeToken?: string; embedded?: boolean; initialPrompt?: string; setupGuidance?: string } = {}) {
  const chatStorageKey = embedded && projectId ? `wireup.project.${projectId}.chats.v1` : storageKey
  const [initial] = useState(() => {
    const saved = readChats(chatStorageKey)
    if (!saved.chats.length && setupGuidance) saved.chats = [{ id: crypto.randomUUID(), title: 'Getting started', updatedAt: Date.now(), messages: [{ id: crypto.randomUUID(), role: 'assistant', content: setupGuidance, status: 'done' }] }]
    return saved
  })
  const [chats, setChats] = useState(initial.chats)
  const [warning, setWarning] = useState(initial.warning)
  const [draft, setDraft] = useState(initialPrompt || '')
  const initialSent = useRef(false)
  const [drawer, setDrawer] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [health, setHealth] = useState<{ configured: boolean; model: string; providers?: ModelOption[] } | null>(null)
  const [provider, setProvider] = useState<ModelProvider>(readModelSelection)
  const [modelId, setModelId] = useState(readSelectedModel)
  const providerModels = health?.providers || defaultModels.map(option => option.id === 'groq' && health ? { ...option, model: health.model, configured: health.configured } : option)
  const models: ModelOption[] = providerModels.filter(option => option.configured || option.id === provider)
  const selectedModel = models.find(option => option.model === modelId) || models.find(option => option.id === provider) || providerModels[0]!
  const selectionKey = modelOptionKey(selectedModel)
  const changeModel = (key: string) => {
    const option = models.find(option => modelOptionKey(option) === key)
    if (!option) return
    setProvider(option.id)
    setModelId(option.model)
    try { localStorage.setItem('wireup.model-provider', option.id); localStorage.setItem('wireup.model-id', option.model) } catch { /* Selection remains available for this page. */ }
  }
  const selectedKeyName = { groq: 'GROQ_API_KEY', nvidia: 'NVIDIA_API_KEY', bedrock: 'BEDROCK_API_KEY', azure: 'AZURE_API_KEY', sarvam: 'SARVAM_API_KEY', openrouter: 'OPENROUTER_API_KEY' }[provider]
  const [connectionError, setConnectionError] = useState('')
  const [rename, setRename] = useState<{ id: string; value: string } | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [setup, setSetup] = useState<{ chatId: string; data: ProjectQuestions } | null>(null)
  const controller = useRef<AbortController | null>(null)
  const navigate = useNavigate()
  const location = useLocation()
  const { id: routeId } = useParams()
  const [embeddedId, setEmbeddedId] = useState<string | undefined>(() => initial.chats[0]?.id)
  const id = embedded ? embeddedId : routeId
  const active = chats.find(chat => chat.id === id)
  const home = !id

  useEffect(() => {
    if (initial.warning && chats.length === 0) return
    try {
      if (initial.warning && localStorage.getItem(`${chatStorageKey}.recovery`) === null) {
        const original = localStorage.getItem(chatStorageKey)
        if (original !== null) localStorage.setItem(`${chatStorageKey}.recovery`, original)
      }
      localStorage.setItem(chatStorageKey, JSON.stringify(chats.map(chat => ({ ...chat, messages: chat.messages.map(message => ({ ...message, blocks: message.blocks?.filter(block => block.type !== 'text' || block.channel !== 'thinking') })) })))) }
    catch { setWarning('Your browser could not save these chats. They will remain available until you close this page.') }
  }, [chats, chatStorageKey, initial.warning])

  const checkHealth = useCallback(async () => {
    try {
      const response = await fetch(apiUrl('/api/health'), { credentials: 'include', signal: AbortSignal.timeout(10_000) })
      if (!response.ok) throw new Error()
      const data = await response.json()
      if (typeof data.configured !== 'boolean' || typeof data.model !== 'string') throw new Error()
      setHealth(data)
      setConnectionError('')
    } catch {
      setConnectionError('The AI server is offline. Run npm run dev from the project folder, then check again.')
    }
  }, [])
  useEffect(() => { void checkHealth() }, [checkHealth])
  useEffect(() => () => controller.current?.abort(), [])
  useLayoutEffect(() => {
    if (!embedded) setDraft('')
    setDrawer(false)
  }, [location.pathname, embedded])

  const updateMessage = (chatId: string, messageId: string, update: (message: Message) => Message) => {
    setChats(previous => previous.map(chat => chat.id === chatId ? {
      ...chat, messages: chat.messages.map(message => message.id === messageId ? update(message) : message),
    } : chat))
  }

  const confirmedProjectRequirements = projectId ? [...chats].sort((a, b) => b.updatedAt - a.updatedAt).find(chat => chat.projectId === projectId && chat.requirements)?.requirements : undefined
  const generate = async (chat: Conversation, context: Message[], answers = (chat.projectId === projectId ? chat.requirements : undefined) || confirmedProjectRequirements, approval?: { assessment_id: string; choice: string }) => {
    if (controller.current) return
    const abort = new AbortController()
    controller.current = abort
    setBusy(chat.id)
    const runId = newRunId()
    const prompt = context.findLast(message => message.role === 'user')?.content || ''
    recordBetaEvent('chat.message_sent', {
      provider, model: selectedModel.model,
      prompt_preview: prompt.slice(0, 300), prompt_length: prompt.length,
      approval: Boolean(approval), message_count: context.length,
    }, projectId, { runId, chatId: chat.id })
    const reply: Message = { id: crypto.randomUUID(), role: 'assistant', content: '', status: 'streaming', blocks: [], startedAt: Date.now() }
    setChats(previous => {
      const updated = { ...chat, messages: [...context, reply], updatedAt: Date.now(), requirements: answers, projectId }
      return previous.some(item => item.id === chat.id)
        ? previous.map(item => item.id === chat.id ? updated : item)
        : [updated, ...previous].slice(0, 100)
    })
    let receivedBlocks: Message['blocks'] = []
    try {
      await streamReply(context, abort.signal, event => {
        if (abort.signal.aborted) return
        if (event.type === 'step_start' || event.type === 'step_end' || event.type === 'text') {
          const activityEvent = event as ActivityEvent
          receivedBlocks = appendActivity(receivedBlocks || [], activityEvent)
          const blocks = receivedBlocks
          if (activityEvent.type === 'step_end' && activityEvent.status === 'success' && projectId) window.dispatchEvent(new CustomEvent('wireup-project-updated', { detail: projectId }))
          updateMessage(chat.id, reply.id, message => ({ ...message,
            blocks,
            content: activityEvent.type === 'text' && activityEvent.channel !== 'narration' && activityEvent.channel !== 'thinking' ? message.content + activityEvent.text : message.content,
          }))

        } else if (event.type === 'delta' && 'text' in event && typeof event.text === 'string') {
          updateMessage(chat.id, reply.id, message => ({ ...message, content: message.content + event.text }))

        } else if (event.type === 'feasibility' && event.id && event.issues && event.choices) {
          if (!isFeasibility(event)) throw new Error('The backend returned an invalid project review.')
          const report = event as unknown as FeasibilityReport
          setChats(previous => previous.map(item => item.id === chat.id ? { ...item, messages: item.messages.map(message => message.id === reply.id ? { ...message, feasibility: report, feasibilityChoice: report.status === 'approved' ? undefined : item.messages.find(old => old.feasibility?.id === report.id)?.feasibilityChoice } : message.feasibility?.id === report.id ? { ...message, feasibility: undefined } : message) } : item))
        } else if (event.type === 'questions' && event.questions) {
          const data = { summary: event.summary || '', questions: event.questions! }
          if (!isQuestions(data)) throw new Error('The backend returned invalid project questions.')
          updateMessage(chat.id, reply.id, message => ({ ...message, questions: data }))
          setSetup({ chatId: chat.id, data })
        } else if (event.type === 'done') {
          if (event.status === 'error') updateMessage(chat.id, reply.id, message => {
            const notice = message.blocks?.findLast(block => block.type === 'text' && block.channel === 'narration')
            return { ...message, error: message.error || (notice?.type === 'text' ? notice.text : 'The agent could not complete this request. Check the activity details and retry.') }
          })
          updateMessage(chat.id, reply.id, message => ({ ...message, run: { status: event.status || 'success', reason: event.reason, changes: event.changes, successfulTools: event.successfulTools, toolCalls: event.toolCalls, modelCalls: event.modelCalls }, elapsedMs: event.elapsedMs, usage: event.usage || undefined, model: event.model || message.model }))
        } else if (event.type === 'notice') {
          setWarning(event.message || '')
        }
      }, { projectId, runtimeToken, answers, provider, model: selectedModel.model, approval, runId, chatId: chat.id })
      updateMessage(chat.id, reply.id, message => ({ ...message, status: message.error ? 'error' : message.run?.status === 'cancelled' ? 'stopped' : 'done', blocks: message.blocks?.map(block => block.type === 'step' && block.status === 'running' ? { ...block, status: 'stopped', output: 'The run ended without a completion event for this tool.' } : block) }))
    } catch (error) {
      updateMessage(chat.id, reply.id, message => ({
        ...message, run: { status: abort.signal.aborted ? 'interrupted' : 'error', reason: abort.signal.aborted ? 'local_cancel' : 'transport_error' }, blocks: message.blocks?.map(block => block.type === 'step' && block.status === 'running' ? { ...block, status: abort.signal.aborted ? 'stopped' : 'error', output: abort.signal.aborted ? 'Generation stopped.' : 'Connection ended before the step completed.' } : block), status: abort.signal.aborted ? 'stopped' : 'error', elapsedMs: Date.now() - (message.startedAt || Date.now()),
        error: abort.signal.aborted ? undefined : error instanceof Error ? error.message : 'Unable to generate a reply. Please try again.',
      }))
    } finally {
      if (controller.current === abort) {
        controller.current = null
        setBusy(null)
      }
    }
  }

  const send = (override?: string) => {
    const text = (override ?? draft).trim()
    if (!text || controller.current || (id && !active)) return
    const chat = active || { id: crypto.randomUUID(), title: projectTitle(text), messages: [], updatedAt: Date.now() }
    // Discard incomplete assistant turns before sending the next prompt.
    const context: Message[] = [...chat.messages.filter(message => message.role === 'user' || message.status === 'done'), {
      id: crypto.randomUUID(), role: 'user', content: text,
    }]
    setDraft('')
    if (!active) {
      if (embedded) setEmbeddedId(chat.id)
      else navigate(`/chat/${chat.id}`)
    }
    void generate(chat, context)
  }

  useEffect(() => {
    if (embedded && projectId && initialPrompt && !initialSent.current) {
      initialSent.current = true
      const state = { ...(location.state as Record<string, unknown> | null) }
      delete state.initialPrompt
      navigate(location.pathname, { replace: true, state })
      if (!chats.some(chat => chat.messages.some(message => message.role === 'user' && message.content === initialPrompt.trim()))) send(initialPrompt)
      else setDraft('')
    }
  }, [embedded, projectId, initialPrompt])

  const confirmRequirements = (answers: Record<string, string>) => {
    if (!active || busy) return
    const questionData = setup?.chatId === active.id ? setup.data : active.messages.findLast(item => item.questions)?.questions
    setSetup(null)
    const descriptions = questionData?.questions.map(question => `${question.question}: ${question.options.find(option => option.id === answers[question.id])?.label || (answers[question.id] === 'ai_choose' ? 'Let AI choose a suitable supported option' : answers[question.id])}`).join('\n') || ''
    const message: Message = { id: crypto.randomUUID(), role: 'user', hidden: true, content: 'Project requirements confirmed: ' + JSON.stringify(answers) + '\n' + descriptions + '\nBuild now using tools. Place and connect the circuit components, generate and compile firmware only if the prototype requires a microcontroller, and inspect available real results. Ask if a required capability is unavailable.' }
    void generate(active, [...active.messages.filter(item => item.role === 'user' || item.status === 'done'), message], answers)
  }

  const chooseFeasibility = (assessmentId: string, choice: string) => {
    if (!active || busy) return
    const reviewed: Conversation = { ...active, messages: active.messages.map(message => message.feasibility?.id === assessmentId ? { ...message, feasibilityChoice: choice } : message) }
    const message: Message = { id: crypto.randomUUID(), role: 'user', approval: { assessment_id: assessmentId, choice }, content: choice === 'hardware_only' ? 'Proceed with the reviewed project plan and its explicit limitations.' : choice === 'cancel' ? 'Cancel this build without changing the project.' : 'Return to planning; I want to revise this design.' }
    void generate(reviewed, [...reviewed.messages.filter(item => item.role === 'user' || item.status === 'done'), message], reviewed.requirements, { assessment_id: assessmentId, choice })
  }

  const retry = () => {
    if (!active || busy) return
    const lastUser = active.messages.findLastIndex(message => message.role === 'user')
    if (lastUser < 0) return
    const last = active.messages[lastUser]
    const answeredReview = active.messages.slice(0, lastUser).findLast(message => message.feasibility && message.feasibilityChoice)
    const approval = last.approval || (answeredReview?.feasibility && last.content.startsWith('Proceed with the reviewed') ? { assessment_id: answeredReview.feasibility.id, choice: answeredReview.feasibilityChoice! } : undefined)
    void generate(active, active.messages.slice(0, lastUser + 1).filter(message => message.role === 'user' || message.status === 'done'), active.requirements, approval)
  }

  const deleteChat = () => {
    if (!deleting) return
    if (busy === deleting) controller.current?.abort()
    setChats(previous => previous.filter(chat => chat.id !== deleting))
    if (id === deleting) {
      if (embedded) setEmbeddedId(undefined)
      else navigate('/assistant')
    }
    setDeleting(null)
  }

  const sidebar = (
    <>
      <div className="mb-8 flex items-center justify-between">
        <Link to="/" className="flex items-center gap-2 text-lg font-semibold tracking-tight"><img src="/wireup-logo.png" alt="" className="size-8 rounded-md object-contain" /> WireUp</Link>
        <button className="rounded-lg p-2 text-neutral-400 md:hidden" aria-label="Close sidebar" onClick={() => setDrawer(false)}><X size={20} /></button>
      </div>
      <Link to="/" className="mb-3 flex items-center gap-2 rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2.5 text-sm text-neutral-200 hover:border-neutral-600"><MessageSquare size={17} /> Back to WireUp</Link>
      <button onClick={() => { navigate('/assistant'); setDrawer(false); setDraft('') }} className="mb-8 flex w-full items-center gap-2 rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2.5 text-sm hover:border-neutral-600"><Plus size={17} /> New chat</button>
      <p className="mb-3 text-xs font-medium uppercase tracking-wider text-neutral-500">Your conversations</p>
      <div className="min-h-0 flex-1 overflow-y-auto space-y-1">
        {chats.length === 0 && <p className="px-2 py-4 text-sm leading-6 text-neutral-500">Your chats will appear here after your first message.</p>}
        {[...chats].sort((a, b) => b.updatedAt - a.updatedAt).map(chat => (
          <div key={chat.id} className={`group flex items-center rounded-lg ${id === chat.id ? 'bg-neutral-800' : 'hover:bg-neutral-900'}`}>
            <Link to={`/chat/${chat.id}`} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-3 text-sm text-neutral-300"><MessageSquare size={15} className="shrink-0 text-neutral-500" /><span className="truncate">{chat.title}</span>{busy === chat.id && <span aria-label="Backend request active" className="size-1.5 shrink-0 rounded-full bg-neutral-300" />}</Link>
            <button aria-label={`Rename ${chat.title}`} onClick={() => setRename({ id: chat.id, value: chat.title })} className="p-1.5 text-neutral-500 hover:text-white"><Pencil size={13} /></button>
            <button aria-label={`Delete ${chat.title}`} onClick={() => setDeleting(chat.id)} className="mr-1 p-1.5 text-neutral-500 hover:text-red-400"><Trash2 size={13} /></button>
          </div>
        ))}
      </div>
      <div className="mt-4 border-t border-neutral-800 pt-4 text-xs leading-5 text-neutral-500">
        <div className="flex items-center gap-2"><span className={`size-1.5 rounded-full ${health?.configured && !connectionError ? 'bg-emerald-400' : 'bg-amber-400'}`} />{health?.model || 'Groq · LangChain'}</div>
        <p className="mt-2">History is saved on this browser only.</p>
      </div>
    </>
  )

  return (
    <div className={`dark flex overflow-hidden text-neutral-100 ${embedded ? 'wireup-chat-embedded h-full min-h-0 w-full bg-transparent' : 'h-svh bg-[#0a0a0a]'}`}>
      {!embedded && <aside className="z-20 hidden w-64 shrink-0 flex-col border-r border-neutral-800/70 bg-[#101010] p-4 md:flex">{sidebar}</aside>}
      {drawer && <div className="fixed inset-0 z-40 md:hidden"><button aria-label="Close navigation" onClick={() => setDrawer(false)} className="absolute inset-0 bg-black/70" /><aside className="relative flex h-full w-[min(85vw,300px)] flex-col border-r border-neutral-800 bg-[#101010] p-4">{sidebar}</aside></div>}
      <main className="relative isolate flex min-h-0 min-w-0 flex-1 flex-col">
        {!embedded && <div aria-hidden="true" className={`pointer-events-none absolute inset-0 -z-10 ${home ? '' : 'opacity-15'}`}><GradientOrb /></div>}
        <header className={`flex h-12 shrink-0 items-center justify-between gap-3 border-b border-neutral-800/60 px-3 sm:px-4 ${embedded ? 'bg-transparent' : 'bg-[#0a0a0a]/70 backdrop-blur-md'}`}>
          <div className="flex min-w-0 items-center gap-3">{!embedded && <button onClick={() => setDrawer(true)} aria-label="Open sidebar" className="rounded-lg p-2 text-neutral-400 hover:text-white md:hidden"><Menu size={20} /></button>}<img src="/wireup-logo.png" alt="" className="size-5 shrink-0 rounded object-contain md:hidden" /><span className="truncate text-[13px] font-medium text-neutral-200">{active?.title || 'WireUp agent'}</span></div>
          {embedded ? <button onClick={() => { setEmbeddedId(undefined); setDraft('') }} className="flex shrink-0 items-center gap-1.5 rounded-md border border-neutral-800 px-2 py-1 text-xs text-neutral-400 hover:border-neutral-600 hover:text-neutral-200"><Plus size={13} /> New chat</button>
            : <span className="shrink-0 rounded-full border border-neutral-800 px-3 py-1 text-xs text-neutral-400">{selectedModel.label} <span className="hidden sm:inline">· LangGraph</span></span>}
        </header>
        {(warning || connectionError || health && !selectedModel.configured) && <div role="status" className="shrink-0 border-b border-amber-400/10 bg-[#211b11]/95 px-4 py-2 text-center text-xs leading-5 text-amber-200">
          {warning || connectionError || `Add ${selectedKeyName} to the project .env file, then retry to enable ${selectedModel.label} replies.`}
          <button onClick={() => { setWarning(''); void checkHealth() }} className="ml-3 underline underline-offset-2">Check again</button>
        </div>}
        {busy && busy !== id && <div role="status" className="shrink-0 border-b border-neutral-800 px-4 py-2 text-xs text-neutral-300">Another conversation is running. <button type="button" className="ml-2 underline" onClick={() => { if (embedded) setEmbeddedId(busy); else navigate(`/chat/${busy}`) }}>Open running conversation</button><button type="button" className="ml-3 underline" onClick={() => controller.current?.abort()}>Stop other conversation</button></div>}
        {id && !active ? <div className="flex flex-1 flex-col items-center justify-center gap-4 p-6 text-center"><MessageSquare className="text-neutral-500" size={32} /><h1 className="text-xl font-semibold">Conversation not found</h1><p className="text-sm text-neutral-400">This chat may have been deleted or saved in another browser.</p><Link to="/assistant" className="rounded-xl bg-white px-4 py-2 text-sm text-black">Start a new chat</Link></div>
          : home && embedded ? <div className="flex min-h-0 flex-1 flex-col justify-end gap-3 p-4">
              <div className="space-y-2">{['List compatible microcontrollers for simulation', 'Explain how to connect an LED to Arduino Uno', 'Compare Arduino Uno and ESP32 capabilities'].map(prompt => <button key={prompt} onClick={() => setDraft(prompt)} className="block w-full rounded-xl border border-neutral-800 bg-[#1b1c1f] px-4 py-2.5 text-left text-[13px] leading-5 text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-[#202127]">{prompt}</button>)}</div>
              <ChatComposer compact={embedded} provider={provider} models={models} selectionKey={selectionKey} onModelChange={changeModel} value={draft} onChange={setDraft} onSend={send} onStop={() => controller.current?.abort()} busy={!!busy} />
            </div>
          : home ? <div className="flex min-h-0 flex-1 flex-col justify-center overflow-y-auto px-4 py-10"><div className="mx-auto w-full max-w-3xl space-y-8"><div className="text-center"><h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">What can I help you build?</h1><p className="mt-3 text-sm text-neutral-400">Describe an idea, paste an error, or pick a starting point below.</p></div><ChatComposer compact={embedded} provider={provider} models={models} selectionKey={selectionKey} onModelChange={changeModel} value={draft} onChange={setDraft} onSend={send} onStop={() => controller.current?.abort()} busy={!!busy} /><div className="flex flex-wrap justify-center gap-2">{['Help me build a landing page', 'Explain a complex idea', 'Review my code', 'Brainstorm a project'].map(prompt => <button key={prompt} onClick={() => setDraft(prompt)} className="rounded-lg border border-neutral-800 bg-neutral-900/95 px-4 py-2 text-xs text-neutral-300 hover:border-neutral-600 hover:text-white">{prompt}</button>)}</div></div></div>
            : <AssistantThread key={active!.id} messages={active!.messages} busy={busy === active!.id} locked={!!busy && busy !== active!.id} draft={draft} onDraftChange={setDraft} onSend={send} onStop={() => controller.current?.abort()} onRetry={!busy ? retry : undefined} onOpenQuestions={message => { if (message.questions) setSetup({ chatId: active!.id, data: message.questions }) }} onFeasibility={chooseFeasibility} provider={provider} models={models} selectionKey={selectionKey} onModelChange={changeModel} compact={embedded}/> }
      {setup && setup.chatId === active?.id && <ProjectSetupCard data={setup.data} busy={!!busy} onConfirm={confirmRequirements} onClose={() => setSetup(null)}/>}
      </main>
      {rename && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"><form role="dialog" aria-modal="true" aria-labelledby="rename-title" onSubmit={event => { event.preventDefault(); if (!rename.value.trim()) return; setChats(previous => previous.map(chat => chat.id === rename.id ? { ...chat, title: rename.value.trim().slice(0, 80) } : chat)); setRename(null) }} className="w-full max-w-sm rounded-2xl border border-neutral-700 bg-neutral-900 p-6"><h2 id="rename-title" className="mb-4 text-lg font-semibold">Rename conversation</h2><input autoFocus aria-label="Conversation name" value={rename.value} maxLength={80} onChange={event => setRename({ ...rename, value: event.target.value })} className="w-full rounded-lg border border-neutral-600 bg-neutral-950 p-3 text-sm outline-none focus:border-neutral-500" /><div className="mt-5 flex justify-end gap-3"><button type="button" onClick={() => setRename(null)} className="px-3 py-2 text-sm text-neutral-400">Cancel</button><button type="submit" disabled={!rename.value.trim()} className="rounded-lg bg-white px-4 py-2 text-sm text-black disabled:opacity-40">Save</button></div></form></div>}
      {deleting && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"><div role="dialog" aria-modal="true" aria-labelledby="delete-title" className="w-full max-w-sm rounded-2xl border border-neutral-700 bg-neutral-900 p-6"><h2 id="delete-title" className="text-lg font-semibold">Delete conversation?</h2><p className="mt-3 text-sm text-neutral-400">This removes the conversation from this browser and cannot be undone.</p><div className="mt-5 flex justify-end gap-3"><button autoFocus onClick={() => setDeleting(null)} className="px-3 py-2 text-sm text-neutral-400">Cancel</button><button onClick={deleteChat} className="rounded-lg bg-red-500 px-4 py-2 text-sm text-white">Delete conversation</button></div></div></div>}
    </div>
  )
}
