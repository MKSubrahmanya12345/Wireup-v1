import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { ArrowDown, Check, Copy, Menu, MessageSquare, Pencil, Plus, RotateCcw, Trash2, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { GradientOrb } from './ui/gradient-orb'
import { FeasibilityCard } from './ui/feasibility-card'
import type { FeasibilityReport } from './ui/feasibility-card'
import { ProjectSetupCard } from './ui/project-setup-card'
import { AgentGears } from './ui/agent-gears'
import type { ProjectQuestions } from './ui/project-questions'
import { ChatComposer } from './ui/chat-composer'
import { AgentActivityFeed, appendActivity } from './ui/agent-activity-feed'
import type { ActivityEvent } from './ui/agent-activity-feed'
import { readChats, storageKey, streamReply } from '@/lib/chat'
import type { Conversation, Message } from '@/lib/chat'
import { useTypedMessage } from '@/lib/use-typed-message'
import { projectTitle } from '@/lib/project-title'
import { defaultModels, readModelSelection } from '@/lib/model-selection'
import type { ModelOption, ModelProvider } from '@/lib/model-selection'

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
  const models = health?.providers || defaultModels.map(option => option.id === 'groq' && health ? { ...option, model: health.model, configured: health.configured } : option)
  const selectedModel = models.find(option => option.id === provider) || defaultModels.find(option => option.id === provider)!
  const selectedKeyName = { groq: 'GROQ_API_KEY', nvidia: 'NVIDIA_API_KEY', bedrock: 'BEDROCK_API_KEY', azure: 'AZURE_API_KEY' }[provider]
  const changeProvider = (next: ModelProvider) => {
    setProvider(next)
    try { localStorage.setItem('wireup.model-provider', next) } catch { /* Selection remains available for this page. */ }
  }
  const [connectionError, setConnectionError] = useState('')
  const [rename, setRename] = useState<{ id: string; value: string } | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [atBottom, setAtBottom] = useState(true)
  const [setup, setSetup] = useState<{ chatId: string; data: ProjectQuestions } | null>(null)
  const controller = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()
  const location = useLocation()
  const { id: routeId } = useParams()
  const [embeddedId, setEmbeddedId] = useState<string | undefined>(() => initial.chats[0]?.id)
  const id = embedded ? embeddedId : routeId
  const active = chats.find(chat => chat.id === id)
  const home = !id

  useEffect(() => {
    try { localStorage.setItem(chatStorageKey, JSON.stringify(chats.map(chat => ({ ...chat, messages: chat.messages.map(message => ({ ...message, blocks: message.blocks?.filter(block => block.type !== 'text' || block.channel !== 'thinking') })) })))) }
    catch { setWarning('Your browser could not save these chats. They will remain available until you close this page.') }
  }, [chats, chatStorageKey])

  const checkHealth = useCallback(async () => {
    try {
      const response = await fetch('/api/health', { signal: AbortSignal.timeout(10_000) })
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
    setAtBottom(true)
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [location.pathname, embedded])
  useEffect(() => {
    const node = scrollRef.current
    if (node && node.scrollHeight - node.scrollTop - node.clientHeight >= 80) setAtBottom(false)
  }, [active?.messages])

  const updateMessage = (chatId: string, messageId: string, update: (message: Message) => Message) => {
    setChats(previous => previous.map(chat => chat.id === chatId ? {
      ...chat, messages: chat.messages.map(message => message.id === messageId ? update(message) : message),
    } : chat))
  }

  const generate = async (chat: Conversation, context: Message[], answers = chat.projectId === projectId ? chat.requirements : undefined, approval?: { assessment_id: string; choice: string }) => {
    if (controller.current) return
    const abort = new AbortController()
    controller.current = abort
    setBusy(chat.id)
    const reply: Message = { id: crypto.randomUUID(), role: 'assistant', content: '', status: 'streaming', blocks: [], startedAt: Date.now() }
    setChats(previous => {
      const updated = { ...chat, messages: [...context, reply], updatedAt: Date.now(), requirements: answers, projectId }
      return previous.some(item => item.id === chat.id)
        ? previous.map(item => item.id === chat.id ? updated : item)
        : [updated, ...previous].slice(0, 100)
    })
    try {
      await streamReply(context, abort.signal, event => {
        if (abort.signal.aborted) return
        if (event.type === 'step_start' || event.type === 'step_end' || event.type === 'text') {
          const activityEvent = event as ActivityEvent
          if (activityEvent.type === 'step_end' && activityEvent.status === 'success' && projectId) window.dispatchEvent(new CustomEvent('wireup-project-updated', { detail: projectId }))
          updateMessage(chat.id, reply.id, message => ({ ...message,
            blocks: appendActivity(message.blocks || [], activityEvent),
            content: activityEvent.type === 'text' && activityEvent.channel !== 'narration' && activityEvent.channel !== 'thinking' ? message.content + activityEvent.text : message.content,
          }))

        } else if (event.type === 'delta' && 'text' in event && typeof event.text === 'string') {
          updateMessage(chat.id, reply.id, message => ({ ...message, content: message.content + event.text }))

        } else if (event.type === 'feasibility' && event.id && event.issues && event.choices) {
          const report = event as unknown as FeasibilityReport
          setChats(previous => previous.map(item => item.id === chat.id ? { ...item, messages: item.messages.map(message => message.id === reply.id ? { ...message, feasibility: report, feasibilityChoice: report.status === 'approved' ? undefined : item.messages.find(old => old.feasibility?.id === report.id)?.feasibilityChoice } : message.feasibility?.id === report.id ? { ...message, feasibility: undefined } : message) } : item))
        } else if (event.type === 'questions' && event.questions) {
          const data = { summary: event.summary || '', questions: event.questions! }
          updateMessage(chat.id, reply.id, message => ({ ...message, questions: data }))
          setSetup({ chatId: chat.id, data })
        } else if (event.type === 'done') {
          if (event.status === 'error') updateMessage(chat.id, reply.id, message => {
            const notice = message.blocks?.findLast(block => block.type === 'text' && block.channel === 'narration')
            return { ...message, error: message.error || (notice?.type === 'text' ? notice.text : 'The agent could not complete this request. Check the activity details and retry.') }
          })
          updateMessage(chat.id, reply.id, message => ({ ...message, elapsedMs: event.elapsedMs, usage: event.usage || undefined, model: event.model || message.model }))
        } else if (event.type === 'notice') {
          setWarning(event.message || '')
        }
      }, { projectId, runtimeToken, answers, provider, approval })
      updateMessage(chat.id, reply.id, message => ({ ...message, status: message.error ? 'error' : 'done' }))
    } catch (error) {
      updateMessage(chat.id, reply.id, message => ({
        ...message, blocks: message.blocks?.map(block => block.type === 'step' && block.status === 'running' ? { ...block, status: abort.signal.aborted ? 'stopped' : 'error', output: abort.signal.aborted ? 'Generation stopped.' : 'Connection ended before the step completed.' } : block), status: abort.signal.aborted ? 'stopped' : 'error', elapsedMs: Date.now() - (message.startedAt || Date.now()),
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
    setAtBottom(true)
    if (!active) {
      if (embedded) setEmbeddedId(chat.id)
      else navigate(`/chat/${chat.id}`)
    }
    void generate(chat, context)
  }

  useEffect(() => {
    if (embedded && projectId && initialPrompt && !initialSent.current && selectedModel.configured) {
      initialSent.current = true
      send()
    }
  })

  const confirmRequirements = (answers: Record<string, string>) => {
    if (!active || busy) return
    const questionData = setup?.chatId === active.id ? setup.data : active.messages.findLast(item => item.questions)?.questions
    setSetup(null)
    const descriptions = questionData?.questions.map(question => `${question.question}: ${question.options.find(option => option.id === answers[question.id])?.label || (answers[question.id] === 'ai_choose' ? 'Let AI choose a suitable supported option' : answers[question.id])}`).join('\n') || ''
    const message: Message = { id: crypto.randomUUID(), role: 'user', hidden: true, content: 'Project requirements confirmed: ' + JSON.stringify(answers) + '\n' + descriptions + '\nBuild now using tools. Place and connect the circuit components, generate and compile firmware only if the prototype requires a microcontroller, and inspect available real results. Ask if a required capability is unavailable.' }
    void generate(active, [...active.messages.filter(item => item.role === 'user' || (item.status === 'done' && item.content)), message], answers)
    setAtBottom(true)
  }

  const chooseFeasibility = (assessmentId: string, choice: string) => {
    if (!active || busy) return
    const reviewed: Conversation = { ...active, messages: active.messages.map(message => message.feasibility?.id === assessmentId ? { ...message, feasibilityChoice: choice } : message) }
    const message: Message = { id: crypto.randomUUID(), role: 'user', content: choice === 'hardware_only' ? 'Proceed with the reviewed project plan and its explicit limitations.' : choice === 'cancel' ? 'Cancel this build without changing the project.' : 'Return to planning; I want to revise this design.' }
    void generate(reviewed, [...reviewed.messages.filter(item => item.role === 'user' || item.status === 'done'), message], reviewed.requirements, { assessment_id: assessmentId, choice })
  }

  const retry = () => {
    if (!active || busy) return
    const lastUser = active.messages.findLastIndex(message => message.role === 'user')
    if (lastUser < 0) return
    const last = active.messages[lastUser]
    const answeredReview = active.messages.slice(0, lastUser).findLast(message => message.feasibility && message.feasibilityChoice)
    const approval = answeredReview?.feasibility && last.content.startsWith('Proceed with the reviewed') ? { assessment_id: answeredReview.feasibility.id, choice: answeredReview.feasibilityChoice! } : undefined
    void generate(active, active.messages.slice(0, lastUser + 1).filter(message => message.role === 'user' || message.status === 'done'), active.requirements, approval)
    setAtBottom(true)
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
            <Link to={`/chat/${chat.id}`} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-3 text-sm text-neutral-300"><MessageSquare size={15} className="shrink-0 text-neutral-500" /><span className="truncate">{chat.title}</span>{busy === chat.id && <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-neutral-300" />}</Link>
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
        {id && !active ? <div className="flex flex-1 flex-col items-center justify-center gap-4 p-6 text-center"><MessageSquare className="text-neutral-500" size={32} /><h1 className="text-xl font-semibold">Conversation not found</h1><p className="text-sm text-neutral-400">This chat may have been deleted or saved in another browser.</p><Link to="/assistant" className="rounded-xl bg-white px-4 py-2 text-sm text-black">Start a new chat</Link></div>
          : home && embedded ? <div className="flex min-h-0 flex-1 flex-col justify-end gap-3 p-4">
              <div className="space-y-2">{['List compatible microcontrollers for simulation', 'Explain how to connect an LED to Arduino Uno', 'Compare Arduino Uno and ESP32 capabilities'].map(prompt => <button key={prompt} onClick={() => setDraft(prompt)} className="block w-full rounded-xl border border-neutral-800 bg-[#1b1c1f] px-4 py-2.5 text-left text-[13px] leading-5 text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-[#202127]">{prompt}</button>)}</div>
              <ChatComposer compact={embedded} provider={provider} models={models} onProviderChange={changeProvider} value={draft} onChange={setDraft} onSend={send} onStop={() => controller.current?.abort()} busy={!!busy} />
            </div>
          : home ? <div className="flex min-h-0 flex-1 flex-col justify-center overflow-y-auto px-4 py-10"><div className="mx-auto w-full max-w-3xl space-y-8"><div className="text-center"><h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">What can I help you build?</h1><p className="mt-3 text-sm text-neutral-400">Describe an idea, paste an error, or pick a starting point below.</p></div><ChatComposer compact={embedded} provider={provider} models={models} onProviderChange={changeProvider} value={draft} onChange={setDraft} onSend={send} onStop={() => controller.current?.abort()} busy={!!busy} /><div className="flex flex-wrap justify-center gap-2">{['Help me build a landing page', 'Explain a complex idea', 'Review my code', 'Brainstorm a project'].map(prompt => <button key={prompt} onClick={() => setDraft(prompt)} className="rounded-lg border border-neutral-800 bg-neutral-900/95 px-4 py-2 text-xs text-neutral-300 hover:border-neutral-600 hover:text-white">{prompt}</button>)}</div></div></div>
            : <>
              <div ref={scrollRef} onScroll={event => {
                const node = event.currentTarget
                setAtBottom(node.scrollHeight - node.scrollTop - node.clientHeight < 100)
              }} className={`min-h-0 flex-1 overflow-y-auto ${embedded ? 'px-3 py-3' : 'px-4 py-8 sm:px-6'}`}>
                <div className="mx-auto max-w-3xl space-y-6" role="log" aria-label="Conversation">
                  {active!.messages.map((message, index) => message.hidden ? null : <MessageView key={message.id} message={message} onOpenQuestions={() => { if (message.questions) setSetup({chatId:active!.id,data:message.questions}) }} onFeasibility={chooseFeasibility} busy={!!busy} retry={!busy && index === active!.messages.length - 1 && message.role === 'assistant' ? retry : undefined} />)}
                </div>
              </div>
              {!atBottom && <button onClick={() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }); setAtBottom(true) }} className="absolute bottom-44 right-6 rounded-full border border-neutral-700 bg-neutral-900 p-3 shadow-lg" aria-label="Scroll to latest message"><ArrowDown size={18} /></button>}
              <div className={`shrink-0 bg-gradient-to-t ${embedded ? 'px-2 pt-2 pb-1 from-[#17181c] via-[#17181c]/95' : 'px-4 pt-4 pb-3 sm:px-6 from-[#0a0a0a] via-[#0a0a0a]/95'}`}><div className="mx-auto max-w-3xl"><ChatComposer compact={embedded} provider={provider} models={models} onProviderChange={changeProvider} value={draft} onChange={setDraft} onSend={send} onStop={() => controller.current?.abort()} busy={!!busy} /><p className="mt-2 text-center text-[10px] text-neutral-600">AI can make mistakes.</p></div></div>
            </>}
      {setup && setup.chatId === active?.id && <ProjectSetupCard data={setup.data} busy={!!busy} onConfirm={confirmRequirements} onClose={() => setSetup(null)}/>}
      </main>
      {rename && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"><form role="dialog" aria-modal="true" aria-labelledby="rename-title" onSubmit={event => { event.preventDefault(); if (!rename.value.trim()) return; setChats(previous => previous.map(chat => chat.id === rename.id ? { ...chat, title: rename.value.trim().slice(0, 80) } : chat)); setRename(null) }} className="w-full max-w-sm rounded-2xl border border-neutral-700 bg-neutral-900 p-6"><h2 id="rename-title" className="mb-4 text-lg font-semibold">Rename conversation</h2><input autoFocus aria-label="Conversation name" value={rename.value} maxLength={80} onChange={event => setRename({ ...rename, value: event.target.value })} className="w-full rounded-lg border border-neutral-600 bg-neutral-950 p-3 text-sm outline-none focus:border-neutral-500" /><div className="mt-5 flex justify-end gap-3"><button type="button" onClick={() => setRename(null)} className="px-3 py-2 text-sm text-neutral-400">Cancel</button><button type="submit" disabled={!rename.value.trim()} className="rounded-lg bg-white px-4 py-2 text-sm text-black disabled:opacity-40">Save</button></div></form></div>}
      {deleting && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"><div role="dialog" aria-modal="true" aria-labelledby="delete-title" className="w-full max-w-sm rounded-2xl border border-neutral-700 bg-neutral-900 p-6"><h2 id="delete-title" className="text-lg font-semibold">Delete conversation?</h2><p className="mt-3 text-sm text-neutral-400">This removes the conversation from this browser and cannot be undone.</p><div className="mt-5 flex justify-end gap-3"><button autoFocus onClick={() => setDeleting(null)} className="px-3 py-2 text-sm text-neutral-400">Cancel</button><button onClick={deleteChat} className="rounded-lg bg-red-500 px-4 py-2 text-sm text-white">Delete conversation</button></div></div></div>}
    </div>
  )
}

function MessageView({ message: originalMessage, retry, onOpenQuestions, onFeasibility, busy }: { message: Message; retry?: () => void; onOpenQuestions: () => void; onFeasibility: (id: string, choice: string) => void; busy: boolean }) {
  const message = useTypedMessage(originalMessage)
  const [copied, setCopied] = useState(false)
  const [copyError, setCopyError] = useState('')
  return (
    <article aria-label={`${message.role === 'user' ? 'You' : 'WireUp'} message`} className={message.role === 'user' ? 'ml-auto max-w-[88%] rounded-xl border border-neutral-800 bg-neutral-800/40 px-3.5 py-2.5' : 'wireup-assistant-message min-w-0'}>
      <div className="mb-1.5 flex items-center gap-2 text-[11px] font-medium text-neutral-500">{message.role === 'assistant' && <img src="/wireup-logo.png" alt="" className="size-4 rounded object-contain" />}{message.role === 'user' ? 'You' : 'WireUp'}</div>
      {message.feasibility && <FeasibilityCard report={message.feasibility} onChoose={onFeasibility} busy={busy} selected={message.feasibilityChoice} />}
      {message.questions && <button type="button" onClick={onOpenQuestions} disabled={busy} className="my-3 rounded-lg border border-neutral-700 px-4 py-2 text-xs text-neutral-200">Open project setup · {message.questions.questions.length} questions</button>}
      {message.role === 'assistant' && message.blocks?.length ? <AgentActivityFeed blocks={message.blocks} running={message.status === 'streaming'} outcome={message.status === 'streaming' ? undefined : message.status} startedAt={message.startedAt} elapsedMs={message.elapsedMs} /> : null}
      {message.role === 'user' ? <p className="whitespace-pre-wrap break-words text-sm leading-6">{message.content}</p> : !message.blocks?.length ? <div className="chat-markdown text-sm leading-6"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}>{message.content}</ReactMarkdown></div> : null}
      {message.status === 'streaming' && <div role="status" className="mt-2 flex items-center gap-2 text-xs text-neutral-400" aria-label="Generating response"><AgentGears/><span>Agent is working. You can stop generation.</span></div>}
      {message.error && <p role="alert" className="mt-3 rounded-lg border border-red-400/20 bg-red-400/5 p-3 text-sm text-red-300">{message.error}</p>}
      {message.status === 'stopped' && <p className="mt-2 text-xs text-neutral-500">Response stopped.</p>}
      {message.role === 'assistant' && message.status !== 'streaming' && <div className="mt-3 flex items-center gap-3 text-neutral-500">
        {message.content && <button aria-label="Copy response" onClick={async () => { try { await navigator.clipboard.writeText(originalMessage.content); setCopied(true); setCopyError(''); setTimeout(() => setCopied(false), 2000) } catch { setCopyError('Copy failed. Select the response text to copy it.') } }} className="flex items-center gap-1.5 text-xs hover:text-white">{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? 'Copied' : 'Copy'}</button>}
        {retry && <button onClick={retry} className="flex items-center gap-1.5 text-xs hover:text-white"><RotateCcw size={14} />{message.status === 'done' ? 'Regenerate' : 'Retry'}</button>}
      </div>}
      {copyError && <p role="status" className="mt-2 text-xs text-amber-300">{copyError}</p>}
    </article>
  )
}
