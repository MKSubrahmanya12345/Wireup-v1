import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) test(`workspace opens as soon as project data arrives at ${width}px`, async ({ page, request }) => {
  await page.setViewportSize({ width, height: 900 })
  const response = await request.post('/api/hardware/projects', { data: { name: 'Immediate workspace verification', board: 'arduino-uno' } })
  const project = await response.json()
  let release!: () => void
  const held = new Promise<void>(resolve => { release = resolve })
  await page.route(`**/api/hardware/projects/${project.id}`, async route => {
    await held
    await route.fulfill({ json: project })
  })
  try {
    await page.goto(`/project/${project.id}`, { waitUntil: 'domcontentloaded' })
    const loading = page.getByRole('status', { name: 'Loading workspace', exact: true })
    await expect(loading).toBeVisible()
    release()
    await expect(loading).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'Circuit canvas', exact: true })).toBeVisible()
  } finally {
    release()
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    await request.delete(`/api/hardware/projects/${project.id}`)
  }
})
