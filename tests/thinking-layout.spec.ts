import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) for (const embedded of [false, true]) test(`compact thinking indicator ${embedded ? 'project' : 'standalone'} at ${width}px`, async ({ page, request }) => {
 const project = embedded ? await (await request.post('/api/hardware/projects', { data: { name: 'Thinking layout verification' } })).json() : null
 await page.setViewportSize({ width, height: 900 })
 await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'gpt-6.1-sol', providers: [{ id: 'azure', label: 'Azure', model: 'gpt-6.1-sol', configured: true }] } }))
 let release!: () => void
 const held = new Promise<void>(resolve => { release = resolve })
 await page.route('**/api/chat', async route => { await held; await route.fulfill({ contentType: 'application/x-ndjson', body: '{"type":"text","channel":"answer","text":"Finished checking."}\n{"type":"done","status":"success","reason":null}\n' }) })
 try {
  await page.goto(embedded ? `/project/${project.id}` : '/assistant', { waitUntil: 'domcontentloaded' })
  if (embedded && width === 390) await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Check this project')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  const indicator = page.locator('.wireup-aui__thinking')
  await expect(indicator).toBeVisible()
  await expect(indicator.locator('img')).toHaveCount(3)
  const gears = await indicator.locator('.wireup-agent-gears').boundingBox()
  const text = await indicator.locator('.wireup-aui__thinking-label').boundingBox()
  expect(gears!.width).toBe(28)
  expect(gears!.height).toBe(26)
  expect(text!.x - gears!.x - gears!.width).toBeGreaterThanOrEqual(10)
  expect(Math.abs(text!.y + text!.height / 2 - gears!.y - gears!.height / 2)).toBeLessThan(1)
  await expect(indicator.locator('.wireup-aui__thinking-label')).toHaveCSS('font-size', '12px')
  await expect(indicator.locator('img').first()).toHaveCSS('animation-name', 'aui-gear-turn')
  await page.screenshot({ path: `test-results/thinking-compact-${embedded ? 'project' : 'standalone'}-${width}.png` })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(indicator.locator('img').first()).toHaveCSS('animation-name', 'none')
  release()
  await expect(page.getByText('Finished checking.', { exact: true })).toBeVisible()
  await expect(indicator).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Stop generation', exact: true })).toHaveCount(0)
 } finally { release(); if (project) { await page.goto('/', { waitUntil: 'domcontentloaded' }); await request.delete(`/api/hardware/projects/${project.id}`) } }
})
