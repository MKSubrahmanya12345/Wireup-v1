import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) {
  test(`done closes generation even when transport stays open at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'test' } }))
    await page.addInitScript(() => {
      const original = fetch.bind(window)
      window.fetch = (input, init) => {
        if (input !== '/api/chat') return original(input, init)
        const encoder = new TextEncoder()
        return Promise.resolve(new Response(new ReadableStream({ start(controller) {
          controller.enqueue(encoder.encode(JSON.stringify({ type: 'text', channel: 'answer', text: 'A complete response.' }) + '\n' + JSON.stringify({ type: 'done', status: 'success' }) + '\n'))
        } })))
      }
    })
    await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
    await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Hello')
    await page.getByRole('button', { name: 'Send message', exact: true }).click()
    await expect(page.getByText('A complete response.', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible()
  })
}

test('silent stream ends with a recoverable timeout rather than loading forever', async ({ page }) => {
  test.setTimeout(75000)
  await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'test' } }))
  await page.addInitScript(() => {
    const original = fetch.bind(window)
    window.fetch = (input, init) => input === '/api/chat' ? Promise.resolve(new Response(new ReadableStream({ start() {} }))) : original(input, init)
  })
  await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Hello')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('stopped sending updates', { timeout: 55000 })
  await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
})
