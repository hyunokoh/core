import type { Page } from '@playwright/test';

/**
 * Proof-of-Liabilities verify page. The page mounts the live status card,
 * polls /pol/latest-epoch, and exposes the action bar once an inclusion
 * proof is available.
 */
export class VerifyPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/app/verify.html');
    await this.page.waitForSelector('#status-card');
  }

  /** Wait until the action bar surfaces, i.e. an inclusion proof exists. */
  async waitForProofReady(timeoutMs = 30_000): Promise<void> {
    await this.page.waitForSelector('#action-bar:not([hidden])', { timeout: timeoutMs });
  }

  /** Trigger the JSON download. Returns the saved file path. */
  async downloadProof(): Promise<{ filename: string; path: string }> {
    const downloadPromise = this.page.waitForEvent('download');
    await this.page.click('#download-btn');
    const dl = await downloadPromise;
    const p = await dl.path();
    return { filename: dl.suggestedFilename(), path: p ?? '' };
  }
}
