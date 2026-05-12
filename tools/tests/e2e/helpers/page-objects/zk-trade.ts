import type { Page } from '@playwright/test';

/**
 * Page object for the ZK commit-reveal trade UI. The page has a continuous
 * phase machine running (commit window → reveal window → batch). The auth
 * gate hides the order form until a session is present, so installSession()
 * must be called on the browser context before goto().
 */
export class ZkTradePage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/app/zk-trade.html');
    await this.page.waitForSelector('#phase-bar');
  }

  /** Wait for the phase badge to read either COMMIT or REVEAL. */
  async waitForPhase(phase: 'COMMIT' | 'REVEAL', timeoutMs = 30_000): Promise<void> {
    await this.page.waitForFunction(
      (p) => {
        const el = document.getElementById('phase-badge');
        return el && el.textContent?.trim().toUpperCase() === p;
      },
      phase,
      { timeout: timeoutMs },
    );
  }
}
