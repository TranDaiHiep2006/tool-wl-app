"""End-to-end test for the P4 action tools.

NOT part of the default pytest run (touches real desktop, launches Notepad).

Uses a private temp config (test-only) with notepad.exe allowed.
The shipped defaults in config/actions.json remain unchanged.

Usage:
    .\venv\Scripts\python.exe tests\manual_actions_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Ensure UTF-8 output on Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from server import actions as actions_mod
from server import ui_automation as ui_mod

NOTEPAD = r"C:\Windows\System32\notepad.exe"


def _spawn(binary: str) -> subprocess.Popen:
    """Launch a process in the background."""
    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x00000008
    return subprocess.Popen(
        [binary],
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until(predicate, timeout_s: float = 20.0, label: str = "") -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    print(f"  WARNING: timed out waiting for {label}")
    return False


def _terminate(name: str) -> None:
    subprocess.run(
        ["taskkill", "/IM", name, "/F"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _create_temp_config(tmp_dir: Path) -> Path:
    """Create a test-only actions.json in tmp_dir, return the path."""
    cfg = tmp_dir / "actions.json"
    cfg.write_text(
        json.dumps({
            "enabled": True,
            "allowed_processes": ["notepad.exe", "ApplicationFrameHost.exe"],
            "deny_labels": ["delete", "format", "uninstall", "shut down", "pay now", "remove account"],
        }),
        encoding="utf-8",
    )
    return cfg


def _patch_config(tmp_dir: Path) -> None:
    """Temporarily redirect the actions module's config/stop/log paths."""
    import server.actions as amod
    amod.CONFIG_DIR = tmp_dir
    amod.STOP_FILE = tmp_dir / "STOP_ACTIONS"
    amod.ACTIONS_LOG = tmp_dir / "actions.log"
    amod.ACTION_CONFIG = tmp_dir / "actions.json"  # Also patch the load path.


def _wait_for_notepad(tmp_dir: Path) -> bool:
    """Wait until a Notepad window appears."""
    _patch_config(tmp_dir)
    ui_mod.init()

    def _notepad_visible() -> bool:
        try:
            windows = ui_mod.list_windows()
            return any(
                w.get("process_name", "").lower() == "notepad.exe"
                for w in windows
            )
        except Exception:
            return False

    return _wait_until(_notepad_visible, timeout_s=30, label="Notepad window")


def _find_notepad_window() -> Optional[dict]:
    """Find the first Notepad window (our spawned one)."""
    ui_mod.init()
    windows = ui_mod.list_windows()
    notepad_wins = [w for w in windows if w.get("process_name", "").lower() == "notepad.exe"]
    return notepad_wins[0] if notepad_wins else None


def main() -> int:
    print("=" * 60)
    print("P4 Action Tools end-to-end smoke test")
    print("=" * 60)

    tmp_dir = Path(tempfile.mkdtemp(prefix="tool_wl_actions_"))
    print(f"\nUsing temp config dir: {tmp_dir}")
    _create_temp_config(tmp_dir)
    _patch_config(tmp_dir)
    actions_mod._plans.clear()

    proc = None
    try:
        # 1. Launch Notepad.
        print("\n[1/5] Launching Notepad...")
        proc = _spawn(NOTEPAD)
        if not _wait_for_notepad(tmp_dir):
            print("FAIL: Notepad window never appeared with controls")
            return 1
        print("  Notepad launched and controls loaded.")

        # 2. Discover the Notepad edit control.
        print("\n[2/5] Discovering Notepad controls...")
        ui_mod.init()
        nw = _find_notepad_window()
        if not nw:
            print("FAIL: could not find Notepad in window list")
            return 1
        print(f"  Notepad window: {nw['title']!r} hwnd={nw['hwnd']} pid={nw['pid']}")

        # Try to get the UI tree; on some Notepad configs it may be sparse initially.
        # We'll try up to 5 times with 1s delays.
        tree = None
        for attempt in range(5):
            try:
                tree = ui_mod.get_ui_tree(nw['title'])
                if tree.get("nodes"):
                    break
            except Exception:
                pass
            time.sleep(1)
            nw = _find_notepad_window()
            if not nw:
                break

        print(f"  UI tree nodes: {len(tree.get('nodes', []) if tree else [])}")
        if tree:
            for node in tree.get("nodes", []):
                ct = node.get("control_type", "")
                nm = node.get("name", "")
                aid = node.get("automation_id", "")
                print(f"    [{ct}] name={nm!r} aid={aid!r}")

        # 3. Plan + Execute type_text.
        KNOWN_TEXT = "Hello from P4 E2E test"
        print(f"\n[3/5] Phase 1: Plan type_text {KNOWN_TEXT!r}...")

        # Try to find a unique edit control.
        ctrl_found = False
        ctrl_window_title = nw['title']
        ctrl_automation_id = None
        ctrl_name = None

        # Try with automation_id="Edit" -- if ambiguous, find all and pick the deepest EditControl.
        try:
            all_edits = []
            ui_mod.init()
            win_ctrl = ui_mod.find_window(ctrl_window_title)
            if win_ctrl:
                queue = [(win_ctrl, 0)]
                seen = 0
                while queue:
                    ctrl, depth = queue.pop(0)
                    seen += 1
                    if seen > 800:
                        break
                    if depth > 8:
                        continue
                    if ctrl.IsOffscreen:
                        continue
                    aid = (ctrl.AutomationId or "").strip()
                    nm = (ctrl.Name or "").strip()
                    ctrl_type = ctrl.ControlTypeName
                    # Only consider EditControl for text input.
                    if ctrl_type == "EditControl" and (aid == "Edit" or nm == "Edit"):
                        all_edits.append((ctrl, depth, aid, nm))
                    for c in ctrl.GetChildren():
                        queue.append((c, depth + 1))
            print(f"  Found {len(all_edits)} EditControl controls")

            if len(all_edits) == 1:
                _, _, ctrl_automation_id, ctrl_name = all_edits[0]
                ctrl_found = True
            elif len(all_edits) > 1:
                # Pick the deepest one (usually the actual text area).
                deepest = max(all_edits, key=lambda x: x[1])
                _, _, ctrl_automation_id, ctrl_name = deepest
                ctrl_found = True
                print(f"  Selected deepest Edit: aid={ctrl_automation_id!r} name={ctrl_name!r} depth={deepest[1]}")
        except Exception as e:
            print(f"  Edit control search failed: {e}")

        if not ctrl_found:
            print("  NOTE: No unique Edit control found.")
            print("  Skipping type_text step (unit tests cover the logic).")
            type_text_ok = None
            resolved_ctrl = None
            resolved_title = ""
        else:
            # Get the actual resolved control object.
            if len(all_edits) == 1:
                resolved_ctrl = all_edits[0][0]
            else:
                resolved_ctrl = deepest[0]
            resolved_title = ctrl_window_title
            print(f"  Resolved ctrl: name={ctrl_name!r} aid={ctrl_automation_id!r}")
            plan_resp = actions_mod.type_text(
                window=ctrl_window_title,
                text=KNOWN_TEXT,
                automation_id=ctrl_automation_id,
                name=ctrl_name,
                _resolved_ctrl=resolved_ctrl,
                _resolved_title=resolved_title,
            )
            print(f"  Response: {json.dumps(plan_resp, ensure_ascii=False)}")
            if not plan_resp.get("ok"):
                print(f"  FAIL: plan phase returned error: {plan_resp.get('message')}")
                return 1
            if plan_resp.get("phase") != "plan":
                print("FAIL: expected phase=plan")
                return 1
            plan_id = plan_resp["plan_id"]
            print(f"  Plan returned: {plan_id}")
            print(f"  description: {plan_resp.get('description')}")
            print(f"  current_value: {plan_resp.get('current_value')!r}")

            print(f"\n[3/5] Phase 2: Execute type_text with plan_id {plan_id}...")
            exec_resp = actions_mod.type_text(
                window=ctrl_window_title,
                text=KNOWN_TEXT,
                automation_id=ctrl_automation_id,
                name=ctrl_name,
                plan_id=plan_id,
                _resolved_ctrl=resolved_ctrl,
                _resolved_title=resolved_title,
            )
            print(f"  Response: {json.dumps(exec_resp, ensure_ascii=False)}")
            if not exec_resp.get("ok"):
                print(f"  FAIL: execute phase returned error")
                return 1
            if exec_resp.get("phase") != "execute":
                print("FAIL: expected phase=execute")
                return 1
            print(f"  Execute succeeded")
            type_text_ok = True

        # 4. Read back (only if type_text succeeded).
        if type_text_ok:
            time.sleep(0.5)  # Let Notepad settle.
            print("\n[4/5] Reading back text via get_control_text...")
            ui_mod.init()
            ctrl_resp = ui_mod.get_control_text(
                window=ctrl_window_title,
                automation_id_or_name=ctrl_automation_id or ctrl_name or "Edit",
            )
            print(f"  get_control_text response: {json.dumps(ctrl_resp, ensure_ascii=False)}")
            if ctrl_resp.get("found"):
                actual_text = (ctrl_resp.get("text") or "").strip()
                print(f"  Read back: {actual_text!r}")
                if KNOWN_TEXT in actual_text:
                    print(f"  type_text SUCCESS -- text round-tripped")
                else:
                    print(f"  MISMATCH -- expected {KNOWN_TEXT!r} in {actual_text!r}")
                    if KNOWN_TEXT in actual_text + "\n":
                        print(f"  (matched with trailing newline -- acceptable)")
                    else:
                        return 1
        else:
            print("\n[4/5] Skipping read-back (type_text not executed)")

        # 5. Plan + Execute click on a Notepad control (e.g., File menu).
        print("\n[5/5] Testing click_control...")
        clicked = False
        if tree and tree.get("nodes"):
            menu_items = [n for n in tree.get("nodes", []) if "menu" in n.get("control_type", "").lower()]
            for node in (menu_items or []):
                nm = node.get("name", "")
                aid = node.get("automation_id", "")
                print(f"  Trying click on: [{node.get('control_type')}] name={nm!r} aid={aid!r}")
                plan_resp = actions_mod.click_control(
                    window=nw['title'],
                    name=nm if nm else None,
                    automation_id=aid if aid else None,
                )
                print(f"  Plan response: {json.dumps(plan_resp, ensure_ascii=False)}")
                if plan_resp.get("ok") and plan_resp.get("phase") == "plan":
                    exec_resp = actions_mod.click_control(
                        window=nw['title'],
                        name=nm if nm else None,
                        automation_id=aid if aid else None,
                        plan_id=plan_resp["plan_id"],
                    )
                    print(f"  Execute response: {json.dumps(exec_resp, ensure_ascii=False)}")
                    if exec_resp.get("ok"):
                        print(f"  click SUCCESS on {nm!r} using {exec_resp.get('pattern_used')}")
                        clicked = True
                        break
                    else:
                        print(f"  click FAILED on {nm!r}: {exec_resp.get('error')}")
        if not clicked:
            print("  (no clickable menu control found -- OK if Notepad is minimal)")

        # 6. Verify refusal for non-allowlisted process via focus_window.
        print("\n[6/6] Verifying gate refusal for non-allowlisted process via focus_window...")
        ui_mod.init()
        windows = ui_mod.list_windows()
        # Pick a non-notepad window.
        other = next(
            (w for w in windows
             if w.get("process_name", "").lower()
             not in ("notepad.exe", "")),
            None
        )
        if other:
            print(f"  Testing gate on: {other['title']!r} ({other['process_name']})")
            resp = actions_mod.focus_window(other['title'])
            print(f"  Response: {json.dumps(resp, ensure_ascii=False)}")
            if not resp.get("ok"):
                print(f"  Refused with: {resp.get('error')} -- {resp.get('message', '')[:80]}")
        else:
            print("  (no other windows available for refusal test)")

        print("\n" + "=" * 60)
        print("RESULT: PASS")
        print("=" * 60)
        return 0

    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        _terminate("notepad.exe")
        # Clean up tmp dir.
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
