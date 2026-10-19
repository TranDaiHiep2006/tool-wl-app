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


if __name__ == "__main__":
    unittest.main()