"""Tests for `server.screenshots` and the `screenshot` MCP tool.

Covers tool 5: screenshot screen / region. JPEG long edge ≤ 1280, bytes ≤ ~200KB.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server import screenshots  # noqa: E402


class TestScreenshots(unittest.TestCase):
    def setUp(self):
        self.saved: list[str] = []

    def tearDown(self):
        for p in self.saved:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _track(self, result):
        self.saved.append(result.path)
        return result

    def test_screen_capture(self):
        result = self._track(screenshots.screenshot_screen())
        self.assertTrue(os.path.exists(result.path))
        self.assertGreater(result.bytes_jpeg, 1024)
        # Long edge must be ≤ 1280
        self.assertLessEqual(max(result.width, result.height), 1280)
        # JPEG bytes must be ≤ ~250KB (the spec says ~200KB; allow margin)
        self.assertLessEqual(result.bytes_jpeg, 250_000)
        # File extension
        self.assertTrue(result.path.lower().endswith((".jpg", ".jpeg")))

    def test_region_capture(self):
        result = self._track(screenshots.screenshot_region(0, 0, 800, 600))
        self.assertTrue(os.path.exists(result.path))
        self.assertLessEqual(max(result.width, result.height), 1280)
        self.assertLessEqual(result.bytes_jpeg, 250_000)

    def test_region_invalid_size(self):
        with self.assertRaises(ValueError):
            screenshots.screenshot_region(0, 0, 0, 100)


if __name__ == "__main__":
    unittest.main()