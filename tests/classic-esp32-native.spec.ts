import { expect, test } from '@playwright/test'

const source = `void setup() {
  Serial.begin(115200);
  pinMode(2, OUTPUT);
  pinMode(4, INPUT_PULLUP);
  Serial.println("CLASSIC_NATIVE_READY");
}
void loop() {
  bool pressed = digitalRead(4) == LOW;
  digitalWrite(2, pressed ? HIGH : LOW);
  Serial.println(pressed ? "BUTTON_DOWN" : "BUTTON_UP");
  delay(100);
}
`

test('classic ESP32 executes firmware and bridges UART, GPIO output, LED, and button input', async ({ page, request }) => {
  test.setTimeout(300_000)
  let project = await (await request.post('/api/hardware/projects', {
    data: { name: 'Classic ESP32 native integration', board: 'esp32-devkit-v1' },
  })).json()
  const command = async (name: string, args: Record<string, unknown>) => {
    const response = await request.post(`/api/hardware/project/${project.id}/command`, {
      data: { name, args, runtime_token: project.runtime_token },
      timeout: 180_000,
    })
    expect(response.ok(), await response.text()).toBe(true)
    const value = await response.json()
    if (value?.id === project.id) project = value
    return value
  }

  try {
    await command('add_component', { type: 'led', id: 'led1', x: 470, y: 120 })
    await command('add_component', { type: 'resistor', id: 'r1', x: 380, y: 120, properties: { value: '220' } })
    await command('add_component', { type: 'pushbutton', id: 'button1', x: 470, y: 260 })
    for (const [from, to] of [
      [{ component: 'board', pin: 'D2' }, { component: 'r1', pin: '1' }],
      [{ component: 'r1', pin: '2' }, { component: 'led1', pin: 'A' }],
      [{ component: 'led1', pin: 'C' }, { component: 'board', pin: 'GND' }],
      [{ component: 'board', pin: 'D4' }, { component: 'button1', pin: '1.l' }],
      [{ component: 'board', pin: 'GND' }, { component: 'button1', pin: '2.l' }],
    ] as const) await command('connect_wire', { from, to })
    await command('generate_firmware', { source })
    await command('compile_firmware', {})

    await page.goto(`/project/${project.id}`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Simulation', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeEnabled({ timeout: 30_000 })
    await page.getByRole('button', { name: 'Run', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeEnabled({ timeout: 30_000 })
    await expect(page.locator('.hw-runtime-limit')).toContainText('sampled digital GPIO')
    await page.getByRole('button', { name: 'Serial monitor', exact: true }).click()
    await expect(page.getByLabel('Serial output', { exact: true })).toContainText('CLASSIC_NATIVE_READY', { timeout: 30_000 })
    await expect(page.getByLabel('Serial output', { exact: true })).toContainText('BUTTON_UP')

    await page.getByRole('button', { name: 'Results', exact: true }).click()
    await expect(page.locator('.hw-pin-results')).toContainText('GPIO2', { timeout: 10_000 })
    await expect(page.locator('.hw-runtime-limit')).toContainText('sampled digital GPIO')
    await expect(page.getByText('Classic ESP32 GPIO and connected circuit components are not simulated')).toHaveCount(0)
    await expect.poll(() => page.locator('wokwi-led').evaluate(element => Boolean((element as HTMLElement & { value?: boolean }).value))).toBe(false)

    await page.locator('wokwi-pushbutton').evaluate(element => element.dispatchEvent(new Event('button-press')))
    await expect.poll(() => page.locator('wokwi-led').evaluate(element => Boolean((element as HTMLElement & { value?: boolean }).value)), { timeout: 10_000 }).toBe(true)
    await page.getByRole('button', { name: 'Serial monitor', exact: true }).click()
    await expect(page.getByLabel('Serial output', { exact: true })).toContainText('BUTTON_DOWN', { timeout: 10_000 })

    await page.locator('wokwi-pushbutton').evaluate(element => element.dispatchEvent(new Event('button-release')))
    await expect.poll(() => page.locator('wokwi-led').evaluate(element => Boolean((element as HTMLElement & { value?: boolean }).value)), { timeout: 10_000 }).toBe(false)

    await page.setViewportSize({ width: 390, height: 844 })
    await expect(page.getByRole('button', { name: 'Simulation', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Results', exact: true }).click()
    await expect(page.locator('.hw-pin-results')).toContainText('GPIO2')
    await page.getByRole('button', { name: 'Serial monitor', exact: true }).click()
    await expect(page.getByLabel('Serial output', { exact: true })).toContainText('BUTTON_UP')
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
  } finally {
    await page.goto('about:blank')
    await request.delete(`/api/hardware/projects/${project.id}`).catch(() => {})
  }
})
