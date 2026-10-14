import { expect, test } from '@playwright/test'
for (const width of [1440, 390]) test(`new project chat reuses confirmed setup at ${width}px`, async ({ page, request }) => {
 const project = await (await request.post('/api/hardware/projects', { data: { name: 'Setup once verification', board: 'arduino-uno' } })).json()
 const answers = { board: 'arduino-uno', behavior: 'blink' }
 await page.setViewportSize({ width, height: 900 })
 await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'gpt-6.1-sol', providers: [{ id: 'azure', label: 'Azure', model: 'gpt-6.1-sol', configured: true }] } }))
 let submitted: { project_answers?: unknown; messages?: { content: string }[] } | undefined
 await page.route('**/api/chat', route => { submitted = route.request().postDataJSON(); return route.fulfill({ contentType: 'application/x-ndjson', body: '{"type":"text","channel":"answer","text":"Requested component removed."}\n{"type":"done","status":"success","reason":null}\n' }) })
 try {
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await page.evaluate(({ id, requirements }) => localStorage.setItem(`wireup.project.${id}.chats.v1`, JSON.stringify([{ id: 'initial-setup', title: 'Build', updatedAt: 1, projectId: id, requirements, messages: [{ id: 'user', role: 'user', content: 'Build a blinking light' }, { id: 'assistant', role: 'assistant', content: 'Built.', status: 'done' }] }])), { id: project.id, requirements: answers })
  await page.goto(`/project/${project.id}`, { waitUntil: 'domcontentloaded' })
  if (width === 390) await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await page.getByRole('button', { name: 'New chat', exact: true }).click()
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Remove the LED')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByText('Requested component removed.', { exact: true })).toBeVisible()
  expect(submitted?.project_answers).toEqual(answers)
  expect(submitted?.messages?.at(-1)?.content).toBe('Remove the LED')
  await expect(page.getByRole('dialog', { name: 'Project setup', exact: true })).toHaveCount(0)
  await page.reload({ waitUntil: 'domcontentloaded' })
  if (width === 390) await page.getByRole('button', { name: 'Agent', exact: true }).click()
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Change the firmware delay to 1000 milliseconds')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByText('Requested component removed.', { exact: true })).toHaveCount(2)
  expect(submitted?.project_answers).toEqual(answers)
  await page.getByRole('textbox', { name: 'Message WireUp' }).fill('Add one LED to the circuit')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByText('Requested component removed.', { exact: true })).toHaveCount(3)
  expect(submitted?.project_answers).toEqual(answers)
  expect(submitted?.messages?.at(-1)?.content).toBe('Add one LED to the circuit')
  await expect(page.getByRole('dialog', { name: 'Project setup', exact: true })).toHaveCount(0)
 } finally { await page.goto('/', { waitUntil: 'domcontentloaded' }); await request.delete(`/api/hardware/projects/${project.id}`) }
})
