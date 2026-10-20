# tool-wl-app config directory

This directory holds the per-install **bridge token** (`bridge_token`) that
authenticates the browser extension to the loopback WebSocket bridge.

The file is auto-generated the first time the MCP server starts (see
`server/bridge_auth.py`). Rotate it any time with:

```
.\.venv\Scripts\python.exe scripts\rotate_bridge_token.py
```

The token never leaves loopback (127.0.0.1:57532) — it is the shared secret
the extension sends in its `hello` handshake so the bridge can refuse
unauthorized local connections.