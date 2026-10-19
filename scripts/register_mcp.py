"""Add `tool-wl-app` to Cursor's user-level mcp.json.

Run this once after install. Re-run is safe (idempotent).

Cursor on Windows stores its MCP config at %USERPROFILE%\.cursor\mcp.json.
This script merges our `tool-wl-app` entry into the existing JSON without
overwriting other servers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MCP_PATH = Path(os.environ["USERPROFILE"]) / ".cursor" / "mcp.json"

SERVER_KEY = "tool-wl-app"
PYTHON = Path(os.environ.get("WL_PYTHON", r"D:\tool-wl-app\.venv\Scripts\python.exe"))
CWD = Path(os.environ.get("WL_CWD", r"D:\tool-wl-app"))


def main() -> None:
    if not MCP_PATH.exists():
        cfg = {"mcpServers": {}}
    else:
        cfg = json.loads(MCP_PATH.read_text(encoding="utf-8"))
    cfg.setdefault("mcpServers", {})

    cfg["mcpServers"][SERVER_KEY] = {
        "command": str(PYTHON),
        "args": ["-m", "server.mcp_server"],
        "cwd": str(CWD),
    }

    MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {MCP_PATH}")
    print(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    main()