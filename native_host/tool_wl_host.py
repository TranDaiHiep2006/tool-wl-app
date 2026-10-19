"""Native Messaging host for tool-wl-app.

This script is what the browser launches when `chrome.runtime.sendNativeMessage(
"com.trandaqhiep.tool_wl_app", message)` is called from the extension. The
browser writes a length-prefixed JSON message to the host's stdin; the host
writes a length-prefixed JSON response to stdout.

The host is also usable as a standalone CLI for testing message framing
(`--stdin` reads a single JSON request from stdin, prints the JSON response,
with no actual browser involved).

P2 actual data path
-------------------
The MCP server (`server/browser_dom.py`) runs a WebSocket server on
`127.0.0.1:57532` and the extension's background script connects to it. This
native host is the *standard* entry point that satisfies Chrome's Native
Messaging requirement (and the prompt's section 4 wiring). When the extension
calls `chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, ...)` for a
diagnostic ping, this host responds with a small "ok" payload.

CLI
---
    python native_host/tool_wl_host.py --stdin
        Reads a single JSON request from stdin (raw JSON, NOT length-prefixed),
        writes a JSON response to stdout. For local testing without a browser.

    python native_host/tool_wl_host.py --self-test
        Echoes a canned {op: "ping"} message through the length-prefixed
        framing and exits 0 if the response is well-formed.

Constraints (read-only)
-----------------------
This host is strictly read-only. It does NOT click, type, focus, or mutate
state. The only thing it ever does is respond to diagnostic pings.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
from typing import Any, Dict


# Must match the host name registered by scripts/register_native_host.py
NATIVE_HOST_NAME = "com.trandaqhiep.tool_wl_app"

PROTOCOL_VERSION = "0.1.0"


def read_exact(stream, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


def read_native_message(stream) -> Dict[str, Any]:
    """Read one Chrome Native Messaging message from `stream`.

    Format: 4-byte little-endian uint32 length, then UTF-8 JSON payload.
    """
    header = read_exact(stream, 4)
    if len(header) < 4:
        raise EOFError("native host: stdin closed before message header")
    (length,) = struct.unpack("<I", header)
    if length == 0:
        raise ValueError("native host: empty message")
    # Chrome caps messages at 1 MB; be defensive.
    if length > 1_048_576:
        raise ValueError(f"native host: message too large ({length} bytes)")
    payload = read_exact(stream, length)
    if len(payload) < length:
        raise EOFError("native host: stdin closed mid-message")
    return json.loads(payload.decode("utf-8"))


def write_native_message(stream, obj: Dict[str, Any]) -> None:
    """Write a Native Messaging response to `stream`."""
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def handle(message: Dict[str, Any]) -> Dict[str, Any]:
    """Process one request and return a response dict (NOT yet framed)."""
    op = message.get("op")
    if op == "ping":
        return {
            "ok": True,
            "host": NATIVE_HOST_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "ts": int(time.time() * 1000),
        }
    if op == "describe":
        return {
            "ok": True,
            "description": "tool-wl-app read-only DOM bridge host",
            "protocol_version": PROTOCOL_VERSION,
            "data_path": "ws://127.0.0.1:57532/",
            "capabilities": ["ping", "describe"],
        }
    return {"ok": False, "error": "unknown op: " + str(op)}


def run_stdin_loop() -> int:
    """Read Native Messaging messages from stdin, write responses to stdout.

    Loop until stdin closes (browser closed the host). Used when the browser
    launches this script via chrome.runtime.sendNativeMessage.
    """
    try:
        # Unbuffered binary IO is required for Native Messaging.
        sin = os.fdopen(sys.stdin.fileno(), "rb", buffering=0)
        sout = sys.stdout
    except Exception as exc:
        sys.stderr.write(f"native host: cannot open stdio: {exc!r}\n")
        return 2
    try:
        while True:
            try:
                msg = read_native_message(sin)
            except EOFError:
                return 0
            except Exception as exc:
                # One bad message should not kill the host; respond with error
                # only if we can write one (header may be malformed).
                sys.stderr.write(f"native host: read error: {exc!r}\n")
                return 1
            try:
                reply = handle(msg)
            except Exception as exc:  # noqa: BLE001
                reply = {"ok": False, "error": "handler: " + repr(exc)}
            try:
                write_native_message(sout, reply)
            except Exception as exc:
                sys.stderr.write(f"native host: write error: {exc!r}\n")
                return 1
    except KeyboardInterrupt:
        return 0


def run_stdin_once() -> int:
    """Read ONE raw JSON message from stdin (not length-prefixed) and print the
    raw JSON response. For local testing with `python tool_wl_host.py --stdin`.
    """
    try:
        raw = sys.stdin.read()
        if not raw:
            print(json.dumps({"ok": False, "error": "empty input"}))
            return 1
        msg = json.loads(raw)
        reply = handle(msg)
        sys.stdout.write(json.dumps(reply, ensure_ascii=False))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"native host: stdin test error: {exc!r}\n")
        print(json.dumps({"ok": False, "error": repr(exc)}))
        return 1


def run_self_test() -> int:
    """Round-trip a canned message through the length-prefixed framing using
    an in-memory buffer. Catches regressions in the framing code."""
    import io

    canned = {"op": "ping"}
    buf_out = io.BytesIO()
    write_native_message(buf_out, canned)
    framed = buf_out.getvalue()
    if len(framed) < 4:
        print("FAIL: framed message too short", file=sys.stderr)
        return 1
    (length,) = struct.unpack("<I", framed[:4])
    payload = framed[4:4 + length]
    if payload.decode("utf-8") != json.dumps(canned, ensure_ascii=False):
        print("FAIL: round-trip mismatch", file=sys.stderr)
        return 1
    # Now read it back and verify the handler.
    parsed = json.loads(payload.decode("utf-8"))
    reply = handle(parsed)
    if not reply.get("ok"):
        print(f"FAIL: handler returned {reply}", file=sys.stderr)
        return 1
    print(json.dumps(reply, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="tool-wl-app native host")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--stdin", action="store_true",
                   help="Read ONE raw JSON message from stdin, print JSON response. Local test mode.")
    g.add_argument("--loop", action="store_true",
                   help="Read Native Messaging length-prefixed messages from stdin, write responses. Used by the browser.")
    g.add_argument("--self-test", action="store_true",
                   help="Run an in-memory round-trip self test of the framing code.")
    args = p.parse_args()

    if args.stdin:
        return run_stdin_once()
    if args.loop:
        return run_stdin_loop()
    if args.self_test:
        return run_self_test()
    return 2


if __name__ == "__main__":
    sys.exit(main())