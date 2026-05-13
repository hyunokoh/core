/**
 * Chain / wallet seeding helpers.
 *
 * The demo stack exposes a fire-and-forget /deposit/{amount}_test-ethereum_{asset}/{user}_MAIN
 * endpoint on the wallet API (proxied at /deposit/ from the homepage server).
 * Each deposit is fixed at integer amounts of the asset. Signing up already
 * seeds 10 ETH + 10 USDT so most specs need no extra funding; this helper
 * is for specs that want a specific balance.
 */
import type { APIRequestContext } from '@playwright/test';

/**
 * Seed N units of each asset into the user's MAIN account.
 *
 * Resolves once all deposit calls return. If the wallet API is overloaded
 * or refuses one of the calls, the spec calling this will fail with a
 * clear error rather than silently undercounting.
 */
export async function seedFunds(
  request: APIRequestContext,
  opts: { opex: string },
  amounts: Record<string, number>,
): Promise<void> {
  for (const [asset, qty] of Object.entries(amounts)) {
    for (let i = 0; i < qty; i++) {
      const ref = `e2e-${asset}-${i}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
      const path =
        `/deposit/1_test-ethereum_${asset}/${encodeURIComponent(opts.opex)}_MAIN` +
        `?description=e2e&transferRef=${encodeURIComponent(ref)}`;
      const r = await request.post(path);
      if (!r.ok()) {
        throw new Error(`deposit seed failed for ${asset}: ${r.status()} ${await r.text()}`);
      }
    }
  }
}

/**
 * Wait for the user's wallet balance for `asset` to reach at least
 * `minAmount`. Polls /v1/owner/{opex}/wallets — the raw ledger view that
 * the wallet API exposes without auth. We don't go via the matching API
 * because its account endpoint is bearer-auth gated and currently 500s
 * on demo stacks where the matching engine's auth wiring is partial.
 */
export async function waitForBalance(
  request: APIRequestContext,
  opts: { opex: string; asset: string; minAmount: number; timeoutMs?: number },
): Promise<void> {
  const deadline = Date.now() + (opts.timeoutMs ?? 10_000);
  let lastErr = '';
  while (Date.now() < deadline) {
    const r = await request.get(`/v1/owner/${encodeURIComponent(opts.opex)}/wallets`);
    if (r.ok()) {
      const rows = (await r.json()) as Array<{ asset: string; balance: number }>;
      const row = rows.find((b) => b.asset === opts.asset);
      const bal = row ? Number(row.balance) : 0;
      if (bal >= opts.minAmount) return;
      lastErr = `${opts.asset} balance=${bal} < ${opts.minAmount}`;
    } else {
      lastErr = `${r.status()} ${await r.text()}`;
    }
    await new Promise((res) => setTimeout(res, 300));
  }
  throw new Error(
    `timed out waiting for ${opts.asset} >= ${opts.minAmount}; last=${lastErr}`,
  );
}
