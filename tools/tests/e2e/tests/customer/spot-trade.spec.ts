import { test, expect } from '@playwright/test';
import { createTestUserAndLogin } from '../../fixtures/auth.js';

test('verified user can open the trade page and see the order panel', async ({
  page,
  context,
  request,
}) => {
  // Spot trading requires a verified user *and* a funded account. The
  // signup seed gives us 10 ETH / 10 USDT which is plenty for the demo
  // matching engine.
  await createTestUserAndLogin(context, request, {
    verifyKyc: true,
    emailTag: 'spot',
  });

  await page.goto('/app/trade.html?symbol=BTCUSDT');
  await page.waitForLoadState('domcontentloaded');

  // The order panel is the unit-under-test here. The chart depends on
  // ws-feed which may or may not be wired up; we don't assert on it.
  await expect(page.locator('body')).toContainText(/BUY|매수/i);
  await expect(page.locator('body')).toContainText(/SELL|매도/i);
});
