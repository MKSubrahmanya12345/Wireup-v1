import { expect, test } from '@playwright/test'
const ids = ['gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-6-astra', 'gpt-6-luna', 'gpt-6-sol', 'gpt-6.1-sol']
for (const width of [1440, 390]) test(`compact seven-model menu works across chats at ${width}px`, async ({ page, request }) => {
 const project = await (await request.post('/api/hardware/projects', { data: { name: 'Seven model verification' } })).json()
 await page.setViewportSize({ width, height: 900 })
 await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'groq-model', providers: [{ id: 'azure', label: 'Azure', model: 'gpt-6.1-sol', configured: true }] } }))
 const sent: { provider: string; model: string }[] = []
 await page.route('**/api/chat', route => { sent.push(route.request().postDataJSON()); return route.fulfill({ contentType: 'application/x-ndjson', body: '{"type":"text","channel":"answer","text":"Reply."}\n{"type":"done","status":"success","reason":null}\n' }) })
 const send = async () => { await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Hello'); await page.getByRole('button', { name: 'Send message', exact: true }).click() }
 const choose = async (name: string) => { await page.getByRole('button', { name: /^Model:/ }).click(); await page.getByRole('option', { name, exact: true }).click() }
 try {
  await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /^Model:/ }).click()
  await expect(page.getByRole('option')).toHaveCount(7)
  expect(await page.getByRole('option').allTextContents()).toEqual(ids)
  await expect(page.getByRole('searchbox')).toHaveCount(0)
  await expect(page.getByRole('tablist')).toHaveCount(0)
  await expect(page.getByRole('listbox')).not.toContainText('Azure')
  await page.keyboard.press('Escape')
  await choose('gpt-5.6-luna')
  await send()
  await expect(page.getByText('Reply.', { exact: true })).toBeVisible()
  expect(sent.at(-1)).toMatchObject({ provider: 'azure', model: 'gpt-5.6-luna' })
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('button', { name: 'Model: gpt-5.6-luna', exact: true })).toBeVisible()
  await page.goto(`/project/${project.id}`, { waitUntil: 'domcontentloaded' })
  if (width === 390) await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await send()
  await expect(page.getByText('Reply.', { exact: true })).toBeVisible()
  await choose('gpt-6-sol')
  await send()
  expect(sent.at(-1)).toMatchObject({ provider: 'azure', model: 'gpt-6-sol' })
  await page.getByRole('button', { name: /^Model:/ }).click()
  await page.screenshot({ path: `test-results/seven-models-${width}.png` })
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Model: gpt-6.1-sol', exact: true })).toBeVisible()
 } finally { await page.goto('/', { waitUntil: 'domcontentloaded' }); await request.delete(`/api/hardware/projects/${project.id}`) }
})
