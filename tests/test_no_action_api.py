"""Automated scan: assert the server code uses NO action APIs.

P1 is strictly read-only. The action-API symbols below come from
`uiautomation` and Win32. If any of them appears in `server/*.py`,
the test fails.

Run via: `python -m pytest tests/test_no_action_api.py -v`
"""

from __future__ import annotations

import os
import pathlib
import re
import unittest


# Action APIs that must never appear in P1 source code.
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
    """Return [(lineno, is_in_string), ...] marking which lines sit inside a string."""
    # Very simple state tracker: counts triple-quoted runs and # comments.
    lines = source.splitlines()
    states: list[tuple[int, bool]] = []
    in_string: str | None = None  # '"""' or "'''"
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if in_string:
            # In a multi-line string. Does this line close it?
            if in_string in line:
                # Close on this line; everything after the closer is not in_string
                in_string = None
            states.append((i + 1, True))
            continue
        # Look for an opening triple-quote
        opens = []
        for q in ('"""', "'''"):
            if q in line:
                opens.append(q)
        if not opens:
            states.append((i + 1, False))
            continue
        # Find first occurrence
        first_q = min(opens, key=lambda q: line.find(q))
        idx = line.find(first_q)
        # If line is only whitespace + comment + docstring, the whole line is a docstring
        prefix = line[:idx]
        if prefix.strip().startswith("#") or not prefix.strip():
            in_string = first_q
            states.append((i + 1, True))
        else:
            states.append((i + 1, False))
    return states


class TestNoActionAPI(unittest.TestCase):
    def test_no_action_api_in_source(self):
        offenders: list[tuple[str, int, str]] = []

        for py in sorted(HERE.glob("*.py")):
            text = py.read_text(encoding="utf-8")
            states = _scan_string_states(text)
            for (lineno, in_string), line in zip(states, text.splitlines()):
                if in_string:
                    continue
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                for sym in FORBIDDEN_SYMBOLS:
                    if re.search(rf"\b{re.escape(sym)}\b", stripped):
                        offenders.append((py.name, lineno, f"{sym!r} in: {stripped[:120]}"))
        if offenders:
            msg = "\n".join(f"{f}:{l}: {s}" for f, l, s in offenders)
            self.fail("Forbidden action API found in source:\n" + msg)


if __name__ == "__main__":
    unittest.main()