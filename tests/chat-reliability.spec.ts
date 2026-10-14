import { expect, test } from '@playwright/test'

const start = { type: 'step_start', id: 'tool-1', label: 'Check wiring', command: 'check_connections({})', toolName: 'check_connections', args: {} }
const end = { type: 'step_end', id: 'tool-1', status: 'success', output: 'OK' }
const done = { type: 'done', status: 'success', reason: null }
const cases = [
  { name: 'duplicate tool start', events: [start, start, done], error: 'duplicate tool activity ID' },
  { name: 'orphan tool result', events: [end, done], error: 'without a matching start' },
  { name: 'duplicate tool result', events: [start, end, end, done], error: 'more than once' },
  { name: 'premature success', events: [start, done], error: 'before all tools completed' },
]
for (const width of [1440, 390]) for (const scenario of cases) test(`${scenario.name} stays recoverable at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 })
  await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'fixture' } }))
  let calls = 0
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/chat', route => {
    calls++
    const events = calls === 1 ? scenario.events : [{ type: 'text', channel: 'answer', text: 'Recovered after retry.' }, done]
    return route.fulfill({ contentType: 'application/x-ndjson', body: events.map(event => JSON.stringify(event)).join('\n') + '\n' })
  })
  await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Check my wiring')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText(scenario.error, { timeout: 15000 })
  await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByText('Recovered after retry.', { exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(errors).toEqual([])
})
