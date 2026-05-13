import { test, expect } from '@playwright/test';
import { SignInPage } from '../../helpers/page-objects/signin.js';
import { DEFAULT_PASSWORD, makeE2EEmail } from '../../fixtures/test-data.js';

test('user can sign up via the UI and lands on the KYC funnel', async ({ page }) => {
  const email = makeE2EEmail('signup');
  const signin = new SignInPage(page);
  await signin.goto();
  await signin.switchTab('signup');

  // The Sign-up form is now visible. Fill it.
  await signin.fillSignUp({ email, password: DEFAULT_PASSWORD, name: 'E2E Signup' });
  await signin.submitSignUp();

  // Server returns 201 + a token; the SPA stashes the session and
  // redirects to /app/kyc.html?next=/app/.
  await page.waitForURL(/\/app\/kyc\.html/, { timeout: 10_000 });

  // localStorage has the session payload.
  const sessionJson = await page.evaluate(() => {
    return (
      window.localStorage.getItem('zkcex.session') ||
      window.localStorage.getItem('zkcex_session')
    );
  });
  expect(sessionJson, 'session not persisted after signup').toBeTruthy();
  const parsed = JSON.parse(sessionJson!);
  expect(parsed.user.email).toBe(email.toLowerCase());
  expect(parsed.user.opex_user).toMatch(/^u-\d+$/);
  expect(parsed.user.kyc_status).toBe('none');
});

test('signup with an already-registered email surfaces a 409 error', async ({ page, request }) => {
  const email = makeE2EEmail('dup');

  // Seed the email via the API so we know it's taken.
  const r = await request.post('/auth/signup', {
    data: { email, password: DEFAULT_PASSWORD, name: 'Pre-existing' },
  });
  expect(r.ok()).toBeTruthy();

  // Now try via the UI.
  const signin = new SignInPage(page);
  await signin.goto();
  await signin.switchTab('signup');
  await signin.fillSignUp({ email, password: DEFAULT_PASSWORD, name: 'E2E Dup' });
  await signin.submitSignUp();

  // The form's help text flips into the error state with the i18n string.
  await expect(page.locator('#su-help.error')).toContainText(/이미 가입된 이메일|already registered/i);
  // We should still be on the signin page (not redirected).
  await expect(page).toHaveURL(/signin\.html/);
});
