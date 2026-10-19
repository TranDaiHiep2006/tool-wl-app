# P2 — tool-wl-app: Deep reading — browser DOM via extension + hardening

## 1. Role
You are a senior full-stack developer, experienced with browser extensions (MV3), Native Messaging, Python MCP servers, and Windows.

## 2. Background & Goal
- P1 done: read-only MCP server with 10 tools (UIA tree, screenshot, Windows OCR, browser tab info...). FIRST read D:\tool-wl-app\PROJECT_CONTEXT.md and D:\tool-wl-app\REPORT.md — they are the source of truth for the existing architecture. Do not re-derive it.
- P2 goal: DEEP browser reading — the agent must read actual page content (DOM), not just the tab URL. Still READ-ONLY. No clicking/typing.
- Repo: https://github.com/TranDaiHiep2006/tool-wl-app, work in D:\tool-wl-app, pull latest, commit + push to main.
- Communicate with me in English. P1 quota rules still apply: batch commands, read with purpose, fix in clusters, size-limited outputs.

## 3. Scope — DO and DO NOT
DO:
A. Browser extension (MV3, unpacked/dev mode) + Native Messaging host, adding 2 MCP tools:
   1. `get_page_text()` — main readable text of the active tab (best-effort boilerplate removal: skip nav/footer/ads); max ~8000 chars, truncate + report.
   2. `get_page_structure()` — pruned interactive structure: headings, links (text + href), buttons, inputs (label + type); max ~200 elements, truncate + report.
   Target Chromium first (Chrome/Edge). Firefox best-effort only — if not cleanly feasible, report why and ship Chromium-only.
B. Hardening:
   - Fix the 2 desktop-state-sensitive pytest failures (they fail on rerun when no windows are open) — make them hermetic with mocks, or skip with a clear reason. The suite must pass regardless of desktop state.
   - Clean up: remove tests/raw_smoke.py and D:\tool-wl-app\logs\raw_server.* (or relocate properly) — decide, do it, document it.
DO NOT:
- Any action tools (click/type/focus) — still forbidden.
- Office COM reading — that is P3, do not start it.
- Publishing the extension to any web store.

## 4. Technical Decisions — FINAL where stated
- Extension: MV3. Content script extracts DOM → background service worker → Native Messaging (length-prefixed JSON over stdin/stdout) → Python native host script in this repo.
- Native host runs on Python 3.11 (same interpreter/venv as the MCP server). Register per-browser via HKCU registry keys — provide scripts/register_native_host.py AND scripts/unregister_native_host.py (must be reversible).
- New tools live in the existing FastMCP server as server/browser_dom.py. English descriptions, size limits, and a priority line ("prefer get_page_text over screenshot when reading page content").
- If the extension is not installed/connected, tools must return a clear actionable error (how to load the unpacked extension + run the register script) within a 10s timeout — never hang the server.
- If any decision above is infeasible on this machine: REPORT before changing approach (same rule as P1).

## 5. Common Pitfalls — read before coding
- MV3 service workers are ephemeral — never keep DOM state there; pull fresh from the content script on every request.
- Native Messaging registry path must match the exact installed browser channel — verify on this machine, do not guess.
- Never block the MCP server event loop waiting for the browser — timeouts everywhere.
- Prune aggressively: a raw modern page DOM can be MBs — never return it raw.

## 6. Workflow (follow in order)
1. Read PROJECT_CONTEXT.md + REPORT.md.
2. Build extension/ (manifest.json, content.js, background.js) + native_host/ + register/unregister scripts.
3. Add server/browser_dom.py with the 2 tools; wire into mcp_server.py.
4. Tests: unit tests for pruning/limits + native message framing; fix the 2 flaky P1 tests; full suite green regardless of desktop state.
5. Manual end-to-end: load unpacked extension in Chrome/Edge, run register script, restart Cursor, call get_page_text/get_page_structure from Cursor Agent on 2–3 real pages (e.g., a news article and a GitHub page) and confirm readable output.
6. Update README.md, PROJECT_CONTEXT.md (new tools, limits, install steps), REPORT.md (append a P2 section).
7. Measure sample output sizes of the 2 new tools (KB).
8. Commit + push main. Write the REPORT per section 8 template.

## 7. Acceptance (report REAL results per item)
- [ ] pytest fully green on a clean run AND on rerun with no windows open.
- [ ] Extension loads unpacked with no errors; register AND unregister scripts work.
- [ ] From Cursor Agent: get_page_text returns readable article text (within ~8KB); get_page_structure lists headings/links/buttons (within ~200 items).
- [ ] Extension missing → tools return clear install instructions within 10s, no hang.
- [ ] Metrics: sample sizes of both new tools in KB.
- [ ] Commit hash pushed to main.

## 8. REPORT (mandatory — write after section 7; copy the template below and fill it in)
## P2 REPORT
1. What was done: ...
2. Files created/modified (full paths): ...
3. Acceptance per item (pass/fail + brief evidence): ...
4. New tool quota metrics: get_page_text ? KB / get_page_structure ? KB
5. Remaining limits / issues: ...
6. Proposed P3 content: ...
7. Commit <hash> pushed to main.
