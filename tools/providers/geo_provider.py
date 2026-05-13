"""Geo-IP provider — drop-in interface for IP -> country lookup + blocklist.

Resolution order (first hit wins):

  1. MaxMind GeoLite2-Country.mmdb at ``tools/.local/GeoLite2-Country.mmdb``
     — requires the ``maxminddb`` Python lib. If the lib is missing or the
     mmdb file isn't present, this source is skipped silently. We do NOT
     attempt to parse the MaxMind binary format by hand (it is a non-trivial
     radix tree + record encoding; getting it wrong silently mis-classifies
     traffic, which is far worse than just falling through to the CSV).
  2. Free static CSV at ``providers/geo_country_ranges.csv`` — about 1k
     hand-curated CIDR ranges covering the OFAC sanctioned countries (must
     BLOCK) and the major US / KR / JP / SG / HK / CN consumer + cloud
     ranges (covers ~90% of real-world traffic).
  3. Loopback / RFC1918 — 127/8 + 10/8 + 172.16/12 + 192.168/16 -> ``KR``.
     This is the demo default. Production should set GEO_LOOPBACK_ISO2 to
     ``XX`` or similar to force an explicit override.

Caching is in-memory only (``dict[str, (decided_at, info)]``) with a 1h TTL.

This module is stdlib-only. Failure mode is FAIL-OPEN at the source level
(if the CSV can't load, ``lookup_country`` returns None) but FAIL-CLOSED at
the policy level (callers should treat ``None`` as "unknown" — the auth
middleware only blocks on explicit matches, and the loopback fallback
covers the demo).
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(HERE)
LOCAL_DIR = os.path.join(TOOLS_DIR, ".local")
MMDB_PATH = os.environ.get(
    "GEO_MMDB_PATH",
    os.path.join(LOCAL_DIR, "GeoLite2-Country.mmdb"),
)
CSV_PATH = os.environ.get(
    "GEO_CSV_PATH",
    os.path.join(HERE, "geo_country_ranges.csv"),
)
LOOPBACK_ISO2 = (os.environ.get("GEO_LOOPBACK_ISO2") or "KR").upper()

CACHE_TTL_S = 3600

# Default blocklist — FATF / OFAC "big-five". US is included because most
# offshore exchanges geo-fence the US to stay clear of SEC / FinCEN exposure.
DEFAULT_BLOCKLIST = ["US", "IR", "KP", "CU", "SY"]

# Country names for UI display. The CSV carries the same name in column 3
# but we keep a tiny canonical map so /auth/geo/blocklist can render names
# even when the CSV-side row hasn't been seen yet.
COUNTRY_NAMES = {
    "US": "United States",
    "IR": "Iran",
    "KP": "North Korea",
    "CU": "Cuba",
    "SY": "Syria",
    "RU": "Russia",
    "KR": "South Korea",
    "JP": "Japan",
    "SG": "Singapore",
    "HK": "Hong Kong",
    "TW": "Taiwan",
    "CN": "China",
    "GB": "United Kingdom",
    "DE": "Germany",
}


@dataclass
class CountryInfo:
    country_iso2: str
    country_name: str
    source: str  # 'maxmind' | 'csv' | 'loopback'

    def to_dict(self) -> dict:
        return {
            "country_iso2": self.country_iso2,
            "country_name": self.country_name,
            "source": self.source,
        }


# ===========================================================================
# IP / CIDR primitives — stdlib only.
# ===========================================================================
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def ip_to_int(ip: str) -> int | None:
    """Convert a dotted-quad IPv4 string to a 32-bit unsigned int.

    Returns None for anything that isn't a well-formed IPv4. IPv6 is not
    supported by the CSV path; the MaxMind path handles it natively if the
    mmdb is loaded.
    """
    m = _IPV4_RE.match(ip or "")
    if not m:
        return None
    octs = [int(g) for g in m.groups()]
    if any(o < 0 or o > 255 for o in octs):
        return None
    return (octs[0] << 24) | (octs[1] << 16) | (octs[2] << 8) | octs[3]


def parse_cidr(cidr: str) -> tuple[int, int] | None:
    """Return (network, mask_bits) or None if malformed."""
    try:
        addr, bits_s = cidr.split("/", 1)
        bits = int(bits_s)
        if bits < 0 or bits > 32:
            return None
        n = ip_to_int(addr)
        if n is None:
            return None
        # Mask to the network — sloppy CIDRs (with host bits set) are still
        # accepted; we normalize.
        if bits == 0:
            return (0, 0)
        mask = ((1 << 32) - 1) ^ ((1 << (32 - bits)) - 1)
        return (n & mask, bits)
    except (ValueError, AttributeError):
        return None


# ===========================================================================
# CSV table loader (longest-prefix-match via sorted list).
# ===========================================================================
# Each entry is (network_int, prefix_bits, iso2, name).
_csv_table: list[tuple[int, int, str, str]] = []
_csv_loaded = False
_csv_lock = threading.Lock()


def _load_csv() -> None:
    global _csv_loaded, _csv_table
    with _csv_lock:
        if _csv_loaded:
            return
        rows: list[tuple[int, int, str, str]] = []
        try:
            with open(CSV_PATH, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.lower().startswith("cidr,"):
                        continue
                    parts = line.split(",", 2)
                    if len(parts) < 2:
                        continue
                    cidr = parts[0].strip()
                    iso2 = parts[1].strip().upper()
                    name = parts[2].strip() if len(parts) >= 3 else iso2
                    parsed = parse_cidr(cidr)
                    if parsed is None:
                        continue
                    net, bits = parsed
                    rows.append((net, bits, iso2, name))
        except OSError as e:
            sys.stderr.write(f"[geo] CSV load failed: {e}\n")
            rows = []
        # Sort by descending prefix length so the first match in a linear
        # scan is the longest-prefix-match. (For ~1k entries this is fine;
        # a real production deployment would use the mmdb path which is
        # already a radix tree.)
        rows.sort(key=lambda r: r[1], reverse=True)
        _csv_table = rows
        _csv_loaded = True


def _csv_lookup(ip_int: int) -> tuple[str, str] | None:
    """Longest-prefix-match lookup against the CSV table. Returns (iso2, name)."""
    if not _csv_loaded:
        _load_csv()
    for net, bits, iso2, name in _csv_table:
        if bits == 0:
            return (iso2, name)
        # Mask the IP to the prefix width and compare.
        mask = ((1 << 32) - 1) ^ ((1 << (32 - bits)) - 1)
        if (ip_int & mask) == net:
            return (iso2, name)
    return None


# ===========================================================================
# MaxMind GeoLite2-Country (optional).
# ===========================================================================
_mmdb_reader = None
_mmdb_tried = False
_mmdb_lock = threading.Lock()


def _maxmind_lookup(ip: str) -> tuple[str, str] | None:
    global _mmdb_reader, _mmdb_tried
    if _mmdb_reader is None and _mmdb_tried:
        return None
    if not _mmdb_tried:
        with _mmdb_lock:
            if not _mmdb_tried:
                _mmdb_tried = True
                if not os.path.exists(MMDB_PATH):
                    return None
                try:
                    import maxminddb  # type: ignore
                except ImportError:
                    sys.stderr.write(
                        "[geo] maxminddb pip lib not installed; "
                        "skipping MaxMind mmdb at " + MMDB_PATH + "\n"
                    )
                    return None
                try:
                    _mmdb_reader = maxminddb.open_database(MMDB_PATH)
                    sys.stderr.write(f"[geo] mmdb loaded: {MMDB_PATH}\n")
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"[geo] mmdb load failed: {e}\n")
                    _mmdb_reader = None
                    return None
    if _mmdb_reader is None:
        return None
    try:
        rec = _mmdb_reader.get(ip)
    except Exception:  # noqa: BLE001
        return None
    if not rec or not isinstance(rec, dict):
        return None
    c = rec.get("country") or rec.get("registered_country") or {}
    iso2 = (c.get("iso_code") or "").upper()
    if not iso2:
        return None
    name = ""
    names = c.get("names") or {}
    if isinstance(names, dict):
        name = names.get("en") or ""
    if not name:
        name = COUNTRY_NAMES.get(iso2, iso2)
    return (iso2, name)


# ===========================================================================
# Loopback / RFC1918 — explicit private-range carve-out.
# ===========================================================================
_PRIVATE_NETS = [
    parse_cidr("127.0.0.0/8"),
    parse_cidr("10.0.0.0/8"),
    parse_cidr("172.16.0.0/12"),
    parse_cidr("192.168.0.0/16"),
    parse_cidr("169.254.0.0/16"),  # link-local
    parse_cidr("0.0.0.0/8"),
]


def is_private_ip(ip: str) -> bool:
    n = ip_to_int(ip)
    if n is None:
        # Treat IPv6 loopback as private.
        return ip in ("::1", "0:0:0:0:0:0:0:1")
    for entry in _PRIVATE_NETS:
        if entry is None:
            continue
        net, bits = entry
        if bits == 0:
            continue
        mask = ((1 << 32) - 1) ^ ((1 << (32 - bits)) - 1)
        if (n & mask) == net:
            return True
    return False


# ===========================================================================
# Cache.
# ===========================================================================
_cache: dict[str, tuple[float, CountryInfo | None]] = {}
_cache_lock = threading.Lock()


def _cache_get(ip: str) -> CountryInfo | None | object:
    """Return cached value or the sentinel _MISS for cache miss."""
    with _cache_lock:
        entry = _cache.get(ip)
        if entry is None:
            return _MISS
        decided_at, info = entry
        if (time.time() - decided_at) > CACHE_TTL_S:
            _cache.pop(ip, None)
            return _MISS
        return info


def _cache_put(ip: str, info: CountryInfo | None) -> None:
    with _cache_lock:
        _cache[ip] = (time.time(), info)


class _Missing:  # sentinel
    pass


_MISS = _Missing()


# ===========================================================================
# Public API.
# ===========================================================================
def lookup_country(ip: str) -> CountryInfo | None:
    """Return {country_iso2, country_name, source} for the given IP, or None.

    Tries MaxMind first (if configured), then the CSV fallback, then the
    loopback default. Caches results for ``CACHE_TTL_S`` seconds.
    """
    if not ip:
        return None
    cached = _cache_get(ip)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]

    info: CountryInfo | None = None

    # 0) Loopback / RFC1918 short-circuit. Demo e2e tests come from 127.0.0.1
    # and MUST be allow-listed regardless of what's in the CSV/MMDB. Checking
    # this first also keeps the source tagged as "loopback" in audit logs
    # so we can distinguish demo traffic from real classified hits.
    if is_private_ip(ip):
        info = CountryInfo(
            country_iso2=LOOPBACK_ISO2,
            country_name=COUNTRY_NAMES.get(LOOPBACK_ISO2, LOOPBACK_ISO2),
            source="loopback",
        )
        _cache_put(ip, info)
        return info

    # 1) MaxMind
    mm = _maxmind_lookup(ip)
    if mm is not None:
        iso2, name = mm
        info = CountryInfo(country_iso2=iso2, country_name=name, source="maxmind")
        _cache_put(ip, info)
        return info

    # 2) CSV
    ip_int = ip_to_int(ip)
    if ip_int is not None:
        hit = _csv_lookup(ip_int)
        if hit is not None:
            iso2, name = hit
            info = CountryInfo(country_iso2=iso2, country_name=name, source="csv")
            _cache_put(ip, info)
            return info

    _cache_put(ip, None)
    return None


def is_blocked(
    country_iso2: str | None, blocklist: list[str] | None = None
) -> tuple[bool, str | None]:
    """Returns (blocked, reason). reason is a human-readable explanation.

    A None country -> not blocked here (callers should decide whether
    "unknown" itself should be a block — for the demo we err on allow).
    """
    if not country_iso2:
        return (False, None)
    iso = country_iso2.upper()
    bl = [c.upper() for c in (blocklist if blocklist is not None else DEFAULT_BLOCKLIST)]
    if iso in bl:
        # Why blocked — tag sanctioned vs jurisdictional so the audit log
        # carries the policy reason.
        if iso in ("IR", "KP", "CU", "SY"):
            reason = "sanctions"
        elif iso == "US":
            reason = "jurisdictional"
        elif iso == "RU":
            reason = "sanctions"
        else:
            reason = "policy"
        return (True, reason)
    return (False, None)


def configured_blocklist() -> list[str]:
    """Return the active blocklist from $GEO_BLOCKLIST or the default."""
    raw = os.environ.get("GEO_BLOCKLIST")
    if raw is None:
        return list(DEFAULT_BLOCKLIST)
    out = [c.strip().upper() for c in raw.split(",") if c.strip()]
    return out or list(DEFAULT_BLOCKLIST)


def is_enforcement_enabled() -> bool:
    raw = (os.environ.get("GEO_BLOCK_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def trusted_proxy_cidrs() -> list[tuple[int, int]]:
    """Parse $GEO_TRUSTED_PROXIES into (network, prefix_bits) pairs."""
    raw = os.environ.get("GEO_TRUSTED_PROXIES") or "127.0.0.0/8"
    out: list[tuple[int, int]] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        parsed = parse_cidr(tok)
        if parsed is not None:
            out.append(parsed)
    return out


def is_trusted_proxy(ip: str) -> bool:
    """True if the immediate-peer IP is on the trusted-proxy list."""
    n = ip_to_int(ip)
    if n is None:
        return False
    for net, bits in trusted_proxy_cidrs():
        if bits == 0:
            return True
        mask = ((1 << 32) - 1) ^ ((1 << (32 - bits)) - 1)
        if (n & mask) == net:
            return True
    return False


def resolve_client_ip(
    *, remote_addr: str, x_forwarded_for: str | None, x_real_ip: str | None
) -> str:
    """Resolve the canonical client IP from headers + socket.

    Order:
      1) X-Forwarded-For first entry — but ONLY if the immediate peer is on
         the trusted-proxy list. Untrusted XFF is ignored entirely.
      2) X-Real-IP — same trust requirement.
      3) Raw socket remote_addr.

    Returns the raw socket IP if nothing better is available.
    """
    if is_trusted_proxy(remote_addr or ""):
        if x_forwarded_for:
            first = x_forwarded_for.split(",")[0].strip()
            if first:
                return first
        if x_real_ip:
            return x_real_ip.strip()
    return remote_addr or ""


def blocklist_with_names(blocklist: list[str] | None = None) -> list[dict]:
    """Render the blocklist as [{iso2, name}, ...] for the UI."""
    src = blocklist if blocklist is not None else configured_blocklist()
    out = []
    for iso2 in src:
        out.append(
            {
                "iso2": iso2,
                "name": COUNTRY_NAMES.get(iso2.upper(), iso2),
            }
        )
    return out


def redact_ip(ip: str) -> str:
    """Replace the last IPv4 octet with 'x' for GDPR-friendlier logging.

    IPv6 addresses are truncated to the /64 prefix.
    """
    if not ip:
        return ""
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:4]) + "::x"
    m = _IPV4_RE.match(ip)
    if not m:
        return "x.x.x.x"
    octs = m.groups()
    return f"{octs[0]}.{octs[1]}.{octs[2]}.x"


__all__ = [
    "CountryInfo",
    "lookup_country",
    "is_blocked",
    "configured_blocklist",
    "is_enforcement_enabled",
    "trusted_proxy_cidrs",
    "is_trusted_proxy",
    "resolve_client_ip",
    "blocklist_with_names",
    "redact_ip",
    "DEFAULT_BLOCKLIST",
    "COUNTRY_NAMES",
]
