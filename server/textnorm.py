"""P5 — Vietnamese-safe text matching helpers.

Vietnamese composed text has several Unicode representations of what a
user perceives as "the same string". The two that actually bite us:

  * **NFD (decomposed)** — Windows APIs, the clipboard and some app
    frameworks hand back text where every vowel and several consonants
    are followed by a combining diacritic (U+0300..U+036F). The typed
    Vietnamese Telex/Unikey sequence "o" + "a" + combining horn +
    combining tilde is a 4-code-point string that *looks* identical to
    the 2-code-point precomposed "ở" in "kín lối cũ".
  * **Precomposed (NFC)** — a single code point per grapheme.

Comparing the raw code points of `"lịch thi.xlsx - Excel"` against
either form of the same title silently fails. Every user-visible string
that participates in matching therefore goes through :func:`nfc_fold`
first, on BOTH sides, so the comparison is done in a single canonical
form.

Case folding uses :meth:`str.casefold` rather than :meth:`str.lower`:
``casefold`` is the Unicode-correct full case mapping (it handles ß → ss,
and — relevant for Vietnamese — the Turkish/Vietnamese dotted-I cases
consistently), whereas ``lower`` is locale-independent but incomplete.

This module is deliberately dependency-free and side-effect-free so both
the read-only path (``ui_automation.find_window``) and the action path
(``actions._resolve_control``) can import it without creating a
dependency on the action module.
"""

from __future__ import annotations

import unicodedata
from typing import Optional

__all__ = [
    "nfc",
    "nfc_fold",
    "same_text",
    "contains_text",
]


def nfc(text: Optional[str]) -> str:
    """Normalize `text` to Unicode NFC (canonical composition).

    Returns "" for None so callers can fold optional values without a
    None check at every call site.
    """
    if not text:
        return ""
    # Quick check first: str.isascii() is a fast path for the overwhelmingly
    # common case of a plain-ASCII title like "Untitled - Notepad".
    if text.isascii():
        return text
    return unicodedata.normalize("NFC", text)


def nfc_fold(text: Optional[str]) -> str:
    """Return the canonical, case-insensitive key for `text`.

    Both sides of any comparison should be passed through this function::

        if nfc_fold(needle) in nfc_fold(haystack):
            ...

    NFC is applied *before* casefolding. The order matters: casefold can
    itself alter string length (ß → ss) and, more importantly, applying
    them in the wrong order can leave decomposed sequences intact on one
    side and composed on the other.
    """
    if not text:
        return ""
    if text.isascii():
        # ASCII is already NFC; casefold == lower there, and is faster.
        return text.lower()
    return unicodedata.normalize("NFC", text).casefold()


def same_text(a: Optional[str], b: Optional[str]) -> bool:
    """True when `a` and `b` are the same string, ignoring case and
    Unicode composition (NFD vs NFC)."""
    return nfc_fold(a) == nfc_fold(b)


def contains_text(haystack: Optional[str], needle: Optional[str]) -> bool:
    """True when `needle` occurs inside `haystack`, ignoring case and
    Unicode composition. An empty `needle` never matches."""
    key_hay = nfc_fold(haystack)
    key_needle = nfc_fold(needle)
    if not key_needle:
        return False
    return key_needle in key_hay