"""Register the tool-wl-app native messaging host for Chromium browsers.

Writes three HKCU\\Software\\<browser>\\NativeMessagingHosts keys, each pointing
at a JSON manifest file that names the Python host executable. Per Chrome's
spec, the manifest must contain:
    - "name":      the host's unique identifier (matches the registry key name)
    - "path":      absolute path to the executable
    - "type":      "stdio"
    - "allowed_origins": ["chrome-extension://<EXTENSION_ID>/"]

The extension ID changes per machine, so we discover it from the loaded
extension at runtime OR fall back to a wildcard (development convenience;
NOT recommended for production). For P2 we support both:

    --extension-id <id>     Pin to a specific extension ID.
    --allow-any-origin      Use ["*"] in allowed_origins (DEV ONLY).

Browsers touched
----------------
    - Chrome      (HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts)
    - Edge        (HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts)
    - Brave       (HKCU\\Software\\BraveSoftware\\Brave\\NativeMessagingHosts)
    - Firefox     (HKCU\\Software\\Mozilla\\NativeMessagingHosts; uses a
                   different manifest schema — see notes in code)

The registry keys are per-user (HKCU) so no admin rights are needed.
Run this once after installing the extension.

Idempotent: re-running updates the manifest in place.
Reversible: run scripts/unregister_native_host.py to remove the keys.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

# Make `native_host` importable when running as a script.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from native_host.tool_wl_host import NATIVE_HOST_NAME  # noqa: E402

# Allow `python -m scripts.register_native_host` as well.
HOST_SCRIPT = ROOT / "native_host" / "tool_wl_host.py"
MANIFEST_PATH = ROOT / "native_host" / "host_manifest.json"

# Browsers we register for. Each tuple is (registry_subkey, uses_chromium_manifest).
# Firefox has a different "allowed_extensions" list format.
_BROWSERS = [
    ("Google\\Chrome\\NativeMessagingHosts", True),
    ("Microsoft\\Edge\\NativeMessagingHosts", True),
    ("BraveSoftware\\Brave\\NativeMessagingHosts", True),
    ("Mozilla\\NativeMessagingHosts", False),
]


def _python_executable() -> str:
    """Pick the Python interpreter to launch the host.

    Prefer the project venv. Fall back to current interpreter.
    """
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def build_chromium_manifest(extension_id: str | None, allow_any_origin: bool) -> dict:
    """Build a Chromium-style native messaging manifest."""
    allowed_origins: list[str]
    if allow_any_origin:
        allowed_origins = ["*"]
    elif extension_id:
        allowed_origins = [f"chrome-extension://{extension_id}/"]
    else:
        # No extension ID known — refuse to write a manifest with no allowed origin.
        raise SystemExit(
            "Refusing to write a Chromium manifest with no allowed_origin. "
            "Pass --extension-id <id> or --allow-any-origin (DEV ONLY)."
        )
    return {
        "name": NATIVE_HOST_NAME,
        "description": "tool-wl-app DOM bridge host (read-only)",
        "path": _python_executable(),
        "args": ["-u", str(HOST_SCRIPT), "--loop"],
        "type": "stdio",
        "allowed_origins": allowed_origins,
    }


def build_firefox_manifest(extension_id: str | None, allow_any_origin: bool) -> dict:
    """Build a Firefox-style native messaging manifest.

    Firefox uses `allowed_extensions` (a list of bare extension UUIDs), not
    `allowed_origins`. Firefox support in P2 is best-effort.
    """
    if allow_any_origin:
        allowed_extensions = ["*"]
    elif extension_id:
        # For Firefox WebExtensions the manifest uses the bare UUID (no
        # chrome-extension:// prefix). For now we accept the same id; the
        # Firefox port is best-effort.
        allowed_extensions = [extension_id]
    else:
        raise SystemExit(
            "Refusing to write a Firefox manifest with no allowed_extensions. "
            "Pass --extension-id <id> or --allow-any-origin (DEV ONLY)."
        )
    return {
        "name": NATIVE_HOST_NAME,
        "description": "tool-wl-app DOM bridge host (read-only)",
        "path": _python_executable(),
        "args": ["-u", str(HOST_SCRIPT), "--loop"],
        "type": "stdio",
        "allowed_extensions": allowed_extensions,
    }


def _import_winreg():
    try:
        import winreg  # type: ignore
        return winreg
    except ImportError:
        return None


def _set_hkcu(reg_path: str, value_name: str, value: str, verbose: bool) -> None:
    winreg = _import_winreg()
    if winreg is None:
        raise SystemExit(
            "winreg module not available — this script must run on Windows."
        )
    full = reg_path + "\\" + NATIVE_HOST_NAME
    try:
        winreg.CreateKey(winreg.HKEY_CURRENT_USER, full)
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, full, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, value)
        if verbose:
            print(f"  wrote HKCU\\{full}  =  {value!r}")
    except OSError as exc:
        raise SystemExit(f"failed to write HKCU\\{full}: {exc!r}")


def register(
    extension_id: str | None,
    allow_any_origin: bool,
    browsers: Iterable[str] | None = None,
    manifest_path: Path = MANIFEST_PATH,
    verbose: bool = True,
) -> None:
    """Write the manifest file(s) and create the per-browser registry keys."""
    selected = []
    for sub, is_chromium in _BROWSERS:
        label = sub.split("\\")[0]
        if browsers is None or label in browsers:
            selected.append((sub, is_chromium))

    # Build the on-disk manifest. We write one manifest file; each browser
    # registry key points to the same file. Chromium and Firefox want
    # slightly different content, so we write the Chromium one (Firefox's
    # extension_id is the same when not wildcarded).
    chromium_manifest = build_chromium_manifest(extension_id, allow_any_origin)
    firefox_manifest = build_firefox_manifest(extension_id, allow_any_origin)

    # We need to write BOTH styles somewhere. The simplest: write the
    # Chromium manifest to MANIFEST_PATH and the Firefox one to
    # MANIFEST_PATH.with_suffix('.firefox.json').
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(chromium_manifest, indent=2) + "\n", encoding="utf-8"
    )
    if verbose:
        print(f"wrote Chromium manifest: {manifest_path}")

    ff_manifest_path = manifest_path.with_suffix(".firefox.json")
    ff_manifest_path.write_text(
        json.dumps(firefox_manifest, indent=2) + "\n", encoding="utf-8"
    )
    if verbose:
        print(f"wrote Firefox manifest:   {ff_manifest_path}")

    for sub, is_chromium in selected:
        value = str(manifest_path if is_chromium else ff_manifest_path)
        _set_hkcu(f"Software\\{sub}", NATIVE_HOST_NAME, value, verbose)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--extension-id", default=None,
                   help="Pin allowed_origins to chrome-extension://<id>/ (recommended).")
    p.add_argument("--allow-any-origin", action="store_true",
                   help="DEV ONLY: write allowed_origins=[*]. Do not use in production.")
    p.add_argument("--browser", action="append", default=None,
                   choices=["Google", "Microsoft", "BraveSoftware", "Mozilla"],
                   help="Limit registration to one browser. Repeatable.")
    p.add_argument("--manifest-path", default=str(MANIFEST_PATH),
                   help=f"Where to write the Chromium manifest JSON (default: {MANIFEST_PATH}).")
    args = p.parse_args()

    # We intentionally allow `--allow-any-origin` without an extension id during
    # dev: the user just installed an unpacked extension and we don't yet know
    # the generated ID. They can re-run with --extension-id after loading it.
    browsers = set(args.browser) if args.browser else None
    register(
        extension_id=args.extension_id,
        allow_any_origin=args.allow_any_origin,
        browsers=browsers,
        manifest_path=Path(args.manifest_path),
        verbose=True,
    )
    print("done. To undo: run scripts/unregister_native_host.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())