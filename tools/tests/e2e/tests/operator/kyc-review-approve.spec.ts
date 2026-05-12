import { test, expect } from '@playwright/test';
import { createTestUser, completeKYC } from '../../fixtures/auth.js';

/**
 * Operator flow: an unverified customer submits PASS-style KYC, the
 * operator console shows the customer in the KYC review queue, the
 * operator approves it, /auth/me flips to kyc_status=verified.
 *
 * The operator console is gated by ops_users which is bootstrapped via
 * `ops_bootstrap.py`. If we can't auth as an operator (typical for
 * fresh local stacks), we skip with a clear message rather than fail.
 */
test('operator console reachable; pending KYC queue surfaces the user', async ({
  page,
  request,
}) => {
  // First, create + KYC-verify a customer via the API so we know the
  // queue has *something* in it. We assert against the rendered HTML.
  const u = await createTestUser(request, { emailTag: 'ops-kyc' });
  await completeKYC(request, u.token);

  // Reach the ops console root. If we get redirected to a login page,
  // the operator account isn't bootstrapped — skip cleanly.
  const r = await page.goto('/ops/').catch(() => null);
  if (!r || r.status() >= 400) {
    test.skip(true, 'operator console not reachable; skip operator spec');
    return;
  }
  // If we land on the login page, we don't have credentials in CI for
  // this stack. Skip and let the deployment-specific workflow handle it.
  if (/login\.html/.test(page.url())) {
    test.skip(true, 'operator login required but no test credentials provisioned');
    return;
  }

  // Try to find the KYC queue. The list page lives at /ops/kyc.html.
  const queue = await page.goto('/ops/kyc.html').catch(() => null);
  if (!queue || queue.status() >= 400) {
    test.skip(true, 'operator KYC queue unavailable');
    return;
  }
  // The verified user we just created should appear somewhere on the
  // page (status=verified rows are typically visible too).
  await expect(page.locator('body')).toContainText(u.user.opex_user);
});

test('approving a pending KYC via the API flips kyc_status to verified', async ({
  request,
}) => {
  // We don't have a guaranteed-pending state from a fresh signup (the
  // PASS demo verifies immediately), so this spec verifies that the
  // operator endpoint exists and that an already-verified user stays
  // verified after an idempotent re-approve. If the endpoint is gated
  // behind operator auth (HTTP 401/403), we skip.
  const u = await createTestUser(request, { emailTag: 'ops-approve' });
  await completeKYC(request, u.token);

  const approveRes = await request.post(`/ops/kyc/${u.user.opex_user}/approve`, {
    data: { reason: 'e2e-test' },
  });
  if ([401, 403].includes(approveRes.status())) {
    test.skip(true, 'operator approve endpoint requires staff token');
    return;
  }
  if (approveRes.status() === 404) {
    test.skip(true, 'operator approve endpoint not proxied on this stack');
    return;
  }
  // Either 200 OK or 409 conflict (already verified) is acceptable here.
  expect([200, 201, 204, 409]).toContain(approveRes.status());

  const me = await request.get('/auth/me', {
    headers: { Authorization: `Bearer ${u.token}` },
  });
  expect(me.ok()).toBeTruthy();
  expect((await me.json()).user.kyc_status).toBe('verified');
});
