"""Unit tests for the console command parser and dispatch layer.

Verified headlessly with pytest. Each case is the (raw input, expected
verb, expected args) triple; `rejects` covers the user-facing grammar
guards. Vietnamese text must survive the round-trip unchanged.

The dispatch/format tests use a stub MCP client, so the whole file runs
without touching the real desktop or spawning a server subprocess.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from console_core import (  # noqa: E402
    AutoApprove,
    CommandError,
    ConsoleSession,
    MCPError,
    format_clipboard,
    format_read,
    format_shot,
    format_ui,
    format_windows,
    parse_command,
)

HAPPY = [
    ("windows", "windows", {}),
    ("clip", "clipboard", {}),
    ("shot", "shot", {}),
    ("shot Notepad", "shot", {"window": "Notepad"}),
    ("shot Lịch thi", "shot", {"window": "Lịch thi"}),
    ("help", "help", {}),
    ("quit", "quit", {}),
    ("exit", "quit", {}),
    ("read Notepad", "read", {"window": "Notepad"}),
    ("read Notepad | Editor1", "read", {"window": "Notepad | Editor1"}),
    ("focus  Untitled - Notepad", "focus", {"window": "Untitled - Notepad"}),
    ("ui Calculator", "ui", {"window": "Calculator"}),
    (
        "type Notepad | hello world",
        "type",
        {"window": "Notepad", "text": "hello world"},
    ),
    (
        "type a | Hà Nội",
        "type",
        {"window": "a", "text": "Hà Nội"},
    ),
    (
        "type Lịch thi | lịch thi đã cập nhật",
        "type",
        {"window": "Lịch thi", "text": "lịch thi đã cập nhật"},
    ),
    ("click File | Open", "click", {"window": "File", "control": "Open"}),
]


REJECTS = [
    "",
    "  ",
    "rm_rf",          # unknown verb
    "type a",         # missing pipe
    "type | x",       # empty window part
    "type a |",       # empty text part
    "click a |",      # empty control part
    "click a",        # missing pipe
    "read",           # missing window
    "focus",          # missing window
    "ui",             # missing window
]


@pytest.mark.parametrize("raw,verb,args", HAPPY)
def test_parse_happy(raw: str, verb: str, args: dict) -> None:
    out_v, out_a = parse_command(raw)
    assert out_v == verb
    assert out_a == args


@pytest.mark.parametrize("raw", REJECTS)
def test_parse_rejects(raw: str) -> None:
    with pytest.raises(CommandError):
        parse_command(raw)


def test_window_may_contain_pipe_for_read() -> None:
    # The verb `read` ships the full rest as the window so the pipe form
    # `read Notepad | Editor1` stays in one positional argument.
    v, a = parse_command("read Notepad | Editor1")
    assert v == "read"
    assert a == {"window": "Notepad | Editor1"}


def test_parse_error_messages_are_actionable() -> None:
    with pytest.raises(CommandError) as exc:
        parse_command("type a")
    # The message must show the user the `|` convention they are missing.
    assert "|" in str(exc.value)

    with pytest.raises(CommandError) as exc:
        parse_command("nonsense")
    assert "help" in str(exc.value)


# ------------------------------------------------------------ dispatch ----

class _StubClient:
    """Records calls and replays canned payloads — no real MCP server."""

    def __init__(self, payloads: dict | None = None) -> None:
        self.payloads = payloads or {}
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        self.calls.append((name, dict(args or {})))
        if name not in self.payloads:
            raise AssertionError(f"unexpected tool call: {name}")
        return self.payloads[name]


def _session(client: _StubClient) -> ConsoleSession:
    sess = object.__new__(ConsoleSession)  # skip __init__ (real client)
    sess.root = ROOT
    sess.client = client
    sess.tool_names = []
    return sess


def test_cmd_shot_screen_mode_sends_mode_only() -> None:
    client = _StubClient({"screenshot": {"ok": True, "width": 2560, "height": 1440,
                                          "path": "shot.jpg"}})
    out = _session(client).cmd_shot()
    assert client.calls == [("screenshot", {"mode": "screen"})]
    assert "2560x1440" in out


def test_cmd_shot_window_mode_targets_the_window() -> None:
    client = _StubClient({"screenshot": {"ok": True, "width": 800, "height": 600,
                                          "path": "shot.jpg"}})
    _session(client).cmd_shot("Notepad")
    assert client.calls == [
        ("screenshot", {"mode": "window", "window": "Notepad"})
    ]


def test_cmd_ui_calls_get_ui_tree_and_lists_names() -> None:
    client = _StubClient(
        {
            "get_ui_tree": {
                "window_title": "Untitled - Notepad",
                "hwnd": 4242,
                "nodes": [
                    {"control_type": "Document", "name": "Text editor",
                     "automation_id": ""},
                    {"control_type": "Button", "name": "Save",
                     "automation_id": "AppBarButton"},
                ],
                "truncated": False,
            }
        }
    )
    out = _session(client).cmd_ui("Notepad")
    assert client.calls == [("get_ui_tree", {"window": "Notepad"})]
    assert "Text editor" in out and "AppBarButton" in out


def test_cmd_read_splits_the_pipe_into_two_tool_args() -> None:
    client = _StubClient(
        {"get_control_text": {"found": True, "text": "Xin chào",
                              "name": "Text editor",
                              "control_type": "Document"}}
    )
    out = _session(client).cmd_read("Notepad | Text editor")
    # The tool's real signature is (window, automation_id_or_name).
    assert client.calls == [
        ("get_control_text",
         {"window": "Notepad", "automation_id_or_name": "Text editor"})
    ]
    assert "Xin chào" in out


def test_cmd_read_without_a_selector_explains_itself() -> None:
    client = _StubClient({})
    out = _session(client).cmd_read("Notepad")
    assert "control" in out
    assert client.calls == []


def test_cmd_clipboard_renders_type_and_text() -> None:
    client = _StubClient({"read_clipboard": {"type": "text", "text": "lịch thi"}})
    out = _session(client).cmd_clipboard()
    assert client.calls == [("read_clipboard", {})]
    assert "lịch thi" in out and "[text]" in out


# ------------------------------------------------- the approval contract ---

def test_base_session_cannot_approve() -> None:
    # This is what makes the Tk window's override mandatory: without it the
    # `type` / `click` commands raise instead of asking the user.
    sess = object.__new__(ConsoleSession)
    with pytest.raises(NotImplementedError):
        sess.approve("PLAN: do something")


def _auto_session(plan: dict, execute: dict, answer: bool) -> AutoApprove:
    sess = object.__new__(AutoApprove)
    sess.root = ROOT
    sess.client = _StubClient()  # replaced below with a scripted client
    sess.tool_names = []
    sess.answer = answer
    sess.asked = []

    class _Scripted(_StubClient):
        def call_tool(self, name, args=None):
            self.calls.append((name, dict(args or {})))
            return execute if args and args.get("plan_id") else plan

    sess.client = _Scripted()
    return sess


def test_approved_action_executes_with_the_plan_id() -> None:
    sess = _auto_session(
        {"ok": True, "phase": "plan", "plan_id": "P1", "description": "type in Notepad"},
        {"ok": True, "phase": "execute", "method": "wmtext", "chars_typed": 4},
        answer=True,
    )
    out = sess.cmd_type("Notepad", "abc")
    assert len(sess.client.calls) == 2, "plan then execute, not more"
    assert sess.client.calls[0][0] == "type_text"
    assert "plan_id" not in sess.client.calls[0][1]
    assert sess.client.calls[1][1]["plan_id"] == "P1"
    assert "PLAN" in out and "OK" in out


def test_declined_action_runs_only_the_plan() -> None:
    sess = _auto_session(
        {"ok": True, "phase": "plan", "plan_id": "P2", "description": "click Open"},
        {"ok": True, "phase": "execute", "method": "invoke"},
        answer=False,
    )
    out = sess.cmd_click("File", "Open")
    assert len(sess.client.calls) == 1, "declining must not execute"
    assert "nothing was done" in out


def test_auto_executed_action_is_not_asked_about() -> None:
    sess = _auto_session(
        {"ok": True, "phase": "execute", "auto_executed": True, "method": "wmtext"},
        {"ok": True, "phase": "execute"},
        answer=False,   # even a "no" cannot block an auto-executed action
    )
    out = sess.cmd_type("Notepad", "abc")
    assert sess.asked == []
    assert "auto-executed" in out


# --------------------------------------------------------- formatters -----

def test_format_windows_marks_the_foreground_window() -> None:
    text = format_windows(
        [{"title": "Calculator", "process_name": "calc.exe", "is_foreground": True}]
    )
    assert "*" in text and "Calculator" in text


def test_format_shot_reports_a_server_error() -> None:
    assert "failed" in format_shot({"ok": False, "error": "no display"})


def test_format_read_reports_a_missing_control() -> None:
    assert "[!]" in format_read(
        {"found": False, "error": "not_found", "message": "no such control"}
    )


def test_format_clipboard_handles_empty_clipboard() -> None:
    assert "empty" in format_clipboard({"type": "text", "text": ""})


def test_format_ui_flags_truncation() -> None:
    text = format_ui(
        {"window_title": "W", "hwnd": 1, "nodes": [{"name": "a"}], "truncated": True}
    )
    assert "TRUNCATED" in text


def test_format_ui_survives_a_missing_nodes_key() -> None:
    assert format_ui({"window_title": "W"}).strip() != ""