import { test, expect } from '@playwright/test';

/**
 * PWA install-prompt. Chromium fires `beforeinstallprompt` when the page
 * is installable; WebKit / Firefox don't. We fake the event with
 * dispatchEvent() so we can exercise the banner logic in CI.
 */
test('PWA install banner appears after a synthetic beforeinstallprompt', async ({
  page,
  browserName,
}) => {
  test.skip(browserName !== 'chromium', 'Only Chromium fires beforeinstallprompt');

  await page.goto('/app/');
  await page.waitForLoadState('domcontentloaded');

  // Dispatch a minimal BeforeInstallPromptEvent shim. The real Event
  // type is non-standard so we attach the prompt() method that the
  // install handler invokes when the user taps "Install".
  await page.evaluate(() => {
    const ev: Event & { prompt?: () => Promise<void>; userChoice?: Promise<{ outcome: string }> } =
      new Event('beforeinstallprompt');
    ev.prompt = async () => undefined;
    ev.userChoice = Promise.resolve({ outcome: 'dismissed' });
    window.dispatchEvent(ev);
  });

  // The banner is rendered lazily — give the listener a tick to mount it.
  await page.waitForSelector('.pwa-install-banner', { timeout: 5_000 });
  await expect(page.locator('.pwa-install-banner')).toBeVisible();
});
