import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import { modelOptionKey } from '@/lib/model-selection'
import type { ModelOption } from '@/lib/model-selection'
import './chat-model-picker.css'

export function ChatModelPicker({ models, value, onChange, disabled }: { models: ModelOption[]; value?: string; onChange?: (key: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const selected = models.find(model => modelOptionKey(model) === value) || models[0]
  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLButtonElement>('[role="option"][aria-selected="true"]')?.focus()
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  useEffect(() => { if (disabled) setOpen(false) }, [disabled])
  return <div className="chat-model-picker" ref={root} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false) }} onKeyDown={event => {
    if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!open) { event.preventDefault(); setOpen(true); return }
      const choices = Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)') || [])
      if (!choices.length) return
      event.preventDefault()
      const index = choices.indexOf(document.activeElement as HTMLButtonElement)
      choices[(index + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length]?.focus()
    }
  }}>
    <button ref={trigger} className="chat-model-picker__trigger" type="button" aria-label={`Model: ${selected?.model || 'Select model'}`} aria-haspopup="listbox" aria-expanded={open} disabled={disabled || !onChange} onClick={() => setOpen(!open)}><span>{selected?.model || 'Select model'}</span><ChevronDown size={14}/></button>
    {open && <div className="chat-model-picker__panel" role="listbox" aria-label="Available models">
      {models.map(model => <button type="button" key={modelOptionKey(model)} role="option" aria-selected={modelOptionKey(model) === value} disabled={!model.configured} onClick={() => { onChange?.(modelOptionKey(model)); setOpen(false); trigger.current?.focus() }}><span className="chat-model-picker__check">{modelOptionKey(model) === value && <Check size={16}/>}</span><span>{model.model}</span>{!model.configured && <small>API key required</small>}</button>)}
    </div>}
  </div>
}
