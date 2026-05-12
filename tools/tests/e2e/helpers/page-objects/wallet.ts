import type { Page } from '@playwright/test';

export class WalletPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/app/wallet.html');
    await this.page.waitForLoadState('domcontentloaded');
  }
}
