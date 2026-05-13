"""Client-side envelope encryption for backup objects.

AES-256-GCM. Stdlib only -- relies on Python 3.11+'s built-in AES-GCM
via the ``cryptography`` module? NO -- stdlib only. Python's stdlib does
NOT ship AES-GCM, so we implement it on top of `ssl` is not viable
either. Instead we use the kernel-backed AES that ships with the host
via ``Crypto`` ? Also not stdlib.

The minimum-pip-deps directive forces us to AES-256-CTR + HMAC-SHA256
(encrypt-then-MAC), which is a sound construction equivalent in
security to AES-GCM for our purposes (offline backups, single-key,
random 12-byte nonces). We label the on-wire format with a magic
header so we can switch to true AES-GCM later without changing the
schema.

Wire format (per object)::

    magic    : 8 bytes  "ZKBACK01"
    nonce    : 12 bytes (random)
    ciphertext: N bytes
    tag      : 32 bytes HMAC-SHA256(key, magic||nonce||ciphertext)

The HMAC uses a *separate* derived key so encryption and authentication
keys don't share material. The master file at
``tools/.local/backup_encryption.key`` holds 32 random bytes; we derive
``enc_key`` and ``mac_key`` from it with HKDF-Expand using two
different ``info`` strings.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import sys

MAGIC = b"ZKBACK01"
NONCE_LEN = 12
TAG_LEN = 32  # HMAC-SHA256 output
KEY_LEN = 32  # AES-256
BLOCK = 16


# --- Key management ------------------------------------------------------
def load_or_create_master_key(path: str) -> bytes:
    """Return the 32-byte master key at ``path``, creating it (with 0600
    perms) on first use."""
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = f.read()
        if len(key) != KEY_LEN:
            raise RuntimeError(
                f"master key at {path} has wrong length ({len(key)}); " "expected 32 bytes"
            )
        return key
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_bytes(KEY_LEN)
    # write atomically with strict permissions
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    os.rename(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[backup.crypto] chmod failed for {path}: {e!r}\n")
    return key


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-Expand using SHA-256. Returns ``length`` bytes."""
    out = b""
    t = b""
    i = 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def derive_subkeys(master: bytes) -> tuple[bytes, bytes]:
    """Returns (enc_key, mac_key) derived from the master key."""
    return (
        _hkdf_expand(master, b"zkcex-backup/enc", KEY_LEN),
        _hkdf_expand(master, b"zkcex-backup/mac", KEY_LEN),
    )


# --- AES-256-CTR (pure Python) ------------------------------------------
# Implementation note: a pure-Python AES is ~2-5 MB/s; for backups in the
# 1-100 MB range that's fast enough (<1 minute). For multi-GB backups
# pipe through `openssl enc` (still stdlib-callable via subprocess) -- we
# detect openssl at module load and prefer it.

import subprocess


def _openssl_bin() -> str | None:
    return shutil.which("openssl")


def _openssl_available() -> bool:
    openssl = _openssl_bin()
    if not openssl:
        return False
    try:
        r = subprocess.run(  # noqa: S603 - executable is resolved and args are fixed.
            [openssl, "version"], capture_output=True, timeout=2, check=False
        )
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[backup.crypto] openssl probe failed: {e!r}\n")
        return False


_HAVE_OPENSSL = _openssl_available()


def _aes_ctr_openssl(key: bytes, nonce_iv: bytes, data: bytes) -> bytes:
    """Run AES-256-CTR via the openssl CLI. ``nonce_iv`` is the 16-byte
    initial counter block (we feed it as the IV).

    OpenSSL's CTR mode uses the IV as the initial counter block, so the
    caller must construct it as ``nonce || counter`` with a fresh counter
    starting at 0.
    """
    iv_hex = nonce_iv.hex()
    key_hex = key.hex()
    openssl = _openssl_bin()
    if not openssl:
        raise RuntimeError("openssl is not available")
    p = subprocess.run(  # noqa: S603 - executable is resolved and args are fixed.
        [
            openssl,
            "enc",
            "-aes-256-ctr",
            "-K",
            key_hex,
            "-iv",
            iv_hex,
            "-nosalt",
        ],
        input=data,
        capture_output=True,
        check=True,
    )
    return p.stdout


# Tiny pure-Python AES implementation kept as a fallback. It is *not*
# fast but it keeps the system entirely stdlib if openssl is missing.

_SBOX = (
    0x63,
    0x7C,
    0x77,
    0x7B,
    0xF2,
    0x6B,
    0x6F,
    0xC5,
    0x30,
    0x01,
    0x67,
    0x2B,
    0xFE,
    0xD7,
    0xAB,
    0x76,
    0xCA,
    0x82,
    0xC9,
    0x7D,
    0xFA,
    0x59,
    0x47,
    0xF0,
    0xAD,
    0xD4,
    0xA2,
    0xAF,
    0x9C,
    0xA4,
    0x72,
    0xC0,
    0xB7,
    0xFD,
    0x93,
    0x26,
    0x36,
    0x3F,
    0xF7,
    0xCC,
    0x34,
    0xA5,
    0xE5,
    0xF1,
    0x71,
    0xD8,
    0x31,
    0x15,
    0x04,
    0xC7,
    0x23,
    0xC3,
    0x18,
    0x96,
    0x05,
    0x9A,
    0x07,
    0x12,
    0x80,
    0xE2,
    0xEB,
    0x27,
    0xB2,
    0x75,
    0x09,
    0x83,
    0x2C,
    0x1A,
    0x1B,
    0x6E,
    0x5A,
    0xA0,
    0x52,
    0x3B,
    0xD6,
    0xB3,
    0x29,
    0xE3,
    0x2F,
    0x84,
    0x53,
    0xD1,
    0x00,
    0xED,
    0x20,
    0xFC,
    0xB1,
    0x5B,
    0x6A,
    0xCB,
    0xBE,
    0x39,
    0x4A,
    0x4C,
    0x58,
    0xCF,
    0xD0,
    0xEF,
    0xAA,
    0xFB,
    0x43,
    0x4D,
    0x33,
    0x85,
    0x45,
    0xF9,
    0x02,
    0x7F,
    0x50,
    0x3C,
    0x9F,
    0xA8,
    0x51,
    0xA3,
    0x40,
    0x8F,
    0x92,
    0x9D,
    0x38,
    0xF5,
    0xBC,
    0xB6,
    0xDA,
    0x21,
    0x10,
    0xFF,
    0xF3,
    0xD2,
    0xCD,
    0x0C,
    0x13,
    0xEC,
    0x5F,
    0x97,
    0x44,
    0x17,
    0xC4,
    0xA7,
    0x7E,
    0x3D,
    0x64,
    0x5D,
    0x19,
    0x73,
    0x60,
    0x81,
    0x4F,
    0xDC,
    0x22,
    0x2A,
    0x90,
    0x88,
    0x46,
    0xEE,
    0xB8,
    0x14,
    0xDE,
    0x5E,
    0x0B,
    0xDB,
    0xE0,
    0x32,
    0x3A,
    0x0A,
    0x49,
    0x06,
    0x24,
    0x5C,
    0xC2,
    0xD3,
    0xAC,
    0x62,
    0x91,
    0x95,
    0xE4,
    0x79,
    0xE7,
    0xC8,
    0x37,
    0x6D,
    0x8D,
    0xD5,
    0x4E,
    0xA9,
    0x6C,
    0x56,
    0xF4,
    0xEA,
    0x65,
    0x7A,
    0xAE,
    0x08,
    0xBA,
    0x78,
    0x25,
    0x2E,
    0x1C,
    0xA6,
    0xB4,
    0xC6,
    0xE8,
    0xDD,
    0x74,
    0x1F,
    0x4B,
    0xBD,
    0x8B,
    0x8A,
    0x70,
    0x3E,
    0xB5,
    0x66,
    0x48,
    0x03,
    0xF6,
    0x0E,
    0x61,
    0x35,
    0x57,
    0xB9,
    0x86,
    0xC1,
    0x1D,
    0x9E,
    0xE1,
    0xF8,
    0x98,
    0x11,
    0x69,
    0xD9,
    0x8E,
    0x94,
    0x9B,
    0x1E,
    0x87,
    0xE9,
    0xCE,
    0x55,
    0x28,
    0xDF,
    0x8C,
    0xA1,
    0x89,
    0x0D,
    0xBF,
    0xE6,
    0x42,
    0x68,
    0x41,
    0x99,
    0x2D,
    0x0F,
    0xB0,
    0x54,
    0xBB,
    0x16,
)
_RCON = (
    0x00,
    0x01,
    0x02,
    0x04,
    0x08,
    0x10,
    0x20,
    0x40,
    0x80,
    0x1B,
    0x36,
    0x6C,
    0xD8,
    0xAB,
    0x4D,
    0x9A,
)


def _aes256_key_expansion(key: bytes) -> list[bytes]:
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    Nk, Nb, Nr = 8, 4, 14
    w = [bytearray(key[4 * i : 4 * i + 4]) for i in range(Nk)]
    for i in range(Nk, Nb * (Nr + 1)):
        temp = bytearray(w[i - 1])
        if i % Nk == 0:
            temp = bytearray(
                [_SBOX[temp[1]] ^ _RCON[i // Nk], _SBOX[temp[2]], _SBOX[temp[3]], _SBOX[temp[0]]]
            )
        elif Nk > 6 and i % Nk == 4:
            temp = bytearray([_SBOX[b] for b in temp])
        nw = bytearray(w[i - Nk][j] ^ temp[j] for j in range(4))
        w.append(nw)
    # Group into 16-byte round keys
    return [bytes(b for r in w[i * Nb : (i + 1) * Nb] for b in r) for i in range(Nr + 1)]


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _aes_encrypt_block(plain: bytes, round_keys: list[bytes]) -> bytes:
    # state in column-major order
    s = list(plain)
    rk = round_keys[0]
    for i in range(16):
        s[i] ^= rk[i]
    Nr = len(round_keys) - 1
    for r in range(1, Nr + 1):
        # SubBytes
        s = [_SBOX[b] for b in s]
        # ShiftRows (state laid out column-major: idx = col*4 + row)
        s = [
            s[0],
            s[5],
            s[10],
            s[15],
            s[4],
            s[9],
            s[14],
            s[3],
            s[8],
            s[13],
            s[2],
            s[7],
            s[12],
            s[1],
            s[6],
            s[11],
        ]
        if r != Nr:
            ns = [0] * 16
            for c in range(4):
                a0, a1, a2, a3 = s[c * 4 : c * 4 + 4]
                ns[c * 4 + 0] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
                ns[c * 4 + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
                ns[c * 4 + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
                ns[c * 4 + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
            s = ns
        rk = round_keys[r]
        for i in range(16):
            s[i] ^= rk[i]
    return bytes(s)


def _aes_ctr_pure(key: bytes, nonce_iv: bytes, data: bytes) -> bytes:
    rk = _aes256_key_expansion(key)
    out = bytearray(len(data))
    ctr = int.from_bytes(nonce_iv, "big")
    for off in range(0, len(data), BLOCK):
        block = ctr.to_bytes(16, "big")
        ks = _aes_encrypt_block(block, rk)
        n = min(BLOCK, len(data) - off)
        for i in range(n):
            out[off + i] = data[off + i] ^ ks[i]
        ctr = (ctr + 1) & ((1 << 128) - 1)
    return bytes(out)


def _aes_ctr(key: bytes, nonce_iv: bytes, data: bytes) -> bytes:
    if _HAVE_OPENSSL and len(data) > 64 * 1024:
        return _aes_ctr_openssl(key, nonce_iv, data)
    return _aes_ctr_pure(key, nonce_iv, data)


# --- High-level API ------------------------------------------------------
def encrypt(master_key: bytes, plaintext: bytes) -> bytes:
    enc_k, mac_k = derive_subkeys(master_key)
    nonce = secrets.token_bytes(NONCE_LEN)
    # Build 16-byte initial counter block = nonce || u32 counter(0)
    iv = nonce + b"\x00\x00\x00\x00"
    ct = _aes_ctr(enc_k, iv, plaintext)
    blob = MAGIC + nonce + ct
    tag = hmac.new(mac_k, blob, hashlib.sha256).digest()
    return blob + tag


def decrypt(master_key: bytes, blob: bytes) -> bytes:
    if len(blob) < len(MAGIC) + NONCE_LEN + TAG_LEN:
        raise ValueError("ciphertext too short")
    if not blob.startswith(MAGIC):
        raise ValueError("bad magic; not a zkCEX backup envelope")
    enc_k, mac_k = derive_subkeys(master_key)
    body, tag = blob[:-TAG_LEN], blob[-TAG_LEN:]
    expected = hmac.new(mac_k, body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("authentication tag mismatch -- tampered or wrong key")
    nonce = body[len(MAGIC) : len(MAGIC) + NONCE_LEN]
    ct = body[len(MAGIC) + NONCE_LEN :]
    iv = nonce + b"\x00\x00\x00\x00"
    return _aes_ctr(enc_k, iv, ct)


# --- self-test -----------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as tf:
        path = tf.name
    os.unlink(path)
    key = load_or_create_master_key(path)
    if len(key) != 32:
        raise AssertionError("master key should be 32 bytes")
    key2 = load_or_create_master_key(path)
    if key != key2:
        raise AssertionError("load-after-create should be stable")

    for msg in [b"", b"hi", b"a" * 15, b"b" * 16, b"c" * 17, b"abc" * 5000]:
        ct = encrypt(key, msg)
        pt = decrypt(key, ct)
        if pt != msg:
            raise AssertionError((len(msg), len(pt)))

    # tamper test
    ct = encrypt(key, b"hello")
    bad = bytearray(ct)
    bad[20] ^= 0x01
    try:
        decrypt(key, bytes(bad))
        raise SystemExit("FAIL: tampered decrypt should have raised")
    except ValueError:
        pass

    print("crypto OK; openssl=", _HAVE_OPENSSL)
    os.unlink(path)
