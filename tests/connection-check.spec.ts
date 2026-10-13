import { expect, test } from '@playwright/test'
test.use({ reducedMotion: 'reduce' })
test.setTimeout(120_000)
for (const width of [1440, 390]) test(`Connection checking, repair and shared views at ${width}px`, async ({ page, request }) => {
 const created = await (await request.post('/api/hardware/projects', { data: { name: 'Connection check browser test', board: 'arduino-uno' } })).json()
 try {
  let checks = 0; let repairs = 0
  let current = created
  const report = (repaired = false) => ({ project: current, validation: { project_id: current.id, revision: current.revision, valid: repaired, errors: repaired ? [] : [{ code: 'noncanonical_endpoint', message: 'Wire uses a recognized pin alias instead of the canonical pin.', evidence: { endpoints: [{ component: 'board', pin: 'D13' }] } }], warnings: [], nets: [], scope: 'Limited structural checks. Current and intended firmware behavior are not verified.' }, changed: repaired, repairs: repaired ? [{ operation: 'canonicalize_endpoints', wire_id: 'alias' }] : [], proposed_repairs: repaired ? [] : [{ operation: 'canonicalize_endpoints', wire_id: 'alias' }], before: {}, recheck: null, status: repaired ? 'repaired' : 'needs_review' })
  await page.route(`**/api/hardware/project/${created.id}/command`, async route => {
   const body = route.request().postDataJSON()
   if (body.name !== 'check_connections') return route.continue()
   if (body.args.repair) {
    repairs++; expect(body.args.expected_revision).toBe(current.revision)
    const changed = await (await request.post(`/api/hardware/project/${created.id}/command`, { data: { name: 'modify_component', args: { id: 'board', x: 240, expected_revision: current.revision } } })).json()
    current = changed
    return route.fulfill({ json: report(true) })
   }
   checks++; current = await (await request.get(`/api/hardware/projects/${created.id}`)).json()
   await route.fulfill({ json: report(repairs > 0) })
  })
  await page.setViewportSize({ width, height: 900 }); await page.goto(`/project/${created.id}`)
  await page.getByRole('button', { name: 'Check connections', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Check connections', exact: true })
  await expect(dialog.getByRole('region', { name: 'Connection issues' })).toContainText('recognized pin alias')
  await expect(dialog.getByRole('button', { name: 'Apply safe repairs' })).toBeDisabled()
  await dialog.getByRole('checkbox').check()
  await dialog.getByRole('button', { name: 'Apply safe repairs' }).click()
  await expect(dialog).toContainText('1 safe repair applied. Review the recheck below')
  await expect(dialog).toContainText('No connection issues were found within this check’s limited scope')
  expect(checks).toBeGreaterThanOrEqual(2); expect(repairs).toBe(1)
  await dialog.locator('summary').click(); await expect(dialog).toContainText('Current and intended firmware behavior are not verified')
  await page.screenshot({ path: `test-results/connection-check-${width}.png` })
  expect(await dialog.evaluate(d => { const r = d.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth })).toBe(true)
  await dialog.getByRole('button', { name: 'Close', exact: true }).click()
  await page.getByRole('button', { name: 'Info', exact: true }).click(); await expect(page.locator('.pd-body')).toContainText('Components1')
  await page.getByRole('button', { name: 'Code', exact: true }).click(); await expect(page.getByRole('textbox', { name: 'Firmware source' })).toBeVisible()
  await page.getByRole('textbox', { name: 'Firmware source' }).fill('// unsaved browser check')
  await page.getByRole('button', { name: 'Wiring / Simulation', exact: true }).press('Enter')
  await page.getByRole('button', { name: 'Check connections', exact: true }).click()
  await expect(dialog).toContainText('You have unsaved firmware edits')
  await expect(dialog.getByRole('button', { name: 'Apply safe repairs' })).toBeDisabled()
  await dialog.getByRole('button', { name: 'Close', exact: true }).click()
  await page.getByRole('button', { name: 'Build Guide', exact: true }).click(); await expect(page.locator('.build-guide')).toBeVisible()
  await page.getByRole('button', { name: 'Wiring / Simulation', exact: true }).press('Enter'); await expect(page.getByRole('region', { name: 'Circuit canvas' })).toBeVisible()
 } finally { await request.delete(`/api/hardware/projects/${created.id}`) }
})
test('Real robotic-arm check is read-only and reports manual verification limits', async ({ page, request }) => {
 const id = 'ff0b0110880044c5a725c54ab7249aa0'
 const before = await (await request.get(`/api/hardware/projects/${id}`)).json()
 await page.goto(`/project/${id}`); await page.getByRole('button', { name: 'Check connections', exact: true }).click()
 const dialog = page.getByRole('dialog', { name: 'Check connections', exact: true })
 await expect(dialog).toContainText('Servo supply capacity')
 await expect(dialog).toContainText('No automatic safe repairs are offered')
 await dialog.getByRole('button', { name: 'Recheck', exact: true }).click()
 await expect(dialog).toContainText(`Revision ${before.revision}`)
 await dialog.getByRole('button', { name: 'Close', exact: true }).click()
 const after = await (await request.get(`/api/hardware/projects/${id}`)).json()
 expect(after.revision).toBe(before.revision); expect(after.wires).toEqual(before.wires)
})
test('Connection service failures retry and stale reports block repairs', async ({ page, request }) => {
 const project = await (await request.post('/api/hardware/projects', { data: { name: 'Connection retry test', board: 'arduino-uno' } })).json()
 try {
  let failure = true
  await page.route(`**/api/hardware/project/${project.id}/command`, async route => {
   if (route.request().postDataJSON().name !== 'check_connections') return route.continue()
   if (failure) return route.fulfill({ status: 503, json: { detail: 'Connection service unavailable' } })
   return route.fulfill({ json: { project, validation: { project_id: project.id, revision: project.revision - 1, errors: [], warnings: [], nets: [], scope: 'Limited check', valid: true }, proposed_repairs: [{ operation: 'remove_self_wire', wire_id: 'self' }], repairs: [], changed: false } })
  })
  await page.goto(`/project/${project.id}`); await page.getByRole('button', { name: 'Check connections', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Check connections', exact: true })
  await expect(dialog.getByRole('alert')).toContainText('Connection service unavailable')
  failure = false; await dialog.getByRole('button', { name: 'Recheck', exact: true }).click()
  await expect(dialog).toContainText('different project revision')
  await expect(dialog.getByRole('checkbox')).toBeDisabled()
  await expect(dialog.getByRole('button', { name: 'Apply safe repairs' })).toBeDisabled()
  await page.keyboard.press('Escape'); await expect(dialog).not.toBeAttached()
 } finally { await request.delete(`/api/hardware/projects/${project.id}`) }
})
