"""HTML response post-processor for runtime i18n.

The proxy calls :func:`localize_html` on every static HTML body before
sending it to the client. The function:

1. Sets the ``<html lang="...">`` attribute to the negotiated language.
2. Rewrites bilingual ``KO <span class="en">/ EN</span>`` patterns. For:
   - ``ko``: leaves the spans alone (the page already renders correctly).
   - ``en``: hides the KO half and unwraps the EN half — the result looks
     identical to today's ``/en/index.html``.
   - ``ja|zh|es|de``: looks up the KO half in the catalog and substitutes
     the translated string; the EN ``<span class="en">`` is stripped.
3. Substitutes elements carrying ``data-i18n="key"`` with the translated
   string.
4. Injects a ``<script>window.__ZKCEX_I18N = {...}</script>`` block carrying
   the catalog so client-side ``t("key")`` calls (homepage/app/i18n.js)
   resolve without an extra round trip.
5. Injects a small language-picker helper script at the end of <body>.

Performance: a couple of regex passes over the body. For a typical ~50 KB
HTML page on a developer laptop, this completes in well under 5 ms.
"""

from __future__ import annotations

import html
import json
import re

from .loader import load_catalog

# Native language names. Built via \uXXXX escapes so the source file stays
# in pure ASCII — avoids any chance of a double-UTF-8 round-trip mangling
# the literal display strings.
_LANG_NAMES = {
    "ko": "한국어",  # 한국어
    "en": "English",
    "ja": "日本語",  # 日本語
    "zh": "中文",  # 中文
    "es": "Español",  # Español
    "de": "Deutsch",
}

# ---------------------------------------------------------------------------
# Bilingual-span regexes. Kept loose: the page authors are inconsistent
# about whitespace and attribute order, so we match the essential pattern
# rather than a strict structure.
# ---------------------------------------------------------------------------

# `KO <span class="en">/ EN</span>` — captures both halves.
_BILINGUAL_SPAN_RE = re.compile(
    r"(?P<ko>[^<>\n]{1,200}?)\s*<span\s+class=\"(?:menu-)?en\"\s*>\s*/\s*(?P<en>[^<]+?)</span>",
    re.DOTALL,
)

# Bilingual text without a span wrapper: ``>이름 / Name<`` or
# ``>이름 / Name — zkCEX<``. The `>` / `<` anchors keep us inside one HTML
# element. The KO half must contain at least one Hangul codepoint to avoid
# matching paths like ``href="/a/b"``.
_BILINGUAL_PLAIN_RE = re.compile(
    r">(?P<ko>[^<>\n/]*[가-힣][^<>\n/]*?)\s*/\s*(?P<en>[A-Za-z][A-Za-z0-9 ,.&\-'()]*?)<",
)

# `<title>KO / EN — zkCEX</title>` — title tags can't host spans, so they
# typically use a plain "/" separator instead.
_TITLE_BILINGUAL_RE = re.compile(r"<title>([^<]+)</title>")

# `<el data-i18n="key">...</el>` — captures the element and its inner HTML.
_DATA_I18N_RE = re.compile(
    r"<(?P<tag>[a-zA-Z][a-zA-Z0-9]*)\s+(?P<pre>[^>]*?)data-i18n=\"(?P<key>[^\"]+)\"(?P<post>[^>]*)>(?P<inner>.*?)</(?P=tag)>",
    re.DOTALL,
)

_HTML_LANG_RE = re.compile(r"(<html\b[^>]*?)\blang=\"[^\"]*\"", re.IGNORECASE)

# Catalog keys that look like the JS app.js header menu. We expose a tiny
# subset to the client at runtime so the dynamic header rendered by app.js
# also picks up translations.
_CLIENT_EXPOSE_KEYS = {
    "지갑",  # 지갑 (Wallet)
    "스테이킹",  # 스테이킹 (Staking)
    "선물",  # 선물 (Futures)
    "ZK 거래",  # ZK 거래
    "주문 내역",  # 주문 내역
    "리포트",  # 리포트
    "잔고 검증",  # 잔고 검증
    "본인인증",  # 본인인증
    "친구 초대",  # 친구 초대
    "API 키",  # API 키
    "보안 / 2FA",  # 보안 / 2FA
    "API 문서",  # API 문서
    "AI 에이전트",  # AI 에이전트
    "로그아웃",  # 로그아웃
    "시장",  # 시장
    "거래",  # 거래
    "검증",  # 검증
    "더보기",  # 더보기
    "투명성",  # 투명성
    "요금",  # 요금
    "고객 지원",  # 고객 지원
    "시작하기",  # 시작하기
}


def _set_html_lang(body: str, lang: str) -> str:
    """Replace (or add) the ``lang=`` attribute on the <html> tag."""
    if _HTML_LANG_RE.search(body):
        return _HTML_LANG_RE.sub(r'\1lang="' + lang + '"', body, count=1)
    # Inject a lang="..." into the opening <html ...> tag.
    return re.sub(r"<html\b", f'<html lang="{lang}"', body, count=1)


def _translate_bilingual(body: str, catalog: dict[str, str], lang: str) -> str:
    """Replace bilingual spans with the chosen-language single string."""
    if lang == "ko":
        # Korean is the source — leave spans intact.
        return body
    if lang == "en":

        def _en_span(m: re.Match[str]) -> str:
            return m.group("en").strip()

        def _en_plain(m: re.Match[str]) -> str:
            return f">{m.group('en').strip()}<"

        out = _BILINGUAL_SPAN_RE.sub(_en_span, body)
        out = _BILINGUAL_PLAIN_RE.sub(_en_plain, out)
        return out

    def _repl_span(m: re.Match[str]) -> str:
        ko = m.group("ko").strip()
        en = m.group("en").strip()
        translated = catalog.get(ko)
        if translated:
            return translated
        # Try the "KO / EN" composite key as well — that's how the extractor
        # records titles and certain h1s.
        combined = f"{ko} / {en}"
        if combined in catalog:
            return catalog[combined]
        # Fallback to the EN half — better to show English than mojibake
        # next to Korean for an unsupported locale.
        return en

    def _repl_plain(m: re.Match[str]) -> str:
        ko = m.group("ko").strip()
        en = m.group("en").strip()
        translated = catalog.get(ko)
        if translated:
            return f">{translated}<"
        combined = f"{ko} / {en}"
        if combined in catalog:
            return f">{catalog[combined]}<"
        return f">{en}<"

    out = _BILINGUAL_SPAN_RE.sub(_repl_span, body)
    out = _BILINGUAL_PLAIN_RE.sub(_repl_plain, out)
    return out


def _translate_title(body: str, catalog: dict[str, str], lang: str) -> str:
    """Replace <title>KO / EN — zkCEX</title> with the localised version."""
    if lang == "ko":
        return body

    def _repl(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        if raw in catalog:
            return f"<title>{catalog[raw]}</title>"
        # Split on " / " — first half KO, second half EN.
        if " / " in raw:
            ko, _, rest = raw.partition(" / ")
            ko = ko.strip()
            # The rest may have a trailing " — zkCEX" or similar separator.
            tail = ""
            en = rest
            for sep in (" — ", " - ", " | "):
                if sep in rest:
                    en, _, tail_part = rest.partition(sep)
                    en = en.strip()
                    tail = sep + tail_part
                    break
            translated = catalog.get(ko)
            if not translated and lang == "en":
                translated = en
            if translated:
                return f"<title>{translated}{tail}</title>"
        return m.group(0)

    return _TITLE_BILINGUAL_RE.sub(_repl, body)


def _translate_data_i18n(body: str, catalog: dict[str, str], lang: str) -> str:
    """Substitute <el data-i18n="key">…</el> with the translated inner text."""

    def _repl(m: re.Match[str]) -> str:
        key = m.group("key")
        translated = catalog.get(key)
        if not translated:
            return m.group(0)  # leave as-is when missing
        # Preserve the wrapping element & attributes, replace inner HTML.
        return (
            f"<{m.group('tag')} {m.group('pre')}"
            f"data-i18n=\"{key}\"{m.group('post')}>"
            f"{html.escape(translated, quote=False)}"
            f"</{m.group('tag')}>"
        )

    return _DATA_I18N_RE.sub(_repl, body)


def _build_client_dict(catalog: dict[str, str]) -> dict[str, str]:
    """Subset of the catalog exposed to client JS via window.__ZKCEX_I18N.

    Keeping this small (a few hundred keys at most) reduces page weight.
    """
    out: dict[str, str] = {}
    # Expose the well-known short header keys.
    for k in _CLIENT_EXPOSE_KEYS:
        v = catalog.get(k)
        if v:
            out[k] = v
    # And any catalog entry whose msgid is short (<= 24 chars) and looks
    # like a UI label, since those are likely to be referenced from JS too.
    for k, v in catalog.items():
        if not v:
            continue
        if len(k) <= 24 and ("\n" not in k):
            out[k] = v
    return out


def _build_lang_picker() -> str:
    """Build the language-picker <script> with native names baked in."""
    langs = [[code, _LANG_NAMES[code]] for code in ("ko", "en", "ja", "zh", "es", "de")]
    langs_json = json.dumps(langs, ensure_ascii=False)
    # The script: builds a <select>, mounts it into the DOM at one of a few
    # well-known anchors, and reloads with ?lang= on change. Lightweight and
    # idempotent (a MutationObserver re-mounts after app.js rewrites the
    # header dynamically).
    js = (
        "(function(){"
        "var langs=" + langs_json + ";"
        "var cur=(document.documentElement&&document.documentElement.lang||'ko').toLowerCase().split('-')[0];"
        "function build(){"
        "var sel=document.createElement('select');"
        "sel.setAttribute('data-lang-picker','');"
        "sel.style.cssText='appearance:auto;background:transparent;color:inherit;border:1px solid currentColor;border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer;opacity:0.85;';"
        "langs.forEach(function(p){"
        "var o=document.createElement('option');"
        "o.value=p[0];o.textContent=p[1];"
        "if(p[0]===cur)o.selected=true;"
        "sel.appendChild(o);"
        "});"
        "sel.addEventListener('change',function(){"
        "var u=new URL(window.location.href);"
        "u.searchParams.set('lang',sel.value);"
        "window.location.href=u.toString();"
        "});"
        "return sel;"
        "}"
        "function mount(){"
        "if(document.querySelector('[data-lang-picker]'))return;"
        "var sel=build();"
        "var host=document.getElementById('zkcex-lang-mount');"
        "if(host){host.appendChild(sel);return;}"
        "var tg=document.querySelector('.lang-toggle');"
        "if(tg){tg.innerHTML='';tg.appendChild(sel);return;}"
        "var cta=document.querySelector('.app-header-cta, .header-cta');"
        "if(cta){cta.insertBefore(sel,cta.firstChild);return;}"
        "sel.style.cssText+=';position:fixed;top:8px;right:8px;z-index:9999;background:#fff;color:#0b3aff;';"
        "document.body.appendChild(sel);"
        "}"
        "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',mount);}else{mount();}"
        "var obs=new MutationObserver(function(){mount();});"
        "if(document.body){obs.observe(document.body,{childList:true,subtree:true});}"
        "})();"
    )
    return '<script id="zkcex-lang-picker">' + js + "</script>"


_LANG_PICKER_SCRIPT = _build_lang_picker()


def _inject_runtime(body: str, lang: str, catalog: dict[str, str]) -> str:
    """Inject the __ZKCEX_I18N catalog and the language-picker script."""
    client_dict = _build_client_dict(catalog)
    payload = json.dumps(client_dict, ensure_ascii=False)
    script = (
        "<script>window.__ZKCEX_I18N=" + payload + ";"
        "window.__ZKCEX_LANG=" + json.dumps(lang) + ";</script>"
    )
    # Insert just before </head>; if not found, prepend to <body>.
    if "</head>" in body:
        body = body.replace("</head>", script + "</head>", 1)
    else:
        body = script + body
    # Append the picker before </body>; degrade to end-of-doc if no </body>.
    if "</body>" in body:
        body = body.replace("</body>", _LANG_PICKER_SCRIPT + "</body>", 1)
    else:
        body = body + _LANG_PICKER_SCRIPT
    return body


def localize_html(body: str, lang: str) -> str:
    """Return the body rewritten for ``lang``.

    Safe to call with ``lang="ko"`` — the function detects that case and
    short-circuits the bilingual-span pass so the existing KO/EN markup is
    preserved unchanged. Only the language picker + the runtime catalog get
    injected for every locale (no measurable layout impact).
    """
    if not body:
        return body
    catalog = load_catalog(lang) if lang != "ko" else {}
    out = _set_html_lang(body, lang)
    out = _translate_title(out, catalog, lang)
    out = _translate_bilingual(out, catalog, lang)
    out = _translate_data_i18n(out, catalog, lang)
    out = _inject_runtime(out, lang, catalog)
    return out
