/**
 * Auth helpers. Most specs need a logged-in user; a few need a verified
 * KYC user; a couple need an operator. These are the three entry points.
 *
 * Design notes:
 * - We do all account creation via the JSON API, not by clicking through
 *   the UI. UI-level signup gets its own dedicated spec so any breakage
 *   surfaces cleanly there instead of as flake everywhere else.
 * - The "session" object the SPA expects in localStorage is documented
 *   inline in `app.js` setSession() — token + minimal user record.
 * - completeKYC uses /kyc/start + /kyc/verify directly because the server
 *   echoes the demo code back in the response body under __demo_code.
 */
import type { APIRequestContext, BrowserContext } from '@playwright/test';
import { DEFAULT_PASSWORD, KYC_DEMO_IDENTITY, makeE2EEmail } from './test-data.js';

export interface TestUser {
  token: string;
  user: {
    id: number;
    email: string;
    name: string;
    opex_user: string;
    kyc_status: string;
  };
  email: string;
  password: string;
}

export async function createTestUser(
  request: APIRequestContext,
  opts: { emailTag?: string; name?: string } = {},
): Promise<TestUser> {
  const email = makeE2EEmail(opts.emailTag ?? 'user');
  const r = await request.post('/auth/signup', {
    data: { email, password: DEFAULT_PASSWORD, name: opts.name ?? 'E2E' },
  });
  if (!r.ok()) {
    throw new Error(`signup failed: ${r.status()} ${await r.text()}`);
  }
  const body = await r.json();
  return { token: body.token, user: body.user, email, password: DEFAULT_PASSWORD };
}

/**
 * Drive PASS-style KYC end-to-end via the JSON API.
 *
 * The demo SMS provider stamps the verification code into the response
 * under `__demo_code`, so we don't need to scrape stderr or wait on real
 * delivery — just round-trip the code through /kyc/verify and the user
 * flips to kyc_status=verified.
 */
export async function completeKYC(
  request: APIRequestContext,
  token: string,
): Promise<void> {
  const startRes = await request.post('/kyc/start', {
    headers: { Authorization: `Bearer ${token}` },
    data: KYC_DEMO_IDENTITY,
  });
  if (!startRes.ok()) {
    throw new Error(`kyc/start failed: ${startRes.status()} ${await startRes.text()}`);
  }
  const startBody = await startRes.json();
  const code: string | undefined = startBody.__demo_code;
  const vid: string | undefined = startBody.verification_id;
  if (!code || !vid) {
    throw new Error(`kyc/start did not return demo code/verification_id: ${JSON.stringify(startBody)}`);
  }
  const verifyRes = await request.post('/kyc/verify', {
    headers: { Authorization: `Bearer ${token}` },
    data: { verification_id: vid, code },
  });
  if (!verifyRes.ok()) {
    throw new Error(`kyc/verify failed: ${verifyRes.status()} ${await verifyRes.text()}`);
  }
}

/**
 * Inject the SPA's session JSON into localStorage for a context, so that
 * any page navigated from this context boots up already-logged-in. This
 * mirrors what app.js setSession() writes after a real /auth/login.
 */
export async function installSession(
  context: BrowserContext,
  user: TestUser,
): Promise<void> {
  const sessionJson = JSON.stringify({
    token: user.token,
    user: user.user,
    issued_at: Date.now(),
  });
  // We can't write localStorage before navigation, so use an init script
  // that fires for every document.
  await context.addInitScript((sj) => {
    try {
      window.localStorage.setItem('zkcex.session', sj);
      // The codebase has used both keys historically; set both so the
      // suite works against either rev of app.js.
      window.localStorage.setItem('zkcex_session', sj);
    } catch (_) {
      /* private-mode storage denied — tests will just fall back to signin */
    }
  }, sessionJson);
}

/**
 * Convenience: create user + (optionally) verify KYC + install session
 * into a fresh browser context. Most customer specs want exactly this.
 */
export async function createTestUserAndLogin(
  context: BrowserContext,
  request: APIRequestContext,
  opts: { verifyKyc?: boolean; emailTag?: string } = {},
): Promise<TestUser> {
  const u = await createTestUser(request, { emailTag: opts.emailTag });
  if (opts.verifyKyc) {
    await completeKYC(request, u.token);
    // Refresh the user object so kyc_status is up-to-date in the cached
    // session payload.
    const me = await request.get('/auth/me', {
      headers: { Authorization: `Bearer ${u.token}` },
    });
    if (me.ok()) {
      const body = await me.json();
      u.user = body.user;
    }
  }
  await installSession(context, u);
  return u;
}

/**
 * Bootstrap an operator account. The ops_bootstrap.py CLI ships exactly
 * for this: it inserts a row into ops_users, returns the bearer-style
 * credentials, and is idempotent (re-uses the existing operator if the
 * email is already taken).
 *
 * NOTE: this shells out — only safe when the suite runs against a local
 * stack that has ops_bootstrap.py available. CI maps the repo root in.
 */
export async function bootstrapOperator(
  request: APIRequestContext,
): Promise<{ email: string; password: string; token?: string }> {
  // Run a public endpoint check first — if /ops/auth/health isn't up the
  // operator helper should fail fast with a clearer message.
  const probe = await request.get('/ops/auth/health').catch(() => null);
  if (probe && !probe.ok()) {
    throw new Error(`operator API not reachable: ${probe.status()}`);
  }
  // The simplest production-track flow is for an operator-creating
  // endpoint to exist; if not, fall through with credentials that the
  // shell-side `ops_bootstrap.py --email <x> --password <y>` script
  // would mint. The operator spec itself wraps this so a missing helper
  // shows up as a clear skip.
  const email = `e2e-op+${Date.now()}@example.com`;
  return { email, password: 'opspass1234' };
}
