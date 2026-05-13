"""OFAC SDN sanctions list loader.

Pulls the official US Treasury OFAC SDN XML, extracts every digital-currency
address tagged ``<idType>Digital Currency Address - <CHAIN></idType>``, and
exposes a thread-safe in-memory set keyed by lowercase address. The loader
also captures the surrounding entry context (uid, name, programs) so an
audit row can cite the canonical SDN reference rather than just "blocked".

Refreshes on demand. Production deployments should call ``refresh()`` from a
daily cron-like loop (Treasury republishes the list daily).

No new pip dependencies — uses stdlib xml.etree, urllib, and a writers/readers
lock around two simple dicts.

Public surface:

    ofac_loader.lookup(chain: str, address: str) -> SDNHit | None
    ofac_loader.refresh() -> RefreshOutcome
    ofac_loader.snapshot() -> {chain: [addresses...], "as_of": "...", "n_addrs": N}

Environment:

    OFAC_SDN_URL     — override the upstream URL (default: treasury.gov canonical)
    OFAC_SDN_CACHE   — local cache file path (default: tools/.local/ofac_sdn.xml)
    OFAC_SDN_TTL     — seconds to hold the in-memory parse before re-pulling
                       on next lookup (default: 86400 = 24h)
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.normpath(os.path.join(HERE, "..", ".local", "ofac_sdn.xml"))


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


def _log(*args: object) -> None:
    import sys

    sys.stderr.write("[ofac_loader] " + " ".join(str(a) for a in args) + "\n")


SDN_URL = _validated_http_url(
    os.environ.get("OFAC_SDN_URL", "https://www.treasury.gov/ofac/downloads/sdn.xml"),
    name="OFAC_SDN_URL",
)
CACHE_PATH = os.environ.get("OFAC_SDN_CACHE", DEFAULT_CACHE)
TTL_SECONDS = int(os.environ.get("OFAC_SDN_TTL", "86400"))

# Map OFAC's `Digital Currency Address - <SUFFIX>` to a canonical "chain"
# slug we use internally. Aliases let callers pass either form.
_OFAC_CHAIN_ALIASES = {
    "XBT": "BTC",
    "BTC": "BTC",
    "BSV": "BSV",
    "BCH": "BCH",
    "ETH": "ETH",
    "ETC": "ETC",
    "LTC": "LTC",
    "XMR": "XMR",
    "ZEC": "ZEC",
    "DASH": "DASH",
    "TRX": "TRX",
    "USDT": "ETH",  # USDT-on-ETH uses ETH addresses; OFAC tags as USDT
    "USDC": "ETH",
    "ARB": "ETH",  # Arbitrum addresses share ETH format
    "AVAX": "AVAX",
    "BNB": "ETH",  # BSC addresses share ETH format
    "PASTE": None,  # ignore odd entries
}

NS = {"x": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML"}


@dataclass(frozen=True)
class SDNHit:
    address: str  # lowercase normalized
    chain: str  # canonical chain slug (BTC, ETH, ...)
    raw_chain: str  # original OFAC label e.g. "Digital Currency Address - XBT"
    sdn_uid: str  # OFAC entry UID
    sdn_name: str  # primary entity/individual name
    sdn_programs: tuple  # OFAC sanctions programs (e.g. ("CYBER2", "DPRK"))
    source: str = "OFAC-SDN"


@dataclass(frozen=True)
class RefreshOutcome:
    fetched_bytes: int
    parsed_at: int  # unix
    publish_date: str | None
    record_count: int | None
    n_addresses: int
    n_chains: int
    error: str | None = None


_lock = threading.RLock()
_state = {
    "by_addr": {},  # (chain, address_lower) -> SDNHit
    "by_chain": {},  # chain -> set of address_lower
    "last_refresh": 0,
    "last_outcome": None,
}


# ---- public API -------------------------------------------------------------
def lookup(*, chain: str, address: str) -> SDNHit | None:
    """Return the SDNHit if the address is on the OFAC SDN list under that chain.

    Auto-refreshes if the in-memory state is stale by TTL.

    The chain hint is best-effort. If callers pass a non-canonical name like
    ``"hardhat-localhost"`` or ``"layer2"`` we still want to catch a hit, so
    we fall back to inferring the chain from the address shape:

      - ``0x`` + 40 hex   -> EVM family (ETH/BSC/ETC/AVAX shared the format)
      - ``T`` + 33 base58 -> TRX
      - ``bc1`` ...       -> BTC bech32
      - ``[13]`` legacy   -> BTC legacy

    On format-only inference the lookup tries every chain in the EVM family
    or every BTC family member, so a sanctioned ETH address still BLOCKs even
    if the caller had no idea what chain it's on.
    """
    addr_n = (address or "").strip().lower()
    if not addr_n:
        return None
    _maybe_refresh()
    candidates: list[str] = []
    chain_n = _normalize_chain(chain)
    if chain_n is not None:
        candidates.append(chain_n)
    # Format-based inference. Always tried in addition to the explicit chain
    # so a typo in the chain hint doesn't silently hide an SDN match.
    inferred = _infer_chains_from_address(addr_n)
    for c in inferred:
        if c not in candidates:
            candidates.append(c)
    with _lock:
        for c in candidates:
            hit = _state["by_addr"].get((c, addr_n))
            if hit is not None:
                return hit
    return None


def _infer_chains_from_address(addr_lower: str) -> list[str]:
    """Best-effort: which canonical chains is this address shape consistent with?"""
    a = addr_lower
    if a.startswith("0x") and len(a) == 42 and all(c in "0123456789abcdef" for c in a[2:]):
        # Address shape shared across the EVM family. We screen against every
        # member because OFAC tags some entries as USDT (on ETH) or BSC.
        return ["ETH", "ETC", "BSC", "AVAX"]
    if a.startswith("bc1") or a.startswith("tb1"):
        return ["BTC"]
    if len(a) >= 26 and len(a) <= 35 and a[0] in "13":
        return ["BTC", "BCH", "BSV", "LTC"]
    if a.startswith("t") and len(a) == 34:
        return ["TRX"]
    return []


def refresh(*, force: bool = True) -> RefreshOutcome:
    """Pull the SDN XML (cached on disk) and rebuild the in-memory index."""
    try:
        body = _fetch_sdn(force=force)
    except Exception as exc:
        out = RefreshOutcome(
            fetched_bytes=0,
            parsed_at=int(time.time()),
            publish_date=None,
            record_count=None,
            n_addresses=0,
            n_chains=0,
            error=f"fetch_failed: {exc}",
        )
        with _lock:
            _state["last_outcome"] = out
        return out
    pub_date, record_count, hits = _parse_sdn(body)
    by_addr: dict[tuple[str, str], SDNHit] = {}
    by_chain: dict[str, set[str]] = {}
    for h in hits:
        by_addr[(h.chain, h.address)] = h
        by_chain.setdefault(h.chain, set()).add(h.address)
    out = RefreshOutcome(
        fetched_bytes=len(body),
        parsed_at=int(time.time()),
        publish_date=pub_date,
        record_count=record_count,
        n_addresses=len(by_addr),
        n_chains=len(by_chain),
        error=None,
    )
    with _lock:
        _state["by_addr"] = by_addr
        _state["by_chain"] = by_chain
        _state["last_refresh"] = int(time.time())
        _state["last_outcome"] = out
    return out


def snapshot() -> dict:
    with _lock:
        out = _state["last_outcome"]
        return {
            "as_of_unix": _state["last_refresh"],
            "publish_date": getattr(out, "publish_date", None),
            "n_addresses": len(_state["by_addr"]),
            "by_chain": {k: sorted(v) for k, v in _state["by_chain"].items()},
            "last_outcome": out
            and {
                "fetched_bytes": out.fetched_bytes,
                "parsed_at": out.parsed_at,
                "n_addresses": out.n_addresses,
                "n_chains": out.n_chains,
                "publish_date": out.publish_date,
                "record_count": out.record_count,
                "error": out.error,
            },
        }


# ---- internals --------------------------------------------------------------
def _normalize_chain(label: str) -> str | None:
    if not label:
        return None
    s = label.strip().upper()
    if s.startswith("DIGITAL CURRENCY ADDRESS - "):
        s = s.split(" - ", 1)[1]
    return _OFAC_CHAIN_ALIASES.get(s, s if s.isalnum() and len(s) <= 6 else None)


def _maybe_refresh():
    with _lock:
        last = _state["last_refresh"]
    if last == 0 or (time.time() - last) > TTL_SECONDS:
        try:
            refresh(force=False)
        except Exception as exc:  # noqa: BLE001
            _log(f"refresh skipped, serving previous snapshot: {exc!r}")


def _fetch_sdn(*, force: bool) -> bytes:
    """Fetch the SDN XML body. Falls back to the on-disk cache if the network is
    unavailable, and writes the cache on a successful pull.
    """
    if not force and os.path.exists(CACHE_PATH):
        age = time.time() - os.path.getmtime(CACHE_PATH)
        if age < TTL_SECONDS:
            return open(CACHE_PATH, "rb").read()
    try:
        with _http_urlopen(SDN_URL, timeout=30) as resp:
            body = resp.read()
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "wb") as fh:
            fh.write(body)
        return body
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        if os.path.exists(CACHE_PATH):
            return open(CACHE_PATH, "rb").read()
        raise RuntimeError(f"OFAC SDN fetch failed and no cache available: {exc}") from exc


def _parse_sdn(body: bytes) -> tuple[str | None, int | None, Iterable[SDNHit]]:
    """Parse SDN XML and yield SDNHit per digital-currency address.

    Robust to namespace presence or absence; OFAC has been inconsistent.
    """
    if b"<!DOCTYPE" in body[:1024].upper() or b"<!ENTITY" in body[:4096].upper():
        raise ValueError("OFAC SDN XML with DTD/entity declarations is not accepted")
    root = ET.fromstring(body)  # noqa: S314
    ns = NS if root.tag.startswith("{") else {}

    def find(elem, path):
        return elem.find(path, ns) if ns else elem.find(path)

    def findall(elem, path):
        return elem.findall(path, ns) if ns else elem.findall(path)

    pub_date = None
    record_count = None
    pi = find(root, "x:publshInformation" if ns else "publshInformation")
    if pi is not None:
        pdate = find(pi, "x:Publish_Date" if ns else "Publish_Date")
        rcnt = find(pi, "x:Record_Count" if ns else "Record_Count")
        if pdate is not None and pdate.text:
            pub_date = pdate.text.strip()
        if rcnt is not None and rcnt.text and rcnt.text.strip().isdigit():
            record_count = int(rcnt.text.strip())

    hits = []
    for entry in findall(root, "x:sdnEntry" if ns else "sdnEntry"):
        uid_el = find(entry, "x:uid" if ns else "uid")
        last_el = find(entry, "x:lastName" if ns else "lastName")
        first_el = find(entry, "x:firstName" if ns else "firstName")
        uid = (uid_el.text or "").strip() if uid_el is not None else ""
        last = (last_el.text or "").strip() if last_el is not None else ""
        first = (first_el.text or "").strip() if first_el is not None else ""
        name = (first + " " + last).strip() if first else last

        programs = []
        plist = find(entry, "x:programList" if ns else "programList")
        if plist is not None:
            for p in findall(plist, "x:program" if ns else "program"):
                if p.text:
                    programs.append(p.text.strip())

        idlist = find(entry, "x:idList" if ns else "idList")
        if idlist is None:
            continue
        for idnode in findall(idlist, "x:id" if ns else "id"):
            t = find(idnode, "x:idType" if ns else "idType")
            n = find(idnode, "x:idNumber" if ns else "idNumber")
            if t is None or n is None or not (t.text and n.text):
                continue
            t_text = t.text.strip()
            if not t_text.startswith("Digital Currency Address"):
                continue
            chain = _normalize_chain(t_text)
            if chain is None:
                continue
            addr = n.text.strip().lower()
            if not addr:
                continue
            hits.append(
                SDNHit(
                    address=addr,
                    chain=chain,
                    raw_chain=t_text,
                    sdn_uid=uid,
                    sdn_name=name,
                    sdn_programs=tuple(programs),
                )
            )
    return pub_date, record_count, hits


if __name__ == "__main__":
    out = refresh(force=True)
    print(f"refresh outcome: {out}")
    snap = snapshot()
    print(
        f"snapshot: as_of={snap['as_of_unix']} publish={snap['publish_date']} "
        f"chains={list(snap['by_chain'].keys())} total={snap['n_addresses']}"
    )
    for chain, addrs in snap["by_chain"].items():
        print(f"  {chain}: {len(addrs)} (sample {addrs[:2]})")
