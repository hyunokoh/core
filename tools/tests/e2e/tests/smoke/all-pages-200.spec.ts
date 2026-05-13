import { test, expect } from '@playwright/test';

/**
 * Smoke layer: every static page in the SPA should respond 200 and load
 * without unhandled JS errors. This is the cheapest tier — if something
 * here breaks, all the deeper specs are guaranteed to flake.
 *
 * We deliberately do NOT assert any text content (i18n keeps moving),
 * just the HTTP status + a clean window error stream.
 */

const PAGES: readonly string[] = [
  '/',
  '/en/',
  '/app/',
  '/app/signin.html',
  '/app/kyc.html',
  '/app/trade.html',
  '/app/wallet.html',
  '/app/deposit.html',
  '/app/withdraw.html',
  '/app/verify.html',
  '/app/proof-of-reserves.html',
  '/app/api-keys.html',
  '/app/mcp.html',
  '/app/custody.html',
  '/app/reports.html',
  '/app/futures.html',
  '/app/notifications.html',
  '/app/safu.html',
  '/app/api-docs.html',
  '/app/status.html',
  '/app/travel-rule.html',
  '/app/load-report.html',
  '/app/zk-trade.html',
  '/app/game-day.html',
];

/**
 * Some console errors are environment-noise that don't gate the suite:
 *   - WebSocket connection failures (the local stack often doesn't have
 *     ws_feed up during the smoke pass).
 *   - 401 from auth-guarded fetches on pages that require login.
 *   - Service-worker registration failures (we're not behind https locally).
 *   - 404s from optional providers (Sumsub, push, etc.).
 * The deeper specs handle these scenarios properly; smoke just wants a
 * page that *parses* and *executes* without throwing.
 */
const SUPPRESSED_PATTERNS: readonly RegExp[] = [
  /WebSocket/i,
  /401/,
  /403/,
  /404/,
  /service worker/i,
  /Failed to load resource/i,
  /net::ERR_/i,
  /sumsub/i,
];

for (const p of PAGES) {
  test(`page ${p} loads 200 with no fatal JS errors`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => {
      const msg = String(e);
      if (!SUPPRESSED_PATTERNS.some((re) => re.test(msg))) errors.push(msg);
    });
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return;
      const text = msg.text();
      if (!SUPPRESSED_PATTERNS.some((re) => re.test(text))) errors.push(text);
    });

    const resp = await page.goto(p);
    expect(resp, `no response for ${p}`).not.toBeNull();
    expect(resp!.status(), `bad status for ${p}`).toBe(200);

    await page.waitForLoadState('domcontentloaded');
    // Brief settling so deferred module imports get a chance to throw.
    await page.waitForTimeout(250);

    expect(
      errors,
      `unfiltered JS errors on ${p}:\n${errors.join('\n')}`,
    ).toEqual([]);
  });
}
