import { expect, test } from '@playwright/test'
import { routeWirePaths } from '../src/hardware/wire-path'

test('parallel cables use separate lanes around component bounds', () => {
  const paths = routeWirePaths([
    { id: 'a', start: { component: 'board', x: 150, y: 110, direction: { x: 0, y: -1 } }, end: { component: 'button', x: 660, y: 70, direction: { x: -1, y: 0 } } },
    { id: 'b', start: { component: 'board', x: 162, y: 110, direction: { x: 0, y: -1 } }, end: { component: 'button', x: 660, y: 95, direction: { x: -1, y: 0 } } },
    { id: 'c', start: { component: 'board', x: 300, y: 110, direction: { x: 0, y: -1 } }, end: { component: 'resistor', x: 540, y: 265, direction: { x: -1, y: 0 } } },
  ], [
    { id: 'board', left: 30, top: 100, right: 350, bottom: 350 },
    { id: 'button', left: 660, top: 50, right: 710, bottom: 110 },
    { id: 'resistor', left: 540, top: 255, right: 610, bottom: 275 },
  ])
  expect(routeWirePaths([], [])).toEqual(new Map())
  expect(paths.size).toBe(3)
  expect(paths.get('a')).toMatch(/^M 150 110 L 150 /)
  expect(paths.get('b')).toMatch(/^M 162 110 L 162 /)
  expect(new Set(paths.values()).size).toBe(3)
  for (const path of paths.values()) expect(path).toContain('Q')
})

for (const width of [1440, 390]) test(`multiple cables remain traceable through editing and navigation at ${width}px`, async ({ page, request }, testInfo) => {
  test.setTimeout(120000)
  const project = await (await request.post('/api/hardware/projects', { data: { name: 'Separated cables', board: 'arduino-uno' } })).json()
  const command = async (name: string, args: Record<string, unknown>) => {
    const response = await request.post(`/api/hardware/project/${project.id}/command`, { data: { name, args } })
    expect(response.ok()).toBe(true)
  }
  await command('modify_component', { id: 'board', x: 50, y: 170 })
  for (const args of [{ type: 'pushbutton', id: 'button', x: 660, y: 80 }, { type: 'resistor', id: 'r-led', x: 540, y: 360 }, { type: 'led', id: 'led', x: 700, y: 360 }]) await command('add_component', args)
  for (const [from, to, color] of [
    [{ component: 'board', pin: '2' }, { component: 'button', pin: '1.l' }, '#00b8ef'],
    [{ component: 'board', pin: 'GND' }, { component: 'button', pin: '2.l' }, '#111111'],
    [{ component: 'board', pin: '13' }, { component: 'r-led', pin: '1' }, '#ff2020'],
    [{ component: 'r-led', pin: '2' }, { component: 'led', pin: 'A' }, '#ff2020'],
    [{ component: 'led', pin: 'C' }, { component: 'board', pin: 'GND' }, '#111111'],
  ]) await command('connect_wire', { from, to, color })
  const initial = await (await request.get(`/api/hardware/projects/${project.id}`)).json()
  await page.setViewportSize({ width, height: 1000 })
  await page.goto(`/project/${project.id}`)
  const wires = page.locator('.hw-wire-hit')
  await expect(wires).toHaveCount(5, { timeout: 30000 })
  await expect(page.locator('.hw-notice').filter({ hasText: 'pin layout is not ready' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Fit circuit', exact: true }).click()
  await page.screenshot({ path: testInfo.outputPath(`separated-cables-${width}.png`) })
  const boardPins = page.locator('.hw-connected-pin-dot')
  const geometry = await page.locator('.hw-wires').evaluate(svg => {
    const paths = Array.from(svg.querySelectorAll<SVGPathElement>('.hw-wire-hit'))
    return paths.map(path => {
      const start = path.getPointAtLength(0)
      const next = path.getPointAtLength(5)
      return { d: path.getAttribute('d'), start: { x: start.x, y: start.y }, next: { x: next.x, y: next.y } }
    })
  })
  expect(new Set(geometry.map(path => path.d)).size).toBe(5)
  for (const path of geometry.slice(0, 3)) {
    expect(path.next.x).toBeCloseTo(path.start.x, 1)
    expect(path.next.y).toBeLessThan(path.start.y)
  }
  for (let index = 0; index < 5; index++) {
    await wires.nth(index).focus(); await wires.nth(index).press('Enter')
    const details = page.getByRole('dialog', { name: 'Wire connection details' })
    await expect(details).toContainText(initial.wires[index].from.component)
    await expect(details).toContainText(`Pin ${initial.wires[index].to.pin}`)
    await details.getByRole('button', { name: 'Close wire details' }).click()
  }
  await page.getByRole('tab', { name: 'Schematic', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Schematic view', exact: true })).toBeVisible()
  await page.getByRole('tab', { name: 'Circuit', exact: true }).click()
  await expect(wires).toHaveCount(5)
  await page.getByRole('button', { name: 'Connected pins', exact: true }).click()
  await expect(boardPins).toHaveCount(0)
  await page.getByRole('button', { name: 'Connected pins', exact: true }).click()
  await expect(boardPins).toHaveCount(3)
  await page.reload()
  await expect(wires).toHaveCount(5, { timeout: 30000 })
  const after = await (await request.get(`/api/hardware/projects/${project.id}`)).json()
  expect(after.wires).toEqual(initial.wires)
  expect(after.firmware).toEqual(initial.firmware)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
