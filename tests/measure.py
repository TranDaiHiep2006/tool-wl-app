"""Measure quota-saving metrics per P1 spec section 10."""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from server import ui_automation, screenshots  # noqa: E402

ui_automation.init()


def _kb(n: int) -> float:
    return round(n / 1024.0, 1)


def main() -> None:
    out = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # 1) list_windows: JSON size
    wins = ui_automation.list_windows()
    out["list_windows"] = {
        "count": len(wins),
        "bytes_json": len(json.dumps(wins, ensure_ascii=False).encode("utf-8")),
    }

    # 2) get_ui_tree on the first visible window (default caps).
    if wins:
        target_hwnd = wins[0]["hwnd"]
        tree = ui_automation.get_ui_tree(str(target_hwnd))
        out["get_ui_tree"] = {
            "window_title": tree["window_title"],
            "node_count": len(tree["nodes"]),
            "truncated": tree["truncated"],
            "bytes_json": len(json.dumps(tree, ensure_ascii=False).encode("utf-8")),
        }

    # 3) screenshot full screen.
    s_full = screenshots.screenshot_screen()
    out["screenshot_full_screen"] = {
        "path": s_full.path,
        "width": s_full.width,
        "height": s_full.height,
        "bytes_jpeg": s_full.bytes_jpeg,
    }

    # 4) screenshot region (small).
    s_region = screenshots.screenshot_region(0, 0, 800, 600)
    out["screenshot_region_800x600"] = {
        "path": s_region.path,
        "width": s_region.width,
        "height": s_region.height,
        "bytes_jpeg": s_region.bytes_jpeg,
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()