"""MCP stdio smoke client used to verify the registered tools work end-to-end."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")


def make_request(payload):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [PYTHON, "-u", "-m", "server.mcp_server"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    full = (
        json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0.0.1"},
            },
        }) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    proc.stdin.write(full.encode("utf-8"))
    proc.stdin.flush()
    # Give it a moment
    time.sleep(0.3)
    proc.stdin.write(payload.encode("utf-8") + b"\n")
    proc.stdin.flush()
    # Read the responses
    out = b""
    out += proc.stdout.readline()  # id=1 (initialize result)
    time.sleep(0.3)
    try:
        line = proc.stdout.readline()
        out += line
    except Exception:
        pass
    proc.stdin.close()
    try:
        rest, _ = proc.communicate(timeout=15)
        out += rest
    except subprocess.TimeoutExpired:
        proc.kill()
        out += b"TIMEOUT"
    return out.decode("utf-8").splitlines()


def main() -> int:
    for name, args in [
        ("list_windows", {}),
        ("get_foreground_window", {}),
    ]:
        req = json.dumps({
            "jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        lines = make_request(req)
        print(f"[{name}] lines: {len(lines)}")
        for line in lines:
            print("  ", line[:200])
    return 0


if __name__ == "__main__":
    main()