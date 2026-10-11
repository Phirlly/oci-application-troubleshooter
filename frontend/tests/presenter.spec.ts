import { expect, test } from '@playwright/test';

const password = 'synthetic-browser-presenter-password';

test('keyboard sign-in, errors, refresh, logout and cookie protection', async ({ page, context }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Sign in to your demo' })).toBeVisible();
  const input = page.getByLabel('Presenter password');
  await input.fill('incorrect-synthetic-password');
  await input.press('Enter');
  await expect(page.getByRole('alert')).toHaveText('That password was not accepted. Try again.');
  await expect(page.getByRole('alert')).toBeFocused();
  await input.fill(password);
  await input.press('Enter');
  await expect(page.getByRole('heading', { name: 'Application connected' })).toBeVisible();
  await expect(page.getByText('Signed in. The incident demonstration is not yet available.')).toBeVisible();
  const cookies = await context.cookies();
  expect(cookies).toHaveLength(1);
  expect(cookies[0]).toMatchObject({ name: '__Host-demo-session', secure: true, httpOnly: true, sameSite: 'Strict' });
  expect(await page.evaluate(() => ({ cookie: document.cookie, local: localStorage.length, session: sessionStorage.length })))
    .toEqual({ cookie: '', local: 0, session: 0 });
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Application connected' })).toBeVisible();
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to your demo' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Sign in to your demo' })).toBeVisible();
  expect(errors).toEqual([]);
});

test('narrow screen and browser expiration return to sign-in', async ({ page, context }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await page.goto('/');
  await page.getByLabel('Presenter password').fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Application connected' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await context.clearCookies();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Sign in to your demo' })).toBeVisible();
});

test('network failure gives a retry without losing the page', async ({ page }) => {
  await page.route('**/api/session', route => route.abort());
  await page.goto('/');
  await expect(page.getByRole('alert')).toHaveText('Cannot reach the demo. Try again in a moment.');
  await page.unroute('**/api/session');
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to your demo' })).toBeVisible();
});

test('sign-out reconciles a session changed by another tab', async ({ page, context }) => {
  const second = await context.newPage();
  await page.goto('/');
  await second.goto('/');
  await expect(second.getByLabel('Presenter password')).toBeVisible();
  await page.getByLabel('Presenter password').fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Application connected' })).toBeVisible();
  await second.getByLabel('Presenter password').fill(password);
  await second.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(second.getByRole('heading', { name: 'Application connected' })).toBeVisible();
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to your demo' })).toBeVisible();
  await second.reload();
  await expect(second.getByRole('heading', { name: 'Sign in to your demo' })).toBeVisible();
});
