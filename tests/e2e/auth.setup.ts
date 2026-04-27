import { test as setup, expect } from '@playwright/test';

const ADMIN_EMAIL = process.env.ELAB_EMAIL ?? 'admin@example.com';
const ADMIN_PASSWORD = process.env.ELAB_PASSWORD ?? 'testpass123';

setup('authenticate as admin', async ({ page }) => {
  await page.goto('/login.php');

  const csrf = await page.locator('meta[name="csrf-token"]').getAttribute('content');
  expect(csrf).toBeTruthy();

  // There are two email inputs on the page (navbar search + login form); target the login form
  const form = page.locator('form[action]').filter({ has: page.locator('input[name="password"]') });
  await form.locator('input[name="email"]').fill(ADMIN_EMAIL);
  await form.locator('input[name="password"]').fill(ADMIN_PASSWORD);

  await Promise.all([
    page.waitForURL(/\/(index|dashboard)\.php/),
    form.locator('button[type="submit"]').click(),
  ]);

  await page.context().storageState({ path: 'tests/e2e/.auth/admin.json' });
});
