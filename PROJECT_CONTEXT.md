# PROJECT_CONTEXT

Architecture and conventions for `tool-wl-app` (P1 + P2 + P3 + P4 + P5).

## 1. Goals (long-term)

Cursor Agent can **see** a Windows desktop — open apps, document structure,
the active browser tab, the actual DOM of the active web page, and the
CONTENTS of open Office files (Word / Excel / PowerPoint) — and **ACT
on the desktop** (click, type, focus windows) within a strict safety gate,
guiding a human user step by step through operations they perform manually.

P1-P3 are strictly read-only. P4 adds three action tools behind a
two-phase plan/execute safety gate. P5 makes the whole stack
**Vietnamese-safe end to end**, hardens focus safety with verified
focus-free window-message tiers, and cuts approval spam with standing
consent plus a batched `run_sequence`.

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
├── mcp_server.py     # FastMCP registration of 19 tools (9 P1 + 2 P2 + 3 P3 + 4 P4 + 1 P5)
├── ui_automation.py  # window list, foreground, UIA tree, control text (NFC-safe matching)
├── screenshots.py    # mss + JPEG resize helpers
├── ocr.py            # Windows.Media.Ocr (runs on a dedicated MTA thread; vi-VN preferred)
├── browser.py        # read active tab info from chrome/edge/firefox (address bar)
├── browser_dom.py    # P2: get_page_text / get_page_structure via WS bridge
├── clipboard.py      # read clipboard text or detect non-text formats
├── bridge_auth.py    # P3: per-install token for browser bridge
├── office_com.py     # P3: Office COM read-only tools
├── textnorm.py       # P5: NFC / case-folded Vietnamese-safe text comparison helpers
├── action_token.py   # P4/P5: HMAC-signed plan tokens (title-independent binding)
└── actions.py        # P4/P5: click_control, type_text, focus_window,
                      #        run_sequence + safety gate + focus-free message tiers

native_host/
├── __init__.py
├── tool_wl_host.py   # P2: standard Chrome Native Messaging host (stdio loop)

scripts/
├── register_mcp.py            # P1: merge tool-wl-app into ~/.cursor/mcp.json
├── register_native_host.py    # P2: write HKCU registry keys + manifest JSON
├── unregister_native_host.py  # P2: reverse of register_native_host.py
├── rotate_bridge_token.py     # P3: rotate the per-install browser bridge secret
├── console.py                 # P5: persistent Tkinter MCP console (launcher)
├── console.bat                # P5: one-click launcher for the console
├── console_core.py            # P5: transport + framing + tool-call plumbing
└── console_selftest.py        # P5: in-process FastMCP self-test (`--selftest`)

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
├── test_no_action_api.py        # static scan: zero action APIs except in actions.py
├── test_actions_gate.py         # P4: 20 gate unit tests (hermetic mocks)
├── test_action_focus_safety.py  # P4/P5: method ladder, message tiers, focus guards
├── test_action_token.py         # P4/P5: signed-token issue/verify + refusal taxonomy
├── test_actions_sequence.py     # P5: run_sequence binding + atomic abort semantics
├── test_textnorm.py             # P5: NFC / case-folded matching helpers
├── test_ui_automation.py        # list_windows / foreground / ui_tree / control_text + hermetic mock variants
├── test_screenshots.py          # screenshot size and shape constraints
├── test_ocr.py                # synthetic image OCR + truncation + vi-VN language reporting
├── test_clipboard_browser.py    # clipboard + get_browser_tab_info
├── test_mcp_registration.py     # all 20 tools are registered
├── test_browser_dom.py          # P2: caps, framing, WS round-trip, timeout envelope
├── test_office_com.py           # P3: Office COM mock coverage + no-mutating-API checks
├── test_quota.py               # per-tool envelope size budgets
├── probe_msg_input_candidates.py   # P5: live probe of WM_CHAR / EM_REPLACESEL / WM_PASTE
├── manual_actions_e2e.py      # P4: Notepad E2E (NOT in pytest; run manually)
├── manual_focus_safety_e2e.py # P4/P5: focus-flip / ladder / BM_CLICK E2E
└── manual_vietnamese_e2e.py   # P5: Vietnamese round-trip E2E on real Notepad
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

## 7. P4 — DELIVERED

### 7.1 Safety gate design

Every mutating action (`click_control`, `type_text`) is two-phase:

**Phase 1 — PLAN** (no `plan_id`): validates the target control,
checks all gate rules, and returns a plan `{plan_id, description,
expires_in_s: 300}`. The `description` is a human-readable sentence
stating exactly what will happen (window title, control name, action;
for `type_text` it also shows the current value and new text,
truncated to 200 chars). **Nothing is executed in Phase 1.**

**Phase 2 — EXECUTE** (with `plan_id` from Phase 1): re-checks all gate
rules, verifies the plan exists and is unexpired (300 s) and unused, then
performs the action and consumes the plan (single-use). Any mismatch →
structured error, no action.

`focus_window` has no plan phase, but the same allowlist + STOP check
applies.

**Gate rules checked in BOTH phases:**
1. `config/actions.json`: `enabled=true` AND target window's process in
   `allowed_processes`.
2. `config/STOP_ACTIONS` exists → refuse everything.
3. Target control's `Name` contains any `deny_labels` entry (case-insensitive).
4. Log every plan, execute, and refusal as one JSON line to
   `logs/actions.log` (created automatically, gitignored).

**Config file** (`config/actions.json`, created with defaults on first run):
```json
{
  "enabled": true,
  "allowed_processes": ["notepad.exe"],
  "deny_labels": ["delete", "format", "uninstall", "shut down", "pay now", "remove account"],
  "auto_execute_processes": []
}
```

`auto_execute_processes` is the P5 standing-consent list (§8.3 C1). It is
empty by default; entries bypass the plan phase entirely and return
`auto_executed: true`. Deny labels still apply to them.

### 7.2 Three action tools — `server/actions.py`

| Tool | What it does |
| ---- | ------------ |
| `click_control(window, automation_id?, name?, plan_id?)` | Activate a control via the method ladder (§7.5); reports which method was used. Phase 1 returns a plan. |
| `type_text(window, text, automation_id?, name?, plan_id?)` | Set an editable control's text via the method ladder (§7.5). Phase 1 returns a plan with current + new text. |
| `focus_window(window)` | Bring an allowlisted app's window to foreground via `SetForegroundWindow`. No plan. Returns honest error if Windows refuses foreground. |

**Targeting:** window by hwnd or title; control by `automation_id` and/or
exact `Name`. Resolution must yield **exactly one** control (0 or 2+ = error).

**Static scan:** `test_no_action_api.py` forbids all action APIs
(`Invoke`, `SetValue`, `SetForegroundWindow`, etc.) in ALL
`server/*.py` EXCEPT `actions.py`.

### 7.3 Tests

- **Gate unit tests** (`tests/test_actions_gate.py`): 20 hermetic mock
  tests covering every gate rule (execute without plan → plan returned,
  expired plan → refused, consumed plan → refused, different text →
  refused, non-allowlisted → refused, STOP_ACTIONS → refused,
  deny-label → refused, enabled=false → refused, focus_window gate
  refusals, SetForegroundWindow failure, plan/execute/refusal logging).
- **Focus-safety unit tests** (`tests/test_action_focus_safety.py`): hermetic
  tests for the method-ladder order (`value → wmtext → keyboard`,
  `Invoke → … → bmclick → coordinate`), `WM_SETTEXT` read-back verification
  (a mismatch fails rather than escalating), the focus guard's abort reasons
  (wrong foreground, foreign PID, focus outside target, no foreground),
  chunking at ≤20 chars with a verification before each chunk, the
  mid-type abort reporting the exact partial count, the coordinate-click
  hit-test guard, plan warnings present only for `SendInput` methods, and
  no-silent-escalation (`plan_method_changed`).
- **E2E** (`tests/manual_actions_e2e.py`): launches Notepad with a
  temp config, exercises plan+execute plan for `type_text`, reads back
  via `get_control_text`, tests `click_control` on menu items, and
  demonstrates a real gate refusal for a non-allowlisted process.
  Not part of pytest (touches real desktop).
- **Focus-safety E2E** (`tests/manual_focus_safety_e2e.py`): real desktop,
  real windows. Spawns two real Win32 host processes (`tests/win32_edit_host.py`)
  each with a genuine `Edit` control so keystroke delivery is measurable
  cross-process. Proves (a) a background focus-free write lands without
  changing the foreground or typing into the foreground app; (b) stealing the
  foreground mid-type aborts at a chunk boundary with ZERO characters in the
  stolen window; (c) real Notepad typing round-trips; (d)/(e) the BM_CLICK
  class gate, ladder order, chunk size, and method locking.
  Not part of pytest (touches real desktop).
- **Cross-process** (`tests/cross_process_verify.py`): drives the real MCP
  stdio path — plan on server A, execute on a fresh server B; same-process
  plan+execute; and all refusal buckets (tampered → UNKNOWN, forged →
  MALFORMED, reuse → CONSUME_RACE, wrong text → BINDING_MISMATCH).

### 7.5 The method ladder — focus safety

**Incident that motivated this** (P4 fix round 3): during a `type_text`
execute via the `SendInput` keyboard fallback, the user opened another app's
menu at the exact moment typing started, and the keystrokes went into the
**user's menu** instead of Notepad. `SendInput` follows the *global* foreground
focus, so the action hit whatever the user happened to be doing.

**Invariant:** an action may only ever affect the designated target app, and
**must abort rather than deliver input to the wrong window.**

Focus-free methods are preferred; `SendInput` is a guarded last resort.

| Tier | `type_text` | `click_control` | Touches foreground? |
| ---- | ----------- | --------------- | ------------------- |
| 1 | UIA `ValuePattern.SetValue` | UIA `Invoke` / `Toggle` / `SelectionItem` / `ExpandCollapse` | No |
| 2 | `WM_SETTEXT` on `NativeWindowHandle`, verified by read-back | `BM_CLICK` + `WM_COMMAND/BN_CLICKED` for Button-class controls | No |
| 3 | `WM_CHAR` / `EM_REPLACESEL` / `WM_PASTE` (P5 §8.2) | — | No |
| 4 | `SendInput` keyboard, ≤20-char chunks | `SendInput` coordinate click | Yes — guarded |

Tiers 1-3 are **window messages**, which are addressed to a specific
`HWND` and never consult or change the foreground window. Tier 3 is what
makes background typing possible at all — see §8.2.

**Key code (`server/actions.py`):**

- `type_method_ladder(ctrl)` / `click_method_ladder(ctrl)` — the full order
  for a control, best first (used by both PLAN and the unit tests).
- `_select_type_method` / `_select_click_method` — pick the head of the ladder.
- `_supports_wm_settext(hwnd)` — `Edit` / `RICHEDIT50W` / `RICHEDIT_CLASS` /
  `Static` / `Window` only. Windows 11 Notepad's `RichEditD2DPT` genuinely
  rejects `WM_SETTEXT` (verified: send returns 0, read-back empty), so the
  class gate is load-bearing, not decorative.
- `_supports_bm_click(hwnd)` — `Button` family only, and never for
  owner-drawn buttons (MSDN: `BM_CLICK` is ignored by them).
- `_do_wm_set_text` — write then **read back**; mismatch raises
  `Refusing to escalate to keyboard input` rather than silently falling
  through to `SendInput`.
- `_verify_typing_focus(target_win, target_pid, ctrl, chars_typed)` —
  returns `None` when safe, else the abort reason. Requires BOTH
  `GetForegroundWindow() == target` AND the focused control (via
  `GetGUIThreadInfo`, falling back to UIA's focused element) belonging to the
  target window/process.
- `_send_unicode_text_guarded(...)` — chunks by `TYPING_CHUNK_CHARS = 20`,
  re-verifying **before every chunk**; raises `FocusGuardAbort` carrying the
  honest partial count.
- `_click_guarded(...)` — re-resolves the rect, hit-tests
  `WindowFromPoint(center)` against the target PID, then re-checks the
  foreground immediately before the click.
- `_force_foreground` / `_restore_foreground` — `SetForegroundWindow` plus
  the `AttachThreadInput` fallback; the user's original foreground HWND is
  restored in a `finally` on success, refusal **and** abort.
- `SENDINPUT_METHODS = {"keyboard", "coordinate"}` and `SENDINPUT_WARNING` —
  drive the plan-text warning that only `SendInput` plans carry.

**Method locking:** the method is chosen at PLAN, recorded in the signed
token, and re-verified at EXECUTE. If the control's viable method changed in
between, the action refuses with `plan_method_changed` — no silent
escalation.

**Test seam:** `_MIDTYPE_INTERRUPT_HOOK` is a production-unused hook invoked
with the running character count immediately before each chunk, letting the
E2E steal the foreground mid-type deterministically.

### 7.6 Honest limitation

**True background keyboard input to arbitrary apps is impossible on
Windows.** `SendInput` is a global synthetic-input API; the OS routes it to
whichever window holds the foreground and no API can retarget it.

The guarantee we actually provide:

1. **Focus-free methods first** — for most controls the action runs against
   a background window and never touches focus at all.
2. **SendInput aborts instead of ever typing into the wrong window** — if we
   cannot *prove* the target owns the foreground, nothing is sent; if focus
   moves mid-type, typing stops at the next chunk boundary and the result
   reports exactly how many characters reached the target first.

Worst case is therefore a **partial, reported, target-only** delivery — never
a keystroke in the user's menu.

### 7.4 How the user edits the allowlist

Edit `config/actions.json` directly:

```jsonc
{
  "enabled": true,
  "allowed_processes": ["notepad.exe", "calc.exe", "YourApp.exe"],
  "deny_labels": ["delete", "format", "uninstall"]
}
```

To block all actions immediately, create an empty file named
`config/STOP_ACTIONS` (no content needed). Remove it to restore access.

## 8. P5 — DELIVERED

P5 has four sections. All four are implemented, unit-tested, and
live-E2E-verified.

### 8.1 Section A — Vietnamese end-to-end

**The problem.** Vietnamese has several Unicode representations of the
same visible string. Windows APIs, the clipboard, and some app
frameworks return **NFD (decomposed)** text — `"o"` + combining horn +
combining tilde — while a user typing Telex/Unikey or an agent sending
JSON produces **NFC (precomposed)** text. Comparing raw code points
silently fails: a window titled `"lịch thi.xlsx - Excel"` does not match
the NFC spelling of the same title.

**The fix.** New dependency-free module `server/textnorm.py`:

| Function | Purpose |
| -------- | ------- |
| `nfc(text)` | Canonical composition; ASCII fast-path. |
| `nfc_fold(text)` | NFC **then** `casefold()` — the canonical, case-insensitive key. |
| `same_text(a, b)` | Equality ignoring case and composition. |
| `contains_text(hay, needle)` | Substring test ignoring case and composition. |

NFC is applied *before* casefolding on purpose — `casefold` can change
string length (ß → ss), and the reverse order can leave one side
decomposed and the other composed.

Every user-visible string that participates in matching now goes through
`nfc_fold` on **both** sides:

- `ui_automation.find_window` — window title matching (fuzzy substring)
  and the `list_windows` sort key.
- `ui_automation.get_control_text` — `automation_id` / `Name` lookup.
- `actions._resolve_control` — control name and `automation_id` matching
  on the action path.

**Vietnamese title resolution.** With NFC folding, `"Lịch thi.xlsx - Excel"`
now resolves whether the app reports it decomposed or composed, and the
`get_ui_tree` / `click_control` / `type_text` paths all agree on which
control is meant.

**`KEYEVENTF_UNICODE` typing.** The synthetic-input path already
delivered text as UTF-16 code units via `KEYEVENTF_UNICODE`, so
Vietnamese characters are never routed through a keyboard layout. This
was verified rather than assumed — see §8.2, where the message tiers make
this the *last* resort anyway.

**OCR `vi-VN` preference.** `server/ocr.py` now prefers the `vi-VN`
recognizer language and falls back through the installed packs. Every OCR
response carries a `"language"` field naming the language pack actually
used, so the agent knows whether diacritics were recognised with a
Vietnamese model or guessed by the English one.

**Unit tests** (`tests/test_textnorm.py`, extended `tests/test_ocr.py`)
and a **live E2E** (`tests/manual_vietnamese_e2e.py`): types
`"Hà Nội những ngày đầu đông, sương mù giăng kín lối cũ."` into real
Notepad via `type_text`, reads it back through `get_control_text`, and
asserts **exact NFC equality** between what was sent and what came back.

### 8.2 Section B — focus safety, round 2 (focus-free message tiers)

Round 1 proved typing in the background is impossible with synthetic
input, and made the keyboard fallback abort rather than leak keystrokes
into the wrong window. Round 2 attacks the remaining weakness: **the
keyboard fallback still requires stealing foreground focus at all.**

The fix is to send text as **window messages** instead. A window message
is addressed to a specific `HWND`; it does not consult, change, or
depend on the foreground window. Windows 11 Notepad's editor
(`RichEditD2DPT`) was probed live and accepts all of them.

The ladder, focus-free first:

| Tier | `type_text` method | Mechanism | Focus |
| ---- | ------------------ | --------- | ----- |
| 1 | `value` | UIA `ValuePattern.SetValue` | untouched |
| 2 | `wmtext` | `WM_SETTEXT` on `NativeWindowHandle`, verified by read-back | untouched |
| 3 | `wmchar` | `WM_CHAR` per character | untouched |
| 3 | `emreplace` | `EM_REPLACESEL` (RichEdit family) | untouched |
| 3 | `wmpaste` | `WM_PASTE` with clipboard save + restore | untouched |
| 4 | `keyboard` | `SendInput` Unicode, per-character guarded | borrowed, restored |

`WM_CHAR` is the important one for Vietnamese: it delivers raw character
codes directly to the control, so it is **exact for diacritics** and
involves no IME and no keyboard-layout mapping.

**Clipboard save/restore.** `WM_PASTE` needs the clipboard to hold the
text, so the previous clipboard contents are saved first and restored
afterwards, in a `finally`. Both directions use
`_open_clipboard_with_retry` — a bounded retry loop that sleeps briefly
and retries, because RichEdit can hold the clipboard open transiently
and the first attempt reliably fails.

**Per-character focus guard (round-2 tightening).** The chunked
"re-verify every 20 characters" check is not good enough for tier 4:
focus could move *and* keystrokes could land between two checks. The
guard now re-verifies **before every single character**. Additionally,
`SendInput` typing is aborted immediately if the target is not the
foreground window **at the start of execute**, rather than discovering
it after stealing focus.

**Method equivalence classes.** The signed token binds the resolved
method, but `check_method_mismatch` treats these as internally
compatible, so a re-plan that lands on a sibling focus-free message is
not reported as tampering:

- message tier — `wmtext`, `wmchar`, `emreplace`, `wmpaste`
- synthetic-input tier — `keyboard`, `coordinate`

Anything outside those classes is still a hard `plan_method_changed`
refusal. This is a *documented* equivalence, not a loosening: the
classes still bind exactly.

**Probe and verification.** `tests/probe_msg_input_candidates.py` sends
each candidate message to real Notepad with Vietnamese text and checks
the read-back **and** that the clipboard survives intact.

**Unit tests** (`tests/test_action_focus_safety.py`) cover the full
ladder order, clipboard preservation, the per-character guard, the
abort-on-not-foreground-at-start path, and the equivalence classes.
**Live E2E** (`tests/manual_focus_safety_e2e.py`) proves zero focus
theft, the `BM_CLICK` class gate, and ladder order on real windows.

### 8.3 Section C — approval spam mitigation

The user was drowning in confirmations — ten or more "shall I proceed?"
questions for a routine "open Notepad, open a tab, type a line" task.

**C1 — standing consent.** `config/actions.json` gains:

```jsonc
{
  "enabled": true,
  "allowed_processes": ["notepad.exe"],
  "deny_labels": ["delete", "format", "uninstall", "shut down", "pay now", "remove account"],
  "auto_execute_processes": []
}
```

Any process listed in `auto_execute_processes` runs its action in a
**single call**, returning `"auto_executed": true` instead of a plan.
It is deliberately empty by default: adding a process is an explicit,
per-app grant, and the user must opt in. Deny labels are unaffected —
a `delete` button in an auto-execute app still refuses.

**C2 — `run_sequence`.** New MCP tool (tool #20) that runs up to **10**
ordered steps — `focus_window`, `click_control`, `type_text` — under
**one** plan and therefore one approval.

- The plan's token binds a canonical digest of the whole step list
  (`steps_hash`) **plus** each step's resolved target and method, so
  reordering a step, editing any text, or changing any target
  invalidates the token.
- Steps run in order; the **first failure aborts the rest**, and the
  result reports per-step status (`done` / `failed` / `skipped`).
- The safety gate is re-checked for **every** step at execute time, not
  just once for the batch.
- If every step targets an `auto_execute_processes` app, it runs
  directly with `auto_executed: true` and no plan at all.

**C3 — tool descriptions rewritten.** A shared `_APPROVAL_RULES` block is
now prepended to the action tool descriptions, stating the rule
plainly: **the plan is the approval** — present it once as one short
block and proceed; do not add a second question per step. The one
carve-out is explicit: anything matching a `deny_label` always needs its
own confirmation, every time.

**Tests:** `tests/test_actions_sequence.py` covers token binding
(digest stability, reorder detection, text-change detection) and atomic
abort semantics (abort on first failure, correct per-step statuses,
gate re-checked per step).

### 8.4 Section D — persistent console

Repeated one-shot MCP clients are slow and awkward for interactive
debugging, so P5 ships a small persistent console.

| File | Purpose |
| ---- | ------- |
| `scripts/console.py` | Tkinter UI entry point. |
| `scripts/console_core.py` | Transport, JSON-RPC framing, tool-call plumbing. |
| `scripts/console.bat` | One-click launcher. |
| `scripts/console_selftest.py` | In-process FastMCP self-test (`--selftest`). |

The UI uses a UTF-8-capable font (Segoe UI) so Vietnamese text renders
correctly in the transcript pane.

The self-test runs **in-process against `mcp`** rather than spawning a
child over stdio. That matters: a stdio-based self-test deadlocks when
run from a process that owns the same pipes, whereas the in-process
path exercises the real tool objects directly.

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

## 9. Quota-saving design notes

- `get_ui_tree` walks breadth-first with a hard node cap; when over the cap
  the response carries `"truncated": true`. Most calls return < 60 KB JSON.
- `screenshot` is JPEG q=60, long edge ≤ 1280. Most calls return 30–120 KB.
- OCR truncates to ≤ 200 words; full-screen OCR is intentionally more
  aggressive (≤ 400) for `find_text_on_screen` because matching needs a
  reasonable scan.
- `get_page_text` truncates at 8000 chars; on real pages measured 1–8 KB.
- `get_page_structure` truncates at 200 items; on real pages measured
  0.1–22.5 KB (worst case = GitHub repo home + Wikipedia long article).

## 10. Proposed P6 content

1. **Per-app symbolic prompts.** When `auto_execute_processes` contains
   a process, return the plan description anyway as a soft prompt —
   the user can intercept any single step if they wish, and the tool’s
   audit log already records the auto approval.
2. **DPI-aware multi-monitor capture.** P1’s screenshot caps the long
   edge at 1280 px. With mixed-DPI multi-monitor setups the scaled
   image can lose readable detail. P6 could let the user pick the
   source monitor and a target scale.
3. **Selector-based frame-aware DOM extraction.** `all_frames: true`
   in the manifest plus a selector argument to `get_page_text` /
   `get_page_structure` for embedded iframes.
4. **OCR per-word confidence.** Today the OCR tools return word
   positions but not confidence. A per-word `confidence` field would
   let the agent weigh its own recheck.
5. **`run_sequence_with_rollback`.** Today the first failure aborts
   the rest; adding a `rollback` key per step would let the user
   define a compensating sequence.

## 11. References

- Prompt: `prompt.md` (P1 + P2 + P3 + P4 brief).
- Repo: https://github.com/TranDaiHiep2006/tool-wl-app