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
        # The clipboard may contain arbitrary formats depending on what
        # apps have run on this machine. We accept any reasonable type
        # string — the test only proves read_clipboard() never crashes
        # and always returns a dict with a 'type' key.
        self.assertIsInstance(result["type"], str)
        self.assertTrue(len(result["type"]) > 0)
        # If the type is text, it must include the actual text payload.
        if result["type"] == "text":
            self.assertIn("text", result)
            self.assertIsInstance(result["text"], str)


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