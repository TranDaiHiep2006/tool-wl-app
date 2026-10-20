Read PROJECT_CONTEXT.md and REPORT.md first for full project context. This is phase P4: add ACTION capabilities to the MCP server, behind a strict safety gate. All 15 existing read-only tools and their behavior must stay unchanged. Commit and push to main when done, and update PROJECT_CONTEXT.md and README.md to document P4 (new tools, the gate, the config file, how the user edits the allowlist).

GOAL — add 3 action tools (18 tools total) that operate apps via UI Automation patterns, never via raw screen coordinates:
1. click_control — activate a control (button, menu item, checkbox...) via its UIA pattern.
2. type_text — set the text of an editable control via ValuePattern.
3. focus_window — bring an allowlisted app's window to the foreground.

THE SAFETY GATE (core of this phase — implement exactly)
Every mutating action (click_control, type_text) is two-phase:
- Phase 1, PLAN: called without a plan_id, the tool only VALIDATES: it resolves the target to exactly one control, checks all gate rules below, and returns a plan {plan_id, description, expires_in_s: 300}. description is one human-readable sentence stating exactly what will happen: window title, control name, action; for type_text it also includes the control's CURRENT value (truncated to 200 chars) and the new text (truncated), because SetValue overwrites. NOTHING is executed in phase 1.
- Phase 2, EXECUTE: called again with the same arguments plus that plan_id. The server re-checks all gate rules, verifies the plan exists, is unexpired (300s), unused, and bound to the same tool + same resolved control + same arguments (for type_text, identical text), then performs the action and consumes the plan (single-use). Any mismatch: structured error, no action.
Gate rules, checked in BOTH phases:
- Master config config/actions.json (create with these defaults if missing): enabled=true, allowed_processes=["notepad.exe"], deny_labels=["delete", "format", "uninstall", "shut down", "pay now", "remove account"].
- enabled=false, or target window's process not in allowed_processes: refuse.
- A file named config/STOP_ACTIONS exists: refuse everything.
- Target control's Name contains any deny_labels entry (case-insensitive): refuse.
- Log every plan, execute and refusal as one JSON line appended to logs/actions.log (create the dir, gitignore logs/): timestamp, event, tool, process, window title, control name, detail/result.
focus_window needs no plan, but allowlist + STOP check + logging still apply. Windows may refuse foreground changes from a background process: if SetForegroundWindow fails, return an honest error, never fake success.

TARGETING
Same selector style as get_control_text: window by hwnd or process_name; control by automation_id and/or exact name and/or control_type. Resolution must yield exactly one control: 0 or 2+ matches = structured error, no plan. click_control tries patterns in this order: Invoke, Toggle, SelectionItem, ExpandCollapse, and reports which pattern was used; none supported = error. NO fallback to coordinate clicking; no SendInput anywhere in this phase. type_text requires ValuePattern and uses SetValue.

TOOL DESCRIPTIONS
The MCP descriptions of the 3 new tools must instruct the calling agent: this tool ACTS on the user's real desktop; always call without plan_id first, present the returned plan description to the user, and execute only after the user agrees; never chain plan+execute silently.

TESTS (previous suite must stay green)
- Gate unit tests with mocked UIA/config: execute without plan refused; expired plan refused; reusing a consumed plan refused; executing a plan with different text refused; non-allowlisted process refused; STOP_ACTIONS present refused; deny-label control refused; enabled=false refused.
- Extend the static scan (tests/test_no_action_api.py): action APIs (Invoke, SetValue, SendInput and friends) may exist ONLY in the new action module(s); all read-only modules stay clean.
- E2E script outside the default pytest run (like tests/verify_sw_chrome.py): tests/manual_actions_e2e.py launches Notepad (test may use its own temp config; shipped defaults unchanged), plan+execute type_text of a known string, reads the text back through the existing read path and asserts equality; then plan+execute a click on a pattern-supporting control and assert its effect. If Notepad offers no invocable control, use Calculator buttons for the click part (test config only). The script prints clear PASS/FAIL evidence.
- Final full pytest run: all green.

OUT OF SCOPE (do not build): raw coordinate clicks, drag, hotkeys via SendInput; writing to Office via COM (Office stays read-only: no Save, no setting cells, nothing); DOM actions in the browser extension (extension stays read-only); macros or batched action sequences; anything elevated.

REPORT (append to REPORT.md and paste in this chat): the gate design as implemented, config format, the 3 tools + new total count, full test results including the E2E evidence (what was typed/clicked, what was read back), one real refusal output shown verbatim (e.g. non-allowlisted process), the commit hash, and manual verification steps for the user in Cursor using Notepad.
