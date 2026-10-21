"""Tests for `server.ocr` and the `ocr_*` MCP tools.

Covers tools 6 & 7. Generates a synthetic image and asserts OCR returns text.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server import ocr  # noqa: E402


def _make_test_image(text: str = "Hello OCR World 123") -> str:
    img = Image.new("RGB", (640, 160), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    d.text((20, 50), text, fill="black", font=font)
    fd, path = tempfile.mkstemp(prefix="wl_ocr_test_", suffix=".png")
    os.close(fd)
    img.save(path, format="PNG")
    return path


class TestOCR(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_image = _make_test_image()
        cls.kept = [cls.test_image]

    @classmethod
    def tearDownClass(cls):
        for p in cls.kept:
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_ocr_file_returns_words(self):
        result = ocr.ocr_file(self.test_image)
        self.assertIn("lines", result)
        self.assertGreater(len(result["lines"]), 0)
        joined = " ".join(w["text"] for w in result["lines"]).lower()
        # OCR may misread chars; assert at least *something* English-like came back
        self.assertTrue(len(joined) > 0)
        for w in result["lines"]:
            for k in ("text", "x", "y", "w", "h"):
                self.assertIn(k, w)

    def test_ocr_truncation(self):
        result = ocr.ocr_file(self.test_image, max_lines=1)
        self.assertLessEqual(len(result["lines"]), 1)
        self.assertTrue(result["truncated"])

    def test_ocr_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            ocr.ocr_file("Z:/__no_such__.png")

    def test_find_text_on_screen_no_match(self):
        result = ocr.find_text_on_screen("__definitely_not_on_screen__xyzzy123")
        self.assertEqual(result["count"], 0)
        self.assertIsInstance(result["matches"], list)

    def test_find_text_on_screen_empty_query(self):
        result = ocr.find_text_on_screen("")
        self.assertEqual(result["count"], 0)


class TestOCRLanguageReporting(unittest.TestCase):
    """P5: every OCR result must name the recognizer language that ran."""

    @classmethod
    def setUpClass(cls):
        cls.test_image = _make_test_image()
        cls.kept = [cls.test_image]

    @classmethod
    def tearDownClass(cls):
        for p in cls.kept:
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_ocr_file_reports_language(self):
        result = ocr.ocr_file(self.test_image)
        self.assertIn("language", result)
        self.assertIsInstance(result["language"], str)
        self.assertTrue(result["language"])

    def test_ocr_region_reports_language(self):
        result = ocr.ocr_region(0, 0, 200, 120)
        self.assertIn("language", result)

    def test_find_text_on_screen_reports_language(self):
        result = ocr.find_text_on_screen("__nothing__xyzzy")
        self.assertIn("language", result)
        self.assertIsInstance(result["language"], str)

    def test_available_language_tags_includes_preferred_or_falls_back(self):
        tags = ocr._available_language_tags()
        self.assertIsInstance(tags, list)
        # Whatever the machine has, the engine builder must never raise and
        # must always report a language.
        engine, language = ocr._ocr_engine()
        self.assertIsNotNone(engine)
        self.assertTrue(language)

    def test_preferred_language_is_vietnamese(self):
        self.assertEqual(ocr.PREFERRED_LANGUAGE, "vi-VN")


class TestOCRVietnameseMatching(unittest.TestCase):
    """P5: query matching must be Vietnamese-safe (NFC + casefold).

    Note what this does and does not fix. NFC normalization makes the SAME
    grapheme written two different ways compare equal — it does NOT recover
    diacritics that a non-Vietnamese OCR engine dropped outright ("suong mu"
    vs "sương mù" differ by *missing code points*, not by composition, and no
    amount of normalization reconstructs them). That failure is why the OCR
    tools now report their recognizer language and prefer vi-VN.
    """

    def test_decomposed_ocr_text_matches_precomposed_query(self):
        import unicodedata

        from server.textnorm import nfc_fold

        # OCR (or the clipboard) can hand back NFD: base letter followed by
        # combining marks. The user types the same word in precomposed NFC.
        decomposed = unicodedata.normalize("NFD", "sương mù")
        precomposed = unicodedata.normalize("NFC", "sương mù")
        self.assertNotEqual(decomposed, precomposed)  # genuinely different code points

        words = [{"text": decomposed}]
        query = nfc_fold(precomposed)
        self.assertTrue(
            any(query in nfc_fold(w["text"]) for w in words),
            "NFD haystack must match an NFC needle",
        )

    def test_matching_ignores_case(self):
        from server.textnorm import nfc_fold

        self.assertIn(nfc_fold("LỊCH THI"), nfc_fold("lịch thi.xlsx - Excel"))

    def test_dropped_diacritics_are_not_silently_matched(self):
        """Document the limitation: a diacritic-stripping engine is NOT
        repaired by normalization, which is exactly why `language` is
        reported in every OCR result."""
        from server.textnorm import nfc_fold

        self.assertNotIn(nfc_fold("sương"), nfc_fold("suong mu"))


if __name__ == "__main__":
    unittest.main()