import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config for the zkCEX E2E suite.
 *
 * Tests run serially **within a project** because many specs mutate live
 * state (sign up real users, seed deposits, open futures positions). They
 * still parallelize across projects (chromium / webkit / mobile-chromium)
 * when invoked via the multi-project workflow in CI.
 *
 * The stack must be reachable at BASE_URL (defaults to localhost:5500, where
 * serve_homepage.py listens and reverse-proxies to the API processes).
 */
export default defineConfig({
  testDir: './tests',
  // Keep tests within a project serial so balance / KYC state changes
  // don't race across workers. Each spec invents its own user so cross-
  // spec isolation is fine, but a single spec can have multi-step state.
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : 1,
  // Global per-test timeout. PoL inclusion can take a full epoch (~20s),
  // ZK commit-reveal can take ~25s, so 60s gives plenty of headroom.
  timeout: 60_000,
  expect: {
    timeout: 10_000,
  },
  reporter: [
    ['html', { open: 'never', outputFolder: 'playwright-report' }],
    ['list'],
    ['junit', { outputFile: 'reports/junit.xml' }],
  ],
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:5500',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
    // Default to a Korean-locale browser since the UI is bilingual KO/EN
    // and many assertions match Korean labels first.
    locale: 'ko-KR',
    timezoneId: 'Asia/Seoul',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
    {
      name: 'mobile-chromium',
      use: { ...devices['Pixel 7'] },
    },
  ],
});
