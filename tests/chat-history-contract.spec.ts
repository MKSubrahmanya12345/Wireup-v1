import { expect, test } from '@playwright/test'
import { serializeChatHistory } from '../src/lib/chat'
import type { Message } from '../src/lib/chat'

test('starter guidance stays visible but is excluded from user-first model history', () => {
  const messages: Message[] = [{ id: 'guide', role: 'assistant', content: 'Getting started', status: 'done' }, { id: 'user', role: 'user', content: 'Help me' }]
  expect(serializeChatHistory(messages)).toEqual([{ role: 'user', content: 'Help me' }])
  expect(messages).toHaveLength(2)
})

test('long histories stay within backend limits without deleting UI records', () => {
  const messages: Message[] = Array.from({ length: 300 }, (_, index) => ({ id: String(index), role: index % 2 ? 'assistant' : 'user', content: 'x'.repeat(index === 299 ? 20000 : 1000), status: 'done' }))
  messages.push({ id: 'latest', role: 'user', content: 'Latest question' })
  const history = serializeChatHistory(messages)
  expect(history.length).toBeLessThanOrEqual(200)
  expect(history[0].role).toBe('user')
  expect(history.at(-1)?.content).toBe('Latest question')
  expect(history.every(message => message.content.length <= 16000)).toBe(true)
  expect(history.reduce((total, message) => total + message.content.length, 0)).toBeLessThanOrEqual(48000)
  expect(messages).toHaveLength(301)
  expect(messages[299].content).toHaveLength(20000)
})
