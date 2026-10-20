"""Tests for `server.ui_automation` and tool 1, 2, 3, 4.

These tests run against whatever windows are actually open on the machine,
so they may legitimately skip individual assertions when no compatible app
is in the foreground. They must NOT crash and they MUST pass the
no-action-API scan from `tests/test_no_action_api.py`.

Hermeticity
-----------
Two tests (`test_returns_at_least_one_window`, `test_foreground_marker_present`)
were P1-flaky: they failed when no windows were open (a clean rerun on a
machine with no apps still has the explorer.exe shell window, but
hypothetically a server / kiosk with no shell can be empty). They now:

1. Skip with a clear reason when no windows are visible, AND
2. Have a paired mock-based test that proves the assertions hold against
   a synthetic `list_windows` return — see `test_list_windows_with_mock_*`.

This guarantees the suite passes regardless of desktop state.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server import ui_automation  # noqa: E402


def _fake_window_dict(hwnd=12345, pid=9999, title="Cursor", exe="Cursor.exe", fg=False):
    return {
        "title": title,
        "process_name": exe,
        "pid": pid,
        "hwnd": hwnd,
        "x": 100,
        "y": 100,
        "w": 800,
        "h": 600,
        "is_minimized": False,
        "is_foreground": fg,
    }


class _FakeControl:
    """Minimal stand-in for a uiautomation WindowControl.

    Exposes the attributes `list_windows()` reads from real controls.
    """
    def __init__(self, title="Cursor", pid=9999, hwnd=12345, exe="Cursor.exe"):
        self.Name = title
        self.ProcessId = pid
        self.NativeWindowHandle = hwnd
        self.IsMinimize = False
        self.IsOffscreen = False
        self.IsTopLevel = lambda: True
        class _Rect:
            def __init__(self):
                self.left = 100; self.top = 100
            def width(self): return 800
            def height(self): return 600
        self.BoundingRectangle = _Rect()


class TestListWindows(unittest.TestCase):
    def test_returns_at_least_one_window(self):
        ui_automation.init()
        wins = ui_automation.list_windows()
        self.assertIsInstance(wins, list)
        if not wins:
            self.skipTest("no windows visible (desktop has no top-level windows)")
        self.assertGreater(len(wins), 0, "expected at least one window")
        for w in wins:
            for k in ("title", "process_name", "pid", "hwnd", "x", "y", "w", "h"):
                self.assertIn(k, w)
            self.assertIsInstance(w["pid"], int)
            self.assertIsInstance(w["hwnd"], int)

    def test_foreground_marker_present(self):
        wins = ui_automation.list_windows()
        if not wins:
            self.skipTest("no windows visible (desktop has no top-level windows)")
        fg = [w for w in wins if w["is_foreground"]]
        self.assertGreaterEqual(len(fg), 1)

    # --- hermetic mock-based variants (desktop-state-free) -------------------

    def test_list_windows_with_mock_when_empty(self):
        """When the desktop has no windows, list_windows() returns an empty
        list — does NOT raise. The assertions in the original test are
        trivially true on empty; we still assert no crash.

        Source of truth is now raw Win32 EnumWindows, so we patch that.
        """
        with patch.object(ui_automation, "_enum_visible_top_level_hwnds", return_value=[]):
            wins = ui_automation.list_windows()
            self.assertEqual(wins, [])

    def test_list_windows_with_mock_one_foreground(self):
        """With a fake desktop (one window, one foreground), the assertions
        from `test_returns_at_least_one_window` and
        `test_foreground_marker_present` both hold without touching the
        real desktop."""
        from tests.test_ui_automation import _FakeControl
        fake_ctrl = _FakeControl(title="Cursor", hwnd=12345, exe="Cursor.exe")
        # Need _is_foreground(hwnd) to return True for our fake hwnd.
        with patch.object(ui_automation, "_enum_visible_top_level_hwnds", return_value=[12345]), \
             patch.object(ui_automation, "_hwnd_to_uia_window", return_value=fake_ctrl), \
             patch.object(ui_automation, "_process_exe_path", return_value=r"C:\fake\Cursor.exe"), \
             patch.object(ui_automation, "_is_foreground", return_value=True):
            wins = ui_automation.list_windows()
            self.assertEqual(len(wins), 1)
            for w in wins:
                for k in ("title", "process_name", "pid", "hwnd", "x", "y", "w", "h"):
                    self.assertIn(k, w)
                self.assertIsInstance(w["pid"], int)
                self.assertIsInstance(w["hwnd"], int)
            fg = [w for w in wins if w["is_foreground"]]
            self.assertEqual(len(fg), 1)

    def test_list_windows_includes_minimized_office_windows(self):
        """Regression: Word / Excel / PowerPoint parked at (-32000, -32000)
        must NOT be filtered out. The original UIA-based filter dropped them
        because UIA caches their rect as IsOffscreen=True (see test header
        in `ui_automation.py`). The fix uses Win32 EnumWindows +
        IsWindowVisible, so a hypothetical minimized Office hwnd -1 must
        appear in list_windows output."""
        from tests.test_ui_automation import _FakeControl

        word = _FakeControl(title="zhezhi.docx - Word", hwnd=0x100, pid=1)
        excel = _FakeControl(title="lịch thi.xlsx - Excel", hwnd=0x200, pid=2)
        ppt = _FakeControl(title="nhom14.pptx - PowerPoint", hwnd=0x300, pid=3)
        # A fake hwnd cast that matches NativeWindowHandle.
        word.NativeWindowHandle = 0x100
        excel.NativeWindowHandle = 0x200
        ppt.NativeWindowHandle = 0x300

        all_ctrls = [word, excel, ppt]

        def _wrap(hwnd):
            for c in all_ctrls:
                if c.NativeWindowHandle == hwnd:
                    return c
            return None

        with patch.object(ui_automation, "_enum_visible_top_level_hwnds",
                          return_value=[0x100, 0x200, 0x300]), \
             patch.object(ui_automation, "_hwnd_to_uia_window", side_effect=_wrap), \
             patch.object(ui_automation, "_process_exe_path",
                          side_effect=lambda pid: {1: r"C:\fake\WINWORD.EXE",
                                                   2: r"C:\fake\EXCEL.EXE",
                                                   3: r"C:\fake\POWERPNT.EXE"}[pid]), \
             patch.object(ui_automation, "_is_foreground", return_value=False):
            wins = ui_automation.list_windows()
            titles = {w["title"] for w in wins}
            self.assertIn("zhezhi.docx - Word", titles)
            self.assertIn("lịch thi.xlsx - Excel", titles)
            self.assertIn("nhom14.pptx - PowerPoint", titles)
            procs = {w["process_name"] for w in wins}
            self.assertIn("WINWORD.EXE", procs)
            self.assertIn("EXCEL.EXE", procs)
            self.assertIn("POWERPNT.EXE", procs)


class TestForeground(unittest.TestCase):
    def test_foreground_has_exe_path(self):
        ui_automation.init()
        try:
            fg = ui_automation.get_foreground_window()
        except Exception as exc:
            self.skipTest(f"no foreground window available: {exc!r}")
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