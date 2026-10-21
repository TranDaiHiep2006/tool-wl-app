"""Self-test for the tool-wl-app console (in-process, no subprocess).

Runs the parser, format helpers, and the MCP tool registration via the
in-process FastMCP API (no transport subprocess). Prints PASS/FAIL for
every step and exits 0 only when everything passes.

Run:
    python scripts/console.py --selftest
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from console_core import (  # noqa: E402
    CommandError,
    format_action,
    format_read,
    format_windows,
    parse_command,
)


def _section(title: str) -> None:
    bar = "=" * 72
    print(f"\n{bar}\n  {title}\n{bar}")


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"   {detail}"
    print(line)
    return ok


def run_selftest(root: Path) -> int:
    failures = 0

    # ---- parser tests --------------------------------------------------
    _section("parser")
    cases = [
        ("windows", "windows", {}),
        ("shot", "shot", {}),
        ("help", "help", {}),
        ("quit", "quit", {}),
        ("exit", "quit", {}),
        ("read Notepad", "read", {"window": "Notepad"}),
        ("focus  Untitled - Notepad", "focus", {"window": "Untitled - Notepad"}),
        (
            "type Notepad | hello world",
            "type",
            {"window": "Notepad", "text": "hello world"},
        ),
        ("click File | Open", "click", {"window": "File", "control": "Open"}),
        ("type a | Hà Nội", "type", {"window": "a", "text": "Hà Nội"}),
    ]
    for raw, want_v, want_a in cases:
        try:
            v, a = parse_command(raw)
            ok = v == want_v and a == want_a
        except CommandError:
            ok = False
        if not _check(f"parse {raw!r} -> {(want_v, want_a)}", ok):
            failures += 1

    rejects = [
        "",
        "  ",
        "rm_rf",
        "type a",
        "type | x",
        "type a |",
        "click a |",
        "click a",
        "read",
        "shot a",
    ]
    for raw in rejects:
        try:
            parse_command(raw)
            refused = False
        except CommandError:
            refused = True
        if not _check(f"reject {raw!r}", refused):
            failures += 1

    # ---- format helpers ------------------------------------------------
    _section("format helpers")
    if not _check(
        "format_windows empty",
        "(no visible windows)" in format_windows([]),
    ):
        failures += 1
    if not _check(
        "format_windows with Notepad line",
        "Notepad"
        in format_windows(
            [
                {
                    "title": "Notepad",
                    "is_foreground": True,
                    "process_name": "notepad.exe",
                }
            ]
        ),
    ):
        failures += 1
    if not _check(
        "format_read truncates long text",
        " …" in format_read(
            {
                "found": True,
                "text": "x" * 5000,
                "name": "Doc",
                "control_type": "DocumentControl",
            }
        ),
    ):
        failures += 1
    if not _check(
        "format_action plan",
        "PLAN"
        in format_action({"ok": True, "phase": "plan", "description": "do thing"}),
    ):
        failures += 1
    if not _check(
        "format_action refusal",
        "gate_refused"
        in format_action(
            {"ok": False, "error": "gate_refused", "message": "nope"}
        ),
    ):
        failures += 1

    # ---- in-process MCP via FastMCP API ---------------------------------
    _section("MCP in-process (FastMCP API, no subprocess transport)")
    try:
        from server.mcp_server import mcp as _mcp
        from server import actions as _amod
        from server.textnorm import nfc_fold, same_text

        async def _list():
            tools = await _mcp.list_tools()
            return [t.name for t in tools]

        names = asyncio.run(_list())
        if not _check(
            f"MCP tool count ({len(names)})",
            len(names) >= 18,
            f"tools: {', '.join(sorted(names))}",
        ):
            failures += 1
        if not _check("run_sequence registered", "run_sequence" in names):
            failures += 1
        if not _check("click_control registered", "click_control" in names):
            failures += 1

        # A plan response from run_sequence (no live desktop needed).
        res = _amod.run_sequence([])
        if not _check(
            "run_sequence rejects empty steps",
            not res.get("ok") and res.get("error") == "invalid_sequence",
            f"error={res.get('error')}",
        ):
            failures += 1

        cfg = _amod.ActionConfig()
        if not _check(
            "ActionConfig.auto_execute_processes exists",
            hasattr(cfg, "auto_execute_processes"),
        ):
            failures += 1
        if not _check(
            "ActionConfig.is_auto_executable exists",
            hasattr(cfg, "is_auto_executable"),
        ):
            failures += 1

        if not _check(
            "nfc_fold Vietnamese casefold",
            nfc_fold("lịch thi") == nfc_fold("LỊCH THI"),
        ):
            failures += 1
        if not _check(
            "same_text ignores diacritic composition",
            same_text("Hà Nội", "hà nội"),
        ):
            failures += 1

    except Exception:
        traceback.print_exc()
        _check("MCP in-process", False, "exception raised")
        failures += 1

    # ---- summary -------------------------------------------------------
    _section("summary")
    if failures == 0:
        print("\n  ALL CHECKS PASSED")
        return 0
    print(f"\n  {failures} CHECK(S) FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(run_selftest(Path(__file__).resolve().parent.parent))
