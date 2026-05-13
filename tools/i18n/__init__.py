"""zkCEX gettext-style i18n pipeline.

Submodules:
- ``extract``: walks the homepage HTML/JS tree and produces a ``messages.pot``
  template plus per-language ``.po`` files.
- ``locale``:  Accept-Language / cookie / query-param negotiation.
- ``loader``:  small custom ``.po`` reader returning ``{msgid: msgstr}`` dicts.
- ``postprocess``: HTML rewriter that swaps bilingual ``KO / EN`` spans for the
  negotiated locale and substitutes ``data-i18n="key"`` placeholders.

Stdlib only — no babel / no gettext / no polib. The .po format is parsed by a
hand-rolled reader so the runtime cost stays well under a millisecond per
request after the first cache fill.
"""

SUPPORTED = ["ko", "en", "ja", "zh", "es", "de"]

LANGUAGE_NAMES = {
    "ko": "한국어",
    "en": "English",
    "ja": "日本語",
    "zh": "中文",
    "es": "Español",
    "de": "Deutsch",
}
