"""tool-wl-app console core — parser, MCP stdio client, command execution.

Deliberately free of any Tkinter import so it can be unit-tested and driven
by `--selftest` without a window. `console.py` is the thin UI shell.

Command grammar
---------------
    windows                      list open windows
    shot                         capture the screen
    read <window title substring>
    type <window title substring> | <text>
    click <window title substring> | <control name>
    focus <window title substring>
    help                         show this text
    quit                         close the console

The `|` separator is used instead of a space so that text containing
spaces (and Vietnamese text, which is what this project is mostly used
for) needs no quoting rules.

Protocol
--------
A minimal but correct MCP stdio client: `initialize`, then
`tools/call`. JSON-RPC messages are newline-delimited on stdin/stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# MCP protocol revision we speak. 2024-11-05 is widely supported.
PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "tool-wl-app-console"
CLIENT_VERSION = "5"

# Long text printed by a read is truncated so the console stays readable.
MAX_TEXT_CHARS = 600
MAX_HISTORY = 200


class CommandError(Exception):
    """A user-facing command error. Printed inline; never fatal."""


class MCPError(Exception):
    """The MCP server returned an error or the transport broke."""


HELP_TEXT = """Commands
  windows                       list open windows
  shot                          capture the screen (returns an image ref)
  read <window> | <control>     read text from a specific control in a window
  type <window> | <text>        type text into a window's editable control
  click <window> | <control>    click a named control in a window
  focus <window>                bring an allowlisted window to the front
  help                          show this help
  quit                          close the console

<window> is a case-insensitive substring of the window title. Vietnamese
titles work: matching is Unicode-normalized and ignores diacritic form.

Action commands (type / click) run a plan first and ask 'Approve? y/n'
in this window. Processes listed in auto_execute_processes skip the prompt.

Up/Down arrow recalls previous commands. Ctrl+L clears the output.
"""


# ---------------------------------------------------------------- parser ---

def parse_command(raw: str) -> tuple[str, dict]:
    """Parse one console line into ``(verb, args)``.

    Raises :class:`CommandError` with a precise, user-facing message when
    the line does not match the grammar.
    """
    line = (raw or "").strip()
    if not line:
        raise CommandError("empty command — type 'help'")

    verb, _, rest = line.partition(" ")
    verb = verb.lower().strip()
    rest = rest.strip()

    if verb in ("help", "quit", "exit"):
        return ("quit" if verb == "exit" else verb), {}
    if verb == "windows":
        return "windows", {}
    if verb == "shot":
        if rest:
            raise CommandError("'shot' takes no arguments")
        return "shot", {}
    if verb in ("read", "focus"):
        if not rest:
            raise CommandError(
                f"'{verb}' needs a window title substring, "
                f"e.g. {verb} Notepad"
            )
        return verb, {"window": rest}
    if verb in ("type", "click"):
        if "|" not in rest:
            raise CommandError(
                f"'{verb}' needs: {verb} <window> | <"
                f"{'text' if verb == 'type' else 'control name'}>"
                f"  (the | separates the two parts)"
            )
        window, _, value = rest.partition("|")
        window = window.strip()
        value = value.strip()
        if not window:
            raise CommandError(f"'{verb}': the window part before | is empty")
        if not value:
            raise CommandError(f"'{verb}': the part after | is empty")
        if verb == "type":
            return "type", {"window": window, "text": value}
        return "click", {"window": window, "control": value}
    raise CommandError(f"unknown command {verb!r} — type 'help'")


# ----------------------------------------------------------- MCP client ---

class MCPClient:
    """Minimal stdio JSON-RPC MCP client (initialize + tools/call)."""

    def __init__(self, python_exe: str, cwd: Path) -> None:
        self.python_exe = python_exe
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 0

    # -- lifecycle --

    def start(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.cwd)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [self.python_exe, "-m", "server.mcp_server"],
            cwd=str(self.cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result = self.initialize()
        if result is None:
            raise MCPError("server did not answer initialize")

    def initialize(self) -> Optional[dict]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "version": CLIENT_VERSION,
                },
            },
        )
        # Best-effort notification; older servers ignore it.
        self.notify("notifications/initialized", {})
        return result

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # -- transport --

    def _send(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("server is not running")
        line = json.dumps(payload, ensure_ascii=False)
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(f"lost the server process: {exc}") from exc

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Optional[dict] = None) -> Any:
        self._next_id += 1
        req_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
        )
        while True:
            msg = self._read_message()
            if msg.get("id") != req_id:
                # Server-initiated notification; ignore it.
                continue
            if "error" in msg:
                err = msg["error"] or {}
                raise MCPError(
                    f"{method} failed: {err.get('message', err)}"
                )
            return msg.get("result")

    def _read_message(self) -> dict:
        if self.proc is None or self.proc.stdout is None:
            raise MCPError("server is not running")
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = ""
                if self.proc.stderr is not None:
                    try:
                        err = self.proc.stderr.read() or ""
                    except Exception:
                        err = ""
                raise MCPError(
                    f"the server closed its output stream"
                    f"{(': ' + err.strip()) if err.strip() else ''}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                # Servers sometimes print stray lines on stdout.
                continue

    # -- tools --

    def list_tools(self) -> list[dict]:
        result = self.request("tools/list", {})
        return list((result or {}).get("tools", []))

    def call_tool(self, name: str, args: Optional[dict] = None) -> dict:
        result = self.request(
            "tools/call", {"name": name, "arguments": args or {}}
        )
        return _extract_tool_result(result)


def _extract_tool_result(result: Any) -> dict:
    """Unwrap an MCP tools/call result into a plain dict.

    MCP returns ``{"content": [...], "isError": bool}``; our server puts
    the real payload in a text block as JSON. This normalizes both shapes.
    """
    if not isinstance(result, dict):
        raise MCPError(f"unexpected tools/call result: {type(result).__name__}")
    if result.get("isError"):
        raise MCPError(_content_to_text(result) or "tool reported an error")

    # FastMCP may return a `structuredContent` for dict-returning tools.
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        return structured

    text = _content_to_text(result)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except json.JSONDecodeError:
        return {"text": text}


def _content_to_text(result: dict) -> str:
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


# ------------------------------------------------------------ formatting ---

def format_windows(payload: Any) -> str:
    """Compact table of the windows list_windows returned."""
    windows = payload.get("windows") if isinstance(payload, dict) else None
    if windows is None and isinstance(payload, list):
        windows = payload
    if not windows:
        return "(no visible windows)"

    lines = [f"{len(windows)} window(s):"]
    for w in windows:
        title = (w.get("title") or "").strip()
        if not title:
            continue
        marker = "*" if w.get("is_foreground") else " "
        proc = w.get("process_name") or "?"
        lines.append(f" {marker} {title}  [{proc}]")
    return "\n".join(lines)


def format_shot(payload: Any) -> str:
    """One-line description of a screenshot result."""
    if not isinstance(payload, dict):
        return str(payload)
    if payload.get("ok") is False or payload.get("error"):
        return f"[!] screenshot failed: {payload.get('error')}"
    size = payload.get("size") or {}
    w = payload.get("width")
    h = payload.get("height")
    if not w or not h:
        w = w or size.get("w") or size.get("width")
        h = h or size.get("h") or size.get("height")
    dims = f"{w}x{h}" if (w and h) else "?"
    path = payload.get("path") or payload.get("file") or "(saved)"
    return f"screenshot: {dims} → {path}"


def format_read(payload: Any, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Compact rendering of get_control_text, truncating long text."""
    if not isinstance(payload, dict):
        return str(payload)
    if payload.get("found") is False or payload.get("error"):
        return f"[!] {payload.get('error') or 'control not found'}: {payload.get('message', '')}"
    text = payload.get("text") or ""
    shown = text if len(text) <= max_chars else text[:max_chars] + " …"
    header = (
        f"{payload.get('name') or '?'} "
        f"({payload.get('control_type') or '?'})"
    )
    return f"{header}:\n{shown}"


def format_action(payload: Any) -> str:
    """Render a plan / execute result from an action tool."""
    if not isinstance(payload, dict):
        return str(payload)
    if payload.get("ok") is False:
        error = payload.get("error", "error")
        message = payload.get("message", "")
        return f"[!] {error}: {message}"
    if payload.get("phase") == "plan":
        auto = " (auto-executed — no approval needed)" if payload.get(
            "auto_executed"
        ) else ""
        return f"PLAN{auto}:\n{payload.get('description', '')}"
    if payload.get("phase") == "execute":
        bits = [f"done via {payload.get('method_label') or payload.get('method')}"]
        if payload.get("chars_typed") is not None:
            bits.append(f"{payload['chars_typed']} chars")
        if payload.get("auto_executed"):
            bits.append("auto-executed")
        return "OK — " + ", ".join(bits)
    return json.dumps(payload, ensure_ascii=False)[:MAX_TEXT_CHARS]


# --------------------------------------------------------- the session ----

class ConsoleSession:
    """Owns the MCP subprocess and turns commands into calls."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.client = MCPClient(_find_python(self.root), self.root)
        self.tool_names: list[str] = []

    def start(self) -> int:
        self.client.start()
        self.tool_names = [t.get("name", "") for t in self.client.list_tools()]
        return len(self.tool_names)

    def stop(self) -> None:
        self.client.stop()

    # -- read commands --

    def cmd_windows(self) -> str:
        return format_windows(self.client.call_tool("list_windows", {}))

    def cmd_shot(self) -> str:
        return format_shot(self.client.call_tool("screenshot", {"mode": "screen"}))

    def cmd_read(self, window: str) -> str:
        # `read <window>` alone reads the focused control inside the window;
        # users can target a specific control via the MCP path. The console
        # keeps its grammar small, so an empty selector is a clean error
        # telling the user to provide both pieces.
        if "|" not in window:
            return (
                "read: need 'read <window> | <control_name_or_id>'"
            )
        win_part, _, sel_part = window.partition("|")
        win_part = win_part.strip()
        sel_part = sel_part.strip()
        if not win_part or not sel_part:
            return "read: both the window and the control must be non-empty"
        payload = self.client.call_tool(
            "get_control_text",
            {"window": win_part, "automation_id_or_name": sel_part},
        )
        return format_read(payload)

    # -- action commands (plan → approve → execute) --

    def cmd_focus(self, window: str) -> str:
        payload = self.client.call_tool("focus_window", {"window": window})
        return format_action(payload)

    def cmd_click(self, window: str, control: str) -> str:
        args = {"window": window, "name": control}
        return self._plan_then_execute("click_control", args, self.approve)

    def cmd_type(self, window: str, text: str) -> str:
        args = {"window": window, "text": text}
        return self._plan_then_execute("type_text", args, self.approve)

    # -- approval flow --

    def _plan_then_execute(self, tool: str, args: dict, approve) -> str:
        """Run the plan phase, ask for approval, then execute.

        `approve` is a callable returning True/False. The console injects
        the interactive prompt; tests inject a fixed answer, which is why
        the whole flow stays inside the window without any global input().
        """
        plan = self.client.call_tool(tool, dict(args))
        if not isinstance(plan, dict):
            return str(plan)

        # Auto-executed processes need no approval at all.
        if plan.get("auto_executed") or plan.get("phase") == "execute":
            return format_action(plan)
        if plan.get("phase") != "plan" or not plan.get("plan_id"):
            return format_action(plan)

        shown = format_action(plan)
        if not approve(shown):
            return shown + "\n\n[cancelled — nothing was done]"

        result = self.client.call_tool(
            tool, dict(args, plan_id=plan["plan_id"])
        )
        return shown + "\n\n" + format_action(result)

    def approve(self, shown_plan: str) -> bool:
        """Interactive y/n prompt. Overridden in tests / --selftest."""
        raise NotImplementedError


def _find_python(root: Path) -> str:
    """Locate the interpreter the MCP server should run under.

    Order: WL_PYTHON override → the repo's .venv → whatever is running us.
    """
    override = os.environ.get("WL_PYTHON")
    if override and Path(override).exists():
        return override
    venv = root / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    return sys.executable


class AutoApprove(ConsoleSession):
    """ConsoleSession with a non-interactive approval hook (for tests)."""

    def __init__(self, root: Path, answer: bool = True) -> None:
        super().__init__(root)
        self.answer = answer
        self.asked: list[str] = []

    def approve(self, shown_plan: str) -> bool:
        self.asked.append(shown_plan)
        return self.answer