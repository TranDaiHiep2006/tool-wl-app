# PROJECT_CONTEXT

Architecture and conventions for `tool-wl-app` (P1 + P2, read-only MCP server).

## 1. Goals (long-term)

Cursor Agent can **see** a Windows desktop — open apps, document structure,
the active browser tab, and the actual DOM of the active web page — and use
that information to guide a human user step by step through operations they
perform manually.

P1 is observation only via UIA, screenshots, OCR, clipboard, and the
browser address bar. P2 adds **deep browser reading** — a small MV3
extension + native messaging host that lets the agent read the active
page's DOM (text + pruned interactive structure). Still strictly read-only.

Action upgrades (click, type, focus, run shell commands) belong to P3.

## 2. Tech stack

| Layer            | Choice                          | Why                                                                |
| ---------------- | ------------------------------- | ------------------------------------------------------------------ |
| Language          | Python 3.11                     | All required packages have wheels for 3.11.                        |
| MCP              | `fastmcp` (FastMCP)             | Simplest stdio server in the Python ecosystem.                     |
| UI Automation    | `uiautomation`                  | Read-only access to named controls, automation_ids, bounds.        |
| Screenshots      | `mss` + Pillow                  | Fast, no extra runtime; mss only ever calls `.grab()` on the OS.   |
| OCR              | `Windows.Media.Ocr` via `winrt` | Offline, uses Windows' built-in OCR language packs.                |
| Clipboard        | `uiautomation.GetClipboard*`    | Already shipped; no extra deps.                                    |
| Browser DOM (P2) | MV3 extension + `websockets`    | Read DOM via content script; loopback WS bridge to MCP server.     |

Forbid: `pyautogui`, `pygetwindow`, `pynput`, anything that can move the
mouse, type, or click.

## 3. Module layout

```
server/
├── __init__.py
├── mcp_server.py     # FastMCP registration of the 12 tools
├── ui_automation.py  # window list, foreground, UIA tree, control text
├── screenshots.py    # mss + JPEG resize helpers
├── ocr.py            # Windows.Media.Ocr (runs on a dedicated MTA thread)
├── browser.py        # read active tab info from chrome/edge/firefox (address bar)
├── browser_dom.py    # P2: get_page_text / get_page_structure via WS bridge
└── clipboard.py      # read clipboard text or detect non-text formats

native_host/
├── __init__.py
├── tool_wl_host.py   # P2: standard Chrome Native Messaging host (stdio loop)

scripts/
├── register_mcp.py            # P1: merge tool-wl-app into ~/.cursor/mcp.json
├── register_native_host.py    # P2: write HKCU registry keys + manifest JSON
└── unregister_native_host.py  # P2: reverse of register_native_host.py

extension/
├── manifest.json    # MV3: content scripts + background service worker
├── content.js       # DOM extraction (text + pruned structure)
└── background.js    # service worker: WS bridge to MCP server

tests/
├── __init__.py
├── measure.py                  # P1: quota budget script
├── measure_browser_dom.py      # P2: measure get_page_text / get_page_structure on real pages
├── edge_e2e.py                 # P2: launch Edge with extension, exercise get_page_text on real pages
├── edge_e2e_structure.py       # P2: same for get_page_structure
├── test_no_action_api.py       # static scan: zero action APIs in server/
├── test_ui_automation.py       # list_windows / foreground / ui_tree / control_text + hermetic mock variants
├── test_screenshots.py         # screenshot size and shape constraints
├── test_ocr.py                 # synthetic image OCR + truncation
├── test_clipboard_browser.py   # clipboard + get_browser_tab_info
├── test_mcp_registration.py    # all 12 tools are registered
├── test_browser_dom.py         # P2: caps, framing, WS round-trip, timeout envelope
├── test_quota.py               # per-tool envelope size budgets
└── test_*.py                   # (raw_smoke.py removed; see REPORT.md §6)
```

## 4. Conventions

- All public functions take Python primitives (str, int, dict); tool
  descriptions are English, 1–2 short sentences + 1 line "when to use /
  not use".
- Every tool wraps user-facing errors so one failing call never crashes
  the server process.
- "Forever" constants: JPEG max-long-edge = 1280 px; JPEG quality ≈ 60;
  UIA tree max nodes = 300 (configurable); OCR line cap = 200;
  `get_page_text` max chars = 8000; `get_page_structure` max items = 200.
- OCR runs on a dedicated `ThreadPoolExecutor(max_workers=1)` thread with
  MTA apartment, because WinRT async APIs (`xxx_async().get()`) raise
  `Cannot call blocking method from single-threaded apartment` on STA. The
  executor is shared by `ocr_region`, `ocr_image`, `find_text_on_screen`.

## 5. Browser support matrix (P1, address bar only)

| Browser              | How URL is detected                                           |
| ------------------- | ------------------------------------------------------------ |
| Chrome / Edge / Brave / Opera / Vivaldi | `EditControl` with `automation_id` containing "address" or "url" |
| Firefox             | `ComboBoxControl` with `automation_id="urlbar-input"`        |

If no supported browser is open, `get_browser_tab_info()` returns
`{"supported": false, "reason": "..."}` instead of crashing.

## 5b. Browser DOM support (P2, deep reading)

| Browser / Channel       | Tested      | How                                                            |
| ----------------------- | ----------- | -------------------------------------------------------------- |
| Microsoft Edge (Chromium) | yes — E2E on `example.com`, `en.wikipedia.org`, `github.com` | MV3 unpacked extension + WS bridge on 127.0.0.1:57532 |
| Google Chrome           | same manifest; verified `register_native_host.py --browser Google` writes the registry key |
| Brave                   | same manifest; verified `register_native_host.py --browser BraveSoftware` |
| Firefox                 | best-effort only — extension is MV3 (Firefox WebExtensions have a different schema); native host manifest is written with `allowed_extensions` |

## 6. Known limits (P1 + P2)

- Cannot read Office document bodies (Word, Excel, PowerPoint) — the Office
  ribbon exposes a UIA tree but the document canvas does not. To get
  document content you currently have to OCR the screen region.
- Chromium-based browsers expose URLs only when the user has not scrolled
  the URL bar off-screen; if the bar is hidden we return the active tab
  title only (P1).
- OCR language packs must be installed at the OS level (`Windows Settings
  → Time & language → Language → Add a language → English (United
  States)`).
- `screenshot(mode="window")` requires the window to be on-screen (we
  capture the bounding rect; if it's behind another window, the captured
  image shows whatever is in front).
- `get_page_text` / `get_page_structure` return an error envelope with
  install steps if the MV3 extension is not loaded in the browser;
  timeout is hard-capped at 10 s per call so the MCP server never hangs.
- Browser DOM extraction is **content-script scope only** — frames inside
  a page are not extracted by default (set `all_frames: true` in the
  manifest to change this, but the bridge currently only asks the top
  frame).
- For very dense pages (e.g., GitHub repo home, Wikipedia long
  articles), `get_page_structure` hits the 200-item cap and reports
  `truncated: true`. The agent can still call `get_page_text` for the
  readable content.
- MV3 service workers are **ephemeral** — the extension reconnects to
  the bridge on a backoff schedule (max 30 s). Brief disconnects are
  recovered transparently.

## 7. P3 direction (NOT done yet)

1. **Office DOM access via pywin32** — `win32com.client.Dispatch("Word.Application")`
   reads paragraphs, tables, selection. **HIGHLY destructive** if misused;
   must live behind an explicit "open document" tool the user authorizes.
2. **Action tools** — `click(x,y)`, `set_focus_window(hwnd)`,
   `type_text(text)`, `hotkey(combo)`, `wait_for_window(title, timeout)`.
   Each gated behind an explicit user approval flow because the spec says
   "I will not buy ChatGPT or other bots" — every dangerous call must be
   deliberate.
3. **Multi-monitor coordinate model.** Right now
   `screenshot(mode="screen")` always captures monitor 1. Add `monitor`
   argument so the agent can pick.

## 8. Quota-saving design notes

- `get_ui_tree` walks breadth-first with a hard node cap; when over the cap
  the response carries `"truncated": true`. Most calls return < 60 KB JSON.
- `screenshot` is JPEG q=60, long edge ≤ 1280. Most calls return 30–120 KB.
- OCR truncates to ≤ 200 words; full-screen OCR is intentionally more
  aggressive (≤ 400) for `find_text_on_screen` because matching needs a
  reasonable scan.
- `get_page_text` truncates at 8000 chars; on real pages measured 1–8 KB.
- `get_page_structure` truncates at 200 items; on real pages measured
  0.1–22.5 KB (worst case = GitHub repo home + Wikipedia long article).

## 10. References

- Prompt: `prompt.md` (P1 + P2 brief).
- Repo: https://github.com/TranDaiHiep2006/tool-wl-app