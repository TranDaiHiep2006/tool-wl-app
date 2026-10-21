"""P5 Section D — the tool-wl-app console (Tkinter shell).

A small window that stays open waiting for commands. It spawns ONE MCP
server subprocess — the same command and environment Cursor uses: venv
python, `-m server.mcp_server`, PYTHONPATH set — and keeps it alive for
the whole session, so the server starts once and window state survives
between commands. Closing the window terminates the child cleanly.

All logic lives in `console_core.py` (parser, JSON-RPC client, command
execution). This module is only the Tk shell over it, which is what makes
the console testable headlessly via `--selftest` and the unit tests.

Run the window:
    python scripts/console.py
    (or double-click scripts/console.bat)

Run the self-test (never shows the window):
    python scripts/console.py --selftest
"""

from __future__ import annotations

import sys
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from console_core import (  # noqa: E402
    CommandError,
    ConsoleSession,
    HELP_TEXT,
    parse_command,
)

# Vietnamese-capable UI font. Segoe UI ships with Windows and covers the
# Vietnamese diacritics; we fall back through sensible options so the
# console still starts on a stripped-down Windows install.
FONT_CANDIDATES = ("Segoe UI", "Tahoma", "Arial", "TkDefaultFont")


def _pick_font_family() -> str:
    """Return the first available UI font that renders Vietnamese."""
    probe = tk.Tk()
    probe.withdraw()
    try:
        available = set(tkfont.families(probe))
    finally:
        probe.destroy()
    for name in FONT_CANDIDATES:
        if name in available:
            return name
    return "TkDefaultFont"


class ConsoleApp:
    """The Tk shell. All behaviour is delegated to ConsoleSession."""

    def __init__(self) -> None:
        self.session = ConsoleSession(ROOT)
        self.history: list[str] = []
        self.history_pos = 0

        self.root = tk.Tk()
        self.root.title("tool-wl-app console")
        self.root.geometry("1000x660")
        self.family = _pick_font_family()
        self.root.option_add("*Font", (self.family, 10))

        self.always_on_top = tk.BooleanVar(value=False)
        self._build_ui()
        self._start_server()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Up>", self._history_prev)
        self.root.bind("<Down>", self._history_next)
        self.root.bind("<Control-l>", lambda _e: self._clear_output())

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        toolbar = tk.Frame(self.root)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))

        tk.Button(toolbar, text="Clear", command=self._clear_output).pack(side="left")
        tk.Button(
            toolbar, text="Help", command=lambda: self._write(HELP_TEXT)
        ).pack(side="left", padx=4)
        tk.Checkbutton(
            toolbar,
            text="Always on top",
            variable=self.always_on_top,
            command=self._toggle_topmost,
        ).pack(side="left", padx=8)

        self.status = tk.Label(toolbar, text="starting server…")
        self.status.pack(side="right")

        body = tk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        self.output = tk.Text(
            body,
            wrap="word",
            font=(self.family, 10),
            bg="#12141a",
            fg="#e6e6e6",
            insertbackground="#e6e6e6",
        )
        self.output.pack(side="left", fill="both", expand=True)
        bar = tk.Scrollbar(body, command=self.output.yview)
        bar.pack(side="right", fill="y")
        self.output.configure(yscrollcommand=bar.set)

        entry_row = tk.Frame(self.root)
        entry_row.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(entry_row, text=">").pack(side="left")
        self.entry = tk.Entry(entry_row, font=(self.family, 10))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.focus_set()
        self.entry.bind("<Return>", self._on_enter)

    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.always_on_top.get()))

    def _clear_output(self) -> None:
        self.output.delete("1.0", tk.END)

    def _start_server(self) -> None:
        try:
            count = self.session.start()
        except Exception as exc:
            self.status.configure(text="server FAILED")
            self._write(f"[!] could not start the MCP server: {exc}\n")
            self._write(
                "[!] check that .venv exists, or set WL_PYTHON to the "
                "interpreter you want.\n\n"
            )
            return
        self.status.configure(text=f"server ready ({count} tools)")

    # ---------------- output ----------------

    def _write(self, text: str) -> None:
        self.output.insert(tk.END, text)
        self.output.see(tk.END)

    # ---------------- commands ----------------

    def _on_enter(self, _event=None) -> str:
        raw = self.entry.get()
        self.entry.delete(0, tk.END)
        if not raw.strip():
            return "break"
        self.history.append(raw)
        self.history_pos = len(self.history)
        self._write(f"> {raw}\n")

        try:
            output = self.run_command(raw)
        except CommandError as exc:
            # Errors print inline; the window never exits on a bad command.
            self._write(f"[!] {exc}\n\n")
            self.entry.focus_set()
            return "break"
        except Exception as exc:  # defensive
            self._write(f"[!] unexpected error: {exc}\n\n")
            self.entry.focus_set()
            return "break"

        if output:
            self._write(output if output.endswith("\n") else output + "\n\n")
        self.entry.focus_set()
        return "break"

    def run_command(self, raw: str) -> str:
        """Run one console command and return the text to display."""
        verb, args = parse_command(raw)
        if verb == "help":
            return HELP_TEXT
        if verb == "quit":
            self._on_close()
            return ""
        if verb == "windows":
            return self.session.cmd_windows()
        if verb == "shot":
            return self.session.cmd_shot()
        if verb == "read":
            return self.session.cmd_read(args["window"])
        if verb == "focus":
            return self.session.cmd_focus(args["window"])
        if verb == "click":
            return self.session.cmd_click(args["window"], args["control"])
        if verb == "type":
            return self.session.cmd_type(args["window"], args["text"])
        raise CommandError(f"unknown command {verb!r} — type 'help'")

    # ---------------- history (Up / Down) ----------------

    def _history_prev(self, _event=None) -> str:
        if not self.history:
            return "break"
        self.history_pos = max(0, self.history_pos - 1)
        self.entry.delete(0, tk.END)
        self.entry.insert(0, self.history[self.history_pos])
        return "break"

    def _history_next(self, _event=None) -> str:
        if not self.history:
            return "break"
        self.history_pos = min(len(self.history), self.history_pos + 1)
        self.entry.delete(0, tk.END)
        if self.history_pos < len(self.history):
            self.entry.insert(0, self.history[self.history_pos])
        return "break"

    # ---------------- lifecycle ----------------

    def _on_close(self) -> None:
        try:
            self.session.stop()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self._write(
            "tool-wl-app console — type 'help' for commands.\n"
            "One MCP server stays alive for the whole session.\n"
            "Action commands ask for approval inline (y/n).\n\n"
        )
        self.root.mainloop()


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        from console_selftest import run_selftest

        return run_selftest(ROOT)
    ConsoleApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))