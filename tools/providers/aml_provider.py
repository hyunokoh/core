"""AML provider interface.

Resolution order (first source that decides BLOCK or REVIEW wins; ALLOW is
upgraded by later sources but never downgraded):

  1. zkAML (set ZKAML_URL)               -- full risk + taint + cluster screening
  2. Chainalysis Public Sanctions API    -- set CHAINALYSIS_API_KEY
                                            (free tier: OFAC + global sanctions)
  3. Local OFAC SDN cache                -- set OFAC_SDN_ENABLED=1
                                            (no key, official US Treasury feed)
  4. Deterministic stub                  -- always on, demo fallback

Each source can be enabled independently. The decision shape is identical so
call sites don't have to know which source spoke.

Failure mode is FAIL-CLOSED: if a configured upstream is unreachable, the
module either falls through to the next source OR returns ``REVIEW`` so a
provider outage does not silently let traffic through.

Real zkAML SDK reference: ~/Documents/Projects/zkAML/zkaml_client/
Chainalysis Public API:    https://public.chainalysis.com/api/v1/address/<addr>
OFAC SDN feed:             https://www.treasury.gov/ofac/downloads/sdn.xml
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _optional_http_base_url(name: str, raw_url: str) -> str:
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return ""
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


ZKAML_URL = _optional_http_base_url("ZKAML_URL", os.environ.get("ZKAML_URL", ""))
ZKAML_API_KEY = os.environ.get("ZKAML_API_KEY", "")

CHAINALYSIS_API_KEY = os.environ.get("CHAINALYSIS_API_KEY", "")
CHAINALYSIS_BASE = _optional_http_base_url(
    "CHAINALYSIS_BASE",
    os.environ.get(
        "CHAINALYSIS_BASE",
        "https://public.chainalysis.com/api/v1",
    ),
)

# Local OFAC SDN list (set OFAC_SDN_ENABLED=1 to engage). Lazily imported so a
# missing tools/.local doesn't break import-time on machines that haven't
# initialized it.
OFAC_SDN_ENABLED = os.environ.get("OFAC_SDN_ENABLED", "").lower() in ("1", "true", "yes")

# Demo sanctions list -- addresses to BLOCK irrespective of real screening.
# In production, this list lives in zkAML's database and is refreshed from OFAC.
DEMO_SANCTIONS = {
    "0x000000000000000000000000000000000000dead",  # canonical "burn" addr -- BLOCKED for demo
    "0x7f367cc41522ce07553e823bf3be79a889debe1b",  # synthetic Lazarus-like
}

# Risk score thresholds
HIGH_RISK = 70
MEDIUM_RISK = 30


@dataclass
class AmlDecision:
    decision: str  # 'ALLOW' | 'REVIEW' | 'BLOCK'
    risk_score: int
    sanctioned: bool
    reasons: list
    source: str  # 'stub' | 'zkaml' | 'zkaml-fallback'
    request_id: str
    decided_at: int

    def to_json(self) -> dict:
        return asdict(self)


def screen_address(
    *, chain: str, address: str, customer_id: str | None = None, kyc_level: str | None = None
) -> AmlDecision:
    """Screen a single chain address. Resolution cascades through configured
    sources; the first BLOCK/REVIEW wins, an ALLOW from one source can be
    upgraded by a stricter source but never downgraded.

    customer_id and kyc_level are passed through to zkAML if connected.
    """
    addr = (address or "").lower()

    # Precedence: BLOCK > REVIEW > ALLOW. Any BLOCK short-circuits. A REVIEW
    # from a configured upstream that's failing closed must propagate — we
    # MUST NOT let an outage of Chainalysis or zkAML silently turn into ALLOW
    # via the stub. Only iterate to the next source when the current one
    # said ALLOW cleanly.
    pending_review: AmlDecision | None = None

    # 1) zkAML — full risk + taint screening.
    if ZKAML_URL:
        d = _call_real(
            method="screen/address",
            body={
                "chain": chain,
                "address": addr,
                "customer_id": customer_id,
                "kyc_level": kyc_level,
            },
        )
        if d.decision == "BLOCK":
            return d
        if d.decision == "REVIEW":
            pending_review = d  # don't return yet — sanctions BLOCK can still override

    # 2) Chainalysis Public Sanctions API — sanctions-only.
    if CHAINALYSIS_API_KEY:
        d = _chainalysis_decision(chain=chain, address=addr)
        if d.decision == "BLOCK":
            return d
        if d.decision == "REVIEW" and pending_review is None:
            pending_review = d

    # 3) Local OFAC SDN cache — sanctions-only.
    if OFAC_SDN_ENABLED:
        d = _ofac_decision(chain=chain, address=addr)
        if d.decision == "BLOCK":
            return d
        # OFAC loader is a local file lookup; if it can't find a match it
        # returns ALLOW. There's no "REVIEW" path here.

    if pending_review is not None:
        return pending_review

    # 4) Stub fallback. Reached only when zero upstream sources are
    # configured, OR every configured source returned a clean ALLOW.
    return _stub_decision(chain=chain, address=addr, customer_id=customer_id)


def screen_tx(*, chain: str, tx_hash: str, customer_id: str | None = None) -> AmlDecision:
    """Screen a transaction hash for downstream taint."""
    if ZKAML_URL:
        return _call_real(
            method="screen/tx",
            body={
                "chain": chain,
                "tx_hash": tx_hash,
                "customer_id": customer_id,
            },
        )
    return AmlDecision(
        decision="ALLOW",
        risk_score=0,
        sanctioned=False,
        reasons=["stub: tx-screening not exercised in demo"],
        source="stub",
        request_id=f"stub-tx-{int(time.time()*1000)}",
        decided_at=int(time.time()),
    )


def _stub_decision(*, chain, address, customer_id):
    if address in DEMO_SANCTIONS:
        return AmlDecision(
            decision="BLOCK",
            risk_score=100,
            sanctioned=True,
            reasons=["address on demo sanctions list"],
            source="stub",
            request_id=f"stub-addr-{int(time.time()*1000)}",
            decided_at=int(time.time()),
        )
    # Deterministic pseudo-score from address bytes -- same address always
    # gets the same decision so the demo is reproducible.
    try:
        score = sum(int(c, 16) for c in address[2:6]) % 100
    except Exception:
        score = 0
    if score >= HIGH_RISK:
        decision = "BLOCK"
        reasons = [f"deterministic stub risk score {score} >= {HIGH_RISK}"]
    elif score >= MEDIUM_RISK:
        decision = "REVIEW"
        reasons = [f"deterministic stub risk score {score} >= {MEDIUM_RISK}"]
    else:
        decision = "ALLOW"
        reasons = []
    return AmlDecision(
        decision=decision,
        risk_score=score,
        sanctioned=False,
        reasons=reasons,
        source="stub",
        request_id=f"stub-addr-{int(time.time()*1000)}",
        decided_at=int(time.time()),
    )


def _chainalysis_decision(*, chain: str, address: str) -> AmlDecision:
    """Hit the Chainalysis Public Sanctions API.

    Free tier requires registration at https://go.chainalysis.com/chainalysis-sanctions-api.html
    The API key arrives by email; set ``CHAINALYSIS_API_KEY=<key>`` to enable.

    Response shape (from public docs):
        {
          "identifications": [
            {"category": "sanctions",
             "name": "OFAC SDN List",
             "description": "...", "url": "..."},
            ...
          ]
        }

    Empty ``identifications`` => clean. Any non-empty array under
    category=sanctions => BLOCK.
    """
    url = f"{CHAINALYSIS_BASE}/address/{address}"
    req = _http_request(
        url,
        headers={
            "X-API-Key": CHAINALYSIS_API_KEY,
            "Accept": "application/json",
        },
    )
    rid = f"chainalysis-{int(time.time()*1000)}"
    try:
        with _http_urlopen(req, timeout=3.0) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # 401/403 — bad key. 429 — rate limit. Other 5xx — outage.
        return AmlDecision(
            decision="REVIEW",
            risk_score=0,
            sanctioned=False,
            reasons=[f"chainalysis-http-{e.code}: {e.reason}"],
            source="chainalysis-fallback",
            request_id=rid,
            decided_at=int(time.time()),
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return AmlDecision(
            decision="REVIEW",
            risk_score=0,
            sanctioned=False,
            reasons=[f"chainalysis-unreachable: {type(e).__name__}: {e}"],
            source="chainalysis-fallback",
            request_id=rid,
            decided_at=int(time.time()),
        )
    ids = data.get("identifications", []) or []
    if ids:
        names = [i.get("name") for i in ids if i.get("name")]
        descs = [i.get("description") for i in ids if i.get("description")]
        return AmlDecision(
            decision="BLOCK",
            risk_score=100,
            sanctioned=True,
            reasons=[f"chainalysis: {', '.join(names) or 'sanctioned'}"] + descs[:2],
            source="chainalysis",
            request_id=rid,
            decided_at=int(time.time()),
        )
    return AmlDecision(
        decision="ALLOW",
        risk_score=0,
        sanctioned=False,
        reasons=["chainalysis: no sanctions match"],
        source="chainalysis",
        request_id=rid,
        decided_at=int(time.time()),
    )


def _ofac_decision(*, chain: str, address: str) -> AmlDecision:
    """Look up the address against the locally cached OFAC SDN feed."""
    rid = f"ofac-{int(time.time()*1000)}"
    try:
        from . import ofac_loader
    except ImportError:
        return AmlDecision(
            decision="ALLOW",
            risk_score=0,
            sanctioned=False,
            reasons=["ofac-loader-missing"],
            source="ofac-fallback",
            request_id=rid,
            decided_at=int(time.time()),
        )
    hit = ofac_loader.lookup(chain=chain, address=address)
    if hit is None:
        return AmlDecision(
            decision="ALLOW",
            risk_score=0,
            sanctioned=False,
            reasons=["ofac: no SDN match"],
            source="ofac",
            request_id=rid,
            decided_at=int(time.time()),
        )
    return AmlDecision(
        decision="BLOCK",
        risk_score=100,
        sanctioned=True,
        reasons=[
            f"OFAC SDN uid={hit.sdn_uid} name={hit.sdn_name}",
            f"programs={','.join(hit.sdn_programs) or 'unspecified'}",
        ],
        source="ofac",
        request_id=rid,
        decided_at=int(time.time()),
    )


def _call_real(*, method, body):
    """POST to ZKAML_URL/<method>. Returns AmlDecision; on failure, fail-closed
    to REVIEW (matches zkAML production default).

    The real zkAML response shape (from
    ~/Documents/Projects/zkAML/api/app/services/screening.py):
        {"decision": "ALLOW|REVIEW|BLOCK", "risk_score": int, "sanctioned": bool,
         "reasons": [str, ...], "request_id": str, "decided_at": int}
    """
    headers = {"Content-Type": "application/json"}
    if ZKAML_API_KEY:
        headers["Authorization"] = f"Bearer {ZKAML_API_KEY}"
    req = _http_request(
        url=f"{ZKAML_URL}/{method}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with _http_urlopen(req, timeout=2.0) as r:
            data = json.loads(r.read().decode())
            return AmlDecision(
                decision=data["decision"],
                risk_score=int(data.get("risk_score", 0)),
                sanctioned=bool(data.get("sanctioned", False)),
                reasons=list(data.get("reasons", [])),
                source="zkaml",
                request_id=str(data.get("request_id", "")),
                decided_at=int(data.get("decided_at", int(time.time()))),
            )
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        KeyError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ) as e:
        # Fail-closed -- same model as zkAML's own client when its provider
        # is down.
        return AmlDecision(
            decision="REVIEW",
            risk_score=0,
            sanctioned=False,
            reasons=[f"zkaml-unreachable: {type(e).__name__}: {e}"],
            source="zkaml-fallback",
            request_id="",
            decided_at=int(time.time()),
        )
