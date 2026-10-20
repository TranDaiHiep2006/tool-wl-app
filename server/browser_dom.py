"""Browser DOM bridge — MCP tools that read the active browser tab's DOM.

P2 — read-only. Adds two MCP tools:

  get_page_text()      → main readable text of the active tab (≤ ~8000 chars).
  get_page_structure() → pruned interactive structure: headings, links,
                          buttons, inputs (≤ ~200 elements).

P3 hardening: every connection must present the per-install secret token
that lives at `config/bridge_token` (and mirrored into `extension/token.js`
so the extension can include it in its `hello` handshake). Connections
without a matching token are rejected (WS code 1008, "policy violation")
and never reach our data path.

Priority order (also embedded in tool descriptions):
    1. get_page_text / get_page_structure — preferred for page content.
    2. screenshot / ocr_region — only when DOM tools are insufficient.
    3. find_text_on_screen — last resort.

Architecture
------------
The MCP server runs a tiny asyncio WebSocket server on `127.0.0.1:57532`.
The extension's background service worker connects to it. On each MCP tool
call we send a JSON request, the extension asks the active tab's content
script for the DOM extraction, and returns the result. Everything is
strictly read-only.

Failure handling
* If the extension never connects (e.g., user hasn't loaded it unpacked),
  the tool returns a structured error with copy-pasteable install steps,
  within a 10-second timeout. The MCP server process never hangs.
* If the bridge dies between calls, we attempt to reconnect on the next
  call. Repeated failures surface the same install instructions.

Limits (enforced)
* get_page_text caps at `max_chars` (default 8000, hard ceiling 8000).
* get_page_structure caps at `max_items` (default 200, hard ceiling 200).
* Both responses carry `"truncated": true` when the cap fired.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    # Websockets >= 13: prefer the new asyncio implementation directly to
    # avoid deprecation warnings from websockets.legacy.
    from websockets.asyncio.server import serve as _aio_serve  # type: ignore
    WEBSOCKETS_AVAILABLE = True
    serve = _aio_serve
    WebSocketServerProtocol = object  # type: ignore
except Exception:
    try:
        # Fallback: legacy API on older websockets versions.
        from websockets.server import serve as _legacy_serve  # type: ignore
        WEBSOCKETS_AVAILABLE = True
        serve = _legacy_serve
        WebSocketServerProtocol = object  # type: ignore
    except Exception:
        WEBSOCKETS_AVAILABLE = False
        serve = None
        WebSocketServerProtocol = object  # type: ignore


_log = logging.getLogger("tool_wl_app.browser_dom")


# --------------------- constants -------------------------------------------

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 57532
DEFAULT_TIMEOUT_S = 10.0
HARD_TIMEOUT_S = 10.0

DEFAULT_TEXT_MAX = 8000
HARD_TEXT_MAX = 8000
DEFAULT_STRUCTURE_MAX = 200
HARD_STRUCTURE_MAX = 200

# P3: token requirement can be turned off ONLY for tests/dev. Production
# callers should always set this to True.
AUTH_REQUIRED = True


def secrets_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to thwart timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --------------------- exceptions ------------------------------------------

class BridgeUnavailable(Exception):
    """Raised when the extension is not reachable within the timeout."""


class BridgeTimeout(BridgeUnavailable):
    pass


class BridgePermanentError(BridgeUnavailable):
    pass


# --------------------- install instructions -------------------------------

# Use plain text without f-string curly-brace collisions; build via format().
_INSTALL_STEPS = """\
tool-wl-app DOM bridge is not connected.

To enable get_page_text and get_page_structure:

  1. Open Microsoft Edge (or Chrome) and visit:
       edge://extensions  /  chrome://extensions
  2. Enable 'Developer mode' (toggle in the top-right corner).
  3. Choose 'Load unpacked' and select this folder:
       {ext_dir}
  4. Copy the extension ID shown on the card (32 chars).
  5. Register the native messaging host (run once):
       python scripts/register_native_host.py --extension-id <ID>
     -- or for dev only --
       python scripts/register_native_host.py --allow-any-origin
  6. Fully close and reopen Edge/Chrome so the registry is re-read.
  7. Restart Cursor so this MCP server is re-spawned.

The MCP server then auto-connects to the extension on the next tool call.
"""


def _install_steps() -> str:
    ext_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "extension"))
    return _INSTALL_STEPS.format(ext_dir=ext_dir)


# --------------------- bridge implementation --------------------------------

def _can_bind(host: str, port: int) -> bool:
    """Check whether we can bind to (host, port) without raising."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@dataclass
class _PendingRequest:
    op: str
    future: "asyncio.Future[Dict[str, Any]]"


class DOMBridge:
    """Async WebSocket server on 127.0.0.1:57532, paired with the extension."""

    def __init__(self, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT):
        self.host = host
        self.port = port
        self._server = None  # type: ignore[var-annotated]
        self._server_task: Optional[asyncio.Task] = None
        self._client: Any = None
        self._connected_at: Optional[float] = None
        self._pending: Dict[str, _PendingRequest] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Latest 'hello' metadata (extension version, etc.)
        self._hello: Dict[str, Any] = {}

    # ----- lifecycle -----

    async def start(self) -> bool:
        """Start the WebSocket server. Idempotent. Returns True if listening."""
        if self._server is not None:
            return True
        if not WEBSOCKETS_AVAILABLE:
            _log.warning("websockets module not installed; browser DOM tools unavailable.")
            return False
        self._loop = asyncio.get_running_loop()
        if self._lock is None:
            self._lock = asyncio.Lock()
        # Pre-check bind to fail fast with a clear error.
        if not _can_bind(self.host, self.port):
            _log.warning(
                "cannot bind %s:%d (already in use?); browser DOM tools unavailable",
                self.host, self.port,
            )
            return False
        try:
            self._server = await serve(  # type: ignore[misc]
                self._handle_client,
                self.host,
                self.port,
                # Restrict to loopback for security.
                ping_interval=None,  # extension may not answer pings
                max_size=4 * 1024 * 1024,  # 4 MiB cap on inbound frames
            )
            _log.info("DOM bridge listening on ws://%s:%d", self.host, self.port)
            return True
        except Exception as exc:
            _log.warning("DOM bridge start failed: %r", exc)
            self._server = None
            return False

    async def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()  # type: ignore[union-attr]
            except RuntimeError:
                # Event loop already closed (cross-loop shutdown); nothing we
                # can do — the OS will reap the listening socket on exit.
                self._server = None
                return
            try:
                await self._server.wait_closed()  # type: ignore[union-attr]
            except Exception:
                pass
            self._server = None
        # Cancel any pending requests so callers unblock.
        for req_id, req in self._pending.items():
            if not req.future.done():
                req.future.set_exception(
                    BridgePermanentError("bridge stopped")
                )
        self._pending.clear()

    # ----- connection handler -----

    async def _handle_client(self, ws, path=None):
        """Validate the per-install token (when AUTH_REQUIRED) and then
        process hello/keepalive/reply messages from the extension."""
        from server.bridge_auth import get_or_create_token
        expected_token = get_or_create_token() if AUTH_REQUIRED else None

        # Only one client at a time; close older connections.
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.close(code=1013, reason="replaced")
                except Exception:
                    pass
            self._client = None  # set AFTER auth succeeds
            self._connected_at = None

        try:
            if AUTH_REQUIRED:
                # Wait for the hello handshake. The very first message MUST
                # be a `hello` with the matching token.
                try:
                    first = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    _log.warning("bridge: client did not send hello within 5s; closing")
                    try:
                        await ws.close(code=1008, reason="auth_timeout")
                    except Exception:
                        pass
                    return
                try:
                    hello_msg = json.loads(first)
                except Exception:
                    _log.warning("bridge: hello was not valid JSON; closing")
                    try:
                        await ws.close(code=1008, reason="auth_required")
                    except Exception:
                        pass
                    return
                if not isinstance(hello_msg, dict) or hello_msg.get("type") != "hello":
                    _log.warning("bridge: first message must be hello; closing")
                    try:
                        await ws.close(code=1008, reason="auth_required")
                    except Exception:
                        pass
                    return
                presented = hello_msg.get("token")
                if not isinstance(presented, str) or not presented:
                    _log.warning("bridge: hello missing token; closing")
                    try:
                        await ws.close(code=1008, reason="auth_required")
                    except Exception:
                        pass
                    return
                if not secrets_compare(presented, expected_token or ""):
                    _log.warning("bridge: hello token mismatch; closing")
                    try:
                        await ws.close(code=1008, reason="auth_failed")
                    except Exception:
                        pass
                    return
                # Auth OK — promote the client to "current".
                async with self._lock:
                    if self._client is not None:
                        try:
                            await self._client.close(code=1013, reason="replaced")
                        except Exception:
                            pass
                    self._client = ws
                    self._connected_at = time.time()
                self._hello = hello_msg
                _log.info("extension connected from %s (auth ok)",
                          getattr(ws, "remote_address", "?"))
            else:
                # No auth required (tests/dev): accept the client immediately.
                async with self._lock:
                    self._client = ws
                    self._connected_at = time.time()
                _log.info("extension connected from %s (no auth)",
                          getattr(ws, "remote_address", "?"))

            # Main read loop. Same protocol either way: hello / keepalive / reply.
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "hello":
                    self._hello = msg
                elif mtype == "keepalive":
                    pass
                elif mtype == "reply":
                    self._on_reply(msg)
                else:
                    _log.debug("unknown bridge message: %s", msg)
        except Exception as exc:
            _log.info("bridge client disconnected: %r", exc)
        finally:
            async with self._lock:
                if self._client is ws:
                    self._client = None
                    self._connected_at = None
                for req_id, req in list(self._pending.items()):
                    if not req.future.done():
                        req.future.set_exception(
                            BridgeUnavailable("extension disconnected")
                        )
                self._pending.clear()

    def _on_reply(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("id")
        req = self._pending.pop(str(req_id), None) if req_id is not None else None
        if req is None:
            return
        if not req.future.done():
            req.future.set_result(msg)

    # ----- public API -----

    async def request(
        self,
        op: str,
        args: Optional[Dict[str, Any]] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> Dict[str, Any]:
        """Send a request to the extension and await its reply.

        Raises BridgeUnavailable if the extension never connects within
        `timeout_s` or disconnects mid-request.
        """
        if not await self.start():
            raise BridgePermanentError(
                "DOM bridge failed to start (port busy or websockets missing)."
            )
        if self._client is None:
            # Wait briefly for the extension to connect.
            deadline = time.time() + timeout_s
            while self._client is None and time.time() < deadline:
                await asyncio.sleep(0.1)
            if self._client is None:
                raise BridgeTimeout(
                    f"extension not connected within {timeout_s:.0f}s"
                )
        req_id = f"{int(time.time()*1e6)}-{os.getpid()}"
        fut: "asyncio.Future[Dict[str, Any]]" = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[req_id] = _PendingRequest(op=op, future=fut)
        payload = json.dumps({"id": req_id, "op": op, "args": args or {}})
        try:
            await self._client.send(payload)
        except Exception as exc:
            self._pending.pop(req_id, None)
            raise BridgeUnavailable(f"send failed: {exc!r}")
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise BridgeTimeout(f"reply not received within {timeout_s:.0f}s") from exc


# --------------------- module-level singleton -----------------------------

_bridge: Optional[DOMBridge] = None


def _get_bridge() -> DOMBridge:
    global _bridge
    if _bridge is None:
        _bridge = DOMBridge()
    return _bridge


async def _call(op: str, args: Dict[str, Any], timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Internal helper: send to bridge, normalize errors into structured dicts.

    Returns a JSON-serializable dict. Never raises into the MCP tool layer.
    """
    bridge = _get_bridge()
    started = await bridge.start()
    if not started:
        return {
            "ok": False,
            "error": "bridge_unavailable",
            "message": "DOM bridge failed to start (port busy or websockets missing).",
            "install_steps": _install_steps(),
        }
    try:
        reply = await bridge.request(op, args, timeout_s=min(timeout_s, HARD_TIMEOUT_S))
        return reply
    except BridgeTimeout as exc:
        return {
            "ok": False,
            "error": "bridge_timeout",
            "message": str(exc),
            "timeout_s": timeout_s,
            "install_steps": _install_steps(),
        }
    except BridgePermanentError as exc:
        return {
            "ok": False,
            "error": "bridge_unavailable",
            "message": str(exc),
            "install_steps": _install_steps(),
        }
    except BridgeUnavailable as exc:
        return {
            "ok": False,
            "error": "bridge_unavailable",
            "message": str(exc),
            "install_steps": _install_steps(),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "internal_error",
            "message": repr(exc),
        }


# --------------------- MCP tool wrappers ----------------------------------

# We don't bind @mcp.tool here; mcp_server.py imports these functions and
# registers them with explicit descriptions.


async def get_page_text_async(max_chars: int = DEFAULT_TEXT_MAX) -> Dict[str, Any]:
    """Async implementation. Validates caps, then forwards to bridge."""
    # Cap input to the hard limit (never trust the caller).
    if not isinstance(max_chars, int):
        try:
            max_chars = int(max_chars)
        except Exception:
            max_chars = DEFAULT_TEXT_MAX
    max_chars = max(100, min(HARD_TEXT_MAX, max_chars))
    reply = await _call("get_page_text", {"max_chars": max_chars})
    return _normalize_text_reply(reply, max_chars)


async def get_page_structure_async(max_items: int = DEFAULT_STRUCTURE_MAX) -> Dict[str, Any]:
    """Async implementation."""
    if not isinstance(max_items, int):
        try:
            max_items = int(max_items)
        except Exception:
            max_items = DEFAULT_STRUCTURE_MAX
    max_items = max(10, min(HARD_STRUCTURE_MAX, max_items))
    reply = await _call("get_page_structure", {"max_items": max_items})
    return _normalize_structure_reply(reply, max_items)


def get_page_text_sync(max_chars: int = DEFAULT_TEXT_MAX) -> Dict[str, Any]:
    """Synchronous wrapper used by the FastMCP tool layer.

    FastMCP tools may be sync or async; we expose sync to keep the tool
    surface uniform with the other P1 tools.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or not loop.is_running():
        return asyncio.run(get_page_text_async(max_chars))
    # If we are already inside a running loop (shouldn't happen for FastMCP
    # stdio tools, but be safe), fall back to running in a fresh thread.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(get_page_text_async(max_chars))).result()  # type: ignore[arg-type]


def get_page_structure_sync(max_items: int = DEFAULT_STRUCTURE_MAX) -> Dict[str, Any]:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or not loop.is_running():
        return asyncio.run(get_page_structure_async(max_items))
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(get_page_structure_async(max_items))).result()  # type: ignore[arg-type]


# --------------------- reply normalization -------------------------------

def _normalize_text_reply(reply: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    """Take a raw bridge reply and shape it into the tool's public payload."""
    if not reply.get("ok"):
        return reply  # pass through error shape
    data = reply.get("data") or {}
    text = data.get("text") or ""
    truncated = bool(data.get("truncated")) or len(text) >= max_chars
    return {
        "ok": True,
        "url": reply.get("url"),  # may be absent if extension didn't include
        "title": reply.get("title"),
        "text": text,
        "chars": len(text),
        "max_chars": max_chars,
        "truncated": truncated,
    }


def _normalize_structure_reply(reply: Dict[str, Any], max_items: int) -> Dict[str, Any]:
    if not reply.get("ok"):
        return reply
    data = reply.get("data") or {}
    items = data.get("items") or []
    truncated = bool(data.get("truncated")) or len(items) >= max_items
    # Categorize counts for quota reporting.
    counts: Dict[str, int] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind") or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "ok": True,
        "url": reply.get("url"),
        "title": reply.get("title"),
        "items": items,
        "count": len(items),
        "counts_by_kind": counts,
        "max_items": max_items,
        "truncated": truncated,
    }


# --------------------- install-steps probe (for tests) --------------------

def get_install_steps() -> str:
    """Return the install instructions text. Used by tests and the error path."""
    return _install_steps()


# --------------------- test-only knobs ------------------------------------

def _reset_bridge_for_tests() -> None:
    """Reset the singleton. Used only by tests."""
    global _bridge
    _bridge = None