import type { Page } from '@playwright/test';

export class TradePage {
  constructor(private readonly page: Page) {}

  async goto(symbol = 'BTCUSDT'): Promise<void> {
    await this.page.goto(`/app/trade.html?symbol=${symbol}`);
    // Wait for the chart shell to render. We don't depend on actual market
    // data — the order panel is the unit-under-test.
    await this.page.waitForLoadState('domcontentloaded');
  }
}
