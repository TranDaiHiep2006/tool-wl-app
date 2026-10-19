# tool-wl-app

A **read-only** Model Context Protocol (MCP) server that gives the Cursor Agent
"eyes" on a Windows desktop. P1 covered observation only — P2 adds deep browser
DOM reading via a small MV3 extension.

> Status: **P1 + P2** (read-only). No tool in this server mutates the desktop.

---

## What it can do

12 read-only tools exposed via stdio to any MCP-compatible client (Cursor
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

All tools return English errors on failure. A bad call on one tool never
crashes the server.

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

Then **restart Cursor**. In the chat, the 12 tools will appear under the
`tool-wl-app` MCP server name.

> If Cursor does not see the change, fully close and reopen it (the
> editor caches `mcp.json` until the process exits).

## Tool priority order

When you need screen information, follow this order to save quota:

1. `list_windows` — first call, ever.
2. `get_foreground_window` — when you only care about the active app.
3. `get_ui_tree` — discover named controls in that window.
4. `get_control_text` — read a specific control.
5. **`get_page_text` / `get_page_structure`** — **preferred for browser
   page content** (much cheaper than OCR/screenshot).
6. `ocr_region` / `ocr_image` — only when UIA / DOM tools can't describe
   what you see.
7. `find_text_on_screen` — locate text by visible string.
8. `screenshot` — **last resort**. Never in a loop.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
```

47 tests covering per-tool functional, no-action-API static scan, native
messaging framing, end-to-end WebSocket round-trip, and quota budgets.

## Read-only by design

P1 + P2 ship **only** read-only capabilities. No tool can click, type,
focus a window, move the cursor, launch programs, or modify files
(except temporary screenshot/OCR artifacts which are written to `%TEMP%`
and discarded). The MV3 extension only reads DOM; it never clicks,
types, or modifies pages. Adding action tools belongs to a later P3.

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