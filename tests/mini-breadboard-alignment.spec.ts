import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) {
  for (const artwork of ['fetched', 'fallback'] as const) {
    test(`mini-breadboard sockets and placement align with ${artwork} art at ${width}px`, async ({ page, request }) => {
      test.setTimeout(120000)
      const created = await request.post('/api/hardware/projects', { data: { name: 'Transient mini-breadboard alignment', board: 'arduino-uno' } })
      expect(created.ok()).toBe(true)
      const project = await created.json()
      try {
        const added = await request.post(`/api/hardware/project/${project.id}/command`, {
          data: { name: 'add_component', args: { type: 'breadboard-mini', id: 'mini', x: 400, y: 220 } },
        })
        expect(added.ok()).toBe(true)
        for (const [from, to] of [
          [{ component: 'board', pin: '13' }, { component: 'mini', pin: '1t.a' }],
          [{ component: 'board', pin: 'GND' }, { component: 'mini', pin: '17b.j' }],
        ]) {
          const wired = await request.post(`/api/hardware/project/${project.id}/command`, { data: { name: 'connect_wire', args: { from, to } } })
          expect(wired.ok()).toBe(true)
        }
        if (artwork === 'fallback') {
          await page.route('**/component-svgs/fritzing/breadboard-mini.svg', route => route.fulfill({ status: 404, body: '' }))
        }
        await page.setViewportSize({ width, height: 900 })
        await page.goto(`/project/${project.id}`, { waitUntil: 'domcontentloaded' })
        const element = page.locator('velxio-breadboard-mini')
        await expect(element).toBeVisible({ timeout: 30000 })
        await expect(element.locator('svg')).toHaveCount(1)
        await expect(page.locator('.hw-canvas-part').filter({ has: element }).locator('.hw-pin')).toHaveCount(170)
        await page.getByRole('button', { name: 'Fit circuit', exact: true }).click()

        const checkAlignment = async () => {
          const geometry = await element.evaluate((node, art) => {
            const mini = node as HTMLElement & { pinInfo: Array<{ name: string; x: number; y: number; number: number }> }
            const part = mini.closest('.hw-canvas-part') as HTMLElement
            const rect = mini.getBoundingClientRect()
            const scale = rect.width / Number.parseFloat(mini.style.width)
            const pins = mini.pinInfo
            const socketErrors = pins.map((pin, index) => {
              const [bank, row] = pin.name.split('.')
              const column = bank.slice(0, -1)
              const socket = art === 'fetched'
                ? mini.shadowRoot!.querySelector<SVGGraphicsElement>(`#${row.toUpperCase()}${column}pin circle`)!
                : mini.shadowRoot!.querySelectorAll<SVGGraphicsElement>('rect[fill="#333"]')[index]
              const box = socket.getBBox()
              const center = new DOMPoint(box.x + box.width / 2, box.y + box.height / 2).matrixTransform(socket.getScreenCTM()!)
              const overlay = part.querySelector<HTMLElement>(`button[title="mini.${pin.name}"]`)!.getBoundingClientRect()
              const expected = { x: rect.x + pin.x * scale, y: rect.y + pin.y * scale }
              return {
                pin: Math.hypot(expected.x - center.x, expected.y - center.y) / scale,
                overlay: Math.hypot(overlay.x + overlay.width / 2 - center.x, overlay.y + overlay.height / 2 - center.y) / scale,
              }
            })
            const wireErrors = Array.from(document.querySelectorAll<SVGPathElement>('.hw-wire-hit')).map(path => {
              const name = path.getAttribute('aria-label')!.endsWith('mini.1t.a') ? '1t.a' : '17b.j'
              const pin = pins.find(pin => pin.name === name)!
              const end = path.getPointAtLength(path.getTotalLength())
              const point = new DOMPoint(end.x, end.y).matrixTransform(path.getScreenCTM()!)
              return Math.hypot(point.x - (rect.x + pin.x * scale), point.y - (rect.y + pin.y * scale)) / scale
            })
            return {
              names: pins.map(pin => pin.name), numbers: pins.map(pin => pin.number),
              first: pins[0], last: pins[pins.length - 1], pitch: pins[10].x - pins[0].x,
              socketError: Math.max(...socketErrors.map(error => error.pin)),
              overlayError: Math.max(...socketErrors.map(error => error.overlay)), wireErrors,
              left: part.style.left, top: part.style.top,
            }
          }, artwork)
          const names = Array.from({ length: 17 }, (_, index) => [
            ...['a', 'b', 'c', 'd', 'e'].map(row => `${index + 1}t.${row}`),
            ...['f', 'g', 'h', 'i', 'j'].map(row => `${index + 1}b.${row}`),
          ]).flat()
          expect(geometry.names).toEqual(names)
          expect(geometry.numbers).toEqual(Array.from({ length: 170 }, (_, index) => index + 1))
          expect(geometry.first.x).toBeCloseTo(9.76, 5)
          expect(geometry.first.y).toBeCloseTo(14.48, 5)
          expect(geometry.last.x).toBeCloseTo(163.36, 5)
          expect(geometry.last.y).toBeCloseTo(120.08, 5)
          expect(geometry.pitch).toBeCloseTo(9.6, 5)
          expect(geometry.socketError).toBeLessThan(0.1)
          expect(geometry.overlayError).toBeLessThan(0.15)
          expect(geometry.wireErrors).toHaveLength(2)
          expect(Math.max(...geometry.wireErrors)).toBeLessThan(0.15)
          return geometry
        }

        const initial = await checkAlignment()
        expect(initial.left).toBe('400px')
        expect(initial.top).toBe('220px')
        const handle = page.getByRole('button', { name: 'Select mini', exact: true })
        const box = await handle.boundingBox()
        expect(box).not.toBeNull()
        await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
        await page.mouse.down()
        await page.mouse.move(box!.x + box!.width / 2 + 24, box!.y + box!.height / 2 + 16, { steps: 5 })
        await page.mouse.up()
        await expect.poll(async () => {
          const saved = await (await request.get(`/api/hardware/projects/${project.id}`)).json()
          const mini = saved.components.find((part: { id: string }) => part.id === 'mini')
          return mini.x !== 400 || mini.y !== 220
        }).toBe(true)
        await checkAlignment()
        const saved = await (await request.get(`/api/hardware/projects/${project.id}`)).json()
        const placed = saved.components.find((part: { id: string }) => part.id === 'mini')
        expect(saved.wires.map((wire: { to: { pin: string } }) => wire.to.pin)).toEqual(['1t.a', '17b.j'])
        await page.reload({ waitUntil: 'domcontentloaded' })
        await expect(element).toBeVisible({ timeout: 30000 })
        await expect(element.locator('svg')).toHaveCount(1)
        await expect(page.locator('.hw-canvas-part').filter({ has: element }).locator('.hw-pin')).toHaveCount(170)
        await page.getByRole('button', { name: 'Fit circuit', exact: true }).click()
        const reloaded = await checkAlignment()
        expect(reloaded.left).toBe(`${placed.x}px`)
        expect(reloaded.top).toBe(`${placed.y}px`)
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      } finally {
        try {
          if (!page.isClosed()) await page.goto('about:blank')
        } finally {
          const deleted = await request.delete(`/api/hardware/projects/${project.id}`)
          expect(deleted.ok()).toBe(true)
        }
      }
    })
  }
}
