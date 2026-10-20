# P3 — tool-wl-app: Read Office file contents via COM + lock down the browser bridge

## 1. Role
You are a senior Python developer, experienced with Windows COM automation (pywin32), WebSocket security, and MCP servers.

## 2. Background & Goal
- P1: read-only MCP server (UIA, screenshot, OCR). P2: MV3 extension + get_page_text/get_page_structure via a localhost WebSocket bridge (ws://127.0.0.1:57532). FIRST read D:\tool-wl-app\PROJECT_CONTEXT.md and D:\tool-wl-app\REPORT.md — they are the source of truth. Do not re-derive the architecture.
- P3 has two goals: (A) read the CONTENTS of open Office files (Word/Excel/PowerPoint) — still READ-ONLY; (B) decide and lock down the browser bridge transport (section 3B).
- Repo: https://github.com/TranDaiHiep2006/tool-wl-app, work in D:\tool-wl-app, pull latest, commit + push to main.
- Communicate with me in English. P1/P2 quota rules still apply: batch commands, read with purpose, fix in clusters, size-limited outputs.

## 3. Scope — DO and DO NOT
DO:
A. Office COM reading (pywin32, read-only, never save/modify):
   1. `read_word_text()` — full text of the ACTIVE Word document via GetActiveObject("Word.Application"). No active doc → clear message.
   2. `read_excel_values(sheet=None, max_rows=200, max_cols=50)` — values of the used range (or the named sheet) of the ACTIVE workbook. Trim empty trailing rows/cols. Truncate + report.
   3. `read_powerpoint_text()` — text of all slides of the ACTIVE presentation, as a per-slide list. Truncate + report.
   If Office is not running or no document is open → return a clear message. NEVER launch Office, never crash the server.
B. Browser bridge transport decision — pick ONE and implement it:
   - Option 1 (recommended): keep the WebSocket bridge but lock it down: bind strictly to 127.0.0.1, require a per-install secret token (generated once, stored in a local config file; the extension sends it on connect; reject connections without it), remove any `--allow-any-origin` dev bypass from default paths, document the token location.
   - Option 2: make Native Messaging the live transport (reuse the P2 native_host + registry scripts) and remove the WebSocket bridge entirely.
   Document the choice and WHY in PROJECT_CONTEXT.md. Either way: no unauthenticated localhost listener left open by default.
DO NOT:
- Any action tools (click/type/focus) — still forbidden.
- Saving or modifying Office files — read-only means read-only. The COM code must never call Save/SaveAs/Close-with-save.
- Starting Office applications — attach only to already-running instances.

## 4. Technical Decisions — FINAL where stated
- pywin32 for COM. Use GetActiveObject; never CreateObject (do not launch).
- New tools in server/office_com.py, wired into mcp_server.py. English descriptions + size limits + priority line ("prefer read_word_text / read_excel_values over OCR/screenshot for Office content").
- COM hygiene: release objects properly so no WINWORD.EXE/EXCEL.EXE handle is leaked by our reads.
- Vietnamese Unicode text must survive end-to-end — test with Vietnamese content in each app.
- If Office COM is unusable on this machine (not installed, Click-to-Run quirks): REPORT before changing approach (same rule as P1/P2).

## 5. Common Pitfalls — read before coding
- GetActiveObject raises when the app isn't running — catch it and return the "not running" message.
- Large workbooks: never dump whole sheets — used-range + caps only.
- Never block the MCP server event loop on COM calls that might hang — timeouts where feasible.
- The bridge lockdown must not break the existing P2 tools — re-run their end-to-end after the change.

## 6. Workflow (follow in order)
1. Read PROJECT_CONTEXT.md + REPORT.md.
2. Implement server/office_com.py (3 tools); wire into mcp_server.py.
3. Implement the bridge decision (section 3B); update/remove code accordingly; update README install steps.
4. Tests: unit tests with mocks for COM + caps; test_no_action_api must still pass (extend its forbidden list with Save/SaveAs if missing); full suite green on clean run and rerun.
5. Manual end-to-end: with Word/Excel/PowerPoint open on real files (include Vietnamese text), restart Cursor, call the 3 tools from Cursor Agent and confirm correct content (spot-check 3 Excel cells).
6. Re-verify get_page_text/get_page_structure still work after the bridge change.
7. Update README.md, PROJECT_CONTEXT.md, REPORT.md (append P3 section).
8. Measure sample output sizes (KB).
9. Commit + push main. Write the REPORT per section 8 template.

## 7. Acceptance (report REAL results per item)
- [ ] pytest fully green (clean run + rerun).
- [ ] test_no_action_api passes (no action APIs, no Save/SaveAs in server code).
- [ ] From Cursor Agent with Office apps open: read_word_text returns document text; read_excel_values returns correct cell values (3 spot-checked cells); read_powerpoint_text lists slide texts. Vietnamese text intact.
- [ ] Office not running → clear "not running" message, server stays up.
- [ ] Bridge: no unauthenticated localhost listener by default — verify by connecting without token → rejected (or WS bridge removed entirely).
- [ ] Metrics: sample sizes of the 3 new tools in KB.
- [ ] Commit hash pushed to main.

## 8. REPORT (mandatory — write after section 7; copy the template below and fill it in)
## P3 REPORT
1. What was done: ...
2. Files created/modified (full paths): ...
3. Acceptance per item (pass/fail + brief evidence): ...
4. New tool quota metrics: read_word_text ? KB / read_excel_values ? KB / read_powerpoint_text ? KB
5. Bridge decision: chosen option + why: ...
6. Remaining limits / issues: ...
7. Proposed P4 content: ...
8. Commit <hash> pushed to main.
