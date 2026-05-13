import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import { createTestUserAndLogin } from '../../fixtures/auth.js';
import { VerifyPage } from '../../helpers/page-objects/verify.js';

test('user downloads PoL inclusion proof JSON and it parses', async ({
  page,
  context,
  request,
}) => {
  await createTestUserAndLogin(context, request, {
    verifyKyc: true,
    emailTag: 'verify',
  });

  // The PoL feed snapshots periodically. If the snapshot window is long
  // (e.g. 30s in dev) we may need to wait through one epoch before the
  // user shows up in a leaf. Give it real headroom.
  const verify = new VerifyPage(page);
  await verify.goto();

  // Some envs surface the action bar within a couple of seconds; others
  // need a full epoch. Either way, 45s is the upper bound. If the
  // snapshot feed isn't running, we skip rather than fail — local devs
  // commonly run without the PoL feed wired up.
  let actionBarReady = false;
  try {
    await verify.waitForProofReady(45_000);
    actionBarReady = true;
  } catch (e) {
    // Probe /pol/latest-epoch directly — if it 404s, PoL isn't wired up.
    const probe = await request.get('/pol/latest-epoch').catch(() => null);
    if (!probe || !probe.ok()) {
      test.skip(true, 'PoL feed not running locally; download spec skipped');
    } else {
      throw e;
    }
  }

  if (!actionBarReady) return;

  const { filename, path } = await verify.downloadProof();
  // The download filename follows pol-proof-<opex>-<epoch>.json by spec.
  expect(filename).toMatch(/^pol-proof-.+\.json$/);
  expect(path).toBeTruthy();

  const text = await fs.readFile(path, 'utf-8');
  const proof = JSON.parse(text);
  // The proof envelope ships these load-bearing fields (see
  // pol_server.h_my_proof for the canonical shape):
  //   scheme, hash, sig_scheme, server_pubkey, epoch{...,signature},
  //   leaf{user_hash, balance, asset_breakdown, epoch_nonce},
  //   sibling_path[], verification_recipe[]
  expect(proof.scheme).toBe('zkcex-pol-v1');
  expect(proof.epoch?.signature).toBeTruthy();
  expect(proof.epoch?.root_hash).toBeTruthy();
  expect(Array.isArray(proof.sibling_path)).toBe(true);
  expect(proof.leaf?.user_hash).toBeTruthy();
  expect(Array.isArray(proof.verification_recipe)).toBe(true);
  expect(proof.server_pubkey).toBeTruthy();
});
