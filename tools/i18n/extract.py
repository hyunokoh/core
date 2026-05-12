#!/usr/bin/env python3
"""Extract translatable strings from the homepage HTML/JS tree.

Walks:
- ``homepage/**/*.html``
- ``homepage/app/*.js`` and ``homepage/sw.js``

Picks up:
- Visible text between tags (e.g. ``<span>이름</span>`` → msgid ``이름``).
- ``alt=``, ``title=``, ``placeholder=``, ``aria-label=`` attribute values.
- Bilingual ``한국어 / English`` spans (e.g. ``로그인 <span class="en">/ Sign in</span>``)
  → recorded as a single msgid (the Korean half) with the English half
  pre-filled as ``en.po``'s msgstr.
- ``data-i18n="key"`` explicit keys (no inner text needed).
- JS string literals tagged with ``t("...")`` / ``t('...')`` / ``t(\\`...\\`)``.

Writes:
- ``homepage/i18n/messages.pot`` — gettext POT template.
- Merges new msgids into existing ``homepage/i18n/<lang>.po`` files for each
  supported language. Existing translations are preserved.

Stdlib only — uses ``html.parser`` and a couple of regexes; no babel.

Operator console pages (``homepage/ops/*``) are skipped per spec — those stay
KO/EN-only.
"""

from __future__ import annotations

# When invoked as ``python3 tools/i18n/extract.py`` Python inserts the script's
# own directory at sys.path[0]; that shadows the stdlib ``locale`` module with
# our sibling ``tools/i18n/locale.py``. Strip it explicitly before importing
# anything else, then re-add it later as the *last* entry so we can still
# ``from i18n.loader import ...`` style imports below.
import os as _os_bootstrap
import sys as _sys_bootstrap

_HERE = _os_bootstrap.path.dirname(_os_bootstrap.path.abspath(__file__))
if _sys_bootstrap.path and _sys_bootstrap.path[0] == _HERE:
    _sys_bootstrap.path.pop(0)
# Add tools/ so ``from i18n.loader import parse_po`` works.
_TOOLS_DIR = _os_bootstrap.path.dirname(_HERE)
if _TOOLS_DIR not in _sys_bootstrap.path:
    _sys_bootstrap.path.append(_TOOLS_DIR)

import argparse
import datetime as _dt
import html.parser
import os
import re
import sys
from collections.abc import Iterable

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
HOMEPAGE = os.path.join(REPO_ROOT, "homepage")
I18N_DIR = os.path.join(HOMEPAGE, "i18n")
SUPPORTED_LANGS = ["ko", "en", "ja", "zh", "es", "de"]

# Files / directories we don't translate. The operator console + load-test
# report stay in their current KO/EN form.
SKIP_DIRS = (
    os.path.join("homepage", "ops"),
    os.path.join("homepage", "app", "game-day-report"),
)
SKIP_FILES = {
    os.path.join("homepage", "app", "load-report.html"),
    os.path.join("homepage", "app", "game-day.html"),
}

# Attributes that carry visible text we need to translate.
TRANSLATABLE_ATTRS = ("alt", "title", "placeholder", "aria-label")

# Tags whose text content we don't translate (purely structural / code).
SKIP_TEXT_TAGS = {"script", "style", "code", "pre", "noscript", "svg"}


# ---------------------------------------------------------------------------
# Bilingual-span detector. Many existing pages emit `<el>한국어 <span class="en">/ English</span></el>`.
# The KO half is the canonical msgid; the EN half is its `en.po` translation.
# We record both halves with one extraction pass so en.po doesn't need any
# manual filling.
# ---------------------------------------------------------------------------
_BILINGUAL_RE = re.compile(
    r"""
    ^                              # anchor to the start of the chunk
    (?P<ko>[^<]+?)                 # Korean half (any non-tag text)
    \s*<span\s+class="(?:menu-)?en"\s*>\s*/\s*  # "<span class="en"> /"
    (?P<en>.+?)                    # English half
    </span>\s*$                    # closing span
    """,
    re.VERBOSE | re.DOTALL,
)

# Slash-separated bilingual text without a span (e.g. ``이메일 / Email``,
# ``11자리 / 11 digits starting with 010``). We split on the first `` / ``
# (slash with surrounding spaces) and accept any RHS that contains at least
# two consecutive Latin letters — that's stricter than "any string" but
# permissive enough to catch things like "11 digits" or "256-bit AES" that
# the previous regex rejected.
_SLASH_BILINGUAL_RE = re.compile(r"^\s*(?P<ko>[^/]+?)\s*/\s*(?P<en>.*?[A-Za-z]{2,}.*?)\s*$")


def _looks_korean(text: str) -> bool:
    """Heuristic: at least one Hangul codepoint present."""
    for ch in text:
        if "가" <= ch <= "힣":
            return True
    return False


def _looks_translatable(text: str) -> bool:
    """Skip pure numbers, single symbols, dates, code identifiers."""
    s = text.strip()
    if not s:
        return False
    # Hard cap on length — extracted text should be UI copy, not entire
    # paragraphs or HTML blobs. Anything bigger is almost always a sign
    # we picked up multiple sibling elements at once.
    if len(s) > 200:
        return False
    # Multi-line / blob: skip. Real UI labels are single-line.
    if "\n" in s:
        return False
    # Template-literal interpolation marker → unsafe to localise (the
    # ${...} is a JS expression evaluated at runtime).
    if "${" in s:
        return False
    # Pure number / percentage / currency / decoration / arrow symbols.
    if re.fullmatch(r"[\d.,\s%+\-▲▼→←↑↓·•—–·~$₩¥€£]+", s):
        return False
    # Looks like a placeholder ID (e.g. "a91f…c4d2") or a hash.
    if re.fullmatch(r"[a-fA-F0-9…]+", s):
        return False
    # Single letter / icon
    if len(s) == 1 and not s.isalpha():
        return False
    # Skip strings that are mostly punctuation / symbols.
    word_chars = sum(1 for ch in s if ch.isalnum() or ord(ch) > 127)
    if word_chars < 2:
        return False
    # Skip strings that start with a slash — those are the *English half*
    # of an unwrapped bilingual span captured by accident (e.g. "/ Sign in").
    if s.startswith("/"):
        return False
    return True


# ---------------------------------------------------------------------------
# HTML extractor.
# ---------------------------------------------------------------------------


class _Extractor(html.parser.HTMLParser):
    """Collects msgids + bilingual pairs from one HTML document.

    Strategy: build a stack of (tagname, buffer) frames. Each tag we open
    pushes a new buffer; closing it flushes the buffer into one msgid.
    Nested tags inside the buffer (e.g. ``<strong>foo</strong>``) get
    serialised as part of the text — we only extract the *outermost* visible
    string per leaf tag. This matches the typical i18n granularity (whole
    sentence, not per-word) and matches how `data-i18n` keys are used.
    """

    def __init__(self, source_path: str):
        super().__init__(convert_charrefs=True)
        self.source_path = source_path
        # Each entry: {"tag": str, "data_i18n": str | None, "buffer": list[str], "depth": int}
        self._stack: list[dict] = []
        self._depth = 0
        # Output: {msgid: {"refs": [...], "notes": [...], "en": optional}}.
        self.msgids: dict[str, dict] = {}

    # --- helpers --------------------------------------------------------
    def _ref(self) -> str:
        line, _ = self.getpos()
        rel = os.path.relpath(self.source_path, REPO_ROOT)
        return f"{rel}:{line}"

    def _record(self, msgid: str, *, en: str | None = None, note: str | None = None) -> None:
        if not msgid or not _looks_translatable(msgid):
            return
        msgid = msgid.strip()
        entry = self.msgids.setdefault(msgid, {"refs": [], "notes": [], "en": None})
        ref = self._ref()
        if ref not in entry["refs"]:
            entry["refs"].append(ref)
        if en and not entry["en"]:
            entry["en"] = en.strip()
        if note and note not in entry["notes"]:
            entry["notes"].append(note)

    # --- HTMLParser hooks -----------------------------------------------
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        # Extract attribute-borne strings up front (they don't depend on the
        # element's text content).
        for a in TRANSLATABLE_ATTRS:
            v = attrs_dict.get(a)
            if v:
                self._record(v, note=f"{a} attribute")
        # data-i18n-en lets templates supply the canonical English alongside
        # the KO source. Lets us pre-fill en.po without a separate scan.
        data_en = attrs_dict.get("data-i18n-en")
        # Push a frame so handle_data / handle_endtag can collect content.
        skip_text = tag.lower() in SKIP_TEXT_TAGS
        # Bilingual English-half marker: <span class="en"> or <span class="menu-en">.
        # When set, this frame's text doesn't propagate to ancestor buffers
        # (so the parent's msgid stays purely Korean) and the EN text is
        # later captured by the raw-source regex sweep.
        cls = (attrs_dict.get("class") or "").strip().split()
        is_en_half = tag.lower() == "span" and ("en" in cls or "menu-en" in cls)
        self._stack.append(
            {
                "tag": tag.lower(),
                "data_i18n": attrs_dict.get("data-i18n"),
                "data_en": data_en,
                "buffer": [],
                "skip_text": skip_text,
                "depth": self._depth,
                "_is_en_half": is_en_half,
            }
        )
        self._depth += 1

    def handle_endtag(self, tag):
        # Pop frames until we find the matching tag. Forgiving: deals with
        # the occasional unbalanced HTML in the source.
        if not self._stack:
            return
        # Find matching frame (search from top).
        idx = len(self._stack) - 1
        while idx >= 0 and self._stack[idx]["tag"] != tag.lower():
            idx -= 1
        if idx < 0:
            return
        frame = self._stack.pop(idx)
        # Drop any frames that were left dangling above it.
        while len(self._stack) > idx:
            self._stack.pop()
        self._depth = idx
        text = "".join(frame["buffer"]).strip()
        # data-i18n explicit key: always record. Use the key as msgid, the
        # text as the (Korean) source.
        if frame["data_i18n"]:
            key = frame["data_i18n"]
            self._record(text or key, note=f"data-i18n key: {key}")
            if frame["data_en"]:
                self.msgids[text or key]["en"] = frame["data_en"]
            return
        # Bilingual span pattern: "<el>KO <span class=en>/ EN</span></el>".
        # We match against the raw buffer (which includes the nested
        # `<span class="en">` markup serialised by our handle_starttag /
        # handle_endtag flushers). But our buffer only contains text, not
        # tags — so handle that separately via the `_pending_bilingual`
        # mechanism: when we close a <span class="en">, record the pair.
        if frame["tag"] in ("span",) and frame.get("_is_en_half"):
            return
        if not text:
            return
        # If the text contains a slash-style bilingual marker (no span,
        # plain "KO / EN") and the LHS looks Korean, split into msgid+en.
        m_slash = _SLASH_BILINGUAL_RE.match(text)
        if m_slash and _looks_korean(m_slash.group("ko")):
            ko = m_slash.group("ko").strip()
            en = m_slash.group("en").strip()
            self._record(ko, en=en, note="bilingual KO / EN")
            return
        # Plain text — record as a msgid in whatever language it is.
        self._record(text)

    def handle_startendtag(self, tag, attrs):
        # Self-closing element (e.g. <img alt="...">). Just hit the
        # attribute pass and don't open a frame.
        attrs_dict = dict(attrs)
        for a in TRANSLATABLE_ATTRS:
            v = attrs_dict.get(a)
            if v:
                self._record(v, note=f"{a} attribute")

    def handle_data(self, data):
        if not self._stack:
            return
        top = self._stack[-1]
        if top["skip_text"]:
            return
        top["buffer"].append(data)
        # Also append to ancestor buffers so outer tag close gets the full
        # text. (Important: <h1>이름 <span class="en">/ Name</span></h1>
        # needs the h1 frame to see "이름 / Name" combined.)
        # EXCEPTION: when the top frame is the English half of a bilingual
        # span (`<span class="en">…`), suppress propagation. Otherwise a
        # parent like `<summary>"솔벤시"는 무엇인가요?<span class="en">What does …</span></summary>`
        # would yield a concatenated msgid mixing KO+EN. The English text is
        # recovered separately by the raw-source regex sweep below.
        if top.get("_is_en_half"):
            return
        for frame in self._stack[:-1]:
            if not frame["skip_text"]:
                frame["buffer"].append(data)

    def handle_entityref(self, name):
        self.handle_data(html.parser.unescape(f"&{name};"))

    def handle_charref(self, name):
        self.handle_data(html.parser.unescape(f"&#{name};"))


def _scan_html(path: str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return {}
    p = _Extractor(path)
    try:
        p.feed(src)
        p.close()
    except Exception as exc:  # noqa: BLE001 - bad HTML shouldn't crash extraction
        print(f"warn: cannot parse HTML {path}: {exc}", file=sys.stderr)
    # Also scan the raw source for class="en" /class="menu-en" bilingual
    # patterns. The HTMLParser-driven path collects the *outer* element's
    # full text, but extra patterns may still slip through nested
    # constructions (e.g. h1 inside a section). A regex sweep is a cheap
    # safety net.
    for m in re.finditer(
        r">([^<>]{2,200}?)\s*<span\s+class=\"(?:menu-)?en\"\s*>\s*/?\s*([^<]+?)</span>",
        src,
    ):
        ko = m.group(1).strip()
        en = m.group(2).strip()
        if _looks_korean(ko) and _looks_translatable(ko):
            entry = p.msgids.setdefault(ko, {"refs": [], "notes": [], "en": None})
            if not entry["en"]:
                entry["en"] = en
            if "bilingual KO <span> EN" not in entry["notes"]:
                entry["notes"].append("bilingual KO <span> EN")
    # Title tags are eaten by the parser; capture explicitly so we get
    # something like ``<title>로그인 / Sign in — zkCEX</title>``.
    for m in re.finditer(r"<title>([^<]+)</title>", src):
        title = m.group(1).strip()
        if _looks_translatable(title):
            p.msgids.setdefault(title, {"refs": [], "notes": [], "en": None})
    # meta name=description / og:title / og:description
    for m in re.finditer(
        r"<meta[^>]+(?:name|property)=\"(?:description|og:title|og:description)\"[^>]*content=\"([^\"]+)\"",
        src,
    ):
        v = m.group(1).strip()
        if _looks_translatable(v):
            p.msgids.setdefault(v, {"refs": [], "notes": [], "en": None})
    return p.msgids


# ---------------------------------------------------------------------------
# JS scanner — looks for ``t("...")`` / ``t('...')`` / ``t(\\`...\\`)`` and
# also plain Korean string literals in app.js (because that file emits
# UI strings via template literals into the header HTML).
# ---------------------------------------------------------------------------

_JS_T_CALL_RE = re.compile(r"""\bt\(\s*(['"`])(.+?)\1""", re.DOTALL)
_JS_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)+?)\1""", re.DOTALL)


def _scan_js(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return out
    rel = os.path.relpath(path, REPO_ROOT)
    # Strip /* ... */ block comments and // line comments before scanning so
    # we don't pick up explanatory copy.
    src_clean = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src_clean = re.sub(r"(^|[^:])//[^\n]*", lambda m: m.group(1), src_clean)
    # Explicit t("...") calls — always extract.
    for m in _JS_T_CALL_RE.finditer(src_clean):
        msgid = m.group(2).strip()
        if not _looks_translatable(msgid):
            continue
        line = src_clean[: m.start()].count("\n") + 1
        entry = out.setdefault(msgid, {"refs": [], "notes": ["t() call"], "en": None})
        entry["refs"].append(f"{rel}:{line}")
    # String literals that contain Hangul — likely UI text. We only do this
    # for app.js / futures.js which both embed UI markup as template strings.
    if os.path.basename(path) in ("app.js", "futures.js"):
        for m in _JS_STRING_RE.finditer(src_clean):
            text = m.group(2)
            if not _looks_korean(text) or not _looks_translatable(text):
                continue
            # Slash-bilingual split (e.g. "로그아웃 / Sign out").
            m_slash = _SLASH_BILINGUAL_RE.match(text)
            if m_slash and _looks_korean(m_slash.group("ko")):
                ko = m_slash.group("ko").strip()
                en = m_slash.group("en").strip()
                line = src_clean[: m.start()].count("\n") + 1
                entry = out.setdefault(ko, {"refs": [], "notes": ["JS string"], "en": None})
                entry["refs"].append(f"{rel}:{line}")
                if not entry["en"]:
                    entry["en"] = en
                continue
            line = src_clean[: m.start()].count("\n") + 1
            entry = out.setdefault(text.strip(), {"refs": [], "notes": ["JS string"], "en": None})
            entry["refs"].append(f"{rel}:{line}")
    return out


# ---------------------------------------------------------------------------
# Discovery + merge.
# ---------------------------------------------------------------------------


def _iter_sources() -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(HOMEPAGE):
        # Filter SKIP_DIRS in place so os.walk doesn't recurse into them.
        keep: list[str] = []
        for d in dirnames:
            full = os.path.relpath(os.path.join(dirpath, d), REPO_ROOT)
            if not any(full.startswith(s) for s in SKIP_DIRS):
                keep.append(d)
        dirnames[:] = keep
        # i18n/ itself is our output dir — never scan it.
        if os.path.basename(dirpath) == "i18n":
            dirnames[:] = []
            continue
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, REPO_ROOT)
            if rel in SKIP_FILES:
                continue
            if fn.endswith(".html"):
                yield full
            elif fn.endswith(".js") and os.path.basename(dirpath) in ("app", "homepage"):
                yield full


def _merge(target: dict[str, dict], src: dict[str, dict]) -> None:
    for k, v in src.items():
        e = target.setdefault(k, {"refs": [], "notes": [], "en": None})
        for r in v.get("refs") or []:
            if r not in e["refs"]:
                e["refs"].append(r)
        for n in v.get("notes") or []:
            if n not in e["notes"]:
                e["notes"].append(n)
        if v.get("en") and not e["en"]:
            e["en"] = v["en"]


# ---------------------------------------------------------------------------
# .po / .pot writers.
# ---------------------------------------------------------------------------


def _po_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")


def _write_po(
    path: str,
    header: dict[str, str],
    entries: dict[str, dict],
    prefilled: dict[str, str] | None = None,
) -> int:
    """Write a .po file. Returns number of translated entries.

    ``prefilled`` is an existing ``{msgid: msgstr}`` map (loaded from an
    earlier .po) — non-empty msgstrs from this map are preserved across runs.
    """
    prefilled = prefilled or {}
    lines: list[str] = []
    # Standard PO header — empty msgid, headers in msgstr.
    lines.append('msgid ""')
    lines.append('msgstr ""')
    for k, v in header.items():
        lines.append(f'"{k}: {v}\\n"')
    lines.append("")
    translated = 0
    for msgid in sorted(entries):
        info = entries[msgid]
        notes = info.get("notes") or []
        refs = info.get("refs") or []
        en = info.get("en")
        if notes:
            for note in notes:
                lines.append(f"#. {note}")
        if en and header.get("Language") != "en":
            # English half harvested from the source — useful translator hint.
            lines.append(f"#. EN: {_po_escape(en)}")
        for ref in refs[:8]:
            lines.append(f"#: {ref}")
        lines.append(f'msgid "{_po_escape(msgid)}"')
        # Decide on msgstr: prefilled wins, then English half for en.po, then
        # blank.
        msgstr = prefilled.get(msgid, "")
        if not msgstr and header.get("Language") == "en" and en:
            msgstr = en
        # ko.po: msgstr == msgid (since KO is the canonical source).
        if not msgstr and header.get("Language") == "ko":
            msgstr = msgid
        if msgstr:
            translated += 1
        lines.append(f'msgstr "{_po_escape(msgstr)}"')
        lines.append("")
    text = "\n".join(lines)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return translated


def _load_existing_po(path: str) -> dict[str, str]:
    """Return existing translations from ``path`` (or {} if missing).

    We import lazily to avoid a hard import cycle with extract.py running as a
    standalone script.
    """
    try:
        from i18n.loader import parse_po  # type: ignore
    except ImportError:
        from tools.i18n.loader import parse_po  # type: ignore
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_po(fh.read())
    except OSError:
        return {}


def _header(lang: str) -> dict[str, str]:
    now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M+0000")
    return {
        "Project-Id-Version": "zkCEX 1.0",
        "POT-Creation-Date": now,
        "PO-Revision-Date": now,
        "Language": lang,
        "Language-Team": "zkCEX i18n",
        "MIME-Version": "1.0",
        "Content-Type": "text/plain; charset=UTF-8",
        "Content-Transfer-Encoding": "8bit",
        "X-Generator": "tools/i18n/extract.py (stdlib)",
    }


# Hand-curated translations for the high-value strings live in seeds.py.
# Loaded lazily so the module also works as a standalone script. Untranslated
# msgids fall back to the KO source at runtime.
try:
    from i18n.seeds import BY_LANG as _EXTERNAL_SEEDS  # type: ignore
except ImportError:
    try:
        from tools.i18n.seeds import BY_LANG as _EXTERNAL_SEEDS  # type: ignore
    except ImportError:
        _EXTERNAL_SEEDS = {}

_INLINE_SEEDS: dict[str, dict[str, str]] = {
    # ===== ja =====
    "ja": {
        # Top-level page chrome
        "로그인 / Sign in — zkCEX": "ログイン / Sign in — zkCEX",
        "zkCEX 로그인. 이메일과 비밀번호로 안전하게 시작하세요.": "zkCEX へのログイン。メールアドレスとパスワードで安全にお始めください。",
        "로그인": "ログイン",
        "회원가입": "新規登録",
        "다시 만나서 반갑습니다": "おかえりなさい",
        "계정을 만드세요": "アカウントを作成",
        "이메일 / Email": "メールアドレス",
        "비밀번호 / Password": "パスワード",
        "8자 이상 / At least 8 characters": "8文字以上",
        "2단계 인증": "二段階認証",
        "코드 / Code": "コード",
        "6자리 숫자 / 6 digits": "6桁の数字",
        "확인": "確認",
        "인증 앱에 표시된 6자리 코드를 입력하세요. / Enter the 6-digit code from your authenticator app.": "認証アプリに表示されている6桁のコードを入力してください。",
        # Header / nav
        "시장": "マーケット",
        "잔고 검증": "残高検証",
        "투명성": "透明性",
        "요금": "手数料",
        "고객 지원": "サポート",
        "시작하기": "始める",
        "주 메뉴": "メインメニュー",
        # Hero
        "검증 가능한 거래소": "検証可能な取引所",
        "안전하게 거래하세요.": "安心して取引できます。",
        "잔고는 직접 검증하세요.": "残高はご自身で検証できます。",
        "무료로 시작하기": "無料で始める",
        "잔고 검증 보기": "残高検証を見る",
        "5분마다 잔고 증명": "5分ごとに残高証明",
        "0.05% 메이커 수수료": "メイカー手数料0.05%",
        "24/7 한국어 지원": "24時間365日のサポート",
        # Sidebar / user menu
        "지갑": "ウォレット",
        "스테이킹": "ステーキング",
        "선물": "先物取引",
        "ZK 거래": "ZK取引",
        "주문 내역": "注文履歴",
        "리포트": "レポート",
        "본인인증": "本人確認",
        "본인인증 정보": "本人確認情報",
        "친구 초대": "友達紹介",
        "API 키": "APIキー",
        "보안 / 2FA": "セキュリティ / 2FA",
        "API 문서": "APIドキュメント",
        "AI 에이전트": "AIエージェント",
        "로그아웃": "ログアウト",
        # KYC
        "본인 확인을 완료해 주세요": "本人確認を完了してください",
        "이름": "氏名",
        "주민등록번호 앞 6자리": "生年月日（YYMMDD）",
        "주민등록번호": "本人確認番号",
        "휴대폰 번호": "携帯電話番号",
        "통신사": "通信キャリア",
        "인증번호": "認証コード",
        "다음": "次へ",
        # Trade
        "거래": "取引",
        "매수": "買い",
        "매도": "売り",
        "주문 유형": "注文種別",
        "지정가": "指値",
        "시장가": "成行",
        "가격": "価格",
        "수량": "数量",
        "총액": "合計",
        "수수료 / Fees": "手数料",
        # Wallet
        "잔고": "残高",
        "입금": "入金",
        "출금": "出金",
        "거래 내역": "取引履歴",
        "주문": "注文",
        # Bottom nav (KO singles)
        "검증": "検証",
        "더보기": "詳細",
    },
    # ===== zh =====
    "zh": {
        "로그인 / Sign in — zkCEX": "登录 / Sign in — zkCEX",
        "zkCEX 로그인. 이메일과 비밀번호로 안전하게 시작하세요.": "登录 zkCEX。使用电子邮件和密码安全开始。",
        "로그인": "登录",
        "회원가입": "注册",
        "다시 만나서 반갑습니다": "欢迎回来",
        "계정을 만드세요": "创建您的账户",
        "이메일 / Email": "电子邮箱",
        "비밀번호 / Password": "密码",
        "8자 이상 / At least 8 characters": "至少8个字符",
        "2단계 인증": "双重验证",
        "코드 / Code": "验证码",
        "6자리 숫자 / 6 digits": "6位数字",
        "확인": "确认",
        "인증 앱에 표시된 6자리 코드를 입력하세요. / Enter the 6-digit code from your authenticator app.": "请输入身份验证器应用中显示的6位验证码。",
        "시장": "市场",
        "잔고 검증": "余额验证",
        "투명성": "透明度",
        "요금": "费率",
        "고객 지원": "客户支持",
        "시작하기": "开始使用",
        "주 메뉴": "主菜单",
        "검증 가능한 거래소": "可验证的交易所",
        "안전하게 거래하세요.": "安全交易。",
        "잔고는 직접 검증하세요.": "余额自助验证。",
        "무료로 시작하기": "免费开始",
        "잔고 검증 보기": "查看余额验证",
        "5분마다 잔고 증명": "每5分钟一次余额证明",
        "0.05% 메이커 수수료": "0.05% 挂单手续费",
        "24/7 한국어 지원": "24/7 客户支持",
        "지갑": "钱包",
        "스테이킹": "质押",
        "선물": "合约",
        "ZK 거래": "ZK 交易",
        "주문 내역": "订单历史",
        "리포트": "报告",
        "본인인증": "身份认证",
        "본인인증 정보": "身份认证信息",
        "친구 초대": "邀请好友",
        "API 키": "API 密钥",
        "보안 / 2FA": "安全 / 2FA",
        "API 문서": "API 文档",
        "AI 에이전트": "AI 代理",
        "로그아웃": "退出登录",
        "본인 확인을 완료해 주세요": "请完成身份认证",
        "이름": "姓名",
        "주민등록번호 앞 6자리": "出生日期（YYMMDD）",
        "주민등록번호": "身份证号码",
        "휴대폰 번호": "手机号码",
        "통신사": "运营商",
        "인증번호": "验证码",
        "다음": "下一步",
        "거래": "交易",
        "매수": "买入",
        "매도": "卖出",
        "주문 유형": "订单类型",
        "지정가": "限价",
        "시장가": "市价",
        "가격": "价格",
        "수량": "数量",
        "총액": "总额",
        "수수료 / Fees": "手续费",
        "잔고": "余额",
        "입금": "充值",
        "출금": "提现",
        "거래 내역": "交易历史",
        "주문": "订单",
        "검증": "验证",
        "더보기": "更多",
    },
    # ===== es =====
    "es": {
        "로그인 / Sign in — zkCEX": "Iniciar sesión / Sign in — zkCEX",
        "zkCEX 로그인. 이메일과 비밀번호로 안전하게 시작하세요.": "Inicia sesión en zkCEX. Empieza de forma segura con tu correo y contraseña.",
        "로그인": "Iniciar sesión",
        "회원가입": "Registrarse",
        "다시 만나서 반갑습니다": "Bienvenido de nuevo",
        "계정을 만드세요": "Crea tu cuenta",
        "이메일 / Email": "Correo electrónico",
        "비밀번호 / Password": "Contraseña",
        "8자 이상 / At least 8 characters": "Al menos 8 caracteres",
        "2단계 인증": "Autenticación en dos pasos",
        "코드 / Code": "Código",
        "6자리 숫자 / 6 digits": "6 dígitos",
        "확인": "Verificar",
        "인증 앱에 표시된 6자리 코드를 입력하세요. / Enter the 6-digit code from your authenticator app.": "Introduce el código de 6 dígitos de tu aplicación de autenticación.",
        "시장": "Mercados",
        "잔고 검증": "Verificar saldo",
        "투명성": "Transparencia",
        "요금": "Tarifas",
        "고객 지원": "Soporte",
        "시작하기": "Comenzar",
        "주 메뉴": "Menú principal",
        "검증 가능한 거래소": "Exchange verificable",
        "안전하게 거래하세요.": "Opera con confianza.",
        "잔고는 직접 검증하세요.": "Verifica tu saldo tú mismo.",
        "무료로 시작하기": "Empezar gratis",
        "잔고 검증 보기": "Ver la verificación",
        "5분마다 잔고 증명": "Prueba de saldo cada 5 min",
        "0.05% 메이커 수수료": "0,05 % de comisión maker",
        "24/7 한국어 지원": "Soporte 24/7",
        "지갑": "Cartera",
        "스테이킹": "Staking",
        "선물": "Futuros",
        "ZK 거래": "Trading ZK",
        "주문 내역": "Historial de órdenes",
        "리포트": "Informes",
        "본인인증": "Verificar identidad",
        "본인인증 정보": "Datos de KYC",
        "친구 초대": "Invitar amigos",
        "API 키": "Claves de API",
        "보안 / 2FA": "Seguridad / 2FA",
        "API 문서": "Documentación de API",
        "AI 에이전트": "Agente de IA",
        "로그아웃": "Cerrar sesión",
        "본인 확인을 완료해 주세요": "Completa la verificación de identidad",
        "이름": "Nombre",
        "주민등록번호 앞 6자리": "Fecha de nacimiento (AAMMDD)",
        "주민등록번호": "Documento de identidad",
        "휴대폰 번호": "Número de teléfono",
        "통신사": "Operador",
        "인증번호": "Código de verificación",
        "다음": "Siguiente",
        "거래": "Operar",
        "매수": "Comprar",
        "매도": "Vender",
        "주문 유형": "Tipo de orden",
        "지정가": "Límite",
        "시장가": "Mercado",
        "가격": "Precio",
        "수량": "Cantidad",
        "총액": "Total",
        "수수료 / Fees": "Comisiones",
        "잔고": "Saldo",
        "입금": "Depositar",
        "출금": "Retirar",
        "거래 내역": "Historial de operaciones",
        "주문": "Órdenes",
        "검증": "Verificar",
        "더보기": "Más",
    },
    # ===== de =====
    "de": {
        "로그인 / Sign in — zkCEX": "Anmelden / Sign in — zkCEX",
        "zkCEX 로그인. 이메일과 비밀번호로 안전하게 시작하세요.": "Bei zkCEX anmelden. Sicher starten mit E-Mail und Passwort.",
        "로그인": "Anmelden",
        "회원가입": "Registrieren",
        "다시 만나서 반갑습니다": "Willkommen zurück",
        "계정을 만드세요": "Konto erstellen",
        "이메일 / Email": "E-Mail",
        "비밀번호 / Password": "Passwort",
        "8자 이상 / At least 8 characters": "Mindestens 8 Zeichen",
        "2단계 인증": "Zwei-Faktor-Authentifizierung",
        "코드 / Code": "Code",
        "6자리 숫자 / 6 digits": "6 Ziffern",
        "확인": "Bestätigen",
        "인증 앱에 표시된 6자리 코드를 입력하세요. / Enter the 6-digit code from your authenticator app.": "Geben Sie den sechsstelligen Code aus Ihrer Authenticator-App ein.",
        "시장": "Märkte",
        "잔고 검증": "Guthaben prüfen",
        "투명성": "Transparenz",
        "요금": "Gebühren",
        "고객 지원": "Support",
        "시작하기": "Loslegen",
        "주 메뉴": "Hauptmenü",
        "검증 가능한 거래소": "Überprüfbare Börse",
        "안전하게 거래하세요.": "Sicher handeln.",
        "잔고는 직접 검증하세요.": "Guthaben selbst prüfen.",
        "무료로 시작하기": "Kostenlos starten",
        "잔고 검증 보기": "Überprüfung ansehen",
        "5분마다 잔고 증명": "Alle 5 Min. Guthabennachweis",
        "0.05% 메이커 수수료": "0,05 % Maker-Gebühr",
        "24/7 한국어 지원": "Support rund um die Uhr",
        "지갑": "Wallet",
        "스테이킹": "Staking",
        "선물": "Futures",
        "ZK 거래": "ZK-Handel",
        "주문 내역": "Auftragshistorie",
        "리포트": "Berichte",
        "본인인증": "Identitätsprüfung",
        "본인인증 정보": "KYC-Daten",
        "친구 초대": "Freunde einladen",
        "API 키": "API-Schlüssel",
        "보안 / 2FA": "Sicherheit / 2FA",
        "API 문서": "API-Dokumentation",
        "AI 에이전트": "KI-Agent",
        "로그아웃": "Abmelden",
        "본인 확인을 완료해 주세요": "Bitte schließen Sie die Identitätsprüfung ab",
        "이름": "Name",
        "주민등록번호 앞 6자리": "Geburtsdatum (JJMMTT)",
        "주민등록번호": "Ausweisnummer",
        "휴대폰 번호": "Mobilnummer",
        "통신사": "Mobilfunkanbieter",
        "인증번호": "Bestätigungscode",
        "다음": "Weiter",
        "거래": "Handel",
        "매수": "Kaufen",
        "매도": "Verkaufen",
        "주문 유형": "Auftragsart",
        "지정가": "Limit",
        "시장가": "Markt",
        "가격": "Preis",
        "수량": "Menge",
        "총액": "Summe",
        "수수료 / Fees": "Gebühren",
        "잔고": "Guthaben",
        "입금": "Einzahlen",
        "출금": "Auszahlen",
        "거래 내역": "Handelsverlauf",
        "주문": "Aufträge",
        "검증": "Prüfen",
        "더보기": "Mehr",
    },
}


# Final merged seed table: external (seeds.py — the big curated list) layered
# *over* the small inline starter dict, so the seeds.py entries win on
# conflicts and the inline dict acts as a fallback safety net.
SEED_TRANSLATIONS: dict[str, dict[str, str]] = {
    lang: {**_INLINE_SEEDS.get(lang, {}), **_EXTERNAL_SEEDS.get(lang, {})}
    for lang in set(_INLINE_SEEDS) | set(_EXTERNAL_SEEDS)
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract zkCEX i18n strings.")
    ap.add_argument("--out-dir", default=I18N_DIR, help="output directory for .po/.pot files")
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = ap.parse_args(argv)
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    entries: dict[str, dict] = {}
    n_html = n_js = 0
    for path in _iter_sources():
        if path.endswith(".html"):
            n_html += 1
            _merge(entries, _scan_html(path))
        elif path.endswith(".js"):
            n_js += 1
            _merge(entries, _scan_js(path))

    if not args.quiet:
        sys.stderr.write(
            f"[extract] scanned {n_html} HTML + {n_js} JS files → {len(entries)} unique msgids\n"
        )

    # Write the POT template (empty msgstrs).
    pot_path = os.path.join(out_dir, "messages.pot")
    _write_po(pot_path, _header("POT"), entries)
    if not args.quiet:
        sys.stderr.write(f"[extract] wrote {pot_path}\n")

    # Per-language .po files.
    for lang in SUPPORTED_LANGS:
        po_path = os.path.join(out_dir, f"{lang}.po")
        existing = _load_existing_po(po_path)
        # Layer in the SEED_TRANSLATIONS that match this run's msgids.
        seed = SEED_TRANSLATIONS.get(lang, {})
        prefilled: dict[str, str] = {}
        for msgid in entries:
            if msgid in existing and existing[msgid]:
                prefilled[msgid] = existing[msgid]
            elif msgid in seed:
                prefilled[msgid] = seed[msgid]
        translated = _write_po(po_path, _header(lang), entries, prefilled=prefilled)
        if not args.quiet:
            pct = 100 * translated / max(1, len(entries))
            sys.stderr.write(
                f"[extract] {lang}.po: {translated}/{len(entries)} translated ({pct:.0f}%)\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
