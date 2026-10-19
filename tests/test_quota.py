"""Quota-saving metrics: assert every output stays well below its quota budget.

These tests are also useful as documentation: when budgeting quota, look at
the order of magnitude here, not the worst-case.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server import screenshots, ui_automation  # noqa: E402

ui_automation.init()


class TestQuota(unittest.TestCase):
    def test_list_windows_within_quota(self):
        wins = ui_automation.list_windows()
        size = len(json.dumps(wins, ensure_ascii=False).encode("utf-8"))
        # 5 windows ≈ 1 KB; 50 windows max under UIA filter ≈ 30 KB.
        self.assertLess(size, 64_000)

    def test_get_ui_tree_within_quota(self):
        wins = ui_automation.list_windows()
        if not wins:
            self.skipTest("no windows")
        # Foreground first
        tree = ui_automation.get_ui_tree(str(wins[0]["hwnd"]))
        size = len(json.dumps(tree, ensure_ascii=False).encode("utf-8"))
        # Even dense UIA trees rarely exceed 30 KB when pruned.
        self.assertLess(size, 64_000)

    def test_screenshot_full_screen_within_quota(self):
        ss = screenshots.screenshot_screen()
        self.assertLessEqual(ss.bytes_jpeg, 250_000)


if __name__ == "__main__":
    unittest.main()