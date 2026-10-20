"""Rotate the per-install bridge token.

Generates a fresh secret token and rewrites BOTH:
  - config/bridge_token
  - extension/token.js (the module the extension imports)

After running this, the currently-connected extension will be rejected
on its next hello (its token is stale) and will reconnect with the new
one. No need to re-register anything — the bridge re-reads the token
from disk on every new connection.

Usage:
    .\\.venv\\Scripts\\python.exe scripts/rotate_bridge_token.py
    .\\.venv\\Scripts\\python.exe scripts/rotate_bridge_token.py --dry-run
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from server.bridge_auth import (  # noqa: E402
    TOKEN_FILE,
    TOKEN_JS_FILE,
    rotate_token,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print the new token but do not write any files.")
    args = p.parse_args()

    if args.dry_run:
        new = secrets.token_urlsafe(32)
        print(f"(dry-run) new token would be: {new}")
        print("(dry-run) no files were modified.")
        return 0

    new = rotate_token()
    print(f"rotated bridge token: {new}")
    print(f"  wrote {TOKEN_FILE}")
    print(f"  wrote {TOKEN_JS_FILE}")
    print()
    print("The currently-connected extension will reconnect on its next")
    print("hello with the new token (MV3 service worker backoff ≤ 30s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())