# PROJECT_CONTEXT

Architecture and conventions for `tool-wl-app` (P1, read-only MCP server).

## 1. Goals (long-term)

Cursor Agent can **see** a Windows desktop — open apps, document structure,
and the active browser tab — and use that information to guide a human user
step by step through operations they perform manually.

P1 (this iteration) is **observation only**. Action upgrades (click, type,
focus, run shell commands) belong to P2.

## 2. Tech stack

| Layer            | Choice                          | Why                                                                |
| ---------------- | ------------------------------- | ------------------------------------------------------------------ |
| Language          | Python 3.11                     | All required packages have wheels for 3.11.                        |
| MCP              | `fastmcp` (FastMCP)             | Simplest stdio server in the Python ecosystem.                     |
| UI Automation    | `uiautomation`                  | Read-only access to named controls, automation_ids, bounds.        |
| Screenshots      | `mss` + Pillow                  | Fast, no extra runtime; mss only ever calls `.grab()` on the OS.   |
| OCR              | `Windows.Media.Ocr` via `winrt` | Offline, uses Windows' built-in OCR language packs.                |
| Clipboard        | `uiautomation.GetClipboard*`    | Already shipped; no extra deps.                                    |

Forbid: `pyautogui`, `pygetwindow`, `pynput`, anything that can move the
mouse, type, or click.

## 3. Module layout

```
server/
├── __init__.py
├── mcp_server.py     # FastMCP registration of the 9 tools
├── ui_automation.py  # window list, foreground, UIA tree, control text
├── screenshots.py    # mss + JPEG resize helpers
├── ocr.py            # Windows.Media.Ocr (runs on a dedicated MTA thread)
├── browser.py        # read active tab info from chrome/edge/firefox
└── clipboard.py      # read clipboard text or detect non-text formats

tests/
├── test_no_action_api.py   # static scan: zero action APIs in server/
├── test_ui_automation.py    # list_windows / foreground / ui_tree / control_text
├── test_screenshots.py     # screenshot size and shape constraints
├── test_ocr.py             # synthetic image OCR + truncation
├── test_clipboard_browser.py  # clipboard + get_browser_tab_info
└── test_mcp_registration.py # all 9 tools are registered
```

## 4. Conventions

- All public functions take Python primitives (str, int, dict); tool
  descriptions are English, 1–2 short sentences + 1 line "when to use / not use".
- Every tool wraps user-facing errors so one failing call never crashes the
  server process.
- "Forever" constants: JPEG max-long-edge = 1280 px; JPEG quality ≈ 60;
  UIA tree max nodes = 300 (configurable); OCR line cap = 200.
- OCR runs on a dedicated `ThreadPoolExecutor(max_workers=1)` thread with
  MTA apartment, because WinRT async APIs (`xxx_async().get()`) raise
  `Cannot call blocking method from single-threaded apartment` on STA. The
  executor is shared by `ocr_region`, `ocr_image`, `find_text_on_screen`.

## 5. Browser support matrix

| Browser              | How URL is detected                                           |
| ------------------- | ------------------------------------------------------------ |
| Chrome / Edge / Brave / Opera / Vivaldi | `EditControl` with `automation_id` containing "address" or "url" |
| Firefox             | `ComboBoxControl` with `automation_id="urlbar-input"`        |

If no supported browser is open, `get_browser_tab_info()` returns
`{"supported": false, "reason": "..."}` instead of crashing.

## 6. Known limits (P1)

- Cannot read Office document bodies (Word, Excel, PowerPoint) — the Office
  ribbon exposes a UIA tree but the document canvas does not. To get document
  content you currently have to OCR the screen region.
- Chromium-based browsers expose URLs only when the user has not scrolled the
  URL bar off-screen; if the bar is hidden we return the active tab title only.
- OCR language packs must be installed at the OS level (`Windows Settings →
  Time & language → Language → Add a language → English (United States)`).
- `screenshot(mode="window")` requires the window to be on-screen (we capture
  the bounding rect; if it's behind another window, the captured image shows
  whatever is in front).

## 7. P2 direction (NOT done yet)

1. **Office DOM access via pywin32** — `win32com.client.Dispatch("Word.Application")`
   reads paragraphs, tables, selection. **HIGHLY destructive** if misused; must
   live behind an explicit "open document" tool the user authorizes.
2. **Browser DOM access** — a tiny Chrome/Firefox extension over the native
   messaging API, or a CDP/Playwright bridge if they're willing to install
   one. The MCP side then exposes `get_page_dom(selector)`.
3. **Action tools** — click(x,y), set_focus_window(hwnd), type_text(text),
   hotkey(combo), wait_for_window(title, timeout). Each gated behind an
   explicit user approval flow because the spec says "I will not buy ChatGPT
   or other bots" — every dangerous call must be deliberate.

## 8. Quota-saving design notes

- `get_ui_tree` walks breadth-first with a hard node cap; when over the cap the
  response carries `"truncated": true`. Most calls return < 60 KB JSON.
- `screenshot` is JPEG q=60, long edge ≤ 1280. Most calls return 30–120 KB.
- OCR truncates to ≤ 200 words; full-screen OCR is intentionally more
  aggressive (≤ 400) for `find_text_on_screen` because matching needs a
  reasonable scan.

## 10. References

- Prompt: `prompt.md` (P1 brief).
- Repo: https://github.com/TranDaiHiep2006/tool-wl-app