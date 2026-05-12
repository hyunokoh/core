import type { Page } from '@playwright/test';

export class FuturesPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/app/futures.html');
    await this.page.waitForLoadState('domcontentloaded');
  }
}
