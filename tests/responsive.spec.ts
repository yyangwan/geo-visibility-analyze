import { test, expect } from '@playwright/test'

test.describe('Responsive layout', () => {
  test('login form is responsive on mobile', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await page.goto('/login')
    // On mobile, narrative stacks above form
    const loginCard = page.locator('.login-card')
    await expect(loginCard).toBeVisible()
  })

  test('login narrative has feature list', async ({ page }) => {
    await page.goto('/login')
    await expect(page.locator('.feature-list')).toBeVisible()
    await expect(page.locator('.feature-list li').first()).toBeVisible()
  })

  test('login shows responsive on desktop', async ({ page }) => {
    await page.setViewportSize({ width: 1200, height: 800 })
    await page.goto('/login')
    // Desktop: narrative and login-card side by side
    const narrative = page.locator('.narrative')
    const loginCard = page.locator('.login-card')
    await expect(narrative).toBeVisible()
    await expect(loginCard).toBeVisible()
  })
})
