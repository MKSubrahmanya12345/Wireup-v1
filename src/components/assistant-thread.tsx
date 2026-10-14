import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  useExternalStoreRuntime,
} from '@assistant-ui/react'
import type {
  MessageStatus,
  ReasoningMessagePartProps,
  TextMessagePartProps,
  ThreadMessageLike,
  ToolCallMessagePart,
  ToolCallMessagePartProps,
} from '@assistant-ui/react'
import { ArrowDown, ArrowUp, Check, ChevronRight, Copy, RotateCcw, Square } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Message } from '@/lib/chat'
import { ChatModelPicker } from './ui/chat-model-picker'
import { AgentGears } from './ui/agent-gears'
import type { ModelOption, ModelProvider } from '@/lib/model-selection'
import { FeasibilityCard } from './ui/feasibility-card'
import type { ActivityBlock } from './ui/agent-activity-feed'
import './assistant-thread.css'

export type AssistantRun = {
  status: string
  reason?: string
  changes?: Record<string, number>
  toolCalls?: number
  modelCalls?: number
  successfulTools?: Record<string, number>
}

type ActivityStep = Extract<ActivityBlock, { type: 'step' }> & {
  toolName?: string
  args?: Record<string, unknown>
  result?: unknown
}
type AssistantMessage = Message & { run?: AssistantRun }

export type AssistantThreadProps = {
  messages: Message[]
  busy: boolean
  locked?: boolean
  draft: string
  onDraftChange: (value: string) => void
  onSend: (text: string) => void | Promise<void>
  onStop: () => void | Promise<void>
  onRetry?: () => void | Promise<void>
  onOpenQuestions: (message: Message) => void
  onFeasibility: (id: string, choice: string) => void
  provider?: ModelProvider
  models?: ModelOption[]
  selectionKey?: string
  onModelChange?: (key: string) => void
  compact?: boolean
}

const ThreadContext = createContext<AssistantThreadProps | null>(null)

function useThreadProps() {
  const value = useContext(ThreadContext)
  if (!value) throw new Error('Assistant thread content requires AssistantThread.')
  return value
}

function useOriginalMessage() {
  const id = useAuiState(state => state.message.id)
  return useThreadProps().messages.find(message => message.id === id) as AssistantMessage | undefined
}

function isActive(status: string) {
  return ['running', 'streaming', 'in_progress'].includes(status)
}

function isStopped(status: string) {
  return ['stopped', 'cancelled', 'canceled'].includes(status)
}

function isFailed(status: string) {
  return ['error', 'failed', 'failure'].includes(status)
}

function messageStatus(message: AssistantMessage): MessageStatus {
  const status = message.run?.status || message.status || 'done'
  if (isActive(status)) return { type: 'running' }
  if (isStopped(status)) return { type: 'incomplete', reason: 'cancelled' }
  if (isFailed(status)) return { type: 'incomplete', reason: 'error', error: message.error || message.run?.reason }
  if (['awaiting_approval', 'awaiting_revision', 'awaiting_answers', 'awaiting_questions', 'requires_action'].includes(status)) {
    return { type: 'requires-action', reason: 'interrupt' }
  }
  if (['blocked', 'interrupted', 'incomplete', 'limit_reached'].includes(status)) return { type: 'incomplete', reason: 'other' }
  return { type: 'complete', reason: 'stop' }
}

function toolArgs(step: ActivityStep): ToolCallMessagePart['args'] {
  // Runtime arguments are JSON; legacy command strings stay available as inputs.
  if (step.args) {
    try { return JSON.parse(JSON.stringify(step.args)) as ToolCallMessagePart['args'] } catch { /* use the legacy command */ }
  }
  return step.command ? { command: step.command } : {}
}

function convertMessage(message: AssistantMessage): ThreadMessageLike {
  if (message.role === 'user') return { id: message.id, role: 'user', content: message.content }
  const content: Exclude<ThreadMessageLike['content'], string>[number][] = []
  let answer = ''
  for (const block of message.blocks || []) {
    if (block.type === 'text') {
      if (!block.text) continue
      const process = block.channel === 'narration' || block.channel === 'thinking'
      content.push({ type: process ? 'reasoning' : 'text', id: block.id, text: block.text })
      if (!process) answer += block.text
      continue
    }
    const step = block as ActivityStep
    const args = toolArgs(step)
    const hasResult = step.result !== undefined || step.status !== 'running' || !!step.output
    content.push({
      type: 'tool-call', toolCallId: step.id, toolName: step.toolName || step.label,
      args, argsText: JSON.stringify(args),
      ...(hasResult ? { result: step.result !== undefined ? step.result : step.output } : {}),
      isPreliminary: step.status === 'running' && hasResult,
      isError: step.status === 'error' || step.status === 'stopped',
    })
  }
  const remainingAnswer = !answer ? message.content : message.content.startsWith(answer) ? message.content.slice(answer.length) : message.content
  if (remainingAnswer) content.push({ type: 'text', id: `${message.id}-answer`, text: remainingAnswer })
  return {
    id: message.id, role: 'assistant', content, status: messageStatus(message),
    ...(message.startedAt ? { createdAt: new Date(message.startedAt) } : {}),
  }
}

export function AssistantThread(props: AssistantThreadProps) {
  const messages = useMemo(() => props.messages.filter(message => !message.hidden), [props.messages])
  const runtime = useExternalStoreRuntime<Message>({
    messages,
    convertMessage,
    isRunning: props.busy,
    isSendDisabled: props.busy || props.locked,
    // All tools execute on the backend, including result-less calls in flight.
    unstable_enableToolInvocations: false,
    onNew: async message => {
      const text = message.content.filter(part => part.type === 'text').map(part => part.text).join('\n').trim()
      if (text && !props.busy && !props.locked) await props.onSend(text)
    },
    onCancel: async () => { await props.onStop() },
  })
  return <ThreadContext.Provider value={props}>
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className={`wireup-aui${props.compact ? ' wireup-aui--compact' : ''}`} aria-label="WireUp assistant">
        <DraftBridge draft={props.draft} />
        <ThreadPrimitive.Viewport className="wireup-aui__viewport">
          <div className="wireup-aui__messages" role="log" aria-label="Conversation">
            <ThreadPrimitive.Empty>
              <div className="wireup-aui__empty"><h2>What can I help you build?</h2><p>Describe your circuit, ask about a board, or paste an error.</p></div>
            </ThreadPrimitive.Empty>
            <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage: AssistantMessageView }} />
            {props.busy && !messages.some(message => message.role === 'assistant' && isActive((message as AssistantMessage).run?.status || message.status || '')) && <ThinkingIndicator/>}
          </div>
        </ThreadPrimitive.Viewport>
        <div className="wireup-aui__footer">
          <ThreadPrimitive.ScrollToBottom className="wireup-aui__latest" aria-label="Scroll to latest message"><ArrowDown size={14} /> Latest message</ThreadPrimitive.ScrollToBottom>
          <AssistantComposer />
        </div>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  </ThreadContext.Provider>
}

function DraftBridge({ draft }: { draft: string }) {
  const aui = useAui()
  useEffect(() => {
    if (aui.composer().getState().text !== draft) aui.composer().setText(draft)
  }, [aui, draft])
  return null
}

function AssistantComposer() {
  const { busy, compact, onDraftChange, models, selectionKey, onModelChange } = useThreadProps()
  return <ComposerPrimitive.Root className="wireup-aui__composer">
    <ComposerPrimitive.Input
      className="wireup-aui__input" aria-label="Message WireUp" placeholder="Ask about your project…"
      minRows={1} maxRows={compact ? 4 : 8} maxLength={16000} addAttachmentOnPaste={false}
      onChange={event => onDraftChange(event.target.value)}
    />
    <div className="wireup-aui__composer-bar">
      {models?.length ? <ChatModelPicker models={models} value={selectionKey} onChange={onModelChange} disabled={busy}/> : <span className="wireup-aui__hint">Enter to send · Shift+Enter for a new line</span>}
      {busy ? <ComposerPrimitive.Cancel className="wireup-aui__stop" aria-label="Stop generation"><Square size={13} /> Stop</ComposerPrimitive.Cancel>
        : <ComposerPrimitive.Send className="wireup-aui__send" aria-label="Send message"><ArrowUp size={17} /></ComposerPrimitive.Send>}
    </div>
  </ComposerPrimitive.Root>
}

function UserMessage() {
  return <MessagePrimitive.Root className="wireup-aui__message wireup-aui__message--user" aria-label="Your message">
    <div className="wireup-aui__author">You</div>
    <MessagePrimitive.Parts components={{ Text: UserText, Empty: EmptyPart }} />
  </MessagePrimitive.Root>
}

function UserText({ text }: TextMessagePartProps) {
  return <p className="wireup-aui__user-text">{text}</p>
}

function EmptyPart() { return null }

function Markdown({ text }: { text: string }) {
  return <div className="wireup-aui__markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}>{text}</ReactMarkdown></div>
}

function AnswerPart({ text }: TextMessagePartProps) { return <Markdown text={text} /> }

function ProcessPart({ text, id }: ReasoningMessagePartProps) {
  const message = useOriginalMessage()
  const block = message?.blocks?.find(block => block.id === id)
  const thinking = block?.type === 'text' && block.channel === 'thinking'
  if (thinking) return <details className="wireup-aui__reasoning"><summary>Reasoning received from backend</summary><Markdown text={text} /></details>
  return <div className="wireup-aui__narration"><Markdown text={text} /></div>
}

function displayValue(value: unknown): string {
  if (typeof value === 'string') return value
  try { return JSON.stringify(value, null, 2) ?? String(value) } catch { return String(value) }
}

function ToolPart({ toolCallId, toolName, args, result, isError }: ToolCallMessagePartProps) {
  const message = useOriginalMessage()
  const block = message?.blocks?.find(block => block.type === 'step' && block.id === toolCallId) as ActivityStep | undefined
  const status = block?.status || (result === undefined ? 'running' : isError ? 'error' : 'success')
  const label = status === 'running' ? 'Running on backend' : status === 'success' ? 'Succeeded' : status === 'stopped' ? 'Stopped' : 'Failed'
  return <details className="wireup-aui__tool" data-status={status}>
    <summary><ChevronRight size={14} className="wireup-aui__chevron" /><span className="wireup-aui__tool-label">{block?.label || toolName}</span><span className="wireup-aui__tool-status">{label}</span></summary>
    <div className="wireup-aui__tool-body">
      {block?.toolName && <p className="wireup-aui__tool-name">{toolName}</p>}
      <h4>Inputs</h4><pre>{displayValue(block?.args ?? args)}</pre>
      {block?.args && block.command && <><h4>Command</h4><pre>{block.command}</pre></>}
      {(block?.result !== undefined || result !== undefined || block?.output) && <><h4>Output</h4><pre>{displayValue(block?.result !== undefined ? block.result : result ?? block?.output)}</pre>{block?.result !== undefined && block.output && block.output !== displayValue(block.result) && <><h4>Backend log</h4><pre>{block.output}</pre></>}</>}
      {status === 'running' && result === undefined && !block?.output && <p className="wireup-aui__waiting">Waiting for the backend result…</p>}
    </div>
  </details>
}

function AssistantMessageView() {
  const message = useOriginalMessage()
  const { busy, messages, onOpenQuestions, onFeasibility, onRetry } = useThreadProps()
  if (!message) return null
  const active = isActive(message.run?.status || message.status || '')
  const hasEvents = !!message.content || !!message.blocks?.some(block => block.type === 'step' || !!block.text) || !!message.questions || !!message.feasibility
  const last = messages.filter(message => !message.hidden).at(-1)?.id === message.id
  return <MessagePrimitive.Root className="wireup-aui__message wireup-aui__message--assistant" role="article" aria-label="WireUp message">
    <div className="wireup-aui__author"><img src="/wireup-logo.png" alt="" /> WireUp{message.model && <span>{message.model}</span>}</div>
    {message.feasibility && <div className="wireup-aui__feasibility"><FeasibilityCard report={message.feasibility} onChoose={onFeasibility} busy={busy} selected={message.feasibilityChoice} /></div>}
    {message.questions && <section className="wireup-aui__setup"><h3>Set up your project</h3>{message.questions.summary && <p>{message.questions.summary}</p>}<button type="button" onClick={() => onOpenQuestions(message)} disabled={busy}>Open project setup · {message.questions.questions.length} questions</button></section>}
    <MessagePrimitive.Parts components={{ Text: AnswerPart, Reasoning: ProcessPart, tools: { Override: ToolPart }, Empty: EmptyPart }} />
    {active && !hasEvents && <ThinkingIndicator/>}
    <RunOutcome message={message} />
    {!active && <MessageActions message={message} retry={!busy && last ? onRetry : undefined} />}
  </MessagePrimitive.Root>
}

function ThinkingIndicator() {
  return <div className="wireup-aui__thinking" role="status"><AgentGears/><span>Thinking<span className="wireup-aui__thinking-dots" aria-hidden="true">…</span></span></div>
}

function RunOutcome({ message }: { message: AssistantMessage }) {
  const run = message.run
  const status = run?.status || message.status
  if (!status || isActive(status) || ['success', 'done', 'completed'].includes(status)) return null
  const stopped = isStopped(status) || status === 'interrupted'
  const failed = isFailed(status)
  const labels: Record<string, string> = {
    success: 'Completed', done: 'Completed', completed: 'Completed',
    stopped: 'Generation stopped', cancelled: 'Generation cancelled', canceled: 'Generation cancelled',
    error: 'Failed', failed: 'Failed', failure: 'Failed',
    awaiting_approval: 'Waiting for your approval', awaiting_revision: 'Waiting for a revised plan',
    awaiting_answers: 'Waiting for project setup', awaiting_questions: 'Waiting for project setup', requires_action: 'Your input is needed',
    blocked: 'Blocked', interrupted: 'Interrupted', incomplete: 'Incomplete', limit_reached: 'Run limit reached',
  }
  const counts = [run?.toolCalls !== undefined ? `${run.toolCalls} tool calls` : '', run?.modelCalls !== undefined ? `${run.modelCalls} model calls` : '', message.elapsedMs !== undefined ? `${(message.elapsedMs / 1000).toFixed(1)}s` : ''].filter(Boolean)
  return <section className="wireup-aui__outcome" data-outcome={stopped ? 'stopped' : failed ? 'error' : ['success', 'done', 'completed'].includes(status) ? 'success' : 'waiting'} role={failed ? 'alert' : 'status'}>
    <strong>{labels[status] || status.replaceAll('_', ' ')}</strong>
    {run?.reason && <p>{run.reason}</p>}
    {message.error && message.error !== run?.reason && <p>{message.error}</p>}
    {stopped && <p>Saved changes have not been reverted. A running simulation has not been stopped.</p>}
    {counts.length > 0 && <small>{counts.join(' · ')}</small>}
    {run?.changes && Object.keys(run.changes).length > 0 && <details><summary>Saved changes reported by backend</summary><dl>{Object.entries(run.changes).map(([name, count]) => <div key={name}><dt>{name.replaceAll('_', ' ')}</dt><dd>{count}</dd></div>)}</dl></details>}
    {run?.successfulTools && Object.keys(run.successfulTools).length > 0 && <details><summary>Successful tools</summary><dl>{Object.entries(run.successfulTools).map(([name, count]) => <div key={name}><dt>{name}</dt><dd>{count}</dd></div>)}</dl></details>}
  </section>
}

function MessageActions({ message, retry }: { message: AssistantMessage; retry?: AssistantThreadProps['onRetry'] }) {
  const [copied, setCopied] = useState(false)
  const [copyError, setCopyError] = useState('')
  const answer = message.blocks?.filter(block => block.type === 'text' && block.channel !== 'narration' && block.channel !== 'thinking').map(block => block.type === 'text' ? block.text : '').join('')
  const text = message.content || answer || ''
  return <div className="wireup-aui__actions">
    {text && <button type="button" aria-label="Copy response" onClick={async () => {
      try { await navigator.clipboard.writeText(text); setCopied(true); setCopyError('') }
      catch { setCopyError('Copy failed. Select the response text to copy it.') }
    }}>{copied ? <Check size={13} /> : <Copy size={13} />}{copied ? 'Copied' : 'Copy'}</button>}
    {retry && <button type="button" onClick={() => { void retry() }}><RotateCcw size={13} />{message.status === 'done' && !isFailed(message.run?.status || '') ? 'Regenerate' : 'Retry'}</button>}
    {copyError && <p role="status">{copyError}</p>}
  </div>
}
