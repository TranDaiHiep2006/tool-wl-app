This is phase P5: usability hardening of tool-wl-app, driven by four problems the user hit in real testing. Keep everything from P1-P4 working (full suite stays green); do NOT touch extension/ in this phase. Commit and push to main; update PROJECT_CONTEXT.md and README.md.

A. VIETNAMESE END-TO-END
- The keyboard typing fallback must inject the exact final string as Unicode key events (KEYEVENTF_UNICODE). It must never type via virtual-key/typewrite-style input that routes through the user's IME (Telex/Unikey garbles composed Vietnamese). If the current implementation does anything else, fix it.
- All matching of window titles and control names (window resolution, control resolution, find_window) must normalize both sides to Unicode NFC and compare case-insensitively in a Vietnamese-safe way. Real cases that must work: a window titled 'lịch thi.xlsx - Excel', 'zhezhi.docx - Word'.
- OCR tools: if a Vietnamese (vi-VN) OCR language pack is installed, use it when it is available and say in the result which language was used; otherwise fall back to the current behavior.
- E2E proof on this machine: type the sentence 'Hà Nội những ngày đầu đông, sương mù giăng kín lối cũ.' into Notepad, read it back through the read path, and assert exact equality (NFC). Also resolve and read a window whose title contains Vietnamese diacritics.

B. FOCUS SAFETY, ROUND 2
The round-1 chunk guard still leaks: checks happen per 20-char chunk, so switching windows mid-chunk lets the rest of the chunk land in the wrong app. Attack from both sides:
- Probe message-based character input for controls that reject WM_SETTEXT (especially Windows 11 Notepad's RichEditD2DPT): candidates are a WM_CHAR sequence to the control HWND, EM_REPLACESEL, and WM_PASTE with clipboard save+restore. Verify each candidate by read-back on THIS machine's Notepad. A candidate joins the ladder (between tier 2 and tier 3) only if read-back proves it writes correctly; report which candidates worked and which did not. If WM_PASTE is used, the user's clipboard content must be restored afterwards.
- SendInput hardening: verify the foreground before EVERY character (not per chunk) and abort on the first mismatch. If the target window is NOT the foreground at execute start, abort with a specific error instead of stealing focus and racing the user. All round-1 guarantees stay (abort reporting, partial counts, foreground restore).
- E2E: start a long type into Notepad via SendInput, flip the foreground at a controlled point mid-type; assert typing stops immediately, the reported count matches what Notepad received, and the other window received at most the single in-flight character (report the actual number).

C. STOP THE APPROVAL SPAM
The user needed 10+ confirmations just to open Notepad, open a tab and type. Fix structurally:
- config/actions.json gains auto_execute_processes (default: []). A process in BOTH allowed_processes and auto_execute_processes lets click_control/type_text execute in ONE call without a plan; the response must carry auto_executed=true. All other rules still apply (deny_labels, STOP_ACTIONS, logging).
- New tool run_sequence (19 tools total becomes 20): input is an ordered list of up to 10 steps; each step is focus_window, click_control or type_text with the existing selector/argument shapes. ONE plan covers the whole sequence: the token binding is a hash of the canonical step list plus each step's resolved target and method. On execute, steps run in order, gate rules re-checked per step; first failure aborts the sequence and the result reports per-step status (done/failed/skipped) — never silently continue. If EVERY step's process is auto-executable, the sequence may run directly without a plan.
- Rewrite the action tools' descriptions: the plan is the ONLY approval step — present it as one short block; never ask extra confirmation questions for routine steps; after the user's single go-ahead for a task, proceed step by step, summarizing each plan in one line. Only destructive-looking actions (deny_labels territory) always need their own explicit approval.
- Tests: auto-execute single-call path (and its refusals: STOP present, deny label, not allowlisted); sequence token binding (a tampered step list is refused); sequence abort semantics (step 2 of 3 fails, step 3 skipped, statuses reported).

D. PERSISTENT CONSOLE WINDOW
Build scripts/console.py plus scripts/console.bat (double-click launcher). A small Tkinter window that stays open waiting for commands:
- It spawns ONE MCP server subprocess (same command and environment as the Cursor registration: venv python, -m server.mcp_server, PYTHONPATH set) and keeps it alive for the whole session; closing the window terminates the child cleanly. Implement a minimal stdio JSON-RPC MCP client (initialize, tools/call) inside the console.
- UI: an output pane and an input line with history (Up arrow). Always-on-top toggle. UTF-8 throughout; use a Vietnamese-capable font (Segoe UI). Errors print inline and the prompt returns — the window never exits after a command.
- Command syntax (also shown by a built-in 'help' command):
    windows
    shot
    read <window title substring>
    type <window title substring> | <text>
    click <window title substring> | <control name>
    focus <window title substring>
  Read commands call the matching read tools and print a compact result (truncate long text). Action commands run plan, then ask 'Approve? y/n' inline in the console, then execute — the whole flow stays in this window. Auto-executable processes skip the y/n.
- Testability: unit tests for the command parser; a --selftest mode that runs the parser plus a live server round-trip (windows, then shot) without showing the window and prints PASS/FAIL. The selftest is outside the default pytest run.

OUT OF SCOPE: any LLM inside the console; Office COM writes; browser extension changes; changes to read-tool behavior beyond section A.

REPORT (append to REPORT.md and paste in chat): per-section outcomes with evidence — the Vietnamese round-trip comparison and the diacritic-title resolution; which message-input candidates worked on Notepad (or that none did) plus the focus-flip E2E numbers; a demo transcript of run_sequence doing open-tab-then-type with ONE approval, and of an auto-executed action; the console selftest output; full pytest results; commit hash; and short user instructions (how to add notepad.exe to auto_execute_processes, how to launch the console).