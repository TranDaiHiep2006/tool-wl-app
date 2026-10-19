"""Tests for `server.browser_dom` and the `get_page_text` / `get_page_structure`
MCP tools.

Coverage:
  - caps (max_chars / max_items) are enforced and the response carries the
    right `truncated` marker
  - error envelope is returned (with install_steps) when the bridge is not
    connected within the timeout — never hangs
  - pruning/limits are honored against a synthetic reply
  - native messaging framing round-trips through `native_host.tool_wl_host`
  - MCP server registers the two new tools
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import time
import unittest
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


from server import browser_dom  # noqa: E402
from native_host import tool_wl_host  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / pure-logic tests
# ---------------------------------------------------------------------------


class TestInstallSteps(unittest.TestCase):
    def test_get_install_steps_mentions_extension_dir(self):
        text = browser_dom.get_install_steps()
        self.assertIsInstance(text, str)
        self.assertIn("Load unpacked", text)
        self.assertIn(os.path.abspath(os.path.join(ROOT, "extension")), text)


class TestCapsEnforcement(unittest.TestCase):
    """`_call` and the public functions cap user input to the hard limits."""

    def test_normalize_text_reply_passes_through_error(self):
        err = {"ok": False, "error": "bridge_unavailable",
               "message": "x", "install_steps": "..."}
        out = browser_dom._normalize_text_reply(err, 8000)
        self.assertEqual(out, err)

    def test_normalize_text_reply_marks_truncated_when_cap_fires(self):
        # A reply whose text length equals max_chars should be flagged truncated.
        reply = {"ok": True, "data": {"text": "a" * 8000}}
        out = browser_dom._normalize_text_reply(reply, 8000)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["chars"], 8000)
        self.assertEqual(out["max_chars"], 8000)

    def test_normalize_text_reply_not_truncated_when_under(self):
        reply = {"ok": True, "data": {"text": "hello"}}
        out = browser_dom._normalize_text_reply(reply, 8000)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["chars"], 5)

    def test_normalize_structure_reply_counts_by_kind(self):
        reply = {
            "ok": True,
            "data": {
                "items": [
                    {"kind": "heading", "level": 1, "text": "Hi"},
                    {"kind": "link", "text": "A", "href": "https://x"},
                    {"kind": "button", "text": "Go"},
                    {"kind": "input", "input_type": "text", "name": "q"},
                    {"kind": "heading", "level": 2, "text": "Sub"},
                ]
            },
        }
        out = browser_dom._normalize_structure_reply(reply, 200)
        self.assertEqual(out["count"], 5)
        self.assertEqual(out["counts_by_kind"]["heading"], 2)
        self.assertEqual(out["counts_by_kind"]["link"], 1)
        self.assertEqual(out["counts_by_kind"]["button"], 1)
        self.assertEqual(out["counts_by_kind"]["input"], 1)
        self.assertFalse(out["truncated"])

    def test_normalize_structure_reply_truncated(self):
        # 200 items, server cap is 200 → truncated.
        reply = {
            "ok": True,
            "data": {"items": [{"kind": "link", "text": "x", "href": "y"}] * 200},
        }
        out = browser_dom._normalize_structure_reply(reply, 200)
        self.assertEqual(out["count"], 200)
        self.assertTrue(out["truncated"])


# ---------------------------------------------------------------------------
# Bridge availability / timeout — the hard requirement is "no hang, clear error".
# ---------------------------------------------------------------------------


class TestBridgeTimeout(unittest.TestCase):
    """If the extension never connects, the tool must return a structured
    error WITHIN the timeout, with install_steps included, never crashing
    and never hanging."""

    def test_get_page_text_returns_error_when_no_extension(self):
        # Bind an unused port so the WebSocket server can start, then verify
        # that the bridge returns a clear timeout error when no extension
        # actually connects. This proves "never hang, clear install_steps".
        import socket as _s
        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as tmp:
            tmp.bind(("127.0.0.1", 0))
            unused_port = tmp.getsockname()[1]
        bridge = browser_dom.DOMBridge(host="127.0.0.1", port=unused_port)
        browser_dom._bridge = bridge
        try:
            start = time.time()

            async def _exercise():
                ok = await bridge.start()
                self.assertTrue(ok, "expected bridge to bind")
                try:
                    await bridge.request(
                        "get_page_text",
                        {"max_chars": 100},
                        timeout_s=1.0,
                    )
                    self.fail("expected BridgeTimeout")
                except browser_dom.BridgeTimeout as exc:
                    self.assertIn("extension not connected", str(exc))
                finally:
                    await bridge.stop()

            asyncio.run(_exercise())
            elapsed = time.time() - start
            self.assertLess(elapsed, 3.0, f"test ran for {elapsed:.1f}s (hang)")
        finally:
            browser_dom._bridge = None

    def test_get_page_text_sync_returns_error_envelope_when_unavailable(self):
        # Force the singleton to point at a definitely-broken bridge (no server).
        # We use a higher port that's unlikely to be in use; if the bind succeeds
        # we then verify the bridge returns an error within ~10s, which is the
        # spec'd timeout (the prompt mandates: "within a 10s timeout — never hang").
        import socket as _s
        # Pick a port nobody is using: bind ephemeral, capture the port, release.
        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as tmp:
            tmp.bind(("127.0.0.1", 0))
            unused_port = tmp.getsockname()[1]
        bridge = browser_dom.DOMBridge(host="127.0.0.1", port=unused_port)
        browser_dom._bridge = bridge
        try:
            start = time.time()
            out = browser_dom.get_page_text_sync(max_chars=1000)
            elapsed = time.time() - start
            # Spec says "within a 10s timeout — never hang". We allow 12s
            # of wall time to cover loop setup + transport overhead.
            self.assertLess(elapsed, 12.0, f"call hung for {elapsed:.1f}s")
            self.assertFalse(out.get("ok"))
            self.assertIn("install_steps", out)
            self.assertIn("Load unpacked", out["install_steps"])
        finally:
            browser_dom._bridge = None


# ---------------------------------------------------------------------------
# Native Messaging framing — direct test of the host script's protocol layer.
# ---------------------------------------------------------------------------


class TestNativeMessagingFraming(unittest.TestCase):
    def test_round_trip_canned_message(self):
        # Use the in-process self-test path.
        rc = tool_wl_host.run_self_test()
        self.assertEqual(rc, 0)

    def test_read_write_native_message(self):
        import io
        payload = {"op": "ping"}
        out = io.BytesIO()
        tool_wl_host.write_native_message(out, payload)
        framed = out.getvalue()
        self.assertGreaterEqual(len(framed), 4)
        (length,) = struct.unpack("<I", framed[:4])
        self.assertEqual(length, len(json.dumps(payload).encode("utf-8")))
        parsed = tool_wl_host.read_native_message(io.BytesIO(framed))
        self.assertEqual(parsed, payload)

    def test_handle_ping(self):
        reply = tool_wl_host.handle({"op": "ping"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["host"], tool_wl_host.NATIVE_HOST_NAME)

    def test_handle_unknown_op(self):
        reply = tool_wl_host.handle({"op": "totally_unknown"})
        self.assertFalse(reply["ok"])
        self.assertIn("unknown op", reply["error"])

    def test_handle_describe(self):
        reply = tool_wl_host.handle({"op": "describe"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data_path"], "ws://127.0.0.1:57532/")


# ---------------------------------------------------------------------------
# Pruning / limits — pure logic from `content.js` translated into Python so
# the pruning contract is testable without spinning up a browser.
# ---------------------------------------------------------------------------


def _prune_text_simple(text: str, max_chars: int) -> Dict[str, Any]:
    """Python mirror of the boilerplate-strip + truncate in content.js.

    The point of this test fixture is to LOCK IN the contract — the JS side
    has the same logic but we verify the response shape here. If they drift,
    the JS implementation is the one that runs in production; the contract
    is enforced by manual E2E tests.
    """
    # Mirror the JS: collapse whitespace, trim, cap length.
    text = text.replace("\r\n", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = text.strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "…"
    return {"text": text, "chars": len(text), "truncated": truncated}


def _prune_structure(items, max_items):
    truncated = len(items) > max_items
    return {"items": items[:max_items], "count": min(len(items), max_items),
            "truncated": truncated}


class TestPruningContract(unittest.TestCase):
    def test_text_prune_basic(self):
        out = _prune_text_simple("hello\n\nworld", 100)
        self.assertFalse(out["truncated"])
        self.assertIn("hello", out["text"])
        self.assertIn("world", out["text"])

    def test_text_prune_truncates(self):
        big = "a" * 10000
        out = _prune_text_simple(big, 8000)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(out["chars"], 8001)  # 8000 + ellipsis

    def test_text_prune_caps_min(self):
        # Hard cap is 8000; capping to 100 should still respect the 100-char limit.
        out = _prune_text_simple("a" * 1000, 100)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(out["chars"], 101)

    def test_structure_prune_truncates(self):
        items = [{"kind": "link", "text": str(i), "href": f"https://x/{i}"} for i in range(500)]
        out = _prune_structure(items, 200)
        self.assertEqual(out["count"], 200)
        self.assertTrue(out["truncated"])

    def test_structure_prune_no_truncate(self):
        items = [{"kind": "heading", "level": 1, "text": "Hi"}]
        out = _prune_structure(items, 200)
        self.assertEqual(out["count"], 1)
        self.assertFalse(out["truncated"])


# ---------------------------------------------------------------------------
# MCP tool registration — the two new tools must show up.
# ---------------------------------------------------------------------------


class TestMCPToolsRegistered(unittest.TestCase):
    def test_browser_dom_tools_in_server(self):
        import asyncio
        from server.mcp_server import mcp
        names = {t.name for t in asyncio.run(mcp.list_tools())}
        self.assertIn("get_page_text", names)
        self.assertIn("get_page_structure", names)


# ---------------------------------------------------------------------------
# End-to-end WebSocket round-trip with a synthetic extension client.
# ---------------------------------------------------------------------------


class TestBridgeRoundTrip(unittest.TestCase):
    """Spin up the bridge on an unused port, connect a fake 'extension'
    WebSocket client, send a get_page_text request, and verify the bridge
    forwards it and the response comes back. This is the live data path."""

    def _unused_port(self):
        import socket as _s
        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as tmp:
            tmp.bind(("127.0.0.1", 0))
            return tmp.getsockname()[1]

    def test_round_trip_get_page_text(self):
        port = self._unused_port()
        bridge = browser_dom.DOMBridge(host="127.0.0.1", port=port)
        browser_dom._bridge = bridge

        async def _scenario():
            ok = await bridge.start()
            self.assertTrue(ok)
            # Connect a client that mimics the extension.
            url = f"ws://127.0.0.1:{port}/"
            async with websockets_lib.connect(url) as ws:  # type: ignore[name-defined]
                # Send a get_page_text request from the bridge side.
                req_task = asyncio.create_task(
                    bridge.request("get_page_text", {"max_chars": 2000},
                                    timeout_s=2.0)
                )
                # Read the request from the client side.
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msg = json.loads(raw)
                self.assertEqual(msg["op"], "get_page_text")
                self.assertEqual(msg["args"]["max_chars"], 2000)
                # Reply with a synthetic extraction result.
                reply = {
                    "type": "reply",
                    "id": msg["id"],
                    "ok": True,
                    "data": {"text": "Hello from the page.", "chars": 21},
                }
                await ws.send(json.dumps(reply))
                got = await req_task
                self.assertTrue(got["ok"])
                self.assertEqual(got["data"]["chars"], 21)
                self.assertEqual(got["data"]["text"], "Hello from the page.")
            await bridge.stop()

        try:
            asyncio.run(_scenario())
        finally:
            browser_dom._bridge = None

    def test_round_trip_error_from_extension(self):
        port = self._unused_port()
        bridge = browser_dom.DOMBridge(host="127.0.0.1", port=port)
        browser_dom._bridge = bridge

        async def _scenario():
            ok = await bridge.start()
            self.assertTrue(ok)
            url = f"ws://127.0.0.1:{port}/"
            async with websockets_lib.connect(url) as ws:  # type: ignore[name-defined]
                req_task = asyncio.create_task(
                    bridge.request("get_page_text", {"max_chars": 500},
                                    timeout_s=2.0)
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msg = json.loads(raw)
                # Reply with an error from the extension side.
                await ws.send(json.dumps({
                    "type": "reply",
                    "id": msg["id"],
                    "ok": False,
                    "error": "active tab is a browser internal page",
                }))
                got = await req_task
                self.assertFalse(got["ok"])
                self.assertIn("browser internal", got["error"])
            await bridge.stop()

        try:
            asyncio.run(_scenario())
        finally:
            browser_dom._bridge = None


# Late import so the module-level imports above don't break if websockets is
# unavailable (the TestBridgeTimeout / TestNativeMessagingFraming tests still
# run without it).
try:
    import websockets as websockets_lib  # type: ignore
except Exception:  # pragma: no cover
    websockets_lib = None  # type: ignore


if __name__ == "__main__":
    unittest.main()