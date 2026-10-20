"""Regression test: the REAL subprocess launch binds the DOM bridge port.

This is deliberately a *subprocess* test, not an in-process lifespan test.

Background
----------
An in-process test that only enters the FastMCP lifespan can pass while the
real MCP server — spawned by Cursor as a stdio subprocess — never keeps the
listener up. The stdio transport binds the bridge on lifespan entry, but the
process must then STAY ALIVE for the port to be observable from outside.

The single most important detail: a real MCP client (Cursor) holds the
child's stdin write handle OPEN for the whole session. If you launch the
child with stdin CLOSED (e.g. `stdin=DEVNULL`), FastMCP's stdio transport
reads EOF on stdin and shuts down gracefully (exit code 0) the instant the
bridge binds — the port is bound and gone within the same tick, so an
external probe (netstat / connect) never sees it LISTENING.

So this test:
  * launches  <venv python> -m server.mcp_server
  * with      PYTHONPATH=<repo root>
  * from      a cwd that is NOT the repo root (mirrors Cursor)
  * keeping   stdin OPEN (unwritten) so the server stays alive
  * then      polls for up to ~20s for 127.0.0.1:57532 to be LISTENING
  * and       asserts netstat reports it, via a real external connect().

It is written to be robust in CI/dev boxes that may not have the venv or may
already have the port in use: in those cases it skips rather than fails.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

PYEXE = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 57532


def _venv_available() -> bool:
    return os.path.isfile(PYEXE)


def _port_bound() -> bool:
    """Return True if something is accepting TCP connections on the bridge port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((BRIDGE_HOST, BRIDGE_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


class TestRealSubprocessBridgeBinding(unittest.TestCase):
    """The DOM bridge must be observably LISTENING under a real subprocess."""

    def _launch_child(self, log_path: str):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        logf = open(log_path, "wb")
        child = subprocess.Popen(
            [PYEXE, "-m", "server.mcp_server"],
            # cwd deliberately NOT the repo root — matches how Cursor launches.
            cwd=tempfile.gettempdir(),
            env=env,
            # stdin stays OPEN (and unwritten) so the stdio server stays alive,
            # exactly like a real MCP client holding the session's write handle.
            stdin=subprocess.PIPE,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return child, logf

    def test_subprocess_binds_bridge_port(self):
        if not _venv_available():
            self.skipTest(f"venv python not found at {PYEXE}")

        # Pre-flight: if the port is already taken by something else, we can't
        # make a clean assertion about *our* child binding it.
        if _port_bound():
            self.skipTest("bridge port already in use by another process")

        log_path = os.path.join(tempfile.gettempdir(), "test_mcp_child.log")
        child, logf = self._launch_child(log_path)

        try:
            deadline = time.time() + 20.0
            bound = False
            while time.time() < deadline:
                if _port_bound():
                    bound = True
                    break
                if child.poll() is not None:
                    # Child exited before binding — surface its log.
                    logf.flush()
                    with open(log_path, "rb") as fh:
                        tail = fh.read().decode("utf-8", errors="replace")[-4000:]
                    self.fail(
                        "child exited (code %r) before the bridge bound.\n"
                        "--- child log tail ---\n%s" % (child.returncode, tail)
                    )
                time.sleep(0.2)

            self.assertTrue(
                bound,
                "bridge port %s:%d never showed LISTENING within 20s under a real "
                "subprocess launch (see %s)" % (BRIDGE_HOST, BRIDGE_PORT, log_path),
            )

            # Also assert the child is still alive while the port is bound —
            # this is the property the in-process test could not verify.
            self.assertIsNone(
                child.poll(),
                "child exited while the bridge port was bound; it must stay alive "
                "for the duration of an MCP session",
            )
        finally:
            logf.flush()
            if child.poll() is None:
                # Kill the whole process tree: on Windows the venv
                # `python.exe` is a launcher that re-execs a worker
                # `python.exe -m server.mcp_server`. Killing only the wrapper
                # leaves the worker alive holding the bridge port, which would
                # leak into the next run and make the pre-flight "port in use"
                # skip fire spuriously.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(child.pid)],
                    capture_output=True,
                )
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            logf.close()


if __name__ == "__main__":
    unittest.main()