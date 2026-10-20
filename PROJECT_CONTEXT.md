# PROJECT_CONTEXT

Architecture and conventions for `tool-wl-app` (P1 + P2 + P3, read-only MCP server).

## 1. Goals (long-term)

Cursor Agent can **see** a Windows desktop — open apps, document structure,
the active browser tab, the actual DOM of the active web page, and the
CONTENTS of open Office files (Word / Excel / PowerPoint) — and use
that information to guide a human user step by step through operations
they perform manually.

P1 is observation only via UIA, screenshots, OCR, clipboard, and the
browser address bar. P2 adds **deep browser reading** — a small MV3
extension + native messaging host that lets the agent read the active
page's DOM (text + pruned interactive structure). P3 adds **Office
document content** via pywin32 COM (read-only) and **locks down the
WebSocket bridge** with a per-install secret token. Everything remains
strictly read-only.

Action upgrades (click, type, focus, run shell commands) belong to P4.

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
├── mcp_server.py     # FastMCP registration of 15 tools (9 P1 + 2 P2 + 3 P3)
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

## 6. Known limits (P1 + P2 + P3)

- Word / Excel / PowerPoint must already be open before any COM tool is
  invoked. The implementation attaches via `GetActiveObject` and never
  launches Office. Closing the app returns
  `{"ok": false, "error": "office_not_running"}` and the MCP server
  stays up.
- Word's `read_word_text` caps at 60 000 chars; over the cap the
  response carries `"truncated": true`.
- Excel reads the used range; default caps `200 rows × 50 cols`, hard
  caps identical. Empty trailing rows / cols are trimmed. Cells come
  back as `str` / `int` / `float` / ISO-formatted `date` — number-format
  strings and formulas are NOT returned.
- PowerPoint reads slide titles + body text via
  `TextFrame.TextRange.Text`; tables are walked too. Charts, SmartArt,
  embedded images, and text effects are NOT extracted.
- Chromium-based browsers expose URLs only when the user has not
  scrolled the URL bar off-screen; if the bar is hidden we return the
  active tab title only (P1).
- OCR language packs must be installed at the OS level (`Windows
  Settings → Time & language → Language → Add a language → English
  (United States)`).
- `screenshot(mode="window")` requires the window to be on-screen (we
  capture the bounding rect; if it's behind another window, the
  captured image shows whatever is in front).
- `get_page_text` / `get_page_structure` return an error envelope with
  install steps if the MV3 extension is not loaded in the browser;
  timeout is hard-capped at 10 s per call so the MCP server never
  hangs.
- The browser bridge (P3) requires the per-install secret token;
  unauthenticated connections are closed with WebSocket code 1008
  ("policy violation"). Rotate the token any time via
  `scripts/rotate_bridge_token.py`.
- Browser DOM extraction is **content-script scope only** — frames
  inside a page are not extracted by default (set `all_frames: true`
  in the manifest to change this, but the bridge currently only asks
  the top frame).
- For very dense pages (e.g., GitHub repo home, Wikipedia long
  articles), `get_page_structure` hits the 200-item cap and reports
  `truncated: true`. The agent can still call `get_page_text` for the
  readable content.
- MV3 service workers are **ephemeral** — the extension reconnects to
  the bridge on a backoff schedule (max 30 s). Brief disconnects are
  recovered transparently.
- `extension/token.js` is auto-generated by the server on first run
  and is `.gitignore`d. If you load the extension BEFORE the MCP
  server has run, `token.js` is missing and the service worker logs a
  warning — run the MCP server once, then **reload the extension**
  (the SW caches dynamic imports).

## 7. P4 direction (NOT done yet; future)

P3 (Office COM + bridge lockdown) is delivered (see §7b above). What
remains for a future P4:

1. **Action tools** — `click(x,y)`, `set_focus_window(hwnd)`,
   `type_text(text)`, `hotkey(combo)`, `wait_for_window(title, timeout)`.
   Each gated behind an explicit user approval flow because the spec says
   "I will not buy ChatGPT or other bots" — every dangerous call must be
   deliberate.
2. **Multi-monitor coordinate model.** Right now
   `screenshot(mode="screen")` always captures monitor 1. Add `monitor`
   argument so the agent can pick.

## 7b. P3 — DELIVERED

### 7b.1 Office COM (read-only) — `server/office_com.py`

Three new tools attached to already-running Office apps via
`win32com.client.GetActiveObject` (NEVER `CreateObject` / `DispatchEx`):

| Tool                  | What it returns                                                |
| --------------------- | -------------------------------------------------------------- |
| `read_word_text()`             | Full body text of the active Word document (≤ 60 000 chars) |
| `read_excel_values(sheet, max_rows, max_cols)` | Values of the named sheet's used range, empty trailing rows/cols trimmed; defaults `max_rows=200`, `max_cols=50`, hard caps `200` / `50` |
| `read_powerpoint_text(max_slides)` | Per-slide text of the active presentation; default + cap `100` |

**Hard read-only contract:** the module never calls `Save`, `SaveAs`,
`Close`, `Quit`, `PrintOut`, etc. `tests/test_no_action_api.py` static
scan was extended with `Save`, `SaveAs`, `SaveAs2`, `Close`, `Quit`,
`PrintOut`, `PrintPreview`, `NewDocument`, `NewWorkbook`,
`NewPresentation`, `AddSlide`, `DeleteSlide`, `CreateObject` so any
attempt to introduce a mutating call fails the suite.

**COM hygiene:** every call runs in a short-lived worker thread with its
own `pythoncom.CoInitialize` / `pythoncom.CoUninitialize`. A 10-second
timeout surfaces as `{"ok": false, "error": "com_timeout"}` so the MCP
server never hangs. Never-launch-Office is enforced by always using
`GetActiveObject` (which raises when the app is not running) — failure
becomes `{"ok": false, "error": "office_not_running", "message": "..."}`.

**Vietnamese / Unicode:** cell values come back via `Range.Value`,
Word body via `doc.Content.Text`, PowerPoint via
`shape.TextFrame.TextRange.Text`. Vietnamese characters round-trip
identically (cf. `test_office_com.py::TestReadExcelValues::test_explicit_sheet_lookup`).

### 7b.2 Browser bridge lockdown (Option 1: WebSocket + token)

The P2 loopback WebSocket bridge (`ws://127.0.0.1:57532`) now requires
the connecting extension to present a **per-install secret token**:

- Token generated the first time it is needed, persisted at
  `config/bridge_token`. Mirrored to `extension/token.js` (gitignored),
  which the service worker imports at startup.
- The extension's `hello` message now carries `{"type": "hello",
  "token": "<the-token>", ...}`. Constant-time comparison
  (`hmac.compare_digest`) on the server side.
- Connections that don't send a `hello` within 5 s, send a non-JSON
  message, send a malformed `hello`, send a wrong / empty token, etc.
  are closed with WebSocket code **1008** ("policy violation") and
  never see a request.
- No unauthenticated localhost listener. `--allow-any-origin` is still
  available as an explicit opt-in on `register_native_host.py` for
  development but is NEVER the default path.
- Rotate the token any time via
  `python scripts/rotate_bridge_token.py` (or `--dry-run` to preview).

Tests in `tests/test_browser_dom.py::TestBridgeAuth` cover:
- `test_correct_token_is_accepted` — full request/reply round-trip.
- `test_missing_token_rejected` — hello without token, expect close.
- `test_wrong_token_rejected` — hello with bad token, expect close.
- `test_no_hello_within_timeout_closes` — silent client, expect close.
- Plus `TestBridgeAuthModule` covering token persistence, idempotency,
  rotation, and `hmac.compare_digest`.

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