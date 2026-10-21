"""Unit tests for the console command parser.

Verified headlessly with pytest. Each case is the (raw input, expected
verb, expected args) triple; `rejects` covers the user-facing grammar
guards. Vietnamese text must survive the round-trip unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from console_core import CommandError, parse_command  # noqa: E402


HAPPY = [
    ("windows", "windows", {}),
    ("shot", "shot", {}),
    ("help", "help", {}),
    ("quit", "quit", {}),
    ("exit", "quit", {}),
    ("read Notepad", "read", {"window": "Notepad"}),
    ("read Notepad | Editor1", "read", {"window": "Notepad | Editor1"}),
    ("focus  Untitled - Notepad", "focus", {"window": "Untitled - Notepad"}),
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
    "shot a",         # takes no arguments
    "focus",          # missing window
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
