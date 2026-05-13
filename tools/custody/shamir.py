"""Shamir secret sharing over the secp256k1 group order.

Pure stdlib. Used to split an ECDSA private key (a 32-byte scalar in Z/nZ
where n is the secp256k1 group order) into N shares such that any THRESHOLD
of them reconstructs the secret via Lagrange interpolation in Z/nZ.

This module is intentionally tiny and dependency-free. It is NOT
constant-time and is NOT side-channel resistant — for a demo of the
*architecture* of threshold custody, this is fine; for production you would
NEVER reconstruct the key in one place anyway, so the secret-share math
stops being load-bearing the moment you switch to FROST/GG18.

Field
-----
We work in Z/nZ where n is the secp256k1 group order
    n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

Shares
------
A share is a pair (x, y_bytes) where:
  - x is a non-zero integer in [1, n-1] (the polynomial evaluation point)
  - y_bytes is a 32-byte big-endian encoding of f(x) mod n

The polynomial is f(t) = secret + a_1 * t + a_2 * t^2 + ... + a_{k-1} * t^{k-1}
with all coefficients drawn uniformly at random from [0, n-1] using
secrets.randbelow.
"""

from __future__ import annotations

import secrets

# secp256k1 group order
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _modinv(a: int, m: int) -> int:
    """Modular inverse via extended Euclidean. Raises if a % m == 0."""
    a %= m
    if a == 0:
        raise ZeroDivisionError("no inverse for 0")
    # Python 3.8+: pow(a, -1, m) does this directly.
    return pow(a, -1, m)


def _eval_poly(coeffs: list[int], x: int, p: int) -> int:
    """Horner's evaluation of the polynomial whose coefficients (a_0..a_{k-1})
    are listed low-to-high. Returns f(x) mod p."""
    acc = 0
    for c in reversed(coeffs):
        acc = (acc * x + c) % p
    return acc


def split(secret_bytes: bytes, *, n: int, threshold: int) -> list[tuple[int, bytes]]:
    """Split a 32-byte secret into n shares with the given threshold.

    Each returned share is a tuple (index, payload_bytes) where index is a
    small integer 1..n and payload_bytes is the 32-byte big-endian encoding
    of f(index) mod N (the secp256k1 group order).
    """
    if not isinstance(secret_bytes, (bytes, bytearray)):
        raise TypeError("secret_bytes must be bytes")
    if len(secret_bytes) != 32:
        raise ValueError(f"secret must be exactly 32 bytes, got {len(secret_bytes)}")
    if n < 2:
        raise ValueError("n must be >= 2")
    if threshold < 2 or threshold > n:
        raise ValueError("threshold must satisfy 2 <= threshold <= n")
    secret_int = int.from_bytes(secret_bytes, "big")
    if secret_int == 0 or secret_int >= N:
        raise ValueError("secret must be in [1, N-1]")

    # Build polynomial: a_0 = secret, a_1..a_{k-1} random in [0, N-1].
    coeffs = [secret_int]
    for _ in range(threshold - 1):
        coeffs.append(secrets.randbelow(N))

    shares: list[tuple[int, bytes]] = []
    for i in range(1, n + 1):
        y = _eval_poly(coeffs, i, N)
        shares.append((i, y.to_bytes(32, "big")))
    return shares


def combine(shares: list[tuple[int, bytes]]) -> bytes:
    """Reconstruct the secret via Lagrange interpolation at x=0.

    Caller is responsible for supplying at least `threshold` shares
    (this function does not know the original threshold). With fewer than
    `threshold` shares the returned bytes are uniformly random in the field
    and will NOT equal the original secret — that's the point.
    """
    if not shares or len(shares) < 2:
        raise ValueError("need at least 2 shares to combine")
    xs: list[int] = []
    ys: list[int] = []
    for x, payload in shares:
        if not isinstance(x, int) or x <= 0 or x >= N:
            raise ValueError(f"bad share index: {x!r}")
        if not isinstance(payload, (bytes, bytearray)) or len(payload) != 32:
            raise ValueError("share payload must be 32 bytes")
        xs.append(x)
        ys.append(int.from_bytes(payload, "big"))
    if len(set(xs)) != len(xs):
        raise ValueError("duplicate share indices")

    # Lagrange interpolation at t = 0:
    #   f(0) = sum_j y_j * prod_{m!=j} (-x_m) / (x_j - x_m)
    secret = 0
    k = len(xs)
    for j in range(k):
        num = 1
        den = 1
        for m in range(k):
            if m == j:
                continue
            num = (num * (-xs[m])) % N
            den = (den * (xs[j] - xs[m])) % N
        term = (ys[j] * num) % N
        term = (term * _modinv(den, N)) % N
        secret = (secret + term) % N
    return secret.to_bytes(32, "big")


# Self-test runnable as `python3 -m tools.custody.shamir`.
def _selftest() -> None:
    s = secrets.token_bytes(32)
    # Make sure it's in range.
    while int.from_bytes(s, "big") == 0 or int.from_bytes(s, "big") >= N:
        s = secrets.token_bytes(32)
    shares = split(s, n=5, threshold=3)
    if len(shares) != 5:
        raise AssertionError("expected 5 shares")
    # Round trip with the first 3.
    rec = combine(shares[:3])
    if rec != s:
        raise AssertionError("round-trip failed")
    # Round trip with a different 3.
    rec2 = combine([shares[0], shares[2], shares[4]])
    if rec2 != s:
        raise AssertionError("round-trip with non-contiguous shares failed")
    # 5 of 5.
    rec3 = combine(shares)
    if rec3 != s:
        raise AssertionError("5-of-5 reconstruction failed")
    # 2 of 5 should NOT recover the secret (vanishingly small chance of
    # coincidence; expected behavior is junk).
    rec_bad = combine(shares[:2])
    if rec_bad == s:
        raise AssertionError("2-of-5 should NOT reconstruct")
    # Duplicate index detection.
    try:
        combine([shares[0], shares[0], shares[1]])
        raise AssertionError("expected duplicate-index rejection")
    except ValueError:
        pass
    print("shamir self-test ok")


if __name__ == "__main__":
    _selftest()
