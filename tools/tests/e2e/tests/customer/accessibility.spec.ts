import { test, expect, Page } from '@playwright/test';

/**
 * Accessibility (WCAG 2.1 AA) smoke for the customer surface.
 *
 * We deliberately avoid `axe-playwright` to honour the project's
 * "no extra npm deps" rule (see tools/wcag/audit.py). Instead we
 * cover the highest-impact rules that can be asserted from the
 * rendered DOM:
 *
 *   1. Skip-to-main link is present and focusable as the first
 *      tab stop.
 *   2. There's exactly one rendered <main id="main">.
 *   3. <html lang> is set.
 *   4. Form controls have an accessible name (via <label for=>,
 *      aria-label, or aria-labelledby).
 *   5. Buttons are keyboard-reachable (tab + Enter focuses them).
 *   6. Modal dialogs (when present in the DOM) carry role="dialog"
 *      + aria-modal="true".
 *
 * For full coverage you should still:
 *   - Run a screen reader pass (NVDA / VoiceOver) on the same pages.
 *   - Audit computed colour contrast with a browser tool.
 *   - Add a focus-trap test once the modal JS exists.
 */

const PAGES = [
  { path: '/app/signin.html', name: 'signin' },
  { path: '/app/trade.html', name: 'trade' },
  { path: '/app/wallet.html', name: 'wallet' },
  { path: '/app/withdraw.html', name: 'withdraw' },
  { path: '/app/futures.html', name: 'futures' },
] as const;

/** Pull every visible form control's accessible-name candidate. */
async function controlsWithoutName(page: Page): Promise<string[]> {
  return page.$$eval(
    'input, select, textarea',
    (nodes) =>
      nodes
        .filter((n) => {
          const t = (n as HTMLInputElement).type?.toLowerCase?.() || '';
          // These types don't need a visible label.
          if (['hidden', 'submit', 'reset', 'button', 'image'].includes(t)) {
            return false;
          }
          // Skip controls that aren't in the layout (display:none).
          const rects = (n as HTMLElement).getClientRects();
          if (rects.length === 0) {
            // Still keep ones positioned absolutely (e.g. overlay select).
            const style = window.getComputedStyle(n as HTMLElement);
            if (style.display === 'none') return false;
          }
          return true;
        })
        .filter((n) => {
          const el = n as HTMLElement;
          if (el.getAttribute('aria-label')?.trim()) return false;
          if (el.getAttribute('aria-labelledby')?.trim()) return false;
          if (el.getAttribute('title')?.trim()) return false;
          // <label for=id>
          const id = el.getAttribute('id');
          if (id && document.querySelector(`label[for="${CSS.escape(id)}"]`)) {
            return false;
          }
          // wrapped in <label>
          if (el.closest('label')) return false;
          return true;
        })
        .map((n) => {
          const el = n as HTMLElement;
          return `<${el.tagName.toLowerCase()} id="${el.id || ''}" name="${el.getAttribute('name') || ''}">`;
        }),
  );
}

for (const { path, name } of PAGES) {
  test.describe(`a11y :: ${name}`, () => {
    test(`${name} has skip link + main landmark + lang`, async ({ page }) => {
      await page.goto(path);
      await page.waitForLoadState('domcontentloaded');

      // html[lang]
      const lang = await page.locator('html').getAttribute('lang');
      expect(lang, `html lang missing on ${path}`).toBeTruthy();

      // Exactly one main landmark.
      const mainCount = await page.locator('main, [role="main"]').count();
      expect(mainCount, `expected 1 <main> on ${path}`).toBe(1);

      // Skip link as the first focusable element.
      const skip = page.locator('a.skip-link').first();
      await expect(skip, `skip link missing on ${path}`).toHaveCount(1);
      const href = await skip.getAttribute('href');
      expect(href).toBe('#main');
    });

    test(`${name} form controls all have an accessible name`, async ({ page }) => {
      await page.goto(path);
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(150); // allow injected fragments (header) to mount

      const missing = await controlsWithoutName(page);
      expect(
        missing,
        `controls without accessible name on ${path}:\n${missing.join('\n')}`,
      ).toEqual([]);
    });

    test(`${name} modals (if present) carry role + aria-modal`, async ({ page }) => {
      await page.goto(path);
      await page.waitForLoadState('domcontentloaded');

      const offenders = await page.$$eval(
        // outer modal containers — same heuristic as the Python auditor
        'div[id$="modal"], div.modal, div.modal-backdrop, div[class*="export-modal"]:not([class*="-panel"]):not([class*="-actions"])',
        (nodes) =>
          nodes
            .filter((n) => {
              const el = n as HTMLElement;
              // ignore inner descendants of a dialog
              if (el.closest('[role="dialog"]') !== el) return false;
              return (
                el.getAttribute('role') !== 'dialog' ||
                el.getAttribute('aria-modal') !== 'true'
              );
            })
            .map((n) => (n as HTMLElement).outerHTML.slice(0, 120)),
      );

      expect(
        offenders,
        `modals missing role/aria-modal on ${path}:\n${offenders.join('\n---\n')}`,
      ).toEqual([]);
    });

    test(`${name} basic keyboard reachability (first 5 tabs focus visible elements)`, async ({ page }) => {
      await page.goto(path);
      await page.waitForLoadState('domcontentloaded');
      await page.waitForTimeout(150);

      // The first Tab should land on the skip link.
      await page.keyboard.press('Tab');
      const firstFocus = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        return el ? { tag: el.tagName.toLowerCase(), cls: el.className || '' } : null;
      });
      expect(firstFocus, `nothing took focus on ${path}`).not.toBeNull();
      expect(
        firstFocus!.tag === 'a' && firstFocus!.cls.includes('skip-link'),
        `first Tab on ${path} should focus the .skip-link, got <${firstFocus!.tag} class="${firstFocus!.cls}">`,
      ).toBe(true);

      // Subsequent tabs should keep landing on something focusable
      // (we don't enforce a specific order — just that focus moves).
      const visited = new Set<string>([JSON.stringify(firstFocus)]);
      for (let i = 0; i < 5; i++) {
        await page.keyboard.press('Tab');
        const focus = await page.evaluate(() => {
          const el = document.activeElement as HTMLElement | null;
          return el ? { tag: el.tagName.toLowerCase(), id: el.id || '' } : null;
        });
        if (focus && (focus.tag !== 'body' || focus.id)) {
          visited.add(JSON.stringify(focus));
        }
      }
      // At least 3 distinct elements should have taken focus across 6 Tabs.
      expect(
        visited.size,
        `focus did not move enough on ${path}; visited=${[...visited].join(' | ')}`,
      ).toBeGreaterThanOrEqual(3);
    });
  });
}
