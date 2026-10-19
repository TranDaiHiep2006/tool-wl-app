"""Unregister the tool-wl-app native messaging host.

Removes the per-browser HKCU\\Software\\<browser>\\NativeMessagingHosts keys
that `register_native_host.py` created. Also deletes the on-disk manifest
files (Chromium + Firefox) so a clean uninstall leaves no residue.

Reverses exactly what register does. Idempotent: running on a clean system
is a no-op.

If `--manifest-path` differs from the path used at registration time, only
the registry keys are removed; the original manifest files (if any) remain.
Use `--keep-manifest` to keep the on-disk manifest even when it matches the
default.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from native_host.tool_wl_host import NATIVE_HOST_NAME  # noqa: E402
from scripts.register_native_host import _BROWSERS, MANIFEST_PATH  # noqa: E402


def _import_winreg():
    try:
        import winreg  # type: ignore
        return winreg
    except ImportError:
        return None


def _delete_hkcu(reg_path: str, verbose: bool) -> bool:
    winreg = _import_winreg()
    if winreg is None:
        raise SystemExit(
            "winreg module not available — this script must run on Windows."
        )
    full = reg_path + "\\" + NATIVE_HOST_NAME
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, full)
        if verbose:
            print(f"  removed HKCU\\{full}")
        return True
    except FileNotFoundError:
        if verbose:
            print(f"  (not present) HKCU\\{full}")
        return False
    except OSError as exc:
        raise SystemExit(f"failed to remove HKCU\\{full}: {exc!r}")


def unregister(
    browsers: Iterable[str] | None = None,
    manifest_path: Path = MANIFEST_PATH,
    keep_manifest: bool = False,
    verbose: bool = True,
) -> None:
    selected = []
    for sub, _ in _BROWSERS:
        label = sub.split("\\")[0]
        if browsers is None or label in browsers:
            selected.append(sub)
    for sub in selected:
        _delete_hkcu(f"Software\\{sub}", verbose)
    if keep_manifest:
        if verbose:
            print(f"(kept) {manifest_path}")
        return
    # Remove on-disk manifests (Chromium + Firefox).
    for p in [manifest_path, manifest_path.with_suffix(".firefox.json")]:
        try:
            p.unlink()
            if verbose:
                print(f"removed {p}")
        except FileNotFoundError:
            if verbose:
                print(f"(not present) {p}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--browser", action="append", default=None,
                   choices=["Google", "Microsoft", "BraveSoftware", "Mozilla"],
                   help="Limit unregistration to one browser. Repeatable.")
    p.add_argument("--manifest-path", default=str(MANIFEST_PATH),
                   help=f"Manifest path to also delete (default: {MANIFEST_PATH}).")
    p.add_argument("--keep-manifest", action="store_true",
                   help="Do not delete the on-disk manifest files.")
    args = p.parse_args()

    browsers = set(args.browser) if args.browser else None
    unregister(
        browsers=browsers,
        manifest_path=Path(args.manifest_path),
        keep_manifest=args.keep_manifest,
        verbose=True,
    )
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())