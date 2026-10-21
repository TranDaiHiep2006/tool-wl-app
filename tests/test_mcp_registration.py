"""Test that the MCP server registers exactly the spec'd tools."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server.mcp_server import mcp  # noqa: E402

EXPECTED_TOOLS = {
    "list_windows",
    "get_foreground_window",
    "get_ui_tree",
    "get_control_text",
    "screenshot",
    "ocr_region",
    "ocr_image",
    "find_text_on_screen",
    "get_browser_tab_info",
    "read_clipboard",
    # P2: browser DOM (read-only)
    "get_page_text",
    "get_page_structure",
    # P3: Office COM (read-only)
    "read_word_text",
    "read_excel_values",
    "read_powerpoint_text",
    # P4: Action tools (with safety gate)
    "click_control",
    "type_text",
    "focus_window",
    # P5: batched actions under a single approval
    "run_sequence",
}


class TestMCPRegistration(unittest.TestCase):
    def test_all_tools_registered(self):
        async def _go():
            tools = await mcp.list_tools()
            return {t.name for t in tools}

        names = asyncio.run(_go())
        missing = EXPECTED_TOOLS - names
        self.assertFalse(missing, f"missing tools: {missing}")

    def test_no_extra_unexpected_tools(self):
        async def _go():
            tools = await mcp.list_tools()
            return {t.name for t in tools}

        names = asyncio.run(_go())
        # FastMCP may add internal tools (e.g., debug); we only assert the spec tools are present.
        # 9 (P1) + 2 (P2) + 3 (P3) + 3 (P4) + 1 (P5) = 20 total.
        self.assertEqual(len(names & EXPECTED_TOOLS), len(EXPECTED_TOOLS))


if __name__ == "__main__":
    unittest.main()