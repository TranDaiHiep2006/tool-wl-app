# tool-wl-app

A **read-only** Model Context Protocol (MCP) server that gives the Cursor Agent
"eyes" on a Windows desktop. P1 covers only observation — clicking, typing,
focus changes, and command execution belong to a later P2 prompt.

> Status: **P1** (read-only). No tool in this server mutates the desktop.

---

## What it can do (P1)

9 read-only tools exposed via stdio to any MCP-compatible client (Cursor Agent,
Claude Desktop, etc.):

| # | Tool                          | What it does                                                                |
| - | ----------------------------- | --------------------------------------------------------------------------- |
| 1 | `list_windows`                | List visible top-level windows (title, pid, hwnd, bounds, foreground flag). |
| 2 | `get_foreground_window`       | Return the foreground window + its full exe path.                           |
| 3 | `get_ui_tree`                 | Return a pruned UIA tree (visible, named controls only, ≤ 300 nodes by default). |
| 4 | `get_control_text`            | Read the text/Name of a single control by automation_id or exact Name.     |
| 5 | `screenshot`                  | Capture screen / window / region as JPEG ≤ 1280 long-edge, ≤ ~200 KB.       |
| 6 | `ocr_region`, `ocr_image`     | Offline OCR via Windows.Media.Ocr → [{text, x, y, w, h}].                   |
| 7 | `find_text_on_screen`         | OCR the screen, then substring-search; returns coordinates only (no clicks). |
| 8 | `get_browser_tab_info`       | Active browser tab title + URL (Chrome / Edge / Firefox / Brave / Opera / Vivaldi). |
| 9 | `read_clipboard`              | Read Windows clipboard (text or detected non-text format).                  |

All tools return English errors on failure. A bad call on one tool never
crashes the server.

## Install

Requires **Python 3.11+ on Windows** and an OCR language pack (Settings →
Time & language → Language → "Add a language" → English (United States) or
Vietnamese if you read Vietnamese).

```powershell
# From the repo root (D:\tool-wl-app)
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

Then **restart Cursor**. In the chat, the 9 tools will appear under the
`tool-wl-app` MCP server name.

> If Cursor does not see the change, fully close and reopen it (the editor
> caches `mcp.json` until the process exits).

## Tool priority order

When you need screen information, follow this order to save quota:

1. `list_windows` — first call, ever.
2. `get_foreground_window` — when you only care about the active app.
3. `get_ui_tree` — discover named controls in that window.
4. `get_control_text` — read a specific control.
5. `ocr_region` / `ocr_image` — only when UIA can't describe what you see.
6. `find_text_on_screen` — locate text by visible string.
7. `screenshot` — **last resort**. Never in a loop.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
```

The test suite includes:

- per-tool functional tests (`tests/test_*.py`)
- a static scan test (`tests/test_no_action_api.py`) that fails if any action
  API (`SendInput`, `keybd_event`, `mouse_event`, `Click`, `SetForegroundWindow`,
  …) appears in `server/*.py` — P1 only passes when this passes.

## P1 — read-only by design

P1 ships **only** read-only capabilities. None of the 9 tools can click, type,
focus a window, move the cursor, launch programs, or modify files
(except temporary screenshot/OCR artifacts which are written to `%TEMP%`
and discarded). Adding action tools belongs to a separate P2 prompt.

## Quota saving

- `get_ui_tree` truncates at 300 nodes (configurable) and only emits named,
  visible controls.
- `screenshot` downscales so the longest edge ≤ 1280 px and saves JPEG q=60,
  typically ~30–100 KB.
- OCR results truncate at 200 lines (configurable); the response carries
  a `"truncated": true` marker when this happens.

## License

MIT.