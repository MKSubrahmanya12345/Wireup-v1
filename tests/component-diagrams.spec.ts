import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) test(`complete transistor and display diagrams at ${width}px`, async ({ page, request }) => {
  test.setTimeout(90000)
  const project = await (await request.post('/api/hardware/projects', { data: { name: 'Complete diagram verification' } })).json()
  const command = async (action: string, args: Record<string, unknown>) => {
    const response = await request.post(`/api/hardware/project/${project.id}/command`, { data: { name: action, args, runtime_token: project.runtime_token } })
    expect(response.ok(), await response.text()).toBe(true)
  }
  try {
    await command('add_component', { id: 'transistor', type: 'bjt-2n2222', x: 420, y: 100 })
    await command('add_component', { id: 'mosfet', type: 'mosfet-2n7000', x: 530, y: 100 })
    await command('add_component', { id: 'digits', type: '7segment', x: 420, y: 250, properties: { digits: 4 } })
    await command('add_component', { id: 'display', type: 'ssd1306', x: 420, y: 400, properties: { imageData: '' } })
    await command('add_component', { id: 'supply', type: 'source-dc-voltage', x: 640, y: 100 })
    const stored = await (await request.get(`/api/hardware/projects/${project.id}`)).json()
    stored.components.find((part: { id: string }) => part.id === 'digits').properties.digits = '4'
    await page.route(`**/api/hardware/projects/${project.id}`, route => route.fulfill({ json: stored }))
    await page.setViewportSize({ width, height: 900 })
    await page.goto(`/project/${project.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('region', { name: 'Circuit canvas' })).toBeVisible({ timeout: 30000 })
    const transistor = page.locator('.hw-canvas-part').filter({ has: page.getByRole('button', { name: 'Select transistor', exact: true }) })
    const geometry = await transistor.locator('.hw-part-element').evaluate(host => {
      const root = host.firstElementChild!.shadowRoot!
      const svg = root.querySelector('svg')!
      const text = root.querySelector('text:last-child') as SVGGraphicsElement
      const stroke = getComputedStyle(root.querySelector('line')!).stroke
      return { height: svg.getBoundingClientRect().height, labelBottom: text.getBBox().y + text.getBBox().height, stroke }
    })
    expect(geometry.height).toBe(84)
    expect(geometry.labelBottom).toBeLessThanOrEqual(84)
    expect(geometry.stroke).not.toBe('none')
    const digits = page.locator('.hw-canvas-part').filter({ has: page.getByRole('button', { name: 'Select digits', exact: true }) })
    await expect.poll(() => digits.locator('.hw-part-element').evaluate(host => (host.firstElementChild as HTMLElement & { digits: number }).digits)).toBe(4)
    const display = page.locator('.hw-canvas-part').filter({ has: page.getByRole('button', { name: 'Select display', exact: true }) })
    await expect.poll(() => display.locator('.hw-part-element').evaluate(host => (host.firstElementChild as HTMLElement & { imageData: unknown }).imageData instanceof ImageData)).toBe(true)
    await expect(page.getByRole('button', { name: 'Connect supply pin +', exact: true })).toHaveCount(1)
    await expect(page.locator('.hw-unavailable')).toHaveCount(0)
    await page.getByRole('button', { name: 'Fit circuit', exact: true }).click()
    await page.screenshot({ path: `test-results/complete-diagrams-${width}.png` })
  } finally { await page.goto('/', { waitUntil: 'domcontentloaded' }); await request.delete(`/api/hardware/projects/${project.id}`) }
})
