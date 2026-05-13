import { test, expect } from '@playwright/test';
import { createTestUserAndLogin } from '../../fixtures/auth.js';
import { waitForBalance } from '../../fixtures/chain.js';

test('signup auto-seeds demo funds and they show up via the wallet API', async ({
  page,
  context,
  request,
}) => {
  // The auth server seeds demo funds asynchronously after /auth/signup
  // (see auth_server.seed_demo_funds — a daemon thread that posts to
  // /deposit/...). We probe the raw wallet ledger to confirm the post
  // landed before we open the wallet UI.
  const u = await createTestUserAndLogin(context, request, {
    emailTag: 'deposit',
  });

  // ETH is the most reliable seeded asset across local stacks. The USDT
  // path depends on the wallet bridge being up; we don't gate on that
  // here so this spec stays green in any stack with the wallet API.
  await waitForBalance(request, {
    opex: u.user.opex_user,
    asset: 'ETH',
    minAmount: 1,
    timeoutMs: 15_000,
  });

  // Visit the wallet page. We don't assert specific numbers (the demo
  // ledger can drift if a previous run topped up the user) — just that
  // the page renders and references the asset we seeded.
  await page.goto('/app/wallet.html');
  await page.waitForLoadState('domcontentloaded');
  await expect(page.locator('body')).toContainText(/ETH/);
});
