"""Native Messaging host package for tool-wl-app.

Hosts the `chrome.runtime.sendNativeMessage` entry point. See
`tool_wl_host.py` for the protocol implementation.

Production bridge
-----------------
The MCP server (`server/browser_dom.py`) is the actual bridge to the browser
extension over a local WebSocket on `127.0.0.1:57532`. This package provides
the *standard* Chrome Native Messaging entry point, satisfying Chrome's
security model (the browser must know the path of the host binary and a
unique nativeMessagingHosts manifest name in the registry).
"""