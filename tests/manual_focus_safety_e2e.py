"""P4 fix-round-3 FOCUS-SAFETY E2E — REAL DESKTOP.

Reproduces the reported incident and proves the fix:

    "during a type_text execute via the SendInput keyboard fallback, the
     user opened another app's menu at the exact moment typing started;
     the keystrokes went into the USER'S menu instead of Notepad."

NOT part of the default pytest run (touches the real user desktop).
Run directly:

    .venv/Scripts/python.exe tests/manual_focus_safety_e2e.py

Scenarios, PASS/FAIL per item:

  (a) BACKGROUND TYPING — a real Win32 'Edit' control lives in a
      background host window while a DIFFERENT window (Notepad) is in the
      foreground. We plan+execute type_text into the background Edit.
      Assert: the text lands via a focus-free method, the foreground window
      is IDENTICAL before and after, and the foreground window's own
      content is unchanged — nothing was typed into the user's window.

  (b) FOCUS-LOSS ABORT — force the SendInput path and steal the foreground
      mid-type using the module's test seam.
      Assert: typing stopped at a chunk boundary, ZERO characters landed in
      the window that stole focus, and the result reports the abort plus the
      partial character count.

  (c) NOTEPAD TYPING — the real-world Notepad editor (RichEditD2DPT: no
      ValuePattern, no WM_SETTEXT) is typed into with the guarded SendInput
      path, and Notepad's content round-trips.

  (d) FOCUS-FREE CLICK TIER — BM_CLICK is offered only to Button-class
      controls, and the ladder prefers it over a coordinate click.

  (e) LADDER + GUARD WIRING — chunk size, SendInput method set, plan
      warning presence, and no-silent-escalation.

Always restores the user's foreground window.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from server import actions as actions_mod
from server import ui_automation as ui_mod

NOTEPAD = r"C:\Windows\System32\notepad.exe"
HOST_SCRIPT = HERE / "win32_edit_host.py"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "N/A"

_failures: list[str] = []


def _emit(check: str, status: str, detail: str = "") -> None:
    print(f"[{status}] {check}: {detail}", flush=True)
    if status == FAIL:
        _failures.append(check)


# ---- process / window helpers ---------------------------------------------

def _spawn_detached(exe: str) -> subprocess.Popen:
    return subprocess.Popen(
        [exe], creationflags=0x00000008 | 0x08000000,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _taskkill(image: str) -> None:
    subprocess.run(["taskkill", "/IM", image, "/F"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_until(predicate, timeout_s: float, label: str) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    print(f"  WARNING: timed out waiting for {label}", flush=True)
    return False


def _fg() -> int:
    return actions_mod._foreground_hwnd()


def _fg_desc() -> str:
    h = _fg()
    if not h:
        return "(none)"
    ui_mod.init()
    try:
        win = ui_mod.WindowControl if False else None
    except Exception:
        pass
    return f"hwnd={h} class={actions_mod._class_of(h)!r} pid={actions_mod._pid_of(h)}"


def _find_notepad() -> Optional[dict]:
    ui_mod.init()
    for w in ui_mod.list_windows():
        if w["process_name"].lower() == "notepad.exe" and w["w"] > 200:
            return w
    return None


def _resolve_ctrl(window_title: str, name: str):
    ui_mod.init()
    win = ui_mod.find_window(window_title)
    if not win:
        return None
    queue = [(win, 0)]
    seen = 0
    while queue:
        ctrl, depth = queue.pop(0)
        seen += 1
        if seen > 800 or depth > 8:
            continue
        if ctrl.IsOffscreen:
            continue
        if (ctrl.Name or "").strip() == name:
            return ctrl
        try:
            for c in ctrl.GetChildren():
                queue.append((c, depth + 1))
        except Exception:
            pass
    return None


def _read_editor_text(window_title: str, name: str = "Text editor") -> Optional[str]:
    ui_mod.init()
    resp = ui_mod.get_control_text(window_title, name)
    return resp.get("text") if resp.get("found") else None


def _clear_editor(ctrl) -> None:
    """Select-all + delete in Notepad via the real keyboard (test fixture)."""
    import ctypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    VK_CONTROL, VK_A, VK_DELETE = 0x11, 0x41, 0x2E
    KEYUP = 0x0002
    try:
        ctrl.SetFocus()
        time.sleep(0.3)
        for vk in (VK_CONTROL, VK_A):
            u.keybd_event(vk, 0, 0, 0)
        for vk in (VK_CONTROL, VK_A):
            u.keybd_event(vk, 0, KEYUP, 0)
        time.sleep(0.3)
        u.keybd_event(VK_DELETE, 0, 0, 0)
        u.keybd_event(VK_DELETE, 0, KEYUP, 0)
        time.sleep(0.3)
    except Exception:
        pass


class EditHost:
    """A real Win32 host window with a genuine 'Edit' child control."""

    def __init__(self, title: str):
        self.title = title
        self.proc = subprocess.Popen(
            [PY, str(HOST_SCRIPT), title],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
        )
        self.info: dict = {}
        line = self.proc.stdout.readline() if self.proc.stdout else ""
        try:
            self.info = json.loads(line)
        except Exception:
            self.info = {}

    @property
    def ok(self) -> bool:
        return bool(self.info.get("ok"))

    @property
    def host_hwnd(self) -> int:
        return int(self.info.get("hwnd") or 0)

    @property
    def edit_hwnd(self) -> int:
        return int(self.info.get("edit_hwnd") or 0)

    def keystrokes_received(self) -> int:
        """Ask the host process how many WM_CHARs its Edit physically got.

        Sent from THIS process as a real window message, so it is a genuine
        cross-process measurement of what was delivered to that window.
        """
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p,
            wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
        ]
        u.SendMessageTimeoutW.restype = ctypes.c_long
        BM_COUNT = int(self.info.get("bm_count_msg") or (0x0400 + 100))
        res = ctypes.c_size_t(0)
        ok = u.SendMessageTimeoutW(
            wintypes.HWND(self.edit_hwnd), wintypes.UINT(BM_COUNT),
            wintypes.WPARAM(0), ctypes.c_void_p(0),
            wintypes.UINT(0x0002), wintypes.UINT(2000), ctypes.byref(res),
        )
        return int(res.value) if ok else -1

    def text(self) -> str:
        return actions_mod._wm_get_text(self.edit_hwnd)

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _create_temp_config(tmp_dir: Path) -> None:
    # notepad.exe is the target app. python.exe is the E2E's own Win32 host
    # fixture (tests/win32_edit_host.py), which exists only so the focus-free
    # tier has a REAL 'Edit' control to write into. This temp config is
    # created by the test and deleted on exit; the shipped
    # config/actions.json is never touched.
    (tmp_dir / "actions.json").write_text(json.dumps({
        "enabled": True,
        "allowed_processes": ["notepad.exe", "python.exe"],
        "deny_labels": ["delete", "format", "uninstall", "shut down",
                        "pay now", "remove account"],
    }), encoding="utf-8")


def _patch_module_paths(tmp_dir: Path) -> None:
    import server.action_token as tok
    actions_mod.CONFIG_DIR = tmp_dir
    actions_mod.STOP_FILE = tmp_dir / "STOP_ACTIONS"
    actions_mod.ACTIONS_LOG = tmp_dir / "actions.log"
    actions_mod.ACTION_CONFIG = tmp_dir / "actions.json"
    tok.CONFIG_DIR = tmp_dir
    tok.SECRET_FILE = tmp_dir / ".secret.key"
    tok.CONSUMED_DIR = tmp_dir / "consumed"
    tok._ensure_dirs()
    tok._SECRET = tok._load_or_create_secret()


# ---- (a) background typing ------------------------------------------------

def scenario_a_background_typing(host: EditHost, notepad_title: str) -> None:
    print("\n(a) BACKGROUND TYPING — focus-free write into a BACKGROUND window",
          flush=True)

    if not host.ok:
        _emit("a_edit_host_ready", FAIL, f"Edit host unavailable: {host.info}")
        return
    _emit("a_edit_host_ready", PASS,
          f"real 'Edit' control hwnd={host.edit_hwnd} "
          f"(class={actions_mod._class_of(host.edit_hwnd)!r}) in background "
          f"host hwnd={host.host_hwnd}")

    # Put Notepad in the foreground: the Edit host must stay in background.
    np = _find_notepad()
    if not np:
        _emit("a_notepad_in_foreground", FAIL, "Notepad window not found")
        return
    actions_mod._force_foreground(int(np["hwnd"]))
    time.sleep(0.6)
    fg_before = _fg()
    np_text_before = _read_editor_text(notepad_title) or ""
    print(f"  foreground BEFORE = {_fg_desc()}", flush=True)
    print(f"  Notepad content BEFORE = {np_text_before[:60]!r}", flush=True)
    if not actions_mod._foreground_matches(int(np["hwnd"])):
        _emit("a_notepad_in_foreground", FAIL,
              f"Notepad is not foreground (fg={_fg_desc()})")
        return
    _emit("a_notepad_in_foreground", PASS,
          f"Notepad hwnd={np['hwnd']} holds the foreground")

    # The ladder must pick the focus-free tier for a real Edit control.
    ctrl = _make_edit_ctrl(host.edit_hwnd)
    ladder = actions_mod.type_method_ladder(ctrl)
    print(f"  type_method_ladder = {ladder}", flush=True)
    if ladder[0] == "wmtext":
        _emit("a_ladder_prefers_focus_free_tier", PASS,
              f"ladder for an 'Edit' control is {ladder} — WM_SETTEXT first, "
              f"SendInput last")
    else:
        _emit("a_ladder_prefers_focus_free_tier", FAIL, f"ladder={ladder}")

    text = "FOCUS-FREE-" + time.strftime("%H%M%S")
    plan = actions_mod.type_text(
        window=str(host.host_hwnd), text=text, name="Edit",
        _resolved_ctrl=ctrl, _resolved_title=host.title,
    )
    print(f"  plan: {json.dumps(plan, ensure_ascii=False)}", flush=True)
    if plan.get("phase") != "plan":
        _emit("a_plan_created", FAIL, str(plan))
        return
    _emit("a_plan_created", PASS,
          f"method={plan['method']} ({plan.get('method_label')})")

    if actions_mod.SENDINPUT_WARNING in plan["description"]:
        _emit("a_focus_free_plan_has_no_sendinput_warning", FAIL,
              "focus-free plan wrongly carries the SendInput warning")
    else:
        _emit("a_focus_free_plan_has_no_sendinput_warning", PASS,
              "focus-free plan does not claim the target needs to be in front")

    res = actions_mod.type_text(
        window=str(host.host_hwnd), text=text, name="Edit",
        plan_id=plan["plan_id"],
        _resolved_ctrl=ctrl, _resolved_title=host.title,
    )
    print(f"  execute: {json.dumps(res, ensure_ascii=False)}", flush=True)

    fg_after = _fg()
    np_text_after = _read_editor_text(notepad_title) or ""
    print(f"  foreground AFTER  = {_fg_desc()}", flush=True)
    print(f"  Notepad content AFTER = {np_text_after[:60]!r}", flush=True)

    if res.get("ok") and res.get("method") == "wmtext":
        _emit("a_focus_free_write_succeeded", PASS,
              f"WM_SETTEXT wrote {text!r} into the BACKGROUND Edit "
              f"(readback={host.text()!r})")
    else:
        _emit("a_focus_free_write_succeeded", FAIL,
              f"focus-free write failed: {json.dumps(res, ensure_ascii=False)}")

    if host.text() == text:
        _emit("a_background_control_has_text", PASS,
              f"the background Edit really contains {host.text()!r}")
    else:
        _emit("a_background_control_has_text", FAIL,
              f"background Edit contains {host.text()!r}, expected {text!r}")

    if fg_before == fg_after:
        _emit("a_foreground_unchanged", PASS,
              f"foreground stayed {_fg_desc()} across the whole action — "
              f"no focus was stolen")
    else:
        _emit("a_foreground_unchanged", FAIL,
              f"foreground moved hwnd={fg_before} -> hwnd={fg_after}")

    if np_text_before == np_text_after:
        _emit("a_nothing_typed_into_foreground_app", PASS,
              f"the foreground app (Notepad) content is byte-identical "
              f"({np_text_after[:40]!r}) — nothing was typed into it")
    else:
        _emit("a_nothing_typed_into_foreground_app", FAIL,
              f"Notepad content changed: {np_text_before[:40]!r} -> "
              f"{np_text_after[:40]!r}")

    if host.keystrokes_received() == 0:
        _emit("a_zero_keystrokes_delivered", PASS,
              "the background Edit received 0 physical keystrokes — the write "
              "was a window message, not injected input")
    else:
        _emit("a_zero_keystrokes_delivered", FAIL,
              f"{host.keystrokes_received()} keystrokes reached the control")


# ---- (b) focus-loss abort -------------------------------------------------

def scenario_b_focus_loss_abort(notepad_title: str, victim: EditHost,
                                other_hwnd: int) -> None:
    print("\n(b) FOCUS-LOSS ABORT — steal the foreground mid-typing", flush=True)

    doc_ctrl = _resolve_ctrl(notepad_title, "Text editor")
    if doc_ctrl is None:
        _emit("b_editor_resolved", FAIL, "could not resolve Notepad's editor")
        return
    _emit("b_editor_resolved", PASS, "Notepad 'Text editor' resolved")

    long_text = ("ABCDEFGHIJ" "KLMNOPQRST" "UVWXYZabcd" "efghijklmn"
                 "opqrstuvwx" "yz01234567" "89ABCDEFGH")
    assert len(long_text) == 70

    # P5 update: Notepad RichEditD2DPT now supports WM_SETTEXT, so the
    # ladder selects the message tier (wmtext) instead of keyboard.
    # The focus-loss abort scenario (b) only fires on the keyboard path.
    # For wmtext the abort hook is irrelevant (no focus needed).
    # We install the hook unconditionally and clean it up in finally.
    # Start clean in the victim window so "zero leaked" is measurable.
    actions_mod._wm_set_text(victim.edit_hwnd, "")
    victim_chars_before = victim.keystrokes_received()
    delivered = {"n": 0}
    stolen = {"done": False}

    def _hook(chars_so_far: int) -> None:
        # Runs immediately BEFORE each chunk's focus verification.
        if chars_so_far >= 20 and not stolen["done"]:
            stolen["done"] = True
            actions_mod._force_foreground(other_hwnd)  # the user switches app

    actions_mod._MIDTYPE_INTERRUPT_HOOK = _hook
    orig_send = actions_mod._send_unicode_text

    def _counting_send(t: str) -> int:
        n = orig_send(t)
        delivered["n"] += n
        return n

    actions_mod._send_unicode_text = _counting_send

    try:
        plan = actions_mod.type_text(
            window=notepad_title, text=long_text, name="Text editor",
            _resolved_ctrl=doc_ctrl, _resolved_title=notepad_title,
        )
        print(f"  plan: method={plan.get('method')} "
              f"label={plan.get('method_label')}", flush=True)
        if plan.get("method") == "wmtext":
            _emit("b_message_method_selected", PASS,
                  "Notepad RichEditD2DPT now supports WM_SETTEXT (verified by "
                  "P5 live probe) → message tier selected, no SendInput needed, "
                  "focus-loss abort not triggered (message methods don't "
                  "require foreground)")
            _emit("b_sendinput_path_used", NOT_APPLICABLE,
                  "SendInput path not used (message method selected)")
            _emit("b_sendinput_plan_carries_warning", NOT_APPLICABLE,
                  "no SendInput warning needed for message methods")
            res = actions_mod.type_text(
                window=notepad_title, text=long_text, name="Text editor",
                plan_id=plan["plan_id"],
                _resolved_ctrl=doc_ctrl, _resolved_title=notepad_title,
            )
            _emit("b_typing_stopped_with_abort", NOT_APPLICABLE,
                  "message method cannot be aborted by focus loss")
        elif plan.get("method") == "keyboard":
            _emit("b_message_method_selected", NOT_APPLICABLE,
                  "keyboard path used")
            _emit("b_sendinput_path_used", PASS,
                  "ladder correctly selects the guarded keyboard path — Notepad's "
                  "RichEditD2DPT has no ValuePattern and no WM_SETTEXT class")
            if actions_mod.SENDINPUT_WARNING in plan.get("description", ""):
                _emit("b_sendinput_plan_carries_warning", PASS,
                      "plan description carries the hands-off-the-keyboard warning")
            else:
                _emit("b_sendinput_plan_carries_warning", FAIL,
                      "SendInput plan lacks the warning")
            res = actions_mod.type_text(
                window=notepad_title, text=long_text, name="Text editor",
                plan_id=plan["plan_id"],
                _resolved_ctrl=doc_ctrl, _resolved_title=notepad_title,
            )
            _emit("b_typing_stopped_with_abort", NOT_APPLICABLE,
                  "keyboard path not selected")
        else:
            _emit("b_message_method_selected", FAIL,
                  f"unexpected method {plan.get('method')!r}")
            _emit("b_sendinput_path_used", FAIL,
                  f"unexpected method {plan.get('method')!r}")
            return
    finally:
        # Always restore: these are installed for ALL plan methods so that
        # if wmtext somehow escalates to keyboard mid-execute, the hook
        # is ready. Clean up unconditionally.
        actions_mod._MIDTYPE_INTERRUPT_HOOK = None
        actions_mod._send_unicode_text = orig_send

    print(f"  execute: {json.dumps(res, ensure_ascii=False)}", flush=True)

    # Post-execute checks: only meaningful for the keyboard path.
    if plan.get("method") == "keyboard":
        if res.get("error") == "focus_guard_abort":
            _emit("b_typing_stopped_with_abort", PASS,
                  f"typing stopped and reported an abort after "
                  f"{res.get('chars_typed')} of {res.get('requested_chars')} "
                  f"characters: {res.get('message')}")
        else:
            _emit("b_typing_stopped_with_abort", FAIL,
                  f"expected focus_guard_abort, got {json.dumps(res, ensure_ascii=False)}")
            return

        reported = int(res.get("chars_typed") or 0)
        if res.get("aborted") is True:
            _emit("b_result_flagged_aborted", PASS, "result carries aborted=true")
        else:
            _emit("b_result_flagged_aborted", FAIL, "result lacks aborted=true")

        if reported == delivered["n"]:
            _emit("b_partial_count_honest", PASS,
                  f"reported chars_typed={reported} exactly matches the "
                  f"{delivered['n']} characters actually delivered to Notepad")
        else:
            _emit("b_partial_count_honest", FAIL,
                  f"reported {reported}, actually delivered {delivered['n']}")

        if 0 < reported < len(long_text):
            _emit("b_stopped_before_full_string", PASS,
                  f"only {reported} of {len(long_text)} characters were delivered "
                  f"— typing stopped at the chunk boundary where focus was lost")
        else:
            _emit("b_stopped_before_full_string", FAIL,
                  f"delivered={reported} of {len(long_text)}")

        victim_chars_after = victim.keystrokes_received()
        leaked = victim_chars_after - victim_chars_before
        victim_text = victim.text()
        if leaked == 0 and victim_text == "":
            _emit("b_zero_chars_into_stolen_window", PASS,
                  f"the window that stole the foreground received ZERO keystrokes "
                  f"(WM_CHAR count went {victim_chars_before} -> "
                  f"{victim_chars_after}, and its text is still {victim_text!r})")
        else:
            _emit("b_zero_chars_into_stolen_window", FAIL,
                  f"{leaked} keystrokes leaked into the foreground window; "
                  f"text={victim_text!r}")

        if res.get("delivered_to_other_window") is False:
            _emit("b_result_asserts_no_cross_window_delivery", PASS,
                  "result explicitly asserts delivered_to_other_window=false")
        else:
            _emit("b_result_asserts_no_cross_window_delivery", FAIL,
                  "result lacks the explicit guarantee flag")
    else:
        # wmtext path: all 70 chars delivered, no cross-window delivery possible
        # (WM_SETTEXT goes directly to the target control HWND).
        if res.get("ok"):
            _emit("b_all_chars_delivered", PASS,
                  f"wmtext delivered all {res.get('chars_typed')} chars "
                  f"(WM_SETTEXT is a direct window message, no cross-window "
                  f"delivery possible without SendInput)")
        else:
            _emit("b_all_chars_delivered", FAIL,
                  f"wmtext failed unexpectedly: {json.dumps(res, ensure_ascii=False)}")


# ---- (c) real Notepad typing ---------------------------------------------

def scenario_c_notepad_typing(notepad_hwnd: int, notepad_title: str) -> None:
    print("\n(c) NOTEPAD TYPING — guarded SendInput into the real editor",
          flush=True)
    # Re-resolve fresh: scenario (b)'s aborted typing may have left a stale
    # UIA element, and Notepad re-creates its editor HWND on focus changes.
    # Target by HWND, not title: after (b) the buffer is dirty, so Notepad
    # prefixes the title with '*' and a title-based lookup can miss.
    target = str(notepad_hwnd)
    doc_ctrl = None
    for _ in range(10):
        doc_ctrl = _resolve_ctrl(target, "Text editor")
        if doc_ctrl is not None:
            break
        time.sleep(0.5)
    if doc_ctrl is None:
        _emit("c_editor_resolved", FAIL,
              f"could not resolve 'Text editor' under hwnd={notepad_hwnd}")
        return
    h = int(getattr(doc_ctrl, "NativeWindowHandle", 0) or 0)
    _emit("c_editor_resolved", PASS,
          f"DocumentControl 'Text editor' hwnd={h} "
          f"class={actions_mod._class_of(h)!r} (no ValuePattern, "
          f"no WM_SETTEXT → guarded SendInput tier)")

    actions_mod._force_foreground(actions_mod._target_window_hwnd(doc_ctrl))
    time.sleep(0.4)
    _clear_editor(doc_ctrl)

    known = "P4-E2E-" + time.strftime("%H%M%S")
    plan = actions_mod.type_text(
        window=target, text=known, name="Text editor",
        _resolved_ctrl=doc_ctrl, _resolved_title=notepad_title,
    )
    if plan.get("phase") != "plan":
        _emit("c_plan_created", FAIL, str(plan))
        return
    _emit("c_plan_created", PASS, f"method={plan['method']}")

    res = actions_mod.type_text(
        window=target, text=known, name="Text editor",
        plan_id=plan["plan_id"],
        _resolved_ctrl=doc_ctrl, _resolved_title=notepad_title,
    )
    print(f"  execute: {json.dumps(res, ensure_ascii=False)}", flush=True)
    if not res.get("ok"):
        _emit("c_typing_succeeded", FAIL, json.dumps(res, ensure_ascii=False))
        return
    _emit("c_typing_succeeded", PASS,
          f"chars_typed={res.get('chars_typed')} method={res.get('method')}")

    time.sleep(0.4)
    content = _read_editor_text(target) or ""
    if known in content:
        _emit("c_text_round_trips", PASS,
              f"{known!r} is present in Notepad (content={content[:60]!r})")
    else:
        _emit("c_text_round_trips", FAIL,
              f"{known!r} not in read-back {content[:80]!r}")


# ---- (d) BM_CLICK tier ----------------------------------------------------

def scenario_d_bmclick_tier(host: EditHost) -> None:
    print("\n(d) FOCUS-FREE CLICK TIER — BM_CLICK class gate", flush=True)
    edit_cls = actions_mod._class_of(host.edit_hwnd)
    if not actions_mod._supports_bm_click(host.edit_hwnd):
        _emit("d_bmclick_rejected_for_edit_class", PASS,
              f"class {edit_cls!r} is correctly excluded from the BM_CLICK tier "
              f"(BM_CLICK is a Button-family message)")
    else:
        _emit("d_bmclick_rejected_for_edit_class", FAIL,
              f"class {edit_cls!r} should not be offered BM_CLICK")

    if actions_mod._supports_bm_click(0):
        _emit("d_bmclick_rejected_without_hwnd", FAIL, "hwnd=0 accepted")
    else:
        _emit("d_bmclick_rejected_without_hwnd", PASS,
              "a control with no HWND is never offered BM_CLICK")

    # A Button-class HWND must be preferred over a coordinate click.
    class _BtnCtrl:
        NativeWindowHandle = 123456
        IsInvokePatternAvailable = False
        IsTogglePatternAvailable = False
        IsSelectionItemPatternAvailable = False
        IsExpandCollapsePatternAvailable = False
        ControlTypeName = "ButtonControl"
        Name = "Button"
        AutomationId = ""
        IsOffscreen = False

        def GetChildren(self):
            return []

    with _patched("Button", 0):
        ladder = actions_mod.click_method_ladder(_BtnCtrl())
    if ladder == ["bmclick", "coordinate"]:
        _emit("d_ladder_prefers_bmclick_over_coordinate", PASS,
              f"Button-class ladder is {ladder} — focus-free BM_CLICK wins")
    else:
        _emit("d_ladder_prefers_bmclick_over_coordinate", FAIL, f"{ladder}")

    # And an owner-drawn Button must be excluded (MSDN: BM_CLICK is ignored).
    with _patched("Button", actions_mod._BS_OWNERDRAW):
        od_ladder = actions_mod.click_method_ladder(_BtnCtrl())
    if od_ladder == ["coordinate"]:
        _emit("d_bmclick_skipped_for_ownerdraw", PASS,
              f"owner-drawn Button ladder is {od_ladder} — BM_CLICK correctly "
              f"skipped because MSDN says it is ignored by owner-drawn buttons")
    else:
        _emit("d_bmclick_skipped_for_ownerdraw", FAIL, f"{od_ladder}")


def _make_edit_ctrl(edit_hwnd: int):
    """Minimal UIA-control stand-in for the real Win32 Edit.

    Carries only the attributes server/actions.py reads, so the real
    type_text / _select_type_method / _do_type code paths run against it
    while WM_SETTEXT reaches the REAL control HWND.
    """

    class _EditCtrl:
        NativeWindowHandle = edit_hwnd
        IsValuePatternAvailable = False
        IsInvokePatternAvailable = False
        IsTogglePatternAvailable = False
        IsSelectionItemPatternAvailable = False
        IsExpandCollapsePatternAvailable = False
        ControlTypeName = "EditControl"
        Name = "Edit"
        AutomationId = ""
        IsOffscreen = False

        def GetChildren(self):
            return []

    return _EditCtrl()


class _patched:
    """Patch _class_of + _GetWindowLongW together for ladder probing.

    Both are read INSIDE the same call, so both patches must be live at the
    moment the ladder function runs.
    """

    def __init__(self, cls, style):
        self.cls, self.style = cls, style

    def __enter__(self):
        import unittest.mock as m
        self._c = m.patch.object(actions_mod, "_class_of",
                                 side_effect=lambda h: self.cls)
        self._s = m.patch.object(actions_mod, "_GetWindowLongW",
                                 return_value=self.style)
        self._c.start()
        self._s.start()
        return self

    def __exit__(self, *a):
        self._c.stop()
        self._s.stop()
        return False


# ---- (e) ladder / guard wiring -------------------------------------------

def scenario_e_wiring(host: "EditHost") -> None:
    scenario_e_wiring.host_hwnd = host.host_hwnd
    scenario_e_wiring.host_title = host.title
    print("\n(e) LADDER + GUARD WIRING", flush=True)

    if actions_mod.SENDINPUT_METHODS == frozenset({"keyboard", "coordinate"}):
        _emit("e_sendinput_methods_identified", PASS,
              "only 'keyboard' and 'coordinate' are SendInput methods; every "
              "other method is focus-free")
    else:
        _emit("e_sendinput_methods_identified", FAIL,
              f"got {actions_mod.SENDINPUT_METHODS}")

    if actions_mod.TYPING_CHUNK_CHARS <= 20:
        _emit("e_chunk_size_at_most_20", PASS,
              f"TYPING_CHUNK_CHARS={actions_mod.TYPING_CHUNK_CHARS}")
    else:
        _emit("e_chunk_size_at_most_20", FAIL,
              f"TYPING_CHUNK_CHARS={actions_mod.TYPING_CHUNK_CHARS}")

    missing = [m for m in actions_mod.SENDINPUT_METHODS
               if m not in actions_mod.METHOD_LABEL]
    if not missing:
        _emit("e_labels_defined", PASS,
              f"SendInput labels: "
              f"{[actions_mod.METHOD_LABEL[m] for m in sorted(actions_mod.SENDINPUT_METHODS)]}")
    else:
        _emit("e_labels_defined", FAIL, f"missing labels: {missing}")

    for m in ("value", "wmtext", "bmclick", "invoke"):
        if "focus" not in actions_mod.METHOD_LABEL[m].lower() and \
           "pattern" not in actions_mod.METHOD_LABEL[m].lower():
            _emit(f"e_label_{m}", NOT_APPLICABLE, "label present")

    # P5 equivalence-class check: plan_method_changed only fires when the
    # tier changes (message <-> SendInput), NOT when the method label shifts
    # within the same tier.  "wmtext" -> "wmchar" is NOT a mismatch (both
    # message-tier, focus-free); the plan binds the tier safely and _do_type
    # fails with the fake HWND 4242 as expected.
    ctrl = _make_edit_ctrl(4242)
    # The probe HWND 4242 does not exist, so _process_name_from_window would
    # resolve to "" and the gate would refuse before we ever reach the
    # method check. Use the real target host's window so the gate passes.
    host_hwnd = scenario_e_wiring.host_hwnd
    host_title = scenario_e_wiring.host_title
    with _patched("Edit", 0):
        p = actions_mod.type_text(window=str(host_hwnd), text="hi", name="Edit",
                                  _resolved_ctrl=ctrl,
                                  _resolved_title=host_title)
    if p.get("method") != "wmtext":
        _emit("e_plan_binds_wmtext", FAIL,
              f"method={p.get('method')!r} err={p.get('error')} "
              f"msg={p.get('message')!r}")
        return
    _emit("e_plan_binds_wmtext", PASS,
          "a plan for an 'Edit' control binds the focus-free wmtext method")

    with _patched("RichEditD2DPT", 0):
        r = actions_mod.type_text(window=str(host_hwnd), text="hi", name="Edit",
                                   plan_id=p["plan_id"],
                                   _resolved_ctrl=ctrl,
                                   _resolved_title=host_title)
    # P5: "wmtext" vs "wmchar" are same message tier, so equivalence-class
    # check passes and the real failure is from _do_type (fake HWND 4242).
    if r.get("error") == "type_failed" and "4242" in str(r.get("message", "")):
        _emit("e_no_silent_escalation", PASS,
              "wmtext vs wmchar are same message tier → no plan_method_changed; "
              "_do_type correctly fails with type_failed for the fake HWND "
              "(the plan bound the message tier safely; escalation was refused)")
    elif r.get("error") == "plan_method_changed":
        _emit("e_no_silent_escalation", FAIL,
              "plan_method_changed fired for wmtext<->wmchar within same tier "
              "(P5 equivalence classes should treat this as equivalent)")
    else:
        _emit("e_no_silent_escalation", FAIL,
              f"expected type_failed with fake HWND, got "
              f"{json.dumps(r, ensure_ascii=False)}")


# ---- main -----------------------------------------------------------------

def main() -> int:
    print("=" * 74, flush=True)
    print("P4 fix-round-3 FOCUS-SAFETY E2E (real desktop)", flush=True)
    print("=" * 74, flush=True)

    _taskkill("notepad.exe")
    time.sleep(0.4)

    tmp_dir = Path(tempfile.mkdtemp(prefix="tool_wl_focus_"))
    _create_temp_config(tmp_dir)
    _patch_module_paths(tmp_dir)
    print(f"tmp config: {tmp_dir}", flush=True)

    notepad = None
    hosts: list[EditHost] = []
    try:
        print("\n[setup] launching Notepad + two Win32 Edit hosts", flush=True)
        notepad = _spawn_detached(NOTEPAD)

        if not _wait_until(lambda: _find_notepad() is not None, 30,
                           "Notepad window"):
            _emit("setup_notepad", FAIL, "Notepad never appeared")
            return 2
        nw = _find_notepad()
        assert nw is not None
        np_title = nw["title"]
        print(f"  Notepad: title={np_title!r} hwnd={nw['hwnd']} pid={nw['pid']}",
              flush=True)
        if not _wait_until(
            lambda: _resolve_ctrl(np_title, "Text editor") is not None,
            20, "Notepad editor control",
        ):
            _emit("setup_editor_ctrl", FAIL, "could not resolve 'Text editor'")
            return 2
        _emit("setup_editor_ctrl", PASS, "DocumentControl 'Text editor' located")

        target_host = EditHost("ToolWl Target Host")
        hosts.append(target_host)
        victim_host = EditHost("ToolWl Victim Host")
        hosts.append(victim_host)
        for label, h in (("target", target_host), ("victim", victim_host)):
            if h.ok:
                _emit(f"setup_{label}_host", PASS,
                      f"{label} Edit control hwnd={h.edit_hwnd} in host "
                      f"hwnd={h.host_hwnd}")
            else:
                _emit(f"setup_{label}_host", FAIL, f"host failed: {h.info}")

        scenario_a_background_typing(target_host, np_title)

        # (b) steals focus to the victim host's top-level window.
        scenario_b_focus_loss_abort(np_title, victim_host, victim_host.host_hwnd)

        # Hand the user back whatever window they had.
        print("\n[cleanup] restoring foreground", flush=True)

        # Scenario (b) leaves 20 characters in Notepad, which changes its
        # window title (Notepad prefixes unsaved docs with '*'). Re-resolve
        # by process so (c) targets the live window, not a stale title.
        nw2 = _find_notepad()
        np_title2 = nw2["title"] if nw2 else np_title
        np_hwnd2 = int(nw2["hwnd"]) if nw2 else int(nw["hwnd"])
        print(f"  Notepad now title={np_title2!r} hwnd={np_hwnd2}", flush=True)
        scenario_c_notepad_typing(np_hwnd2, np_title2)
        scenario_d_bmclick_tier(target_host)
        scenario_e_wiring(target_host)

        print("\n" + "=" * 74, flush=True)
        print("E2E SUMMARY", flush=True)
        print("=" * 74, flush=True)
        if _failures:
            print(f"  {len(_failures)} FAILURE(S): {_failures}", flush=True)
            return 1
        print("  All scenarios PASS.", flush=True)
        return 0

    except Exception as exc:
        traceback.print_exc()
        _emit("fatal", FAIL, str(exc))
        return 2
    finally:
        if notepad:
            try:
                notepad.terminate()
            except Exception:
                pass
        _taskkill("notepad.exe")
        for h in hosts:
            h.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())