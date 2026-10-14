import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

async function mockHealth(page: Page) {
  await page.route('**/api/health', route => route.fulfill({ json: { configured: true, model: 'groq-test' } }))
}
const reply = (text: string) => JSON.stringify({ type: 'text', channel: 'answer', text }) + '\n' + JSON.stringify({ type: 'done' }) + '\n'

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`chat conversation lifecycle at ${viewport.width}px`, async ({ page }) => {
    const errors: string[] = []
    const requests: { messages: { role: string; content: string }[] }[] = []
    page.on('pageerror', error => errors.push(error.message))
    await mockHealth(page)
    await page.route('**/api/chat', route => {
      requests.push(route.request().postDataJSON())
      return route.fulfill({ contentType: 'application/x-ndjson', body: reply('**Hello Ana.**\n\n```js\nconsole.log("hello")\n```') })
    })
    await page.setViewportSize(viewport)
    await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'What can I help you build?' })).toBeVisible()
    await expect(page.locator('canvas')).toBeVisible()
    const input = page.getByRole('textbox', { name: 'Message WireUp' })
    const send = page.getByRole('button', { name: 'Send message', exact: true })
    await expect(send).toBeDisabled()
    await input.fill('   ')
    await expect(send).toBeDisabled()
    await page.getByRole('button', { name: 'Review my code', exact: true }).click()
    await expect(input).toHaveValue('Review my code')
    await input.fill('My name is Ana')
    await input.press('Shift+Enter')
    await input.press('b')
    await expect(input).toHaveValue('My name is Ana\nb')
    await input.press('Enter')
    await expect(page).toHaveURL(/\/chat\//)
    await expect(page.getByRole('article', { name: 'WireUp message' })).toContainText('Hello Ana.')
    await expect(page.locator('pre code')).toContainText('console.log')
    await expect(input).toHaveValue('')
    await input.fill('What is my name?')
    await send.click()
    await expect(page.getByRole('article', { name: 'WireUp message' })).toHaveCount(2)
    await expect(send).toBeVisible()
    expect(requests[1].messages.map(message => message.role)).toEqual(['user', 'assistant', 'user'])
    await page.getByRole('button', { name: 'Regenerate', exact: true }).click()
    await expect(send).toBeVisible()
    await expect(page.getByRole('article', { name: 'WireUp message' })).toHaveCount(2)
    await input.fill(Array.from({ length: 30 }, (_, i) => `Long line ${i}`).join('\n'))
    const maximumInputHeight = await input.evaluate(element => {
      const styles = getComputedStyle(element)
      return `${parseFloat(styles.lineHeight) * 8 + parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom)}px`
    })
    const inputHeight = await input.evaluate(element => element.getBoundingClientRect().height)
    expect(inputHeight).toBeLessThanOrEqual(parseFloat(maximumInputHeight) + 2)
    expect(inputHeight).toBeGreaterThan(parseFloat(maximumInputHeight) - 10)
    await expect(input).toHaveCSS('overflow-y', 'auto')
    await input.fill('')
    await page.screenshot({ path: `test-results/conversation-${viewport.width}.png` })
    await page.reload()
    await expect(page.getByRole('article', { name: 'WireUp message' })).toHaveCount(2)
    if (viewport.width < 768) await page.getByRole('button', { name: 'Open sidebar' }).click()
    await page.getByRole('button', { name: 'Rename My Name Is Ana', exact: true }).click()
    await page.getByRole('textbox', { name: 'Conversation name' }).fill('Saved chat')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await page.getByRole('button', { name: 'New chat', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'What can I help you build?' })).toBeVisible()
    await page.screenshot({ path: `test-results/home-${viewport.width}.png` })
    if (viewport.width < 768) await page.getByRole('button', { name: 'Open sidebar' }).click()
    await page.getByRole('link', { name: 'Saved chat' }).click()
    await expect(page.getByRole('article', { name: 'WireUp message' })).toHaveCount(2)
    if (viewport.width < 768) await page.getByRole('button', { name: 'Open sidebar' }).click()
    await page.getByRole('button', { name: 'Delete Saved chat', exact: true }).click()
    await page.getByRole('button', { name: 'Delete conversation', exact: true }).click()
    await expect(page).toHaveURL('/assistant')
    await page.goto('/chat/unknown')
    await expect(page.getByRole('heading', { name: 'Conversation not found' })).toBeVisible()
    await page.getByRole('link', { name: 'Start a new chat' }).click()
    await page.goto('/orb')
    await expect(page.locator('canvas')).toBeVisible()
    await page.goBack()
    await expect(input).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(errors).toEqual([])
  })
}

test('errors, retry, stop, and corrupted storage recover safely', async ({ page }) => {
  await mockHealth(page)
  let call = 0
  await page.route('**/api/chat', async route => {
    call++
    if (call === 1) return route.fulfill({ status: 503, json: { detail: 'Groq is not configured.' } })
    if (call === 3) { await new Promise(resolve => setTimeout(resolve, 2000)); return route.fulfill({ body: reply('Late response') }).catch(() => {}) }
    return route.fulfill({ contentType: 'application/x-ndjson', body: reply('Recovered response') })
  })
  await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
  await page.evaluate(() => localStorage.setItem('wireup.chats.v1', '{bad json'))
  await page.reload()
  await expect(page.getByRole('status')).toContainText('Saved chats could not be loaded')
  await page.getByRole('textbox').fill('Hello')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Groq is not configured')
  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByRole('article', { name: 'WireUp message' })).toContainText('Recovered response')
  await page.getByRole('button', { name: 'Regenerate', exact: true }).click()
  await page.getByRole('button', { name: 'Stop generation', exact: true }).click()
  await expect(page.getByRole('article', { name: 'WireUp message' })).toContainText('Interrupted')
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible()
})

test('Groq rate limit guidance allows retry', async ({ page }) => {
  await mockHealth(page)
  let requests = 0
  await page.route('**/api/chat', route => {
    requests++
    return route.fulfill({ contentType: 'application/x-ndjson', body: requests === 1
      ? JSON.stringify({ type: 'error', message: 'Groq rate limit reached. Check https://console.groq.com/settings/limits, then retry.' }) + '\n'
      : reply('Request recovered') })
  })
  await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
  await page.getByRole('textbox').fill('Hello')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Groq rate limit reached')
  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByRole('article', { name: 'WireUp message' })).toContainText('Request recovered')
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('missing Groq configuration is visible', async ({ page }) => {
  await page.route('**/api/health', route => route.fulfill({ json: { configured: false, model: 'llama-3.3-70b-versatile' } }))
  await page.goto('/assistant', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('status')).toContainText('GROQ_API_KEY')
})
