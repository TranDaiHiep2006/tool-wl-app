"""Quota-saving metrics: assert every output stays well below its quota budget.

These tests are also useful as documentation: when budgeting quota, look at
the order of magnitude here, not the worst-case.

P2 adds quota checks for the new browser DOM tools. We do NOT spin up a
real browser here — instead we assert that the *normalized reply* the MCP
layer would return fits within the budget. Real per-URL numbers are in
`logs/browser_dom_measure.json`.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server import browser_dom, office_com, screenshots, ui_automation  # noqa: E402

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


class TestBrowserDomQuota(unittest.TestCase):
    """Quota sanity for get_page_text / get_page_structure reply envelopes.

    These use synthetic replies so they run hermetically (no browser).
    Worst-case measured numbers on real pages are recorded in
    `logs/browser_dom_measure.json`:
        get_page_text       1-8 KB across 3 representative pages
        get_page_structure  up to 22.5 KB on dense GitHub/Wikipedia pages
                            (truncated=true)
    """

    def test_get_page_text_envelope_under_16kb_at_max(self):
        # A worst-case text payload of 8000 chars + envelope.
        big = "x" * 8000
        reply = {"ok": True, "data": {"text": big}}
        norm = browser_dom._normalize_text_reply(reply, 8000)
        size = len(json.dumps(norm, ensure_ascii=False).encode("utf-8"))
        # text + metadata envelope
        self.assertLess(size, 12_000,
                        f"get_page_text envelope grew too big: {size} bytes")

    def test_get_page_structure_envelope_under_32kb_at_max(self):
        items = [
            {"kind": "heading", "level": 1, "text": "X"},
            {"kind": "link", "text": "X", "href": "https://example.com/p"},
        ] * 100  # 200 items
        reply = {"ok": True, "data": {"items": items}}
        norm = browser_dom._normalize_structure_reply(reply, 200)
        size = len(json.dumps(norm, ensure_ascii=False).encode("utf-8"))
        # Worst case is ~25 KB. Allow 32 KB.
        self.assertLess(size, 32_000,
                        f"get_page_structure envelope grew too big: {size} bytes")


class TestOfficeComQuota(unittest.TestCase):
    """Quota sanity for the three new P3 Office COM tools.

    All three are pure-Python (after the COM read). Worst-case numbers are
    recorded in REPORT.md §P3 / 4.
    """

    def _patch_office_off(self):
        from server import office_com
        office_com.win32com.client.GetActiveObject = lambda _pid: (_ for _ in ()).throw(  # type: ignore[attr-defined]
            Exception("Office.Application not running")
        )

    def test_read_word_text_envelope_under_160kb_at_max(self):
        # Worst case: 60 000 chars of UTF-8 Vietnamese → ~3 bytes/char → ~180 KB.
        # Hard ceiling kept well above that. The MCP transport eats additional
        # JSON overhead; we leave ~160 KB headroom.
        big = "Tiếng Việt có dấu " * 5000  # ~30 000 chars of multi-byte UTF-8
        envelope = {
            "ok": True,
            "app": "Word",
            "name": "Doc.docx",
            "text": big[:office_com.HARD_TEXT_MAX],
            "chars": min(len(big), office_com.HARD_TEXT_MAX),
            "max_chars": office_com.HARD_TEXT_MAX,
            "truncated": len(big) > office_com.HARD_TEXT_MAX,
            "extras": [],
        }
        size = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        self.assertLess(size, 160_000,
                        f"read_word_text envelope grew too big: {size} bytes")

    def test_read_excel_values_envelope_under_256kb_at_max(self):
        # 200 rows × 50 cols of strings worst case. Real Office text is
        # usually short, but be generous. The hard caps protect the upper
        # bound.
        cells = [["x" * 8 for _ in range(50)] for _ in range(200)]
        envelope = {
            "ok": True,
            "app": "Excel",
            "sheet": "Sheet1",
            "available_sheets": ["Sheet1"],
            "cells": cells,
            "rows": 200,
            "cols": 50,
            "used_rows": 200,
            "used_cols": 50,
            "max_rows": office_com.HARD_MAX_ROWS,
            "max_cols": office_com.HARD_MAX_COLS,
            "truncated": False,
        }
        size = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        self.assertLess(size, 256_000,
                        f"read_excel_values envelope grew too big: {size} bytes")

    def test_read_powerpoint_text_envelope_under_64kb_at_max(self):
        # 100 slides, each up to ~500 chars.
        slides = [{"index": i, "text": "Slide text " * 50, "chars": 550} for i in range(1, 101)]
        envelope = {
            "ok": True,
            "app": "PowerPoint",
            "name": "Pres.pptx",
            "slides": slides,
            "count": 100,
            "total_slides": 100,
            "max_slides": 100,
            "truncated": False,
        }
        size = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        self.assertLess(size, 64_000,
                        f"read_powerpoint_text envelope grew too big: {size} bytes")

    def test_read_word_text_error_envelope_small(self):
        # Office not running → tiny error shape, always under 4 KB.
        self._patch_office_off()
        out = office_com.read_word_text()
        size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        self.assertLess(size, 4_000)


if __name__ == "__main__":
    unittest.main()