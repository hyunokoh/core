import { test, expect } from '@playwright/test';
import { createTestUserAndLogin } from '../../fixtures/auth.js';
import { ZkTradePage } from '../../helpers/page-objects/zk-trade.js';

test('ZK trade page renders the phase bar and exposes the commit form', async ({
  page,
  context,
  request,
}) => {
  await createTestUserAndLogin(context, request, {
    verifyKyc: true,
    emailTag: 'zk',
  });

  const zk = new ZkTradePage(page);
  await zk.goto();

  // The auth gate (#auth-gate) should be hidden because we have a session.
  await expect(page.locator('#auth-gate')).toBeHidden();

  // The phase bar always renders one of COMMIT / REVEAL. We don't pin to
  // a specific phase because the cycle wall-clock is stack-dependent.
  const phase = await page.locator('#phase-badge').textContent();
  expect(phase?.trim().toUpperCase()).toMatch(/^(COMMIT|REVEAL)$/);

  // The order form is wired up with the symbol selector + side toggle.
  await expect(page.locator('#of-symbol')).toBeVisible();
  await expect(page.locator('#of-price')).toBeVisible();
  await expect(page.locator('#of-qty')).toBeVisible();
  await expect(page.locator('#of-commit')).toBeVisible();
});

test('user can fill the commit form (without submitting)', async ({
  page,
  context,
  request,
}) => {
  // Submitting a real commit posts to /zkob/commit and queues a reveal
  // for the next phase. That's covered in unit tests over in
  // zk_orderbook.py — here we just make sure the UI lets us fill it in.
  await createTestUserAndLogin(context, request, {
    verifyKyc: true,
    emailTag: 'zkfill',
  });
  const zk = new ZkTradePage(page);
  await zk.goto();
  await page.locator('#of-symbol').selectOption({ index: 0 });
  await page.locator('#of-price').fill('100');
  await page.locator('#of-qty').fill('0.1');
  await expect(page.locator('#of-commit')).toBeEnabled();
});
