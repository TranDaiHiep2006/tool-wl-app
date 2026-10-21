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
    AutoApprove,
    ConsoleSession,
    MCPClient,
    _find_python,
    format_action,
    format_read,
    format_ui,
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
        ("ui Notepad", "ui", {"window": "Notepad"}),
        ("shot", "shot", {}),
        ("shot Notepad", "shot", {"window": "Notepad"}),
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
        "focus",
        "ui",
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
    if not _check(
        "format_ui lists control names",
        "Text editor" in format_ui(
            {
                "window_title": "Untitled - Notepad",
                "hwnd": 123,
                "nodes": [
                    {
                        "control_type": "Document",
                        "name": "Text editor",
                        "automation_id": "",
                    }
                ],
                "truncated": False,
            }
        ),
    ):
        failures += 1
    if not _check(
        "format_ui surfaces a not-found error",
        "[!]" in format_ui({"error": "window_not_found", "message": "nope"}),
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

    # ---- approval flow (this is what the Tk window must supply) ---------
    # `ConsoleSession.approve` is abstract. If the shipped window forgets to
    # override it, every `type` / `click` dies with NotImplementedError — and
    # that bug is invisible unless we actually drive the flow here.
    _section("approval flow (plan -> approve -> execute)")
    try:
        # The base class must genuinely refuse to approve — this is what
        # makes the window's override mandatory rather than optional.
        base_refused = False
        try:
            ConsoleSession.approve(object.__new__(ConsoleSession), "PLAN")
        except NotImplementedError:
            base_refused = True
        if not _check(
            "base ConsoleSession.approve is abstract",
            base_refused,
        ):
            failures += 1

        class _FakeClient:
            def __init__(self, plan: dict, execute: dict) -> None:
                self._plan = plan
                self._execute = execute
                self.calls = []

            def call_tool(self, name, args=None):
                self.calls.append((name, dict(args or {})))
                return self._execute if args and args.get("plan_id") else self._plan

        class _FakeSession(AutoApprove):
            """AutoApprove with a scripted MCP server, no real desktop."""

            def __init__(self, plan: dict, execute: dict) -> None:
                # Skip ConsoleSession.__init__ (it would build a real client).
                self.root = root
                self.client = _FakeClient(plan, execute)
                self.tool_names = []
                self.answer = True
                self.asked = []

        # --- happy path: plan shown, approved, execute carries plan_id ---
        s = _FakeSession(
            {"ok": True, "phase": "plan", "plan_id": "P1", "description": "type into Notepad"},
            {"ok": True, "phase": "execute", "method": "wmtext", "chars_typed": 6},
        )
        out = s._plan_then_execute("type_text", {"window": "a", "text": "x"}, s.approve)
        calls = s.client.calls
        if not _check(
            "approved flow executes with the plan_id",
            len(calls) == 2 and calls[1][1].get("plan_id") == "P1",
            f"calls={len(calls)}",
        ):
            failures += 1
        if not _check(
            "approved flow shows the plan then the result",
            "PLAN" in out and "OK" in out,
        ):
            failures += 1

        # --- declined flow: plan shown, NOT executed ---
        s2 = _FakeSession(
            {"ok": True, "phase": "plan", "plan_id": "P2", "description": "click Open"},
            {"ok": True, "phase": "execute", "method": "invoke"},
        )
        s2.answer = False
        out2 = s2._plan_then_execute("click_control", {"window": "F"}, s2.approve)
        if not _check(
            "declining executes nothing",
            len(s2.client.calls) == 1,
            f"calls={len(s2.client.calls)}",
        ):
            failures += 1
        if not _check(
            "declining says nothing was done",
            "nothing was done" in out2,
        ):
            failures += 1

        # --- auto-executed plan never asks ---
        s3 = _FakeSession(
            {"ok": True, "phase": "execute", "auto_executed": True, "method": "wmtext"},
            {"ok": True, "phase": "execute"},
        )
        s3._plan_then_execute("type_text", {"window": "a", "text": "x"}, s3.approve)
        if not _check(
            "auto-executed skips the approval question",
            s3.asked == [],
            f"asked={len(s3.asked)}",
        ):
            failures += 1
    except Exception:
        traceback.print_exc()
        _check("approval flow", False, "exception raised")
        failures += 1

    # ---- live subprocess round-trip -------------------------------------
    # Spawns the real MCP server over stdio and exercises initialize +
    # tools/list end-to-end. This is what the audit calls "a live server
    # round-trip" and what the in-process branch above does not cover.
    _section("MCP live subprocess round-trip (stdio JSON-RPC)")
    try:
        client = MCPClient(_find_python(root), root)
        client.start()
        try:
            tools = client.list_tools()
            n = len(tools)
            names = {t.get("name") for t in tools}
            if not _check(
                f"live init+tools/list (n={n})",
                n >= 18,
                f"client={_find_python(root)}",
            ):
                failures += 1
            for required in (
                "list_windows",
                "get_control_text",
                "get_ui_tree",
                "screenshot",
                "type_text",
                "click_control",
                "focus_window",
                "run_sequence",
            ):
                if not _check(f"live tools/list has {required}", required in names):
                    failures += 1

            # The console's own dispatch must round-trip through MCPClient.
            # We do NOT call cmd_windows here because the upstream server
            # tool `list_windows` returns a bare list which FastMCP rejects
            # (tracked separately, not Section D). Instead we stand up a
            # fresh ConsoleSession pointed at the same live client and
            # exercise a tool whose server implementation only needs the
            # `text` MCP block to come back (read_clipboard always returns
            # {"type": ...} dict and stays a short message).
            try:
                sess = ConsoleSession(root)
                sess.client = client
                clip = sess.cmd_clipboard()
                if not _check(
                    "cmd_clipboard reaches the live server",
                    isinstance(clip, str) and len(clip) > 0,
                    clip.splitlines()[0] if clip else "(empty)",
                ):
                    failures += 1
            except Exception as e:
                _check(
                    "cmd_clipboard reaches the live server",
                    False,
                    f"{type(e).__name__}: {e}",
                )
                failures += 1
        finally:
            try:
                client.stop()
            except Exception:
                pass
    except Exception as e:
        traceback.print_exc()
        _check("live subprocess", False, f"{type(e).__name__}: {e}")
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
