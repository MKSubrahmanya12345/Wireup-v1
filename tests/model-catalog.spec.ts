import { expect, test } from '@playwright/test'
for (const width of [1440, 390]) test(`all discovered models select and persist with clean replies at ${width}px`, async ({ page, request }) => {
 const project = await (await request.post('/api/hardware/projects', { data: { name: 'Model catalog verification' } })).json()
 await page.setViewportSize({ width, height: 900 })
 const models = [{ id: 'groq', label: 'Groq', model: 'one', configured: true }, { id: 'groq', label: 'Groq', model: 'two', configured: true }, { id: 'azure', label: 'Azure', model: 'deployment', configured: true }]
 await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'one', providers: [models[0], models[2]] } }))
 await page.route('**/api/models', route => route.fulfill({ json: { models, errors: {} } }))
 const bodies: { provider: string; model: string }[] = []
 await page.route('**/api/chat', route => { bodies.push(route.request().postDataJSON()); return route.fulfill({ contentType: 'application/x-ndjson', body: '{"type":"text","channel":"answer","text":"Clean response."}\n{"type":"done","status":"success","reason":null,"toolCalls":0,"modelCalls":1,"changes":{"firmware":0}}\n' }) })
 const send = async () => { await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Hello'); await page.getByRole('button', { name: 'Send message', exact: true }).click() }
 try {
 await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
 await send()
 const selector = page.getByRole('combobox', { name: 'AI model provider' })
 await expect(selector.locator('option')).toHaveCount(3)
 await expect(selector).not.toContainText('Groq')
 await page.getByRole('searchbox', { name: 'Filter models' }).fill('two')
 await expect(selector.locator('option')).toHaveCount(2)
 await selector.selectOption('groq::two')
 await page.getByRole('searchbox', { name: 'Filter models' }).fill('')
 await send()
 expect(bodies.at(-1)).toMatchObject({ provider: 'groq', model: 'two' })
 await expect(page.getByText('Completed', { exact: true })).toHaveCount(0)
 await expect(page.getByText('Saved changes reported by backend', { exact: true })).toHaveCount(0)
 await page.reload({ waitUntil: 'domcontentloaded' })
 await expect(selector).toHaveValue('groq::two')
 await selector.selectOption('azure::deployment')
 await send()
 expect(bodies.at(-1)).toMatchObject({ provider: 'azure', model: 'deployment' })
 await page.goto(`/project/${project.id}`, { waitUntil: 'domcontentloaded' })
 if (width === 390) await page.getByRole('button', { name: 'Agent', exact: true }).click()
 await send()
 await expect(selector.locator('option')).toHaveCount(3)
 await expect(selector).toHaveValue('azure::deployment')
 await expect(page.locator('[data-outcome="success"]')).toHaveCount(0)
 } finally { await page.goto('/', { waitUntil: 'domcontentloaded' }); await request.delete(`/api/hardware/projects/${project.id}`) }
})
