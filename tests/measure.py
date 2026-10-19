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


def main() -> None:
    out = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}

    wins = ui_automation.list_windows()
    out["list_windows"] = {
        "count": len(wins),
        "bytes_json": len(json.dumps(wins, ensure_ascii=False).encode("utf-8")),
    }

    # Sample get_ui_tree on each window (default caps: max_depth=6, max_nodes=300)
    out["get_ui_tree"] = {}
    for w in wins:
        tree = ui_automation.get_ui_tree(str(w["hwnd"]))
        out["get_ui_tree"][w["title"][:50]] = {
            "node_count": len(tree["nodes"]),
            "truncated": tree["truncated"],
            "bytes_json": len(json.dumps(tree, ensure_ascii=False).encode("utf-8")),
        }

    # Screenshots
    s_full = screenshots.screenshot_screen()
    out["screenshot_full_screen"] = {
        "width": s_full.width,
        "height": s_full.height,
        "bytes_jpeg": s_full.bytes_jpeg,
    }
    s_region = screenshots.screenshot_region(0, 0, 800, 600)
    out["screenshot_region_800x600"] = {
        "width": s_region.width,
        "height": s_region.height,
        "bytes_jpeg": s_region.bytes_jpeg,
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()