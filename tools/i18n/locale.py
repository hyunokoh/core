"""Locale selection helpers for the proxy.

Priority order (first hit wins):

  1. ``?lang=<code>`` URL parameter — explicit user choice on this request.
     The proxy sets a sticky ``Set-Cookie: zkcex_lang=<code>`` so subsequent
     requests skip back to step 2.
  2. ``zkcex_lang`` cookie — remembered preference from an earlier choice.
  3. ``Accept-Language`` header — browser default. Q-values are honoured: we
     walk the comma-separated list, sort by q (descending, stable), and pick
     the first base language tag that's in ``SUPPORTED``.
  4. ``ko`` — fallback. The exchange's canonical source language is Korean.

Everything is stdlib-only and fully synchronous: the typical call costs a
handful of string ops, well under 1ms.
"""

from __future__ import annotations

SUPPORTED: list[str] = ["ko", "en", "ja", "zh", "es", "de"]
DEFAULT_LANG: str = "ko"

# Some Accept-Language tags map to a SUPPORTED language even though the
# primary tag differs. e.g. ``zh-Hans`` / ``zh-CN`` / ``zh-SG`` → ``zh``.
# ``zh-Hant`` / ``zh-TW`` / ``zh-HK`` would map to traditional Chinese which
# we don't yet ship — fall through to the next best match.
_LANG_ALIASES: dict[str, str] = {
    "zh-cn": "zh",
    "zh-sg": "zh",
    "zh-hans": "zh",
    # Traditional variants — we don't ship zh-Hant yet, so map to simplified
    # rather than fall back to KO. Imperfect but better than wrong-language.
    "zh-tw": "zh",
    "zh-hk": "zh",
    "zh-mo": "zh",
    "zh-hant": "zh",
}


def _parse_accept_language(header: str) -> list[str]:
    """Return tags from an Accept-Language header in q-value-sorted order.

    Stable sort: original order breaks ties so ``en-US,en;q=0.9`` prefers
    ``en-US`` (which still maps to ``en``). Malformed q values silently fall
    back to ``1.0`` per RFC 7231 §5.3.1.
    """
    if not header:
        return []
    out: list[tuple[float, int, str]] = []
    for idx, chunk in enumerate(header.split(",")):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ";" in chunk:
            tag, _, params = chunk.partition(";")
            q = 1.0
            for p in params.split(";"):
                k, _, v = p.partition("=")
                if k.strip().lower() == "q":
                    try:
                        q = float(v.strip())
                    except ValueError:
                        q = 1.0
        else:
            tag, q = chunk, 1.0
        tag = tag.strip().lower()
        if not tag or tag == "*":
            continue
        # Negative-q entries are explicit rejections (RFC 7231 only allows
        # 0..1, but we belt-and-brace) — skip them.
        if q <= 0:
            continue
        out.append((-q, idx, tag))  # negative-q so plain sort orders high→low
    out.sort()
    return [tag for _q, _i, tag in out]


def _resolve_tag(tag: str) -> str | None:
    """Map a single Accept-Language tag to a SUPPORTED code, or None."""
    tag = tag.lower()
    # 1. Direct alias hit (zh-CN, zh-Hans, ...). Includes the trad. fallback.
    if tag in _LANG_ALIASES:
        return _LANG_ALIASES[tag]
    # 2. Base tag (en-US -> en).
    base = tag.split("-", 1)[0]
    if base in SUPPORTED:
        return base
    return None


def negotiate(
    accept_language_header: str | None,
    cookie_value: str | None = None,
    query_param: str | None = None,
) -> str:
    """Return the negotiated language code for this request.

    Always returns one of ``SUPPORTED``; falls back to ``DEFAULT_LANG`` (``ko``)
    if nothing matches.
    """
    # 1. Explicit URL parameter wins.
    if query_param:
        qp = query_param.strip().lower()
        if qp in SUPPORTED:
            return qp
    # 2. Sticky cookie.
    if cookie_value:
        cv = cookie_value.strip().lower()
        if cv in SUPPORTED:
            return cv
    # 3. Accept-Language header.
    if accept_language_header:
        for tag in _parse_accept_language(accept_language_header):
            resolved = _resolve_tag(tag)
            if resolved is not None:
                return resolved
    # 4. Fallback.
    return DEFAULT_LANG


def parse_cookie_header(cookie_header: str | None, name: str = "zkcex_lang") -> str | None:
    """Extract a single named cookie value from a raw Cookie header."""
    if not cookie_header:
        return None
    for chunk in cookie_header.split(";"):
        k, _, v = chunk.strip().partition("=")
        if k == name:
            return v.strip() or None
    return None


def parse_query_lang(query_string: str | None, name: str = "lang") -> str | None:
    """Extract ?lang=<code> from a query string. Returns None if absent."""
    if not query_string:
        return None
    for pair in query_string.lstrip("?").split("&"):
        k, _, v = pair.partition("=")
        if k == name:
            return v.strip().lower() or None
    return None
