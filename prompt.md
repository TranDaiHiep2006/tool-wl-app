# P1 — tool-wl-app: READ-ONLY MCP server ("eyes") for Cursor Agent on Windows

## 1. Role
You are a senior Python developer, experienced with Windows APIs (UI Automation, Win32), MCP (Model Context Protocol), and resource-efficient code.

## 2. Background & Goal
- I use Cursor Agent heavily and pay monthly quota → SAVING QUOTA is a hard requirement, both while you work and when the tool runs later. I will not buy ChatGPT or other bots.
- Long-term goal: Cursor Agent can "see" my Windows screen and apps (browser is top priority; Word, Excel, PowerPoint, File Explorer, chat apps... must all be readable) so it can guide me step by step while I operate manually.
- This P1 is READ-ONLY. Absolutely no actions yet. Action upgrades (click/type) belong to a later prompt, not P1.
- Repo: https://github.com/TranDaiHiep2006/tool-wl-app (empty). Clone to `D:\tool-wl-app` if missing, work there, then commit + push to `main`.
- Communicate with me in English (all your output, including the final report).

## 3. P1 Scope — DO and DO NOT
**DO:** Python MCP server (stdio, FastMCP library) with exactly the 9 read-only tools in section 5, registrable in Cursor via `mcp.json`, with tests and docs.
**DO NOT in P1 (forbidden):**
- Any action: clicking, typing, focusing/setting windows, launching apps, running shell/commands, writing files (except logs).
- Browser extension / reading page DOM (P2).
- Reading Office file contents via COM automation (P2 — only note this direction in PROJECT_CONTEXT.md).
- Downloading any heavy ML model (no torch/transformers/vision models).

## 4. Technical Decisions — FINAL (follow them, do not re-decide)
- Python 3.11+, `fastmcp`, stdio transport.
- UI Automation: `uiautomation` package (pip). Screenshots: `mss` + `Pillow`.
- OCR: Windows built-in `Windows.Media.Ocr` via the `winrt` package — offline, nothing to download.
- If any library fails to install/run on my machine: REPORT to me before switching, do not silently switch stacks.
- Each tool description in ENGLISH, 1–2 short sentences + 1 line stating when to use / not use the tool.

## 5. Detailed Spec of the 9 Tools ("done" criteria per tool)
1. `list_windows()` → list of {title, process_name, pid, hwnd, x, y, w, h, is_minimized, is_foreground}. Skip title-less/invisible windows. Foreground first. Done when: lists the windows I actually have open.
2. `get_foreground_window()` → one object as above + exe path (best-effort).
3. `get_ui_tree(window, max_depth=6, max_nodes=300)` — `window` is a title (fuzzy match) or hwnd. Only visible controls with a Name. Each node: {control_type, name, automation_id, x, y, w, h}. Over max_nodes → truncate in traversal order, add `"truncated": true`. Done when: with Word/Excel/Notepad open, the tool shows the main named buttons/menus/inputs.
4. `get_control_text(window, automation_id_or_name)` → text of the control. Not found → clear message, no crash.
5. `screenshot(mode, ...)` — mode: `"screen"` | `"window"` (with window) | `"region"` (with x, y, w, h). Output: JPEG, longest edge ≤ 1280px, quality ~60, each image ≤ ~200KB. Description must state: "use only when get_ui_tree is insufficient; never screenshot in a loop".
6. `ocr_region(x, y, w, h)` and `ocr_image(file_path)` → [{text, x, y, w, h, confidence}], max ~200 lines, truncate + report clearly if exceeded.
7. `find_text_on_screen(query)` → OCR the full screen then search (case-insensitive by default), return [{text, x, y, w, h}]. Return coordinates ONLY — no clicking in P1.
8. `get_browser_tab_info()` → {browser, title, url} of the active tab in Chrome/Edge/Firefox, read via UI Automation (address bar). Browser not readable → return `{"supported": false, "reason": "..."}`, never crash the server.
9. `read_clipboard()` → text in clipboard; if not text → report the content type clearly.

## 6. Common Pitfalls — read before coding to avoid rework
- UIA trees of Chrome/Word can have THOUSANDS of nodes → pruning + limits from section 5 are mandatory, never dump the full tree.
- Every tool must try/except and return meaningful errors in English; one failing tool must not crash the server.
- No `pyautogui`/`pygetwindow` in P1 (both invite accidental actions) — only `mss` + `uiautomation`.
- Cursor's `mcp.json` path on Windows: verify the real path on the machine, do not guess.
- Test against real open apps (browser, Word, Excel), not just stubs.

## 7. Quota Saving — mandatory
While you work:
- Batch all verification into as few command runs as possible: write one test script that runs everything, no one-off commands with one fix at a time.
- Read files with a purpose; no aimless reading.
- Fix in clusters: group related errors and fix in one pass, not one error per run.
When the tool runs (design from P1 so the agent doesn't burn my money later):
- Every output has a size limit + truncation + clear truncation notice (section 5).
- Each tool description states the priority order (UIA first, screenshot last).

## 8. Safety
- All P1 code must contain zero action APIs (SendInput, mouse_event, keybd_event, SetForegroundWindow, ...).
- Write an automated test scanning the whole source to assert the above — P1 only passes when this test passes.
- README.md must state: P1 is read-only by design.

## 9. Workflow (follow in order)
1. Clone the repo (empty) to `D:\tool-wl-app` if missing.
2. Build `server/mcp_server.py` + implement the 9 tools per section 5 spec.
3. Write `tests/`: per-tool functional tests + the "no action API" source scan test.
4. Run tests until all pass (batch runs per section 7).
5. Write `PROJECT_CONTEXT.md` (architecture, tool list, conventions, known limits, P2 direction: COM automation for Office files + browser extension for DOM), `README.md` (install, MCP registration, tool list, P1 limits), script/instructions for `mcp.json` registration.
6. Register MCP in Cursor, restart Cursor, verify each tool from Cursor Agent against real apps (browser + Word + Excel).
7. Measure sample output sizes (section 10).
8. Commit + push `main`. Write the REPORT per section 11 template.

## 10. Acceptance (report REAL results per item, no hand-waving)
- [ ] All tests pass (paste concise output).
- [ ] "No action API" scan test passes.
- [ ] All 9 tools callable from Cursor Agent with real data: `list_windows` lists actually open windows; `get_ui_tree` on Word and Excel shows named controls; `screenshot` returns a viewable image; `get_browser_tab_info` reads the correct active tab URL.
- [ ] Metrics: sample `get_ui_tree` output = ? KB; sample `screenshot` = ? KB.
- [ ] Commit hash pushed to `main`.

## 11. REPORT (mandatory — write after finishing section 10; copy the template below and fill it in)
## P1 REPORT
1. What was done: ...
2. Files created/modified (full paths): ...
3. Acceptance per item (pass/fail + brief evidence): ...
4. MCP registration: actual config file path on machine: ...
5. Quota metrics: get_ui_tree ? KB / screenshot ? KB
6. Remaining limits / issues encountered: ...
7. Proposed P2 content: ...
8. Commit <hash> pushed to main.
