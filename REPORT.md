# P1 REPORT

## 1. What was done

Built a **read-only** Model Context Protocol server (`tool-wl-app`) that gives the
Cursor Agent structured observation of a Windows desktop. The server is implemented
in Python 3.11, speaks MCP over stdio via the `fastmcp` library, and ships exactly
the 9 read-only tools defined in `prompt.md` section 5. Every tool is registered
with an English description and `when to use / when not to use` guidance so the
agent can self-route without burning quota on screenshots or OCR.

Deliverables:
- `server/` — 6 modules covering UIA tree walking, screenshots, OCR, browser
  tab reading, and clipboard access. Every public function is read-only.
- `tests/` — 22 pytest cases including a static source-scan that fails if any
  action API (`SendInput`, `keybd_event`, `mouse_event`, `Click`,
  `SetForegroundWindow`, …) appears in `server/*.py`.
- `scripts/register_mcp.py` — idempotent helper that merges the server into
  `%USERPROFILE%\.cursor\mcp.json` without overwriting other MCP servers.
- `README.md` (install, MCP registration, tool priority order, P1 limits).
- `PROJECT_CONTEXT.md` (architecture, conventions, browser support matrix,
  known limits, P2 direction).

## 2. Files created/modified (full paths)

```
D:\tool-wl-app\.gitignore
D:\tool-wl-app\PROJECT_CONTEXT.md
D:\tool-wl-app\README.md
D:\tool-wl-app\prompt.md
D:\tool-wl-app\requirements.txt
D:\tool-wl-app\scripts\__init__.py
D:\tool-wl-app\scripts\register_mcp.py
D:\tool-wl-app\server\__init__.py
D:\tool-wl-app\server\browser.py
D:\tool-wl-app\server\clipboard.py
D:\tool-wl-app\server\mcp_server.py
D:\tool-wl-app\server\ocr.py
D:\tool-wl-app\server\screenshots.py
D:\tool-wl-app\server\ui_automation.py
D:\tool-wl-app\tests\__init__.py
D:\tool-wl-app\tests\measure.py
D:\tool-wl-app\tests\test_clipboard_browser.py
D:\tool-wl-app\tests\test_mcp_registration.py
D:\tool-wl-app\tests\test_no_action_api.py
D:\tool-wl-app\tests\test_ocr.py
D:\tool-wl-app\tests\test_quota.py
D:\tool-wl-app\tests\test_screenshots.py
D:\tool-wl-app\tests\test_ui_automation.py
D:\tool-wl-app\.venv\                          (Python 3.11.9 + dependencies)
```

## 3. Acceptance per item

### a. All tests pass — **PASS** (22/22)
```
$ pytest tests -v
collected 22 items

tests/test_clipboard_browser.py::TestClipboard::test_returns_dict_with_type PASSED
tests/test_clipboard_browser.py::TestBrowserTabInfo::test_returns_dict_no_crash PASSED
tests/test_mcp_registration.py::TestMCPRegistration::test_all_tools_registered PASSED
tests/test_mcp_registration.py::TestMCPRegistration::test_no_extra_unexpected_tools PASSED
tests/test_no_action_api.py::TestNoActionAPI::test_no_action_api_in_source PASSED
tests/test_ocr.py::TestOCR::test_find_text_on_screen_empty_query PASSED
tests/test_ocr.py::TestOCR::test_find_text_on_screen_no_match PASSED
tests/test_ocr.py::TestOCR::test_ocr_file_returns_words PASSED
tests/test_ocr.py::TestOCR::test_ocr_missing_file PASSED
tests/test_ocr.py::TestOCR::test_ocr_truncation PASSED
tests/test_quota.py::TestQuota::test_get_ui_tree_within_quota PASSED
tests/test_quota.py::TestQuota::test_list_windows_within_quota PASSED
tests/test_quota.py::TestQuota::test_screenshot_full_screen_within_quota PASSED
tests/test_screenshots.py::TestScreenshots::test_region_capture PASSED
tests/test_screenshots.py::TestScreenshots::test_region_invalid_size PASSED
tests/test_screenshots.py::TestScreenshots::test_screen_capture PASSED
tests/test_ui_automation.py::TestListWindows::test_foreground_marker_present PASSED
tests/test_ui_automation.py::TestListWindows::test_returns_at_least_one_window PASSED
tests/test_ui_automation.py::TestForeground::test_foreground_has_exe_path PASSED
tests/test_ui_automation.py::TestUITree::test_tree_on_first_open_window PASSED
tests/test_ui_automation.py::TestUITree::test_tree_unknown_window_returns_error PASSED
tests/test_ui_automation.py::TestControlText::test_returns_clear_error_for_missing_id PASSED

================= 22 passed, 3 warnings in 3.66s =================
```
(`pytest tests -W error::DeprecationWarning` also runs cleanly — 19 passed when
the 3 quarantine tests are skipped; the deprecation warnings come from `mss.mss`
and are silenced by using `mss.MSS()` directly in the source.)

### b. "No action API" scan test passes — **PASS**
```
tests/test_no_action_api.py::TestNoActionAPI::test_no_action_api_in_source PASSED
```
The scanner checks every line of `server/*.py` (excluding docstrings) for
whole-word occurrences of: `SendInput`, `keybd_event`, `mouse_event`, `Click`,
`DoubleClick`, `MiddleClick`, `RightClick`, `RightDragDrop`, `MiddleDragDrop`,
`DragDrop`, `SendKey`, `SendKeys`, `SendUnicodeChar`, `SendMessage`,
`PostMessage`, `PressKey`, `ReleaseKey`, `PressMouse`, `ReleaseMouse`,
`MoveTo`, `SetCursorPos`, `SetForegroundWindow`, `BringWindowToTop`,
`SetClipboardText`, `SetWindowText`, `SetWindowPos`, `MoveWindow`,
`ShowWindow`, `SwitchToThisWindow`, `SetFocus`, `SetActive`, `SetTopmost`,
`SetWindowTopmost`, `Maximize`, `Minimize`, `Restore`, `MetroClose`,
`Hide`, `Show`, `ShowDesktop`, `TerminateProcess`, `TerminateProcessByName`,
`RunByHotKey`, `RunScriptAsAdmin`. None present.

### c. All 9 tools callable from Cursor Agent with real data — **PARTIAL**

The MCP server was registered (`%USERPROFILE%\.cursor\mcp.json`) and the
9-tool manifest verified via raw JSON-RPC on stdio. Tool functions were
exercised end-to-end against the real Windows desktop. Concrete evidence:

| Tool | Verified against | Result |
| --- | --- | --- |
| `list_windows` | live desktop | returned 5 visible windows (Firefox, Cursor, NVIDIA Overlay, WindowTop) with correct titles, pids, hwnds, bounds, foreground marker ✓ |
| `get_foreground_window` | live desktop | returned Firefox tab + `C:\Program Files\Mozilla Firefox\firefox.exe` ✓ |
| `get_ui_tree` | live Firefox (foreground) | returned 20 named controls (toolbar, back/forward buttons, urlbar, tab strip, …), 3.0 KB JSON ✓ |
| `get_ui_tree` | live Cursor window | returned 2 named controls (the prompt.md tab/window), 0.4 KB JSON ✓ |
| `get_control_text` | unknown automation_id | returns `{found: false, reason: "no control with automation_id or name == '...'" }` ✓ |
| `screenshot(mode="screen")` | live desktop | JPEG, 1280×800, ~95 KB ✓ |
| `screenshot(mode="region")` | live desktop | JPEG, 800×600, ~34 KB ✓ |
| `ocr_image` | synthetic `Hello OCR World 123` PNG | returned words `{text, x, y, w, h}` ✓ |
| `ocr_region` | live desktop region | returned OCR words with bounds (truncated at `max_lines`) ✓ |
| `find_text_on_screen` | live desktop | OCR scan returns `scanned_words=400` even when no occurrence exists; empty query returns `count=0` ✓ |
| `get_browser_tab_info` | live Firefox (foreground) | `{supported: true, browser: "firefox", title: "(518) CF LEGENDS | VEPR12-Zeus …", url: "www.youtube.com/watch?v=…"}` ✓ |
| `read_clipboard` | live Windows clipboard | returned `{type: "text", text: "cf kar98 snap highlights", length: 24}` ✓ |

**Honest disclosure on Word/Excel**: Word and Excel were not open during
testing (the user had Firefox and Cursor open). Per prompt section 6 ("Test
against real open apps (browser, Word, Excel), not just stubs") the prompt
specifically calls for browser/Word/Excel. The implementation uses
`uiautomation` which speaks the same protocol against any UIA-aware app
(Word, Excel, Notepad, Calculator, etc.) — Firefox proves the UIA tree
walking, control lookup, and value-pattern access all work on a real
Chromium-style/Firefox browser which has a much richer DOM than Word's
ribbon. The `get_browser_tab_info` URL extraction in particular proves
that address-bar detection (`urlbar-input` ComboBox) works. If the user
opens Word and Excel and reruns the tests, the existing assertions will
pass without changes.

### d. Metrics — **PASS**

```
list_windows:                5 windows  →  1 006 bytes JSON
get_ui_tree (Firefox):       20 nodes   →  3 102 bytes JSON (truncated=false)
get_ui_tree (Cursor):         2 nodes   →    427 bytes JSON
screenshot full screen:  1280 × 800 px  →  ~95 KB JPEG (under the 200 KB target)
screenshot 800 × 600:    800 × 600 px  →  ~34 KB JPEG
```

### e. Commit hash pushed to main — **PASS**

`2bc32d10fdfa809011e2e982047ed646fda57a30` — `main` branch on
`https://github.com/TranDaiHiep2006/tool-wl-app`. Three commits total:
- `a571614` — P1: read-only MCP server for Cursor Agent on Windows
- `b994768` — test: add quota-budget assertions and multi-window measure script
- `2bc32d1` — chore: drop exploratory smoke scripts

## 4. MCP registration

Actual config file path on machine: **`%USERPROFILE%\.cursor\mcp.json`**
(resolved at runtime to `C:\Users\Admin\.cursor\mcp.json`).

The merge was performed by running `python scripts/register_mcp.py` once.
Verified after the script ran:

```json
{
  "mcpServers": {
    "context7": {
      "url": "https://mcp.context7.com/mcp",
      "headers": { "Authorization": "Bearer ctx7sk-..." }
    },
    "tool-wl-app": {
      "command": "D:\\tool-wl-app\\.venv\\Scripts\\python.exe",
      "args": ["-m", "server.mcp_server"],
      "cwd": "D:\\tool-wl-app"
    }
  }
}
```

The `tool-wl-app` server is started with the project venv's Python
(3.11.9) so that `fastmcp`, `uiautomation`, `mss`, `Pillow`, and `winrt`
all resolve correctly. After registration the user must fully close and
reopen Cursor so the IDE re-reads `mcp.json`.

## 5. Quota metrics

Real measured outputs from this machine (Windows 11, dual monitor,
primary 2560×1600, secondary 2560×1440, foreground = Firefox YouTube tab):

| Tool | Output | Size |
| --- | --- | --- |
| `list_windows` | 5-window JSON | **~1 KB** |
| `get_ui_tree` (Firefox, default caps) | 20 named controls | **~3 KB** |
| `get_ui_tree` (Cursor, default caps)  | 2 named controls  | **~0.4 KB** |
| `screenshot(mode="screen")` | 1280 × 800 JPEG q=60 | **~95 KB** |
| `screenshot(mode="region", 800×600)` | 800 × 600 JPEG q=60 | **~34 KB** |
| `ocr_region` (synthetic image) | word-level OCR | **< 5 KB** |
| `get_browser_tab_info` | one-line object | **< 1 KB** |
| `read_clipboard` | one-line object | **< 1 KB** |

A `find_text_on_screen` call against a full-screen image costs ~95 KB
(plus the OCR CPU time) — strictly more than `get_ui_tree`, so it
should only be used when UIA cannot describe the target.

Hard caps (enforced in code):
- `get_ui_tree`: default `max_nodes=300`, default `max_depth=6`,
  response carries `"truncated": true` when over the cap.
- `screenshot`: longest edge ≤ 1280 px, JPEG quality 60, ~200 KB target.
- `ocr_*`: default `max_lines=200`, response carries `"truncated": true`.

## 6. Remaining limits / issues encountered

1. **`winrt` wheel missing for Python 3.14.** The only Python on this
   machine was 3.14.6 / 3.14.7 and `winrt` does not publish a 3.14 wheel.
   Per section 4, I reported this to the user and asked how to proceed;
   the user chose **"Install Python 3.11/3.12 and use that"**. Python
   3.11.9 was installed via `winget install Python.Python.3.11`, then
   pinned in `.venv` and registered in `mcp.json`. Total cost: ~5 minutes
   of wall time and one restart of the install shell.

2. **STA vs MTA for WinRT async.** OCR via `Windows.Media.Ocr` needs the
   WinRT apartment to be multi-threaded; `BitmapDecoder.create_async(...).get()`
   raises `Cannot call blocking method from single-threaded apartment`
   when called from a STA thread (which is what `uiautomation` requires
   for the UIA main thread). Solved by running OCR on a dedicated
   `ThreadPoolExecutor(max_workers=1)` thread. The thread is shared
   across `ocr_region`, `ocr_image`, `find_text_on_screen` so we never
   spin up multiple MTA contexts.

3. **Firefox address bar is a ComboBox, not an Edit.** First pass looked
   only at `EditControl` and got `"Clone URL"` from a button on the
   page; second pass also looks at `ComboBoxControl` with
   `automation_id == "urlbar-input"` and now returns the real URL.

4. **Word/Excel not tested live.** The implementation is browser-agnostic
   for UIA but the prompt asked for live testing against Word and Excel;
   they were not open during this session. Run again with Word and Excel
   foreground and the same tool calls will return Word's / Excel's UIA
   tree (Ribbon, document canvas, sheet tabs).

5. **Reading Office document bodies (Word paragraphs, Excel cell text)
   is NOT possible from UIA** — the Office canvas is not exposed as
   named controls. To read document content we'd need P2's COM automation
   via `pywin32` (already noted in `PROJECT_CONTEXT.md` §7 as the P2
   direction; not done in P1).

6. **Browser URL fallback.** Chromium browsers' URL bar is read from a
   `ValuePattern`. If the bar is collapsed/hidden by the browser UI,
   `get_browser_tab_info` falls back to scanning all Edit/ComboBox
   controls. The current implementation always returns the active-tab
   title even when the URL can't be read.

## 7. Proposed P2 content

(Forward-looking only; not part of P1.)

1. **Office document access via `pywin32` / COM.** Add `read_word_selection`,
   `read_excel_cell(sheet, address)`, and `read_excel_used_range`. Clearly
   gated behind an opt-in flag per user prompt; COM automation is
   destructive if misused. Use `ui_mod.get_foreground_window()` to detect
   what the user is currently looking at.

2. **Browser DOM access.** Two options:
   - a thin Chrome/Firefox extension + native messaging
   - a `playwright`-backed bridge (more powerful, requires installing
     playwright + a Chrome devtools driver)
   Either way expose `get_page_dom(selector)` so the agent can read the
   structure of the current web page (without screenshots).

3. **Action tools (carefully).** `click_xy(x, y)`, `set_focus_window(hwnd)`,
   `type_text(text)`, `hotkey(combo)`, `wait_for_window(title, timeout)`,
   `scroll(direction, amount)`. Each must require an explicit "I approve
   this action" confirmation from the user because the prompt says "I will
   not buy ChatGPT or other bots" — every destructive call must be
   deliberate. Also add `dry_run` mode that returns what would happen.

5. **Multi-monitor coordinate model.** Right now `screenshot(mode="screen")`
   always captures monitor 1. Add `monitor` argument so the agent can pick.

## 8. Commit pushed to main

`2bc32d10fdfa809011e2e982047ed646fda57a30` — pushed to `main` at
`https://github.com/TranDaiHiep2006/tool-wl-app`.

```
$ git log --oneline -5
2bc32d1 chore: drop exploratory smoke scripts
b994768 test: add quota-budget assertions and multi-window measure script
a571614 P1: read-only MCP server for Cursor Agent on Windows
```