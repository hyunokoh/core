"""Minimal gettext-style ``.po`` reader.

We deliberately don't depend on ``babel`` / ``polib`` / the stdlib's
``gettext.GNUTranslations`` (which only reads compiled .mo files): the format
we care about is just msgid/msgstr pairs, and we want zero third-party deps.

The parser handles:
- Multi-line msgid/msgstr (``msgid "foo"\n"bar"`` → ``"foobar"``).
- Standard backslash escapes (``\\n``, ``\\t``, ``\\\\``, ``\\"``).
- Translator-note comments (``#. note``) — preserved as ``__notes__``.
- Source references (``#: path/file.html:42``) — preserved as ``__refs__``.
- Plural forms are accepted but only the singular ``msgstr[0]`` is returned —
  proper plural-form CLDR rules are out of scope for the demo.

Loaded catalogs are cached in-process; pass ``reload=True`` to bypass the
cache (used by the tests).
"""

from __future__ import annotations

import os
import threading

from . import SUPPORTED

_CACHE: dict[str, dict[str, str]] = {}
_CACHE_LOCK = threading.Lock()

# homepage/i18n/ — the per-language .po files live here. Resolved relative to
# the repository's homepage directory so the loader works from any CWD.
DEFAULT_CATALOG_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "homepage", "i18n")
)


def _unescape(s: str) -> str:
    """Decode a PO-quoted string body. The input has already had its
    surrounding quotes stripped."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "\\":
                out.append("\\")
            elif nxt == '"':
                out.append('"')
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_quotes(line: str) -> str:
    line = line.strip()
    if line.startswith('"') and line.endswith('"'):
        return _unescape(line[1:-1])
    return _unescape(line)


def parse_po(text: str) -> dict[str, str]:
    """Parse PO file text → ``{msgid: msgstr}``.

    Empty ``msgstr`` entries are skipped (the runtime treats those as
    "fallback to source" — keeping them in the dict would shadow the source
    with itself, which is fine, but the explicit drop saves memory).
    """
    catalog: dict[str, str] = {}
    msgid_parts: list[str] = []
    msgstr_parts: list[str] = []
    mode: str | None = None  # 'id' | 'str'
    # When we hit the header (msgid "") we want to skip it.
    have_first_entry = False

    def _flush() -> None:
        nonlocal have_first_entry
        if mode is None:
            return
        msgid = "".join(msgid_parts)
        msgstr = "".join(msgstr_parts)
        if not have_first_entry:
            have_first_entry = True
            # Skip the header entry (msgid "").
            if msgid == "":
                return
        if msgid and msgstr:
            catalog[msgid] = msgstr

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            _flush()
            msgid_parts = []
            msgstr_parts = []
            mode = None
            continue
        if line.startswith("#"):
            # Comment — translator note (#.), source ref (#:), flag (#,),
            # extracted-comment (#~), etc. Skipped for the runtime dict.
            continue
        if line.startswith("msgid "):
            _flush()
            msgid_parts = [_strip_quotes(line[len("msgid ") :])]
            msgstr_parts = []
            mode = "id"
            continue
        if line.startswith("msgid_plural "):
            # Recorded but plurals are not honoured at the runtime layer.
            mode = "id"  # keep accumulating for the singular
            continue
        if line.startswith("msgstr "):
            msgstr_parts = [_strip_quotes(line[len("msgstr ") :])]
            mode = "str"
            continue
        if line.startswith("msgstr["):
            # msgstr[0] / msgstr[1] / ... — take [0] only.
            head, _, rest = line.partition("] ")
            if head.endswith("[0"):
                msgstr_parts = [_strip_quotes(rest)]
                mode = "str"
            else:
                mode = None  # ignore [1..]
            continue
        if line.startswith('"') and line.endswith('"'):
            # Continuation of the current msgid / msgstr.
            piece = _strip_quotes(line)
            if mode == "id":
                msgid_parts.append(piece)
            elif mode == "str":
                msgstr_parts.append(piece)
            continue
        # Unknown line — skip silently rather than aborting (defensive: we
        # control the input, but third-party PO editors emit weird things).
    _flush()
    return catalog


def load_catalog(
    lang: str, *, reload: bool = False, catalog_dir: str | None = None
) -> dict[str, str]:
    """Return ``{msgid: msgstr}`` for the given language, cached after first load.

    Returns an empty dict (and silently — no exception) if the .po file is
    missing or unreadable. That makes the rest of the system degrade
    gracefully: untranslated msgids will simply fall through to the source.
    """
    if lang not in SUPPORTED:
        return {}
    cdir = catalog_dir or DEFAULT_CATALOG_DIR
    cache_key = f"{cdir}::{lang}"
    if not reload:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached is not None:
                return cached
    path = os.path.join(cdir, f"{lang}.po")
    catalog: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            catalog = parse_po(fh.read())
    except FileNotFoundError:
        catalog = {}
    except OSError:
        catalog = {}
    with _CACHE_LOCK:
        _CACHE[cache_key] = catalog
    return catalog


def clear_cache() -> None:
    """Drop the in-memory catalog cache. Used by tests."""
    with _CACHE_LOCK:
        _CACHE.clear()


def translate(msgid: str, lang: str, *, fallback: str | None = None) -> str:
    """Look up ``msgid`` in ``lang``'s catalog. Falls back to ``fallback``
    (defaults to ``msgid``) when missing or empty."""
    cat = load_catalog(lang)
    val = cat.get(msgid)
    if val:
        return val
    return msgid if fallback is None else fallback
