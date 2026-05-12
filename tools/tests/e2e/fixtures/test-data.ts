/**
 * Constants shared across the test suite.
 *
 * Keep this small: anything secret-ish or environment-specific belongs in
 * env vars, not source. The values here are demo-grade and match what the
 * stack ships with locally.
 */

export const DEFAULT_PASSWORD = 'pass1234';

/** PASS-style KYC demo identity. The provider in demo mode does a format
 *  check only, so a well-formed RRN front + back1 always verifies. */
export const KYC_DEMO_IDENTITY = {
  name: '홍길동',
  rrn_front: '950101',
  rrn_back1: '1',
  phone: '01012345678',
  carrier: 'SKT' as const,
};

/** Email prefix used for every spec-generated user. Lets the operator
 *  console filter / sweep e2e accounts if cleanup is ever desired. */
export const E2E_EMAIL_PREFIX = 'e2e+';

/** Generate a unique e2e email. Combines wall-clock ms + a short random
 *  suffix so two workers signing up in the same millisecond won't collide. */
export function makeE2EEmail(tag = 'user'): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `${E2E_EMAIL_PREFIX}${tag}-${Date.now()}-${rand}@example.com`;
}
