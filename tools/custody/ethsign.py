"""Tiny pure-stdlib secp256k1 + Keccak256 + RLP + EIP-1559 tx signer.

Used by the custody coordinator to sign Ethereum transactions after
reconstructing the private key from a quorum of Shamir shares.

This is NOT a generally optimised secp256k1 — Python big-int arithmetic is
slow but adequate for one signature per withdrawal request on a demo. A
production deployment uses HSM/MPC hardware that does this in microseconds.

Provides:
    keccak256(b) -> 32 bytes
    privkey_to_pubkey(priv: bytes) -> 64 bytes (uncompressed X||Y)
    pubkey_to_address(pub: bytes) -> "0x..." (lowercase 40-hex)
    privkey_to_address(priv: bytes) -> "0x..."
    sign_eip1559_tx(priv, *, chain_id, nonce, max_priority_fee_per_gas,
                    max_fee_per_gas, gas_limit, to, value, data) -> dict
        { "raw_tx": "0x...", "tx_hash": "0x...", "r": int, "s": int, "v": int }

All inputs accept either ints or hex strings ("0x..."). Output `raw_tx` is
ready to pass to eth_sendRawTransaction.
"""

from __future__ import annotations

# secp256k1 domain parameters
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


# ---------- secp256k1 (affine + Jacobian helpers) -------------------------


def _modinv(a: int, m: int) -> int:
    a %= m
    if a == 0:
        raise ZeroDivisionError("no inverse for 0")
    return pow(a, -1, m)


def _point_add(P1, P2):
    """Affine point add. None means point-at-infinity."""
    if P1 is None:
        return P2
    if P2 is None:
        return P1
    x1, y1 = P1
    x2, y2 = P2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        # doubling
        m = (3 * x1 * x1) * _modinv(2 * y1 % P, P) % P
    else:
        m = (y2 - y1) * _modinv((x2 - x1) % P, P) % P
    x3 = (m * m - x1 - x2) % P
    y3 = (m * (x1 - x3) - y1) % P
    return (x3, y3)


def _scalar_mult(k: int, point) -> tuple[int, int] | None:
    """Constant-ish double-and-add scalar multiplication. Returns affine point."""
    if k % N == 0 or point is None:
        return None
    k = k % N
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def privkey_to_pubkey(priv: bytes) -> bytes:
    """secp256k1 priv -> uncompressed (no 0x04 prefix) 64-byte X||Y."""
    if len(priv) != 32:
        raise ValueError("priv must be 32 bytes")
    d = int.from_bytes(priv, "big")
    if not (1 <= d < N):
        raise ValueError("priv out of range")
    Q = _scalar_mult(d, (GX, GY))
    if Q is None:
        raise ValueError("priv * G is infinity")
    return Q[0].to_bytes(32, "big") + Q[1].to_bytes(32, "big")


# ---------- Keccak256 (Ethereum padding 0x01) -----------------------------

_RC = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)
_R = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rotl64(x: int, n: int) -> int:
    n &= 63
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def _keccak_f(state):
    for rnd in range(24):
        C = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rotl64(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= D[x]
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rotl64(state[x][y], _R[x][y])
        for x in range(5):
            for y in range(5):
                state[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y])
        state[0][0] ^= _RC[rnd]


def keccak256(data: bytes) -> bytes:
    capacity = 512
    suffix = 0x01  # Ethereum keccak (NOT FIPS SHA3 which uses 0x06)
    output_len = 32
    rate = (1600 - capacity) // 8
    state = [[0] * 5 for _ in range(5)]
    msg = bytes(data) + bytes([suffix])
    pad_len = (-len(msg)) % rate
    msg = bytearray(msg + bytes(pad_len))
    msg[-1] |= 0x80
    for offset in range(0, len(msg), rate):
        block = msg[offset : offset + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8 : i * 8 + 8], "little")
            x = i % 5
            y = i // 5
            state[x][y] ^= lane
        _keccak_f(state)
    out = bytearray()
    while len(out) < output_len:
        for y in range(5):
            for x in range(5):
                if len(out) >= output_len:
                    break
                out += state[x][y].to_bytes(8, "little")
                if (x + y * 5 + 1) * 8 >= rate:
                    break
            if len(out) >= output_len:
                break
        if len(out) < output_len:
            _keccak_f(state)
    return bytes(out[:output_len])


def pubkey_to_address(pub: bytes) -> str:
    """64-byte uncompressed pubkey (X||Y, no 0x04 prefix) -> 0x... address."""
    if len(pub) != 64:
        raise ValueError("pubkey must be 64 bytes (uncompressed X||Y)")
    h = keccak256(pub)
    return "0x" + h[12:].hex()


def privkey_to_address(priv: bytes) -> str:
    return pubkey_to_address(privkey_to_pubkey(priv))


# ---------- RLP -----------------------------------------------------------


def _rlp_length_prefix(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    bl = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(bl)]) + bl


def rlp_encode(item) -> bytes:
    if isinstance(item, (bytes, bytearray)):
        b = bytes(item)
        if len(b) == 1 and b[0] < 0x80:
            return b
        return _rlp_length_prefix(len(b), 0x80) + b
    if isinstance(item, str):
        return rlp_encode(item.encode())
    if isinstance(item, int):
        if item < 0:
            raise ValueError("rlp negative int")
        if item == 0:
            return b"\x80"
        b = item.to_bytes((item.bit_length() + 7) // 8, "big")
        return rlp_encode(b)
    if isinstance(item, list):
        body = b"".join(rlp_encode(x) for x in item)
        return _rlp_length_prefix(len(body), 0xC0) + body
    raise TypeError(f"cannot rlp-encode {type(item)}")


# ---------- ECDSA signing (deterministic per RFC 6979 section 3.2) --------
# Using a deterministic k means the signature is reproducible from
# (privkey, message_hash) — important for testing. We follow the Ethereum
# convention of using the low-S form (s <= N/2) and including parity in v.

import hashlib as _hashlib
import hmac as _hmac


def _rfc6979_k(priv_int: int, h_bytes: bytes) -> int:
    """RFC 6979 deterministic k generation, HMAC-SHA256 variant."""
    # Convert to byte sequences per RFC 6979 section 2.3.
    qlen = N.bit_length()
    rlen = (qlen + 7) // 8

    def bits2int(b: bytes) -> int:
        x = int.from_bytes(b, "big")
        bl = len(b) * 8
        if bl > qlen:
            x >>= bl - qlen
        return x

    def int2octets(x: int) -> bytes:
        return x.to_bytes(rlen, "big")

    def bits2octets(b: bytes) -> bytes:
        z1 = bits2int(b)
        z2 = z1 % N
        return int2octets(z2)

    h1 = h_bytes  # already the hash
    x_oct = int2octets(priv_int)
    h1_oct = bits2octets(h1)
    V = b"\x01" * 32
    K = b"\x00" * 32
    K = _hmac.new(K, V + b"\x00" + x_oct + h1_oct, _hashlib.sha256).digest()
    V = _hmac.new(K, V, _hashlib.sha256).digest()
    K = _hmac.new(K, V + b"\x01" + x_oct + h1_oct, _hashlib.sha256).digest()
    V = _hmac.new(K, V, _hashlib.sha256).digest()
    while True:
        T = b""
        while len(T) < rlen:
            V = _hmac.new(K, V, _hashlib.sha256).digest()
            T += V
        k_candidate = bits2int(T[:rlen])
        if 1 <= k_candidate < N:
            return k_candidate
        K = _hmac.new(K, V + b"\x00", _hashlib.sha256).digest()
        V = _hmac.new(K, V, _hashlib.sha256).digest()


def _ecdsa_sign(priv: bytes, h_bytes: bytes) -> tuple[int, int, int]:
    """Return (r, s, recid) for the given priv and 32-byte hash. Low-S form."""
    if len(h_bytes) != 32:
        raise ValueError("hash must be 32 bytes")
    d = int.from_bytes(priv, "big")
    if not (1 <= d < N):
        raise ValueError("priv out of range")
    z = int.from_bytes(h_bytes, "big") % N
    while True:
        k = _rfc6979_k(d, h_bytes)
        R = _scalar_mult(k, (GX, GY))
        if R is None:
            continue
        r = R[0] % N
        if r == 0:
            continue
        s = (_modinv(k, N) * ((z + r * d) % N)) % N
        if s == 0:
            continue
        recid = (R[1] & 1) ^ (1 if s > N // 2 else 0)
        # Force low-S
        if s > N // 2:
            s = N - s
        return r, s, recid


# ---------- EIP-1559 transaction signing ----------------------------------


def _to_int(x) -> int:
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.startswith("0x") or s.startswith("0X"):
            return int(s, 16) if len(s) > 2 else 0
        return int(s)
    raise TypeError(f"cannot convert {type(x)} to int")


def _to_bytes_addr(to: str | bytes | None) -> bytes:
    if to is None or to == "":
        return b""
    if isinstance(to, bytes):
        if len(to) != 20:
            raise ValueError("addr bytes must be 20")
        return to
    s = to.lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 40:
        raise ValueError("addr must be 20 bytes")
    return bytes.fromhex(s)


def _to_bytes_data(d: str | bytes | None) -> bytes:
    if d is None or d == "" or d == "0x":
        return b""
    if isinstance(d, bytes):
        return d
    s = d
    if s.startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s)


def sign_eip1559_tx(
    priv: bytes,
    *,
    chain_id: int | str,
    nonce: int | str,
    max_priority_fee_per_gas: int | str,
    max_fee_per_gas: int | str,
    gas_limit: int | str,
    to: str | bytes | None,
    value: int | str,
    data: str | bytes | None = b"",
    access_list: list | None = None,
) -> dict:
    """Sign an EIP-1559 (type 2) transaction.

    Returns a dict with `raw_tx` (hex 0x..., ready for eth_sendRawTransaction),
    `tx_hash` (the hash of the signed envelope), and the raw r/s/v.
    """
    chain_id_i = _to_int(chain_id)
    nonce_i = _to_int(nonce)
    max_prio_i = _to_int(max_priority_fee_per_gas)
    max_fee_i = _to_int(max_fee_per_gas)
    gas_i = _to_int(gas_limit)
    to_b = _to_bytes_addr(to)
    value_i = _to_int(value)
    data_b = _to_bytes_data(data)
    al = access_list or []

    # Pre-signing payload (EIP-1559):
    #   0x02 || rlp([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas,
    #                gasLimit, to, value, data, accessList])
    pre_list = [
        chain_id_i,
        nonce_i,
        max_prio_i,
        max_fee_i,
        gas_i,
        to_b,
        value_i,
        data_b,
        al,
    ]
    pre = b"\x02" + rlp_encode(pre_list)
    h = keccak256(pre)
    r, s, recid = _ecdsa_sign(priv, h)

    # Signed envelope:
    #   0x02 || rlp([..., yParity, r, s])
    signed_list = pre_list + [recid, r, s]
    raw = b"\x02" + rlp_encode(signed_list)
    tx_hash = keccak256(raw)
    return {
        "raw_tx": "0x" + raw.hex(),
        "tx_hash": "0x" + tx_hash.hex(),
        "r": r,
        "s": s,
        "v": recid,
        "pre_image_hash": "0x" + h.hex(),
    }


# ----- self-test ---------------------------------------------------------
def _selftest() -> None:
    # Hardhat dev account #0:
    priv = bytes.fromhex("ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")
    addr = privkey_to_address(priv)
    if addr != "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266":
        raise AssertionError(addr)
    print("ethsign address derivation ok:", addr)

    # Smoke test: sign a no-op tx and ensure it RLP-encodes cleanly + hashes.
    out = sign_eip1559_tx(
        priv,
        chain_id=31337,
        nonce=0,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_per_gas=2_000_000_000,
        gas_limit=21_000,
        to="0x0000000000000000000000000000000000000001",
        value=0,
        data=b"",
    )
    if not out["raw_tx"].startswith("0x02"):
        raise AssertionError(out["raw_tx"][:4])
    print("ethsign sign smoke ok:", out["tx_hash"])


if __name__ == "__main__":
    _selftest()
