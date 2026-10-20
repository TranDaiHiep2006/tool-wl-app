"""tool-wl-app MCP server entry point.

P1 — READ-ONLY "eyes" for Cursor Agent on Windows.
9 tools, stdio transport via FastMCP.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

import uiautomation as uia

# Ensure relative imports work when launched as a script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import browser as browser_mod  # noqa: E402
from server import browser_dom as browser_dom_mod  # noqa: E402
from server import clipboard as clip_mod  # noqa: E402
from server import ocr as ocr_mod  # noqa: E402
from server import office_com as office_mod  # noqa: E402
from server import screenshots as ss_mod  # noqa: E402
from server import ui_automation as ui_mod  # noqa: E402

from fastmcp import FastMCP  # noqa: E402


mcp = FastMCP("tool-wl-app")


def _safe(fn, *args, **kwargs):
    """Run a tool function and return either the result or a structured error."""
    try:
        return {"ok": True, "data": fn(*args, **kwargs)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


# ---------------------- tool 1: list_windows ------------------------------

@mcp.tool(
    name="list_windows",
    description=(
        "List visible, named top-level windows. Returns title, pid, hwnd, bounds, "
        "minimized/foreground flags.\n"
        "When to use: very first call in a session to see what apps are open. "
        "When NOT to use: when you already know the target window — use get_ui_tree on it."
    ),
)
def list_windows() -> dict:
    ui_mod.init()
    return ui_mod.list_windows()


# ---------------------- tool 2: get_foreground_window ---------------------

@mcp.tool(
    name="get_foreground_window",
    description=(
        "Return the foreground window with full executable path.\n"
        "When to use: quick check of which app currently owns the screen. "
        "When NOT to use: when you need a list of all apps — use list_windows."
    ),
)
def get_foreground_window() -> dict:
    ui_mod.init()
    return ui_mod.get_foreground_window()


# ---------------------- tool 3: get_ui_tree -------------------------------

@mcp.tool(
    name="get_ui_tree",
    description=(
        "Return a pruned UIA tree of a window's visible, named controls.\n"
        "When to use: to discover buttons/menus/inputs in Word/Excel/Notepad/browser "
        "before any action.\n"
        "When NOT to use: for visual-only info or dynamic canvas content — use screenshot."
    ),
)
def get_ui_tree(
    window: str,
    max_depth: int = 6,
    max_nodes: int = 300,
) -> dict:
    ui_mod.init()
    return ui_mod.get_ui_tree(window, max_depth=max_depth, max_nodes=max_nodes)


# ---------------------- tool 4: get_control_text --------------------------

@mcp.tool(
    name="get_control_text",
    description=(
        "Read the text of a specific control in a window by automation_id or exact Name.\n"
        "When to use: when you already know the control id (from get_ui_tree) and only "
        "need its current value.\n"
        "When NOT to use: for unknown controls — use get_ui_tree first."
    ),
)
def get_control_text(window: str, automation_id_or_name: str) -> dict:
    ui_mod.init()
    return ui_mod.get_control_text(window, automation_id_or_name)


# ---------------------- tool 5: screenshot --------------------------------

@mcp.tool(
    name="screenshot",
    description=(
        "Capture screen / window / region as a JPEG file. "
        "Use only when get_ui_tree is insufficient; never screenshot in a loop.\n"
        "Returns the saved file path plus width/height/size info.\n"
        "When to use: confirm a visual state the UI tree can't describe.\n"
        "When NOT to use: as the first call — always try list_windows / get_ui_tree first."
    ),
)
def screenshot(
    mode: str,
    window: Optional[str] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    w: Optional[int] = None,
    h: Optional[int] = None,
) -> dict:
    ui_mod.init()
    mode = (mode or "").lower()
    if mode == "screen":
        result = ss_mod.screenshot_screen()
    elif mode == "region":
        if None in (x, y, w, h):
            raise ValueError("region mode requires x, y, w, h")
        result = ss_mod.screenshot_region(int(x), int(y), int(w), int(h))
    elif mode == "window":
        if not window:
            raise ValueError("window mode requires `window` (hwnd or title)")
        win = ui_mod.find_window(window)
        if not win:
            raise ValueError(f"window not found: {window!r}")
        rect = win.BoundingRectangle
        result = ss_mod.screenshot_region(rect.x, rect.y, rect.width, rect.height)
        result.path = result.path  # type: ignore[attr-defined]
    else:
        raise ValueError(f"unknown mode: {mode!r} (expected screen|window|region)")
    return {
        "path": result.path,
        "width": result.width,
        "height": result.height,
        "bytes_jpeg": result.bytes_jpeg,
    }


# ---------------------- tool 6: ocr_region / ocr_image --------------------

@mcp.tool(
    name="ocr_region",
    description=(
        "Run offline OCR on a screen region. Returns [{text, x, y, w, h, confidence proxy}].\n"
        "When to use: when a UI tree is unavailable (canvas, image-only, remote desktop) and "
        "you need to read text.\n"
        "When NOT to use: for normal UIA apps — get_ui_tree is cheaper."
    ),
)
def ocr_region(
    x: int,
    y: int,
    w: int,
    h: int,
    max_lines: int = 200,
) -> dict:
    return ocr_mod.ocr_region(x, y, w, h, max_lines=max_lines)


@mcp.tool(
    name="ocr_image",
    description=(
        "Run offline OCR on an image file on disk. "
        "Returns [{text, x, y, w, h}] per word.\n"
        "When to use: read text out of a saved screenshot or photo.\n"
        "When NOT to use: for live screen regions — use ocr_region."
    ),
)
def ocr_image(file_path: str, max_lines: int = 200) -> dict:
    return ocr_mod.ocr_file(file_path, max_lines=max_lines)


# ---------------------- tool 7: find_text_on_screen -----------------------

@mcp.tool(
    name="find_text_on_screen",
    description=(
        "OCR the full screen and search for a substring. "
        "Returns [{text, x, y, w, h}] for matches (case-insensitive by default).\n"
        "Returns coordinates ONLY (P1 is read-only — no clicking).\n"
        "When to use: locate a label/button by visible text when its automation_id is unknown.\n"
        "When NOT to use: for whole-document extraction — use ocr_region on a small area."
    ),
)
def find_text_on_screen(
    query: str,
    case_insensitive: bool = True,
    max_lines: int = 400,
) -> dict:
    return ocr_mod.find_text_on_screen(query, case_insensitive=case_insensitive, max_lines=max_lines)


# ---------------------- tool 8: get_browser_tab_info ----------------------

@mcp.tool(
    name="get_browser_tab_info",
    description=(
        "Return {browser, title, url} of the active tab in Chrome / Edge / Firefox "
        "(via the address bar, read-only).\n"
        "When to use: when the user is in a browser and you need the current URL/title.\n"
        "When NOT to use: for arbitrary browsers or private APIs; "
        "if no supported browser is open returns supported:false without crashing."
    ),
)
def get_browser_tab_info() -> dict:
    ui_mod.init()
    return browser_mod.get_browser_tab_info()


# ---------------------- tool 9: read_clipboard ----------------------------

@mcp.tool(
    name="read_clipboard",
    description=(
        "Read the current Windows clipboard. Returns {type, text} or detects "
        "non-text formats (bitmap / html / rtf) and reports the type.\n"
        "When to use: user just said 'copy this' — get the text they copied.\n"
        "When NOT to use: for screenshots — use screenshot instead."
    ),
)
def read_clipboard() -> dict:
    return clip_mod.read_clipboard()


# ---------------------- P2 tools 10/11: browser DOM -----------------------

@mcp.tool(
    name="get_page_text",
    description=(
        "Read the main visible text of the active browser tab. "
        "Prefer this over screenshot/ocr when the user asks what is on a web page.\n"
        "Returns the cleaned, boilerplate-stripped text of the page (up to ~8000 chars), "
        "with `truncated=true` when the cap fired. Strictly read-only.\n"
        "When to use: when the active tab is a real web page and you need its content as text.\n"
        "When NOT to use: for browser internal pages (about:, chrome:, edge:) — returns "
        "an actionable error. If no extension is loaded, returns a clear install-steps "
        "error within ~10s; never hangs the server."
    ),
)
def get_page_text(max_chars: int = 8000) -> dict:
    return browser_dom_mod.get_page_text_sync(max_chars)


@mcp.tool(
    name="get_page_structure",
    description=(
        "Read the pruned interactive structure of the active browser tab: "
        "headings (with level), links (text + href), buttons (text), and inputs "
        "(label, type, name, placeholder). Up to ~200 items, with `truncated=true` "
        "when the cap fired. Strictly read-only.\n"
        "When to use: discover the headings/links/buttons/inputs of a web page "
        "before any future action; or summarize page layout.\n"
        "When NOT to use: for raw DOM (way too large) — this is already pruned. "
        "If no extension is loaded, returns a clear install-steps error within ~10s."
    ),
)
def get_page_structure(max_items: int = 200) -> dict:
    return browser_dom_mod.get_page_structure_sync(max_items)


# ---------------------- P3 tools 13/14/15: Office COM (read-only) ----------

@mcp.tool(
    name="read_word_text",
    description=(
        "Read the full text of the ACTIVE Microsoft Word document via COM. "
        "Attach-only — never launches Word; if Word is not running, returns "
        "{ok: false, error: 'office_not_running'} without crashing the server.\n"
        "PREFER read_word_text over OCR/screenshot when Word is open: it returns "
        "the actual document text in milliseconds, with no image noise. "
        "Vietnamese / Unicode text is preserved end-to-end.\n"
        "Read-only: never saves, modifies, or closes the document. "
        "Caps at 60,000 chars with `truncated: true` when the limit fires.\n"
        "When to use: Word is foreground/visible and you need the document body.\n"
        "When NOT to use: when no Word document is open — use list_windows first."
    ),
)
def read_word_text() -> dict:
    return office_mod.read_word_text()


@mcp.tool(
    name="read_excel_values",
    description=(
        "Read cell values of the used range of the ACTIVE Microsoft Excel "
        "workbook via COM. Optional `sheet` argument (case-insensitive fuzzy "
        "match); `max_rows` (default 200, hard cap 200); `max_cols` "
        "(default 50, hard cap 50). Empty trailing rows/cols are trimmed. "
        "Attach-only — never launches Excel; returns 'office_not_running' "
        "if Excel is not open.\n"
        "PREFER read_excel_values over OCR/screenshot when Excel is open: "
        "it returns actual cell values, not pixel approximations.\n"
        "Read-only: never saves, modifies, or closes the workbook.\n"
        "When to use: Excel is foreground/visible and you need cell contents.\n"
        "When NOT to use: for chart images / pivot tables — text only here."
    ),
)
def read_excel_values(
    sheet: Optional[str] = None,
    max_rows: int = 200,
    max_cols: int = 50,
) -> dict:
    return office_mod.read_excel_values(sheet=sheet, max_rows=max_rows, max_cols=max_cols)


@mcp.tool(
    name="read_powerpoint_text",
    description=(
        "Read the text of all slides of the ACTIVE Microsoft PowerPoint "
        "presentation via COM, returned as a per-slide list. "
        "Attach-only — never launches PowerPoint; returns 'office_not_running' "
        "if PowerPoint is not open.\n"
        "PREFER read_powerpoint_text over OCR/screenshot when PowerPoint is "
        "open: it returns real slide text (title + body + table cells), "
        "preserving Vietnamese / Unicode. Caps at `max_slides` (default 100).\n"
        "Read-only: never saves, modifies, or closes the presentation.\n"
        "When to use: PowerPoint is foreground/visible and you need slide "
        "text. When NOT to use: for images / charts embedded in slides — "
        "this tool returns text only."
    ),
)
def read_powerpoint_text(max_slides: int = 100) -> dict:
    return office_mod.read_powerpoint_text(max_slides=max_slides)


# ---------------------- entry point ---------------------------------------

def main() -> None:
    ui_mod.init()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()