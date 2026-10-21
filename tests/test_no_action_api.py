"""Automated scan: assert the server code uses NO action APIs outside the
designated action module.

P1-P3 are strictly read-only. The action-API symbols below come from
`uiautomation` and Win32. They are ALLOWED in `server/actions.py` (P4)
but MUST NOT appear in any other `server/*.py`.

Run via: `python -m pytest tests/test_no_action_api.py -v`
"""

from __future__ import annotations

import io
import os
import pathlib
import re
import tokenize
import unittest


# Action APIs that must never appear in server/*.py EXCEPT actions.py.
FORBIDDEN_SYMBOLS = {
    # uiautomation actions
    "Click",
    "DoubleClick",
    "MiddleClick",
    "RightClick",
    "RightDragDrop",
    "MiddleDragDrop",
    "DragDrop",
    "SendKey",
    "SendKeys",
    "SendUnicodeChar",
    "SendMessage",
    "PostMessage",
    "PressKey",
    "ReleaseKey",
    "PressMouse",
    "ReleaseMouse",
    "MoveTo",
    "SetCursorPos",
    "SetForegroundWindow",
    "BringWindowToTop",
    "SetClipboardText",
    "SetWindowText",
    "SetWindowPos",
    "MoveWindow",
    "ShowWindow",
    "SwitchToThisWindow",
    "SetFocus",
    "SetActive",
    "SetTopmost",
    "SetWindowTopmost",
    "Maximize",
    "Minimize",
    "Restore",
    "MetroClose",
    "Hide",
    "Show",
    "ShowDesktop",
    "TerminateProcess",
    "TerminateProcessByName",
    "RunByHotKey",
    "RunScriptAsAdmin",
    # Win32 raw input
    "SendInput",
    "keybd_event",
    "mouse_event",
    "SetActiveWindow",
    # P3: Office COM mutating methods (must NEVER appear in server/*.py).
    # These are destructive: Save overwrites user data, SaveAs / Close /
    # PrintOut / Quit change user-visible state. P3 is read-only.
    "Save",
    "SaveAs",
    "SaveAs2",
    "Close",
    "Quit",
    "PrintOut",
    "PrintPreview",
    "NewDocument",
    "NewWorkbook",
    "NewPresentation",
    "AddSlide",
    "DeleteSlide",
    # We use this for COM but only with explicit "Word.Application" /
    # "Excel.Application" / "PowerPoint.Application" strings. Any other
    # prog-id or CreateObject() would launch an Office instance, which the
    # spec explicitly forbids ("never launch Office").
    "CreateObject",
    # P4: UIA action patterns — only allowed in server/actions.py.
    "Invoke",
    "Toggle",
    "SelectionItem",
    "ExpandCollapse",
    "ValuePattern",
    "SetValue",
    "IsInvokePatternAvailable",
    "IsTogglePatternAvailable",
    "IsSelectionItemPatternAvailable",
    "IsExpandCollapsePatternAvailable",
    "IsValuePatternAvailable",
    "GetInvokePattern",
    "GetTogglePattern",
    "GetSelectionItemPattern",
    "GetExpandCollapsePattern",
    "GetValuePattern",
    "IsInvokePressed",
    "ToggleState",
    "IsSelected",
    "IsExpanded",
    "CurrentValue",
    "CurrentIsValuePatternAvailable",
}

# Narrow, reviewed exemptions: (module, symbol) pairs that would otherwise be
# flagged but are demonstrably READ-ONLY uses.
#
# The purpose of this scan is to keep *mutating* action APIs out of the
# read-only modules. `GetValuePattern()` is a pure accessor — it returns the
# pattern object; writing requires a separate `SetValue()` call, which stays
# forbidden everywhere except actions.py. server/browser.py uses it once to
# READ the browser address bar so get_browser_tab_info can report the active
# URL (P1 feature). Exempting the getter does not exempt the setter, so a
# future `SetValue` in browser.py still fails this suite.
ALLOWED_READONLY_GETTERS = {
    # module      symbol            why
    ("browser.py", "GetValuePattern"),  # read the address bar URL (P1)
}

# We DO use these read-only names — confirm they are used, not banned.
ALLOWED = {
    "GetForegroundWindow",
    "GetForegroundControl",
    "GetWindowText",
    "GetWindowRect",
    "GetClipboardText",
    "GetClipboardFormats",
    "GetRootControl",
    "ControlFromHandle",
    "GetWindowLong",
    "GetProcesses",
}


HERE = pathlib.Path(__file__).resolve().parent.parent / "server"


def _scan_string_states(source: str) -> list[tuple[int, bool]]:
    """Return [(lineno, is_in_string), ...] marking which lines sit inside a string.

    Uses the real tokenizer rather than a hand-rolled triple-quote tracker.
    The previous hand-rolled version mis-handled *single-line* docstrings
    (``\"\"\"one-liner\"\"\"``): it treated the opening delimiter as opening a
    string that the same line's closing delimiter was supposed to close, but
    the state machine only ever *entered* a string and left it on a later
    line. Every one-line docstring therefore left `in_string` latched ON,
    which silently hid large regions of real code from the scan. A later
    multi-line docstring would then latch it further, and the scan result
    depended on the exact shape of unrelated docstrings.

    `tokenize` knows the real STRING tokens, so a string that opens and
    closes on the same line marks exactly that one line.
    """
    lines = source.splitlines()
    string_lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.STRING:
                # A multi-line string (or a line-continuation form) covers
                # every line it spans, inclusive.
                for lineno in range(tok.start[0], tok.end[0] + 1):
                    string_lines.add(lineno)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Unparseable source: fall back to "nothing is a string" so real
        # code is still scanned rather than silently skipped.
        return [(i + 1, False) for i in range(len(lines))]

    return [(i + 1, (i + 1) in string_lines) for i in range(len(lines))]


class TestNoActionAPI(unittest.TestCase):
    def test_no_action_api_in_source(self):
        offenders: list[tuple[str, int, str]] = []

        for py in sorted(HERE.glob("*.py")):
            # actions.py is the designated home for P4 action APIs — skip it.
            if py.name == "actions.py":
                continue
            text = py.read_text(encoding="utf-8")
            states = _scan_string_states(text)
            for (lineno, in_string), line in zip(states, text.splitlines()):
                if in_string:
                    continue
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                for sym in FORBIDDEN_SYMBOLS:
                    if (py.name, sym) in ALLOWED_READONLY_GETTERS:
                        continue
                    if re.search(rf"\b{re.escape(sym)}\b", stripped):
                        offenders.append((py.name, lineno, f"{sym!r} in: {stripped[:120]}"))
        if offenders:
            msg = "\n".join(f"{f}:{l}: {s}" for f, l, s in offenders)
            self.fail("Forbidden action API found in source:\n" + msg)


class TestStringStateScanner(unittest.TestCase):
    """Regression tests for _scan_string_states.

    The original hand-rolled tracker latched `in_string` ON for every
    single-line docstring, silently hiding large regions of real code from
    the scan. These tests pin the corrected behaviour so the scan can
    never silently stop scanning again.
    """

    def test_one_line_docstring_does_not_hide_following_code(self):
        src = 'def f():\n    """one liner."""\n    SendInput(1)\n'
        states = _scan_string_states(src)
        self.assertEqual(states[0][1], False)   # def line
        self.assertEqual(states[1][1], True)    # docstring line
        self.assertEqual(states[2][1], False)   # real code AFTER it

    def test_multi_line_docstring_marks_all_its_lines(self):
        src = (
            "def f():\n"
            '    """first\n'
            '    second\n'
            '    third"""\n'
            "    real()\n"
        )
        states = _scan_string_states(src)
        self.assertEqual([s[1] for s in states], [False, True, True, True, False])

    def test_forbidden_symbol_inside_docstring_is_ignored(self):
        src = 'def f():\n    """mentions SendInput in prose."""\n    pass\n'
        states = _scan_string_states(src)
        # The docstring line is a string -> skipped by the caller.
        self.assertTrue(states[1][1])

    def test_plain_code_is_not_a_string(self):
        src = "x = 1\ny = SendInput\n"
        states = _scan_string_states(src)
        self.assertEqual([s[1] for s in states], [False, False])

    def test_unparseable_source_does_not_hide_code(self):
        # A syntax error must not cause the scanner to return "all string",
        # which would silence the scan entirely.
        states = _scan_string_states("def broken(:\n    SendInput(1)\n")
        self.assertEqual([s[1] for s in states], [False, False])


class TestAllowedReadonlyGetters(unittest.TestCase):
    """The read-only getter exemptions must stay minimal and justified."""

    def test_exemptions_are_actually_present_in_source(self):
        # If the exempted symbol disappears from the code, the exemption is
        # dead weight — flag it so it gets cleaned up.
        for module, sym in ALLOWED_READONLY_GETTERS:
            path = HERE / module
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                sym, text,
                f"exemption for {sym!r} in {module} is stale — the symbol is "
                f"no longer used; remove it from ALLOWED_READONLY_GETTERS",
            )

    def test_exemptions_never_cover_mutators(self):
        # Guard rail: no exemption may name a mutating API.
        mutators = {"SetValue", "Invoke", "Toggle", "ExpandCollapse"}
        for module, sym in ALLOWED_READONLY_GETTERS:
            self.assertNotIn(
                sym, mutators,
                f"ALLOWED_READONLY_GETTERS must never exempt a mutating API "
                f"({sym!r} in {module})",
            )


if __name__ == "__main__":
    unittest.main()