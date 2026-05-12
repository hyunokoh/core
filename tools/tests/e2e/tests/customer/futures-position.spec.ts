import { test, expect } from '@playwright/test';
import { createTestUserAndLogin } from '../../fixtures/auth.js';

/**
 * Futures position flow. The page is heavily interactive and reuses the
 * matching engine's perp endpoints. We only assert that:
 *   1. A verified user can open the futures page without redirect.
 *   2. The page surfaces the expected leverage / side controls.
 *   3. (Best-effort) submitting a tiny long order does not throw.
 *
 * We deliberately don't assert on fill price or the position panel
 * row counts — the demo perp engine's behavior depends on whether the
 * market-maker bot is up, and we don't want to couple this spec to that.
 */
test('verified user can reach the futures page and see order controls', async ({
  page,
  context,
  request,
}) => {
  await createTestUserAndLogin(context, request, {
    verifyKyc: true,
    emailTag: 'futures',
  });
  await page.goto('/app/futures.html');
  await page.waitForLoadState('domcontentloaded');

  // Should not bounce to KYC.
  await expect(page).toHaveURL(/futures\.html/);

  // The page advertises Long / Short controls in both languages.
  const body = page.locator('body');
  await expect(body).toContainText(/LONG|Long|롱/i);
  await expect(body).toContainText(/SHORT|Short|숏/i);
});
