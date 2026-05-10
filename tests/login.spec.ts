import { test, expect } from '@playwright/test'

test.describe('Login page', () => {
  test('shows product narrative', async ({ page }) => {
    await page.goto('/login')
    await expect(page.locator('.narrative')).toBeVisible()
    await expect(page.locator('.narrative h1')).toContainText('AI')
  })

  test('has auth form fields', async ({ page }) => {
    await page.goto('/login')
    await expect(page.locator('input').first()).toBeVisible()
    await expect(page.locator('input[type="password"]')).toBeVisible()
  })

  test('shows error on empty submit', async ({ page }) => {
    await page.goto('/login')
    await page.locator('button[type="submit"]').click()
    await expect(page.locator('.error-msg')).toBeVisible()
  })

  test('toggle between login and register', async ({ page }) => {
    await page.goto('/login')
    const submitBtn = page.locator('button[type="submit"]')
    await expect(submitBtn).toHaveText('登录')

    await page.locator('text=注册').click()
    await expect(submitBtn).toHaveText('注册')

    await page.locator('text=登录').click()
    await expect(submitBtn).toHaveText('登录')
  })

  test('has sample insight cards in narrative', async ({ page }) => {
    await page.goto('/login')
    await expect(page.locator('.sample-card').first()).toBeVisible()
  })
})
