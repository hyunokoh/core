# zkCEX threshold custody (demo-grade).
#
# This package implements an M-of-N custodial signing layer that REPLACES the
# single-key custodial signer used by chain_server.py when the env var
# CUSTODY_COORDINATOR_URL is set.
#
# IMPORTANT — what this is NOT:
# ------------------------------
# This is NOT real threshold ECDSA (FROST / GG18 / DKLs / Lindell-17). It is
# Shamir secret-sharing of a secp256k1 private key plus reconstruction-then-
# sign on the coordinator. The reconstructed key briefly lives in coordinator
# RAM during signing and is wiped immediately after broadcast.
#
# Real production custody (Fireblocks, Cobo, AWS CloudHSM, YubiHSM, …) NEVER
# materialises the full key, ever, on a network-connected machine. The
# integration shape we expose here — a coordinator HTTP API the exchange app
# calls in lieu of `eth_sendTransaction` — is intentionally identical to what
# you would build against a real MPC provider, so dropping in Fireblocks etc.
# is just a URL swap + signed-payload shape change.
#
# See README in this folder (if any) and the docstring at the top of
# coordinator.py for more.
