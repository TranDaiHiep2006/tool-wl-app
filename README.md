# tool-wl-app

A Model Context Protocol (MCP) server for Windows desktop. P1-P3 are
strictly read-only. P4 adds action tools behind a strict safety gate.

> Status: **P1 + P2 + P3 + P4** (read-only tools + action tools with safety gate).
> All mutating tools require a plan+confirm cycle before executing.

---

## What it can do

15 read-only tools exposed via stdio to any MCP-compatible client (Cursor
Agent, Claude Desktop, etc.):

| #  | Tool                          | What it does                                                                |
| -- | ----------------------------- | --------------------------------------------------------------------------- |
| 1  | `list_windows`                | List visible top-level windows (title, pid, hwnd, bounds, foreground flag). |
| 2  | `get_foreground_window`       | Return the foreground window + its full exe path.                           |
| 3  | `get_ui_tree`                 | Return a pruned UIA tree (visible, named controls only, ≤ 300 nodes).       |
| 4  | `get_control_text`            | Read the text/Name of a single control by automation_id or exact Name.      |
| 5  | `screenshot`                  | Capture screen / window / region as JPEG ≤ 1280 long-edge, ≤ ~200 KB.       |
| 6  | `ocr_region`, `ocr_image`     | Offline OCR via Windows.Media.Ocr → [{text, x, y, w, h}].                   |
| 7  | `find_text_on_screen`         | OCR the screen, then substring-search; returns coordinates only (no clicks).|
| 8  | `get_browser_tab_info`        | Active browser tab title + URL (Chrome/Edge/Firefox/Brave/Opera/Vivaldi).  |
| 9  | `read_clipboard`              | Read Windows clipboard (text or detected non-text format).                  |
| 10 | **`get_page_text`**           | **P2 — readable article text of the active browser tab (≤ ~8000 chars).**   |
| 11 | **`get_page_structure`**      | **P2 — pruned interactive structure of the active tab (≤ 200 items).**     |
| 12 | **`read_word_text`**          | **P3 — full body text of the active Word document (≤ 60 000 chars).**      |
| 13 | **`read_excel_values`**       | **P3 — values of the active workbook's named sheet (≤ 200×50 cells).**     |
| 14 | **`read_powerpoint_text`**    | **P3 — per-slide text of the active presentation (≤ 100 slides).**         |
| 15 | **`click_control`**           | **P4 — click a control via UIA pattern (requires plan+confirm).**        |
| 16 | **`type_text`**               | **P4 — set editable control text via ValuePattern (requires plan+confirm).** |
| 17 | **`focus_window`**            | **P4 — bring an allowlisted app to foreground (gate + logging).**        |

All tools return English errors on failure. A bad call on one tool never
crashes the server.

## P4 — Action tools with safety gate

Three tools let the agent ACT on the desktop, not just observe it.
Every mutating tool uses a **two-phase plan/confirm cycle**:

**Phase 1** (no `plan_id`): the tool validates the target and returns a
human-readable plan. Show this to the user and wait for their approval.

**Phase 2** (with `plan_id`): the server verifies the plan is valid,
unexpired, and matches the action, then executes it once and consumes the
plan.

### Safety gate

Every action is gated by `config/actions.json`:
- `enabled: true` must be set.
- The target window's process must be in `allowed_processes` (default:
  `["notepad.exe"]`).
- A `config/STOP_ACTIONS` file blocks all actions instantly.
- A control whose Name contains a `deny_labels` entry is blocked.

Edit `config/actions.json` to allow your apps:

```jsonc
{
  "enabled": true,
  "allowed_processes": ["notepad.exe", "calc.exe"],
  "deny_labels": ["delete", "format", "uninstall", "shut down", "pay now", "remove account"]
}
```

Every plan, execute, and refusal is logged to `logs/actions.log`
(gitignored).

### Tools

| Tool | Description |
| ---- | ----------- |
| `click_control(window, automation_id?, name?)` | Click a button/menu via UIA pattern (Invoke > Toggle > SelectionItem > ExpandCollapse). Phase 1 returns a plan. |
| `type_text(window, text, automation_id?, name?)` | Set an editable control's text via ValuePattern.SetValue. Phase 1 returns a plan showing current and new text. |
| `focus_window(window)` | Bring an allowlisted window to foreground. No plan needed; returns honest error if Windows refuses. |

### Read-only by design

P1-P3 tools remain strictly read-only. The static scan in
`tests/test_no_action_api.py` forbids all action APIs (`Invoke`,
`SetValue`, `SendInput`, `SetForegroundWindow`, etc.) everywhere in
`server/` except `actions.py`.

---

## P2 — deep browser reading

The two new tools (`get_page_text`, `get_page_structure`) require a tiny
extension to be loaded in the browser. The MCP server talks to it over a
loopback WebSocket (`ws://127.0.0.1:57532/`) and the extension reads the
active tab's DOM via a content script. Everything is strictly read-only.

### Install the browser side

1. Open Microsoft Edge (or Chrome) at `edge://extensions` and enable
   **Developer mode** (top-right toggle).
2. Click **Load unpacked** and select this folder:

   ```
   D:\tool-wl-app\extension
   ```
3. Copy the extension ID shown on the card.
4. Register the native messaging host (so Chrome/Edge can launch our
   diagnostic host if needed):

   ```powershell
   .\.venv\Scripts\python.exe scripts\register_native_host.py --extension-id <ID>
   ```

   For development you can use `--allow-any-origin` (DEV ONLY).
5. Fully close and reopen Edge/Chrome so the registry is re-read.
6. **Restart Cursor** so the MCP server is re-spawned and the bridge
   WebSocket on 57532 starts fresh.

To undo everything:

```powershell
.\.venv\Scripts\python.exe scripts\unregister_native_host.py
```

### What the new tools return

`get_page_text` returns:

```json
{
  "ok": true,
  "text": "...boilerplate-stripped article text...",
  "chars": 7442,
  "max_chars": 8000,
  "truncated": false
}
```

`get_page_structure` returns:

```json
{
  "ok": true,
  "items": [
    {"kind": "heading", "level": 1, "text": "Web browser"},
    {"kind": "link",    "text": "Jump to content", "href": "...#bodyContent"},
    {"kind": "button",  "text": "Search"},
    {"kind": "input",   "input_type": "text", "name": "search", "placeholder": "Search Wikipedia"}
  ],
  "count": 200,
  "counts_by_kind": {"heading": 12, "link": 188},
  "truncated": true
}
```

Both responses carry `truncated: true` when their respective caps fired.

If the extension is not connected, the tools return a structured error
with install instructions and never hang the MCP server:

```json
{
  "ok": false,
  "error": "bridge_timeout",
  "message": "extension not connected within 10s",
  "timeout_s": 10.0,
  "install_steps": "1. Open Microsoft Edge (or Chrome)..."
}
```

### Per-install secret token (P3)

The P2 WebSocket bridge on `127.0.0.1:57532` now requires the extension
to present a per-install secret token before any request reaches the
data path. Unauthenticated connections are closed with WebSocket code
**1008** ("policy violation").

- The token lives at `config/bridge_token` (auto-generated on the first
  MCP server start, gitignored) and is mirrored to
  `extension/token.js` (also gitignored) which the service worker
  imports at startup.
- If you load the extension BEFORE running the MCP server for the
  first time, `token.js` is missing and the SW logs a console warning.
  After the MCP server has run once, **reload the extension** in
  `chrome://extensions` so the SW re-imports the freshly-written module.
- Rotate the token any time with:

  ```powershell
  .\.venv\Scripts\python.exe scripts\rotate_bridge_token.py
  ```

  The currently-connected extension will reconnect on its next hello
  (MV3 service-worker backoff ≤ 30 s).
- `register_native_host.py --allow-any-origin` is still available as an
  explicit opt-in for development but is never the default.

---

## P3 — Office document reading (read-only)

Three new tools attach to already-running Microsoft Office apps via
`win32com.client.GetActiveObject` (NEVER launch Office). They are
strictly read-only — never `Save`, `SaveAs`, `Close`, etc. — and the
module is statically scanned in `tests/test_no_action_api.py`.

| Tool | What it returns |
| --- | --- |
| `read_word_text()` | Full body text of the active Word document (≤ 60 000 chars). |
| `read_excel_values(sheet=None, max_rows=200, max_cols=50)` | Values of the used range of the named sheet (or `ActiveSheet`). Empty trailing rows/cols trimmed. |
| `read_powerpoint_text(max_slides=100)` | Per-slide text of the active presentation. |

If Office is not running or no document is open, each tool returns
`{"ok": false, "error": "office_not_running", "message": "..."}`
without crashing the server. Vietnamese / Unicode text round-trips
identically. A live end-to-end smoke test (`tests/manual_office_e2e_excel.py`)
launches Excel with a real COM-created workbook and confirms Vietnamese
cell content round-trips correctly.

---

## Install

Requires **Python 3.11+ on Windows** and an OCR language pack
(Settings → Time & language → Language → "Add a language" → English
(United States) or Vietnamese if you read Vietnamese).

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run the MCP server

```powershell
.\.venv\Scripts\python.exe -m server.mcp_server
```

The server speaks MCP over stdio. Register it in Cursor (see below) and
restart the IDE.

## Register in Cursor (Windows)

The real `mcp.json` on Windows lives at:

```
%USERPROFILE%\.cursor\mcp.json
```

Add an entry for this server. The full file should look like:

```jsonc
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

Then **restart Cursor**. In the chat, the 18 tools will appear under the
`tool-wl-app` MCP server name.

> If Cursor does not see the change, fully close and reopen it (the
> editor caches `mcp.json` until the process exits).

## Tool priority order

When you need screen information, follow this order to save quota:

1. `list_windows` — first call, ever.
2. `get_foreground_window` — when you only care about the active app.
3. `get_ui_tree` — discover named controls in that window.
4. `get_control_text` — read a specific control.
5. **`read_word_text` / `read_excel_values` / `read_powerpoint_text`** —
   **preferred for open Office documents** (much cheaper and more
   accurate than OCR).
6. **`get_page_text` / `get_page_structure`** — **preferred for browser
   page content** (much cheaper than OCR/screenshot).
7. `ocr_region` / `ocr_image` — only when UIA / DOM / COM can't describe
   what you see.
8. `find_text_on_screen` — locate text by visible string.
9. `screenshot` — **last resort**. Never in a loop.
10. **`click_control` / `type_text`** — only after the user has approved
    the plan. Show the plan description to the user first.
11. **`focus_window`** — only for allowlisted apps; confirm with user first.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
```

106 tests covering per-tool functional, no-action-API static scan,
native messaging framing, end-to-end WebSocket round-trip, per-install
token auth, Office COM mock coverage, P4 safety gate unit tests, and
quota budgets.

Run the P4 manual E2E (requires real desktop):

```powershell
.\.venv\Scripts\python.exe tests\manual_actions_e2e.py
```

## Read-only by design (P1-P3) + Action safety gate (P4)

P1 + P2 + P3 ship **only** read-only capabilities. No tool can click, type,
focus a window, move the cursor, launch programs, save Office documents,
or modify files (except temporary screenshot/OCR artifacts which are
written to `%TEMP%` and discarded). The MV3 extension only reads DOM; it
never clicks, types, or modifies pages. Adding action tools belongs to a
later P4.

## Quota saving

- `get_ui_tree` truncates at 300 nodes (configurable) and only emits
  named, visible controls.
- `screenshot` downscales so the longest edge ≤ 1280 px and saves JPEG
  q=60, typically ~30–100 KB.
- OCR results truncate at 200 lines (configurable); the response carries
  a `"truncated": true` marker when this happens.
- `get_page_text` truncates at ~8 000 chars and reports `"truncated": true`.
- `get_page_structure` truncates at 200 items (headings/links/buttons/inputs)
  and reports `"truncated": true`. Worst-case measured: 22 KB on a dense
  GitHub or Wikipedia page.

## License

MIT.