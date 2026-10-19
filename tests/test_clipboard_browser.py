"""Tests for `server.clipboard` (tool 9) and `server.browser` (tool 8)."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server import browser, clipboard  # noqa: E402


class TestClipboard(unittest.TestCase):
    def test_returns_dict_with_type(self):
        result = clipboard.read_clipboard()
        self.assertIsInstance(result, dict)
        self.assertIn("type", result)
        self.assertIn(result["type"], {"text", "empty", "bitmap", "rtf",
                                       "csv", "html_format", "dib"} | set(result.get("format_names", [])))


class TestBrowserTabInfo(unittest.TestCase):
    def test_returns_dict_no_crash(self):
        result = browser.get_browser_tab_info()
        self.assertIsInstance(result, dict)
        self.assertIn("supported", result)
        if result["supported"]:
            self.assertIn("browser", result)
            self.assertIn("title", result)
            self.assertIn("url", result)


if __name__ == "__main__":
    unittest.main()