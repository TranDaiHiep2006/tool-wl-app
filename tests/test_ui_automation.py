"""Tests for `server.ui_automation` and tool 1, 2, 3, 4.

These tests run against whatever windows are actually open on the machine,
so they may legitimately skip individual assertions when no compatible app
is in the foreground. They must NOT crash and they MUST pass the
no-action-API scan from `tests/test_no_action_api.py`.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server import ui_automation  # noqa: E402


class TestListWindows(unittest.TestCase):
    def test_returns_at_least_one_window(self):
        ui_automation.init()
        wins = ui_automation.list_windows()
        self.assertIsInstance(wins, list)
        self.assertGreater(len(wins), 0, "expected at least one window")
        for w in wins:
            for k in ("title", "process_name", "pid", "hwnd", "x", "y", "w", "h"):
                self.assertIn(k, w)
            self.assertIsInstance(w["pid"], int)
            self.assertIsInstance(w["hwnd"], int)

    def test_foreground_marker_present(self):
        wins = ui_automation.list_windows()
        fg = [w for w in wins if w["is_foreground"]]
        self.assertGreaterEqual(len(fg), 1)


class TestForeground(unittest.TestCase):
    def test_foreground_has_exe_path(self):
        ui_automation.init()
        fg = ui_automation.get_foreground_window()
        self.assertIn("title", fg)
        self.assertIn("exe_path", fg)
        self.assertTrue(fg["is_foreground"])


class TestUITree(unittest.TestCase):
    def test_tree_on_first_open_window(self):
        wins = ui_automation.list_windows()
        if not wins:
            self.skipTest("no windows")
        target = wins[0]
        tree = ui_automation.get_ui_tree(str(target["hwnd"]), max_depth=3, max_nodes=40)
        self.assertIn("nodes", tree)
        self.assertGreater(len(tree["nodes"]), 0)
        for n in tree["nodes"]:
            self.assertIn("control_type", n)
            self.assertIn("name", n)

    def test_tree_unknown_window_returns_error(self):
        from server.mcp_server import get_ui_tree
        # MCP tool functions return dicts; should raise for unknown
        with self.assertRaises(Exception):
            get_ui_tree.fn("__definitely_does_not_exist__")


class TestControlText(unittest.TestCase):
    def test_returns_clear_error_for_missing_id(self):
        wins = ui_automation.list_windows()
        if not wins:
            self.skipTest("no windows")
        result = ui_automation.get_control_text(str(wins[0]["hwnd"]), "__no_such_control__")
        self.assertFalse(result["found"])
        self.assertIn("reason", result)


if __name__ == "__main__":
    unittest.main()