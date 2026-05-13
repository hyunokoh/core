import type { Page } from '@playwright/test';

/**
 * Sign-in / sign-up page object. The page combines both flows in a tabbed
 * card — switchTab() flips which form is visible.
 */
export class SignInPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/app/signin.html');
    await this.page.waitForSelector('#signin-card-main:not([hidden])');
  }

  async switchTab(tab: 'signin' | 'signup'): Promise<void> {
    await this.page.locator(`.auth-tab[data-tab="${tab}"]`).click();
  }

  async fillSignUp(opts: { email: string; password: string; name: string }): Promise<void> {
    await this.page.fill('#su-email', opts.email);
    await this.page.fill('#su-password', opts.password);
    await this.page.fill('#su-name', opts.name);
  }

  async submitSignUp(): Promise<void> {
    await this.page.click('#su-submit');
  }

  async fillSignIn(opts: { email: string; password: string }): Promise<void> {
    await this.page.fill('#si-email', opts.email);
    await this.page.fill('#si-password', opts.password);
  }

  async submitSignIn(): Promise<void> {
    await this.page.click('#si-submit');
  }
}
