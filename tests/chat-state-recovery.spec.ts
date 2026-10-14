import { expect, test } from '@playwright/test'

const reply = '{"type":"text","channel":"answer","text":"Recovered."}\n{"type":"done","status":"success"}\n'
for (const width of [1440, 390]) test(`corrupted history is preserved before starting a new chat at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'fixture' } }))
  await page.route('**/api/chat', route => route.fulfill({ contentType: 'application/x-ndjson', body: reply }))
  await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
  await page.evaluate(() => localStorage.setItem('wireup.chats.v1', '{broken'))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('status')).toContainText('Saved chats could not be loaded')
  expect(await page.evaluate(() => localStorage.getItem('wireup.chats.v1'))).toBe('{broken')
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('New conversation')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByText('Recovered.', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('wireup.chats.v1.recovery'))).toBe('{broken')
})

for (const width of [1440, 390]) test(`switching conversations identifies the actual running request at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'fixture' } }))
  await page.addInitScript(() => {
    localStorage.setItem('wireup.chats.v1', JSON.stringify([{ id: 'other', title: 'Other conversation', updatedAt: 1, messages: [{ id: 'question', role: 'user', content: 'Earlier question' }, { id: 'answer', role: 'assistant', status: 'done', content: 'Earlier answer' }] }]))
    const original = window.fetch.bind(window)
    window.fetch = (input, init) => {
      if (input !== '/api/chat') return original(input, init)
      return Promise.resolve(new Response(new ReadableStream({ start(controller) {
        init?.signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')), { once: true })
      } })))
    }
  })
  await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Running question')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toBeVisible()
  if (width === 390) await page.getByRole('button', { name: 'Open sidebar' }).click()
  await page.getByRole('link', { name: 'Other conversation', exact: true }).click()
  await expect(page.getByText('Another conversation is running.', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Stop other conversation', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Stop other conversation', exact: true })).toHaveCount(0)
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Now available')
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeEnabled()
})
