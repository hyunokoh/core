import type { Page } from '@playwright/test';

/**
 * Page object for the multi-screen KYC flow (method → carrier → info →
 * code → done). Each method drives one screen; the spec composes the
 * full flow.
 */
export class KycPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/app/kyc.html');
    // Wait until the method screen is actually shown — which only happens
    // after the module script attaches its click listeners. Until then,
    // clicking #pass-continue is a no-op.
    await this.page.waitForSelector('[data-screen="method"]:not([hidden])');
  }

  async startPass(): Promise<void> {
    // Method screen — click "PASS continue". The listener is attached by
    // the module script; we've already waited for the screen to be visible
    // in goto(), which guarantees init() has run.
    await this.page.locator('#pass-continue').click();
    await this.page.waitForSelector('[data-screen="carrier"]:not([hidden])');
  }

  async pickCarrier(carrier: 'SKT' | 'KT' | 'LGU'): Promise<void> {
    await this.page.click(`.carrier-card[data-carrier="${carrier}"]`);
    await this.page.waitForSelector('[data-screen="info"]:not([hidden])');
  }

  async fillInfo(opts: {
    name: string;
    rrn_front: string;
    rrn_back1: string;
    phone: string;
  }): Promise<void> {
    await this.page.fill('#kyc-name', opts.name);
    await this.page.fill('#kyc-rrn-front', opts.rrn_front);
    await this.page.fill('#kyc-rrn-back1', opts.rrn_back1);
    await this.page.fill('#kyc-phone', opts.phone);
    await this.page.click('#info-submit');
    await this.page.waitForSelector('[data-screen="code"]:not([hidden])');
  }

  /** Read back the demo-mode code that the server echoes onto the page. */
  async getDemoCode(): Promise<string> {
    // The banner is shown only when the server reported __demo=true.
    await this.page.waitForSelector('#demo-banner:not([hidden])', { timeout: 5_000 });
    const code = await this.page.locator('#demo-code').textContent();
    if (!code || !/^\d{6}$/.test(code.trim())) {
      throw new Error(`unexpected demo code: ${code}`);
    }
    return code.trim();
  }

  async submitCode(code: string): Promise<void> {
    await this.page.fill('#kyc-code', code);
    await this.page.click('#code-submit');
    await this.page.waitForSelector('[data-screen="done"]:not([hidden])', {
      timeout: 10_000,
    });
  }
}
