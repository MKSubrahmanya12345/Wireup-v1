import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) test(`home prompt starts without waiting for model health at ${width}px`, async ({ page, request }) => {
  test.setTimeout(90000)
  await page.setViewportSize({ width, height: 900 })
  let release!: () => void
  const held = new Promise<void>(resolve => { release = resolve })
  let healthFinished = false
  await page.route('**/api/health', async route => {
    await held
    healthFinished = true
    await route.fulfill({ json: { configured: true, model: 'fixture' } })
  })
  let called = false
  await page.route('**/api/chat', async route => {
    called = true
    expect(healthFinished).toBe(false)
    await route.fulfill({ contentType: 'application/x-ndjson', body: '{"type":"heartbeat"}\n{"type":"text","channel":"answer","text":"Backend response received."}\n{"type":"done","status":"success"}\n' })
  })
  let id = ''
  try {
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    await page.getByRole('textbox', { name: 'Describe your hardware prototype' }).fill('Verify quick prompt handoff')
    await page.getByRole('button', { name: 'Build prototype', exact: true }).click()
    await expect(page).toHaveURL(/\/project\/[a-z0-9]+$/, { timeout: 30000 })
    id = page.url().split('/').pop()!
    await expect.poll(() => called, { timeout: 30000 }).toBe(true)
    release()
    if (width === 390) await page.getByRole('button', { name: 'Agent', exact: true }).click()
    await expect(page.getByText('Backend response received.', { exact: true })).toBeVisible({ timeout: 30000 })
    await expect(page.getByRole('log', { name: 'Conversation' })).not.toContainText('Connecting to')
    await expect(page.getByRole('log', { name: 'Conversation' })).not.toContainText('Preparing board and circuit questions')
    await page.reload({ waitUntil: 'domcontentloaded' })
    if (width === 390) await page.getByRole('button', { name: 'Agent', exact: true }).click()
    await expect(page.getByText('Backend response received.', { exact: true })).toBeVisible({ timeout: 30000 })
    const history = await page.evaluate(projectId => JSON.parse(localStorage.getItem(`wireup.project.${projectId}.chats.v1`) || '[]'), id)
    expect(history[0].messages.filter((message: { role: string }) => message.role === 'user')).toHaveLength(1)
  } finally {
    release()
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    if (id) await request.delete(`/api/hardware/projects/${id}`)
  }
})
