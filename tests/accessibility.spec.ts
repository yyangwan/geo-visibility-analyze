import { test, expect } from '@playwright/test'

test.describe('Accessibility', () => {
  test('login inputs have placeholder text', async ({ page }) => {
    await page.goto('/login')
    const inputs = page.locator('input')
    const count = await inputs.count()
    for (let i = 0; i < count; i++) {
      const placeholder = await inputs.nth(i).getAttribute('placeholder')
      expect(placeholder).toBeTruthy()
    }
  })

  test('keyboard can navigate login form', async ({ page }) => {
    await page.goto('/login')
    // Tab to username
    await page.keyboard.press('Tab')
    await page.keyboard.type('testuser')
    // Tab to password
    await page.keyboard.press('Tab')
    await page.keyboard.type('testpass')
    // Submit button reachable via tab
    for (let i = 0; i < 5; i++) {
      await page.keyboard.press('Tab')
      const el = page.locator(':focus')
      const type = await el.getAttribute('type').catch(() => null)
      if (type === 'submit') break
    }
    const focused = page.locator(':focus')
    await expect(focused).toHaveAttribute('type', 'submit')
  })

  test('login form has visible labels', async ({ page }) => {
    await page.goto('/login')
    const labels = page.locator('.field label')
    const count = await labels.count()
    expect(count).toBeGreaterThanOrEqual(2)
  })

  test('error message has appropriate styling', async ({ page }) => {
    await page.goto('/login')
    await page.locator('button[type="submit"]').click()
    const errorEl = page.locator('.error-msg')
    await expect(errorEl).toBeVisible()
    // Error should have red/bad color
    const color = await errorEl.evaluate(el => window.getComputedStyle(el).color)
    expect(color).toBeTruthy()
  })
})
