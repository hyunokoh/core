import type { Page } from '@playwright/test';

export class OpsConsole {
  constructor(private readonly page: Page) {}

  async login(email: string, password: string): Promise<void> {
    await this.page.goto('/ops/login.html');
    await this.page.fill('input[type="email"], #email', email).catch(() => {});
    await this.page.fill('input[type="password"], #password', password).catch(() => {});
    await this.page.click('button[type="submit"], #login-btn').catch(() => {});
  }

  async openKycQueue(): Promise<void> {
    await this.page.goto('/ops/kyc.html');
    await this.page.waitForLoadState('domcontentloaded');
  }
}
