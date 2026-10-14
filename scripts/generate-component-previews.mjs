import { chromium } from '@playwright/test'
import { readFile, mkdir, access } from 'node:fs/promises'

const selected = new Set(process.argv.slice(2))
const catalog = JSON.parse(await readFile('public/components-metadata.json', 'utf8')).components
await mkdir('public/component-previews', { recursive: true })
const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 400, height: 300 } })
await page.goto('http://127.0.0.1:5173/', { waitUntil: 'domcontentloaded' })
await page.evaluate(async () => {
  const path = '/vendor/velxio/frontend/src/elements-register.ts'
  await import(path)
  document.body.replaceChildren()
  document.body.style.cssText = 'margin:0;background:#151515;--color-symbol-ink:#e2e5eb;--color-symbol-lead:#bec4ce;--color-symbol-label:#e2e5eb;--color-symbol-body:#858d99;display:flex;align-items:center;justify-content:center;width:400px;height:300px;overflow:hidden'
})
let generated = 0
const missing = []
for (const part of catalog) {
  if (selected.size && !selected.has(part.id)) continue
  if (part.thumbnail?.trim().startsWith('<svg')) continue
  try { await access(`public/component-svgs/${part.id}.svg`); continue } catch {}
  const rendered = await page.evaluate(async part => {
    document.body.replaceChildren()
    if (!customElements.get(part.tagName)) return false
    const element = document.createElement(part.tagName)
    Object.assign(element, part.defaultValues)
    document.body.append(element)
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    const width = element.offsetWidth || 100
    const height = element.offsetHeight || 100
    const scale = Math.min(1.5, 360 / width, 260 / height)
    element.style.transform = `scale(${scale})`
    return element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0
  }, part)
  if (!rendered) { missing.push(part.id); continue }
  await page.screenshot({ path: `public/component-previews/${part.id}.png` })
  generated++
}
await browser.close()
console.log(JSON.stringify({ generated, missing }))
if (missing.length) process.exitCode = 1
