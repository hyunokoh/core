import { test, expect } from '@playwright/test';
import { createTestUserAndLogin } from '../../fixtures/auth.js';

test('verified user can open the withdraw page (KYC gate)', async ({
  page,
  context,
  request,
}) => {
  await createTestUserAndLogin(context, request, {
    verifyKyc: true,
    emailTag: 'withdraw',
  });
  await page.goto('/app/withdraw.html');
  await page.waitForLoadState('domcontentloaded');
  // Page should not redirect us to the KYC funnel (because we ARE verified).
  await expect(page).toHaveURL(/withdraw\.html/);
});

test('unverified user gets bounced to the KYC funnel from /withdraw', async ({
  page,
  context,
  request,
}) => {
  await createTestUserAndLogin(context, request, {
    verifyKyc: false,
    emailTag: 'withdraw-nokyc',
  });
  await page.goto('/app/withdraw.html');
  // The SPA's KYC gate either redirects or surfaces the unverified banner.
  // We accept either: a URL change, or a visible KYC-required affordance.
  await page.waitForLoadState('domcontentloaded');
  const url = page.url();
  if (!/kyc\.html/.test(url)) {
    await expect(page.locator('body')).toContainText(/본인인증|KYC|verification/i);
  }
});
