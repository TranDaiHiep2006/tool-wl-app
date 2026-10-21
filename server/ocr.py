"""OCR wrapper around Windows.Media.Ocr (offline).

OCR must run on a multi-threaded apartment (MTA) thread because
WinRT async calls (`xxx_async().get()`) raise 'Cannot call blocking
method from single-threaded apartment' on STA. We isolate OCR work in
a worker thread.

P5 — Vietnamese OCR
-------------------
Windows.Media.Ocr can only recognize text in a language whose OCR
*language pack* is installed at the OS level
(Settings → Time & language → Language & region). When a `vi-VN` pack
is present we prefer it, because the en-US recognizer scores Vietnamese
diacritics poorly and typically drops them entirely, turning
"sương mù" into something close to "suong mu".

When vi-VN is NOT installed we fall back to the previous behaviour
(user-profile languages, then en-US) and say so — the caller must
never be left guessing which recognizer produced a result, because a
Vietnamese OCR result with dropped diacritics looks authoritative but
is wrong.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Optional, TypedDict

from PIL import Image

from server.textnorm import nfc, nfc_fold

Rect = TypedDict("Rect", {"x": float, "y": float, "w": float, "h": float})
Word = TypedDict("Word", {"text": str, "x": float, "y": float, "w": float, "h": float})

# P5: preferred Vietnamese language tag. Checked before the profile
# languages so a machine that has BOTH vi-VN and en-US packs installed
# gets the recognizer that can actually read Vietnamese.
PREFERRED_LANGUAGE = "vi-VN"

_MTA_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wl-ocr-mta")


def _run_on_mta(fn, *args, **kwargs):
    """Submit `fn(*args, **kwargs)` to the MTA worker thread and wait."""
    fut: Future = _MTA_EXECUTOR.submit(fn, *args, **kwargs)
    return fut.result(timeout=60)


# ---------- internal helpers (must be invoked from an MTA thread) ----------


def _available_language_tags() -> List[str]:
    """Language tags with an installed OCR pack (may be empty).

    Never raises: a failure to enumerate simply means we fall back to the
    profile-language engine.
    """
    try:
        from winrt.windows.media.ocr import OcrEngine

        return [L.language_tag for L in OcrEngine.available_recognizer_languages]
    except Exception:
        return []


def _ocr_engine():
    """Build the best OCR engine available, preferring Vietnamese.

    Order of preference:
      1. `vi-VN`, when that language pack is installed.
      2. The user's profile languages (unchanged P1-P4 behaviour).
      3. `en-US`, when the profile yields no engine.

    Returns ``(engine, language_tag)``. The tag is what every public
    function reports back so the agent can tell the user which
    recognizer ran.
    """
    from winrt.windows.media.ocr import OcrEngine

    def _make(tag: str):
        try:
            from winrt.windows.globalization import Language

            return OcrEngine.try_create_from_language(Language(tag))
        except Exception:
            return None

    # 1. Vietnamese, only if the pack is actually installed.
    if PREFERRED_LANGUAGE in _available_language_tags():
        eng = _make(PREFERRED_LANGUAGE)
        if eng is not None:
            return eng, PREFERRED_LANGUAGE

    # 2. Profile languages — unchanged from P1-P4.
    try:
        eng = OcrEngine.try_create_from_user_profile_languages()
        tag = getattr(
            getattr(eng, "recognizer_language", None), "language_tag", None
        )
        if eng is not None:
            return eng, tag or "unknown"
    except Exception:
        eng = None

    # 3. Last-resort en-US.
    eng = _make("en-US")
    if eng is not None:
        return eng, "en-US"

    raise RuntimeError(
        "Windows.Media.Ocr engine not available. "
        "Install an OCR language pack (Settings → Time & language → Language), "
        f"or install the '{PREFERRED_LANGUAGE}' pack to read Vietnamese text."
    )


def _to_software_bitmap(img: Image.Image):
    import tempfile
    import os
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage import StorageFile

    fd, tmpname = tempfile.mkstemp(prefix="wl_ocr_", suffix=".png")
    os.close(fd)
    try:
        img.save(tmpname, format="PNG")
        f = StorageFile.get_file_from_path_async(tmpname).get()
        s = f.open_read_async().get()
        decoder = BitmapDecoder.create_async(s).get()
        return decoder.get_software_bitmap_async().get()
    finally:
        try:
            os.unlink(tmpname)
        except OSError:
            pass


def _words_from_lines(result) -> List[Word]:
    out: List[Word] = []
    for line in result.lines:
        for w in line.words:
            r = w.bounding_rect
            out.append(
                {
                    "text": w.text,
                    "x": float(r.x),
                    "y": float(r.y),
                    "w": float(r.width),
                    "h": float(r.height),
                }
            )
    return out


def _ocr_pil_mta(img: Image.Image, max_lines: int) -> dict:
    engine, language = _ocr_engine()
    bitmap = _to_software_bitmap(img)
    try:
        result = engine.recognize_async(bitmap).get()
    finally:
        try:
            bitmap.close()
        except Exception:
            pass
    words = _words_from_lines(result)
    truncated = len(words) > max_lines
    if truncated:
        words = words[:max_lines]
    return {
        "lines": words,
        "truncated": truncated,
        "max_lines": max_lines,
        # P5: which recognizer actually produced these words. Without this
        # a Vietnamese result with silently-dropped diacritics is
        # indistinguishable from a correct one.
        "language": language,
    }


# ---------- public API -----------------------------------------------------


def ocr_pil(img: Image.Image, max_lines: int = 200) -> dict:
    """Run OCR on a PIL Image. May run on any thread (delegates to MTA)."""
    return _run_on_mta(_ocr_pil_mta, img, max_lines)


def ocr_region(x: int, y: int, w: int, h: int, max_lines: int = 200) -> dict:
    """OCR a screen region."""
    import mss

    if w <= 0 or h <= 0:
        raise ValueError("region width/height must be > 0")
    with mss.MSS() as sct:
        shot = sct.grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    result = ocr_pil(img, max_lines=max_lines)
    result["region"] = {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
    return result


def ocr_file(file_path: str, max_lines: int = 200) -> dict:
    """OCR an image file."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    img = Image.open(file_path)
    img.load()
    result = ocr_pil(img, max_lines=max_lines)
    result["file_path"] = file_path
    return result


def find_text_on_screen(
    query: str,
    case_insensitive: bool = True,
    max_lines: int = 400,
) -> dict:
    """OCR the whole screen, then search for `query`. Returns matched words with rects."""
    import mss

    if not query:
        return {
            "query": query, "matches": [], "count": 0, "scanned_words": 0,
            "language": None,
        }

    with mss.MSS() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.rgb)

    # Full OCR on MTA thread
    raw = _run_on_mta(_ocr_pil_mta, img, max_lines)
    words: List[Word] = raw["lines"]

    # P5: Vietnamese-safe query matching — NFC + casefold on both sides, so
    # searching for "sương mù" matches OCR output that came back decomposed.
    if case_insensitive:
        hay = nfc_fold(query)
    else:
        hay = nfc(query)
    matches: List[Word] = []
    for w in words:
        target = nfc_fold(w["text"]) if case_insensitive else nfc(w["text"])
        if hay in target:
            matches.append(w)

    return {
        "query": query,
        "matches": matches,
        "count": len(matches),
        "scanned_words": len(words),
        "truncated_scan": raw["truncated"],
        "language": raw.get("language"),
    }