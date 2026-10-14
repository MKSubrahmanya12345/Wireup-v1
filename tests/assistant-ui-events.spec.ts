import { createServer, request as proxyRequest } from 'node:http'
import type { ServerResponse } from 'node:http'
import { chromium, expect, test as base } from '@playwright/test'
import type { Locator, Page } from '@playwright/test'
import { seedModelPreference } from './model-picker'

type Event = Record<string, unknown>
type Surface = 'standalone' | 'embedded'
type Target = { storageKey: string; projectId?: string; open: () => Promise<void> }

const test = base.extend<{ target: Target; surface: Surface }>({
  surface: ['standalone', { option: true }],
  target: async ({ page, request, surface }, provide) => {
    await seedModelPreference(page, 'groq')
    await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'assistant-ui-fixture' } }))
    let projectId: string | undefined
    try {
      if (surface === 'embedded') {
        const created = await request.post('/api/hardware/projects', { data: { name: `Assistant UI events ${Date.now()}`, board: 'arduino-uno' } })
        expect(created.ok()).toBe(true)
        projectId = (await created.json()).id
        expect(projectId).toBeTruthy()
      }
      await provide({
        projectId,
        storageKey: projectId ? `wireup.project.${projectId}.chats.v1` : 'wireup.chats.v1',
        open: async () => {
          await page.goto(projectId ? `/project/${projectId}` : '/assistant', { waitUntil: 'domcontentloaded' })
          await showAgent(page, surface)
          await expect(page.getByRole('textbox', { name: 'Message WireUp' })).toBeVisible({ timeout: 30000 })
        },
      })
    } finally {
      if (projectId) {
        if (!page.isClosed()) await page.goto('about:blank')
        await expect.poll(async () => (await request.delete(`/api/hardware/projects/${projectId}`)).status(), { timeout: 10000 }).toBe(200)
      }
    }
  },
})

async function showAgent(page: Page, surface: Surface) {
  if (surface !== 'embedded') return
  await expect(page.getByRole('status', { name: 'Loading workspace' })).toHaveCount(0, { timeout: 45000 })
  if ((page.viewportSize()?.width || 1440) < 900) {
    await page.getByRole('button', { name: 'Agent', exact: true }).click()
  }
  await expect(page.getByRole('textbox', { name: 'Message WireUp' })).toBeVisible({ timeout: 30000 })
}

const ndjson = (events: Event[]) => events.map(event => JSON.stringify(event)).join('\n') + '\n'
const thread = (page: Page) => page.getByRole('log', { name: 'Conversation' })
const tool = (page: Page, label: string) => thread(page).locator('.wireup-aui__tool').filter({ has: page.locator('summary', { hasText: label }) })

async function expand(details: Locator) {
  if (await details.getAttribute('open') === null) await details.locator('summary').click()
}

async function send(page: Page, prompt = 'Check the supplied fixture circuit') {
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill(prompt)
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
}

async function savedReply(page: Page, key: string) {
  return page.evaluate(storageKey => {
    const chats = JSON.parse(localStorage.getItem(storageKey) || '[]')
    return chats[0]?.messages.filter((message: { role: string }) => message.role === 'assistant').at(-1)
  }, key)
}

const failedStart = { type: 'step_start', id: 'check-invalid', label: 'Check invalid pin', command: 'check_connections({"pin":"D99","repair":false})', toolName: 'check_connections', args: { pin: 'D99', repair: false } }
const failedEnd = { type: 'step_end', id: 'check-invalid', status: 'error', output: 'Backend rejected D99.', result: { error: 'invalid_pin', pin: 'D99' } }
const correctedStart = { type: 'step_start', id: 'check-corrected', label: 'Check corrected pin', command: 'check_connections({"pin":"D13","repair":false})', toolName: 'check_connections', args: { pin: 'D13', repair: false } }
const correctedEnd = { type: 'step_end', id: 'check-corrected', status: 'success', output: 'Backend accepted D13.', result: { valid: true, pin: 'D13', connections: 1 } }
const answer = 'The corrected D13 connection passed the backend check.'
const complete = { type: 'done', status: 'success', elapsedMs: 1250, toolCalls: 2, modelCalls: 1, successfulTools: { check_connections: 1 } }

async function expectResults(page: Page) {
  const failed = tool(page, 'Check invalid pin')
  const corrected = tool(page, 'Check corrected pin')
  await expect(failed).toHaveAttribute('data-status', 'error')
  await expect(corrected).toHaveAttribute('data-status', 'success')
  await expand(failed)
  await expand(corrected)
  await expect(failed).toContainText('Failed')
  await expect(failed.locator('pre').nth(0)).toHaveText(JSON.stringify(failedStart.args, null, 2))
  await expect(failed.locator('pre').nth(1)).toHaveText(failedStart.command)
  await expect(failed.locator('pre').nth(2)).toHaveText(JSON.stringify(failedEnd.result, null, 2))
  await expect(failed).toContainText(failedEnd.output)
  await expect(corrected).toContainText('Succeeded')
  await expect(corrected.locator('pre').nth(0)).toHaveText(JSON.stringify(correctedStart.args, null, 2))
  await expect(corrected.locator('pre').nth(2)).toHaveText(JSON.stringify(correctedEnd.result, null, 2))
  await expect(corrected).toContainText(correctedEnd.output)
  await expect(thread(page).locator('.wireup-aui__tool')).toHaveCount(2)
  expect(await thread(page).locator('.wireup-aui__tool-label').allTextContents()).toEqual(['Check invalid pin', 'Check corrected pin'])
}

for (const surface of ['standalone', 'embedded'] as const) {
  for (const width of [1440, 390]) {
    test.describe(`${surface} assistant events at ${width}px`, () => {
      test.use({ surface, viewport: { width, height: 900 } })
      test.setTimeout(120000)

      test('retains failed and corrected tool results, final answer, and completed run on reload', async ({ page, target }) => {
        let calls = 0
        await page.route('**/api/chat', async route => {
          calls++
          const body = route.request().postDataJSON()
          expect(body.provider).toBe('groq')
          expect(body.project_id).toBe(target.projectId)
          await route.fulfill({ contentType: 'application/x-ndjson', body: ndjson([
            { type: 'text', channel: 'narration', text: 'Checking the supplied pin against backend results.' },
            failedStart, failedEnd, correctedStart, correctedEnd,
            { type: 'text', channel: 'answer', text: answer }, complete,
            { type: 'text', channel: 'answer', text: 'This post-done text must be ignored.' },
          ]) })
        })
        await target.open()
        await send(page)
        await expect(thread(page).getByText(answer, { exact: true })).toBeVisible()
        await expectResults(page)
        const outcome = thread(page).locator('.wireup-aui__outcome')
        await expect(outcome).toHaveAttribute('data-outcome', 'success')
        await expect(outcome).toContainText('Completed')
        await expect(outcome).toContainText('2 tool calls · 1 model calls · 1.3s')
        await expect(thread(page)).not.toContainText('This post-done text')
        await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
        await page.reload({ waitUntil: 'domcontentloaded' })
        await showAgent(page, surface)
        await expectResults(page)
        await expect(thread(page).getByText(answer, { exact: true })).toBeVisible()
        await expect(outcome).toContainText('Completed')
        expect((await savedReply(page, target.storageKey)).run.status).toBe('success')
        expect(calls).toBe(1)
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      })

      test('preserves waiting-for-questions outcome without claiming completion', async ({ page, target }) => {
        let calls = 0
        await page.route('**/api/chat', route => {
          calls++
          return route.fulfill({ contentType: 'application/x-ndjson', body: ndjson([
            { type: 'questions', summary: 'Choose an LED behavior before building.', questions: [{ id: 'behavior', question: 'How should the LED work?', options: [{ id: 'blink', label: 'Blink' }, { id: 'steady', label: 'Steady' }] }] },
            { type: 'done', status: 'awaiting_answers', reason: 'project_questions', changes: {}, toolCalls: 0, modelCalls: 1 },
          ]) })
        })
        await target.open()
        await send(page)
        await expect(page.getByRole('dialog', { name: 'Set up your project' })).toBeVisible()
        await page.getByRole('button', { name: 'Close project setup' }).click()
        const outcomes = thread(page).locator('.wireup-aui__outcome')
        await expect(outcomes.first()).toContainText('Waiting for project setup')
        await expect(outcomes.first()).toHaveAttribute('data-outcome', 'waiting')
        await expect(thread(page).locator('.wireup-aui__tool')).toHaveCount(0)
        await page.reload({ waitUntil: 'domcontentloaded' })
        await showAgent(page, surface)
        await expect(outcomes.first()).toContainText('Waiting for project setup')
        expect((await savedReply(page, target.storageKey)).run.status).toBe('awaiting_answers')
        await page.getByRole('button', { name: 'Open project setup · 1 questions' }).click()
        await expect(page.getByRole('form', { name: 'Project requirements' })).toContainText('How should the LED work?')
        await page.getByRole('button', { name: 'Close project setup' }).click()
        await expect(outcomes.first()).not.toContainText('Completed')
        expect(calls).toBe(1)
      })

      test('backend cancellation keeps its status and saved-change warning after reload', async ({ page, target }) => {
        await page.route('**/api/chat', route => route.fulfill({ contentType: 'application/x-ndjson', body: ndjson([
          { type: 'text', channel: 'narration', text: 'The backend cancelled this run.' },
          { type: 'done', status: 'cancelled', reason: 'user_cancelled', changes: { components_added: 1 }, toolCalls: 1, modelCalls: 1 },
        ]) }))
        await target.open()
        await send(page, 'Cancel the proposed build')
        const outcome = thread(page).locator('.wireup-aui__outcome')
        await expect(outcome).toContainText('Generation cancelled')
        await expect(outcome).toHaveAttribute('data-outcome', 'stopped')
        await expect(outcome).toContainText('Saved changes have not been reverted. A running simulation has not been stopped.')
        await outcome.getByText('Saved changes reported by backend', { exact: true }).click()
        await expect(outcome).toContainText('components added')
        await expect(outcome.locator('dd')).toHaveText('1')
        await expect(outcome).not.toContainText('Completed')
        await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
        await page.reload({ waitUntil: 'domcontentloaded' })
        await showAgent(page, surface)
        await expect(outcome).toContainText('Generation cancelled')
        expect((await savedReply(page, target.storageKey)).run.status).toBe('cancelled')
      })

      test('reload marks a persisted in-flight run interrupted instead of successful', async ({ page, target }) => {
        await target.open()
        let chatId = ''
        await page.evaluate(({ key, projectId, start }) => {
          const id = crypto.randomUUID()
          localStorage.setItem(key, JSON.stringify([{ id, title: 'Interrupted backend check', updatedAt: Date.now(), projectId, messages: [
            { id: crypto.randomUUID(), role: 'user', content: 'Check the invalid pin' },
            { id: crypto.randomUUID(), role: 'assistant', content: '', status: 'streaming', run: { status: 'running' }, startedAt: Date.now(), blocks: [{ type: 'step', id: start.id, label: start.label, command: start.command, toolName: start.toolName, args: start.args, status: 'running', output: '' }] },
          ] }]))
        }, { key: target.storageKey, projectId: target.projectId, start: failedStart })
        chatId = await page.evaluate(key => JSON.parse(localStorage.getItem(key)!)[0].id, target.storageKey)
        let calls = 0
        await page.route('**/api/chat', route => { calls++; return route.abort() })
        if (surface === 'standalone') await page.goto(`/chat/${chatId}`, { waitUntil: 'domcontentloaded' })
        else { await page.reload({ waitUntil: 'domcontentloaded' }); await showAgent(page, surface) }
        const interrupted = tool(page, 'Check invalid pin')
        await expect(interrupted).toHaveAttribute('data-status', 'stopped')
        await expand(interrupted)
        await expect(interrupted).toContainText('Generation interrupted by reload.')
        await expect(interrupted.locator('pre').first()).toHaveText(JSON.stringify(failedStart.args, null, 2))
        await expect(thread(page).locator('.wireup-aui__outcome')).toContainText('Interrupted')
        await expect(thread(page)).not.toContainText('Completed')
        await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
        await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible()
        const saved = await savedReply(page, target.storageKey)
        expect(saved.status).toBe('stopped')
        expect(saved.run).toEqual({ status: 'interrupted', reason: 'reload' })
        expect(calls).toBe(0)
      })
    })
  }
}

test('real delayed NDJSON shows only received activity and final text before transport closes', async () => {
  const browser = await chromium.launch({ channel: 'chrome', args: ['--enable-unsafe-swiftshader'] })
  const context = await browser.newContext({ baseURL: 'http://127.0.0.1:5173' })
  const page = await context.newPage()
  const target = { open: async (origin: string) => { await page.goto(origin + '/assistant', { waitUntil: 'domcontentloaded' }); await expect(page.getByRole('textbox', { name: 'Message WireUp' })).toBeVisible() } }
  test.setTimeout(120000)
  let response: ServerResponse | undefined
  let connected!: () => void
  const connection = new Promise<void>(resolve => { connected = resolve })
  const server = createServer((request, reply) => {
    reply.setHeader('Access-Control-Allow-Origin', '*')
    reply.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    reply.setHeader('Access-Control-Allow-Headers', 'Content-Type')
    if (request.url !== '/stream') {
      const upstream = proxyRequest('http://127.0.0.1:5173' + request.url, { method: request.method, headers: { ...request.headers, host: '127.0.0.1:5173' } }, result => { reply.writeHead(result.statusCode || 502, result.headers); result.pipe(reply) })
      upstream.on('error', () => { reply.writeHead(502); reply.end() })
      request.pipe(upstream)
      return
    }
    if (request.method === 'OPTIONS') { reply.writeHead(204); reply.end(); return }
    request.resume()
    reply.writeHead(200, { 'Content-Type': 'application/x-ndjson', 'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff' })
    reply.flushHeaders()
    reply.write(JSON.stringify({ type: 'heartbeat' }) + ' '.repeat(2048) + '\n')
    response = reply
    connected()
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Delayed fixture did not get a TCP port.')
  let calls = 0
  try {
    // Forward the real fetch; route.fetch() would buffer the entire upstream body.
    await page.addInitScript(url => {
      const original = window.fetch.bind(window)
      window.fetch = (input, init) => original(input === '/api/chat' ? url : input, init)
    }, `http://127.0.0.1:${address.port}/stream`)
    page.on('request', request => {
      if (request.url() === `http://127.0.0.1:${address.port}/stream` && request.method() === 'POST') calls++
    })
    await target.open(`http://127.0.0.1:${address.port}`)
    await send(page)
    await Promise.race([
      connection,
      new Promise<never>((_, reject) => {
        const timer = setTimeout(() => reject(new Error('Browser did not connect to the delayed NDJSON fixture within 10 seconds.')), 10000)
        void connection.then(() => clearTimeout(timer))
      }),
    ])
    await expect(thread(page).getByText('Waiting for the backend…', { exact: true })).toBeVisible()
    await page.waitForTimeout(250)
    await expect(thread(page).locator('.wireup-aui__tool, .wireup-aui__reasoning, .wireup-aui__narration, .wireup-aui__outcome')).toHaveCount(0)
    await expect(thread(page)).not.toContainText('Running on backend')
    await expect(page.locator('.agent-gears')).toHaveCount(0)
    const encodedStart = ndjson([failedStart])
    response!.write(encodedStart.slice(0, 30))
    await page.waitForTimeout(100)
    await expect(thread(page).locator('.wireup-aui__tool')).toHaveCount(0)
    response!.write(encodedStart.slice(30))
    const failed = tool(page, 'Check invalid pin')
    await expect(failed).toHaveAttribute('data-status', 'running')
    await expand(failed)
    await expect(failed).toContainText('Running on backend')
    await expect(failed).toContainText('Waiting for the backend result…')
    await expect(failed.locator('pre').first()).toHaveText(JSON.stringify(failedStart.args, null, 2))
    await expect(failed.getByRole('heading', { name: 'Output', exact: true })).toHaveCount(0)
    response!.write(ndjson([failedEnd]))
    await expect(failed).toHaveAttribute('data-status', 'error')
    await expect(failed).toContainText('invalid_pin')
    response!.write(ndjson([correctedStart]))
    await expect(tool(page, 'Check corrected pin')).toHaveAttribute('data-status', 'running')
    response!.write(ndjson([correctedEnd]))
    await expectResults(page)
    const finalText = answer + ' ' + 'Actual backend evidence is displayed without a frontend typing animation. '.repeat(30)
    response!.write(ndjson([{ type: 'text', channel: 'answer', text: finalText }]))
    await expect(thread(page).locator('.wireup-aui__markdown').last()).toHaveText(finalText.trim(), { timeout: 1500 })
    expect(response!.writableEnded).toBe(false)
    await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toBeVisible()
    response!.write(ndjson([complete]))
    await expect(thread(page).locator('.wireup-aui__outcome')).toContainText('Completed')
    await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
    expect(response!.writableEnded).toBe(false)
    expect(calls).toBe(1)
  } finally {
    await context.close()
    await browser.close()
    response?.destroy()
    server.closeAllConnections()
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  }
})
