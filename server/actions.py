"""P4 Action tools — click_control, type_text, focus_window.

All mutations are two-phase:
  Phase 1 (PLAN): called without plan_id → VALIDATE + return a plan.
  Phase 2 (EXECUTE): called with plan_id → verify plan + perform action.

This module is intentionally separate from the read-only ui_automation.py so
the static scan in test_no_action_api.py can keep the action APIs
(IsInvokePressed, SetValue, SetForegroundWindow, etc.) out of every other
module.

Safety gate rules (checked in BOTH phases):
  1. config/actions.json: enabled=true AND target process in allowed_processes.
  2. config/STOP_ACTIONS file exists → refuse everything.
  3. Target control's Name contains any deny_labels entry → refuse.
  4. focus_window: allowlisted + STOP check + logging only (no plan).

METHOD LADDER — focus-free first, SendInput as a guarded last resort:

  type_text    : ValuePattern.SetValue → WM_SETTEXT (read-back verified)
                 → SendInput keyboard, ≤20-char chunks, re-verified each chunk
  click_control: UIA Invoke/Toggle/SelectionItem/ExpandCollapse
                 → BM_CLICK (Button class) → SendInput coordinate click

SendInput follows the GLOBAL foreground focus, which is exactly how a real
incident typed text into the user's own menu instead of the target app. So
before any input is emitted we must PROVE the target owns the foreground:

  * the user's foreground HWND is captured and restored in a `finally`
    (on success, refusal and abort alike);
  * after focusing, GetForegroundWindow() must be the target window AND the
    control holding keyboard focus (GetGUIThreadInfo, else UIA's focused
    element) must belong to the target window/process — else abort BEFORE
    sending anything;
  * typing re-verifies before every ≤20-character chunk and raises
    FocusGuardAbort with the honest partial count if focus is lost;
  * a coordinate click re-resolves the rect, hit-tests WindowFromPoint
    against the target PID, and re-checks the foreground immediately before
    clicking.

Honest limitation: true background keyboard input to arbitrary apps is
IMPOSSIBLE on Windows — SendInput cannot be retargeted. The guarantee is
that focus-free methods are tried first and SendInput aborts rather than
ever typing into the wrong window.

The method is chosen at PLAN, recorded in the signed token, and re-verified
at EXECUTE; a change in between refuses with `plan_method_changed` (no
silent escalation).

Logging: every plan / execute / refusal is appended as one JSON line to
logs/actions.log (created automatically).
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import uiautomation as uia

# Re-use the init() from ui_automation so we have a sane UIA apartment.
from server import ui_automation as _ui_mod

_log = logging.getLogger("tool_wl_app.actions")

from server import action_token as _tok

# --------------------- paths -------------------------------------------------

ROOT_DIR: Path = Path(__file__).resolve().parent.parent
CONFIG_DIR: Path = ROOT_DIR / "config"
LOGS_DIR: Path = ROOT_DIR / "logs"
ACTION_CONFIG: Path = CONFIG_DIR / "actions.json"
STOP_FILE: Path = CONFIG_DIR / "STOP_ACTIONS"
ACTIONS_LOG: Path = LOGS_DIR / "actions.log"


# --------------------- action API symbols (for static scan) -----------------
# These MUST live only in this module. The scanner in test_no_action_api.py
# checks that NONE of these appear in server/*.py EXCEPT this file.
ACTION_FORBIDDEN_IN_OTHER_MODULES = {
    "Invoke",
    "Toggle",
    "SelectionItem",
    "ExpandCollapse",
    "ValuePattern",
    "SetValue",
    "SetForegroundWindow",
    "BringWindowToTop",
    "IsInvokePressed",
    "ToggleState",
    "IsSelected",
    "IsExpanded",
    "CurrentValue",
}


# --------------------- gate config ------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_SetForegroundWindow = _user32.SetForegroundWindow
_SetForegroundWindow.argtypes = [ctypes.c_void_p]
_SetForegroundWindow.restype = wintypes.BOOL


# --------------------- focus-safety Win32 surface ---------------------------
# Everything below exists for ONE reason: an action may only ever affect the
# designated target app. SendInput delivers to whatever the OS thinks the
# foreground window is, so we must be able to *prove* the target owns the
# foreground before a single keystroke or click is emitted, prove it again
# before every chunk, and hand the user's foreground window back afterwards.

_GetForegroundWindow = _user32.GetForegroundWindow
_GetForegroundWindow.argtypes = []
_GetForegroundWindow.restype = wintypes.HWND

_GetAncestor = _user32.GetAncestor
_GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_GetAncestor.restype = wintypes.HWND

_GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
_GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_GetWindowThreadProcessId.restype = wintypes.DWORD

_kernel32_thread = ctypes.WinDLL("kernel32", use_last_error=True)
_GetCurrentThreadId = _kernel32_thread.GetCurrentThreadId
_GetCurrentThreadId.argtypes = []
_GetCurrentThreadId.restype = wintypes.DWORD

_AttachThreadInput = _user32.AttachThreadInput
_AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_AttachThreadInput.restype = wintypes.BOOL

_BringWindowToTop = _user32.BringWindowToTop
_BringWindowToTop.argtypes = [wintypes.HWND]
_BringWindowToTop.restype = wintypes.BOOL

_ShowWindow = _user32.ShowWindow
_ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_ShowWindow.restype = wintypes.BOOL

_WindowFromPoint = _user32.WindowFromPoint
_WindowFromPoint.argtypes = [wintypes.POINT]
_WindowFromPoint.restype = wintypes.HWND

_GetClassNameW = _user32.GetClassNameW
_GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_GetClassNameW.restype = ctypes.c_int

_GetDlgCtrlID = _user32.GetDlgCtrlID
_GetDlgCtrlID.argtypes = [wintypes.HWND]
_GetDlgCtrlID.restype = ctypes.c_int

_GetWindowLongW = _user32.GetWindowLongW
_GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_GetWindowLongW.restype = ctypes.c_long

_GetGUIThreadInfo = _user32.GetGUIThreadInfo
_GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]
_GetGUIThreadInfo.restype = wintypes.BOOL

# SendMessageTimeoutW (not SendMessageW) so a hung target app cannot block
# the server forever. SMTO_ABORTIFHUNG returns immediately if the receiving
# thread has stopped pumping messages.
_SendMessageTimeoutW = _user32.SendMessageTimeoutW
_SendMessageTimeoutW.argtypes = [
    wintypes.HWND,      # hWnd
    wintypes.UINT,      # Msg
    wintypes.WPARAM,    # wParam
    ctypes.c_void_p,    # lParam
    wintypes.UINT,      # fuFlags
    wintypes.UINT,      # uTimeout
    ctypes.c_void_p,    # lpdwResult
]
_SendMessageTimeoutW.restype = ctypes.c_long

# Window messages used by the focus-free (window-message) method tier.
_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E
_WM_SETTEXT = 0x000C
_BM_CLICK = 0x00F5
_WM_COMMAND = 0x0111
_BN_CLICKED = 0x0000

_SMTO_ABORTIFHUNG = 0x0002
_MESSAGE_TIMEOUT_MS = 2000

# GetAncestor / ShowWindow constants.
_GA_ROOT = 2
_SW_RESTORE = 9

# GetWindowLong / style bits.
_GWL_STYLE = -16
_BS_OWNERDRAW = 0x0000000B  # owner-drawn buttons ignore BM_CLICK (MSDN)

# Win32 control classes whose windows honour BM_CLICK.
BUTTON_CLASSES = frozenset({"Button", "CheckBox", "RadioButton", "GroupBox"})


class _GUITHREADINFO(ctypes.Structure):
    """Win32 GUITHREADINFO — tells us which control holds keyboard focus
    inside the foreground window's UI thread."""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


_GUITHREADINFO_IDLE = 0x00000001


class FocusGuardAbort(RuntimeError):
    """Raised when the focus guard refuses to deliver input.

    Carries the honest partial-progress number so the caller can report
    exactly how many characters made it in before we stopped.
    """

    def __init__(self, message: str, *, chars_typed: int = 0, phase: str = ""):
        super().__init__(message)
        self.chars_typed = chars_typed
        self.phase = phase


# The typing guard is deliberately conservative: every chunk re-verifies.
TYPING_CHUNK_CHARS = 20
CHUNK_SETTLE_S = 0.03

# Test-only seam (see tests/manual_focus_safety_e2e.py). When set to a
# callable, it is invoked with the running character count immediately
# BEFORE each chunk is sent, so a test can steal the foreground mid-type.
# It is never set in production code paths.
_MIDTYPE_INTERRUPT_HOOK = None

# --------------------- raw input (SendInput) -------------------------------
# P4 fallback layer. Used ONLY when a resolved control does not support the
# preferred UIA pattern. Confined to this module by tests/test_no_action_api.py.
# Windows structure definitions (winuser.h):

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004

# SM_CXSCREEN / SM_CYSCREEN — virtual screen is used so multi-monitor works.
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

# Virtual-screen origin. Absolute mouse coords in SendInput are 0..65535
# mapped across the *virtual* screen, so we must offset by the leftmost /
# topmost monitor's coordinates (negative on multi-monitor layouts).
_SIGMOUTH = ctypes.WinDLL("user32", use_last_error=True)
_GetSystemMetrics = _SIGMOUTH.GetSystemMetrics
_GetSystemMetrics.argtypes = [ctypes.c_int]
_GetSystemMetrics.restype = ctypes.c_int

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", _INPUTUNION),
    ]


_SendInput = _user32.SendInput
_SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
_SendInput.restype = ctypes.c_uint

# UIA SetFocus() (uses the UIA provider, no raw input)
_control_set_focus = getattr(uia.Control, "SetFocus", None)


def _uia_set_focus(ctrl: uia.Control) -> None:
    """Focus a control via the UIA provider."""
    ctrl.SetFocus()


def _send_unicode_text(text: str) -> int:
    """Type `text` into whatever currently has keyboard focus.

    Uses KEYEVENTF_UNICODE, so this is layout-independent and handles
    Vietnamese / emoji correctly. Returns the number of characters sent.
    """
    batch: List[_INPUT] = []
    for ch in text:
        # SendInput consumes UTF-16 code units; surrogate pairs -> 2 units.
        for unit in _utf16_units(ch):
            down = _INPUT(type=_INPUT_KEYBOARD)
            down.ki = _KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE, 0, None)
            up = _INPUT(type=_INPUT_KEYBOARD)
            up.ki = _KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, None)
            batch.append(down)
            batch.append(up)
    # SendInput's per-call count is capped; send in chunks.
    SENT = 0
    CHUNK = 128
    for i in range(0, len(batch), CHUNK):
        part = batch[i:i + CHUNK]
        arr = (_INPUT * len(part))(*part)
        sent = _SendInput(len(part), arr, ctypes.sizeof(_INPUT))
        if sent != len(part):
            raise RuntimeError(
                f"SendInput sent {sent} of {len(part)} keyboard events "
                f"(win32 error {ctypes.get_last_error()})"
            )
        SENT += sent // 2
        time.sleep(0.005)  # let the target app drain the queue
    return SENT


def _utf16_units(ch: str) -> List[int]:
    """UTF-16 code units for one character (surrogate pairs -> 2 units)."""
    raw = ch.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def _absolute_point(x: int, y: int) -> tuple[int, int]:
    """Map a virtual-screen pixel to SendInput's 0..65535 absolute space."""
    vx = _GetSystemMetrics(_SM_XVIRTUALSCREEN)
    vy = _GetSystemMetrics(_SM_YVIRTUALSCREEN)
    vw = _GetSystemMetrics(_SM_CXSCREEN)
    vh = _GetSystemMetrics(_SM_CYSCREEN)
    if vw <= 0 or vh <= 0:
        raise RuntimeError("could not read virtual screen metrics")
    ax = int((x - vx) * 65535 / max(vw - 1, 1))
    ay = int((y - vy) * 65535 / max(vh - 1, 1))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def _send_click_at(x: int, y: int) -> None:
    """Move the cursor to (x, y) and send a left click there."""
    ax, ay = _absolute_point(x, y)
    move = _INPUT(type=_INPUT_MOUSE)
    move.mi = _MOUSEINPUT(ax, ay, 0, _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, 0, None)
    down = _INPUT(type=_INPUT_MOUSE)
    down.mi = _MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTDOWN, 0, None)
    up = _INPUT(type=_INPUT_MOUSE)
    up.mi = _MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTUP, 0, None)
    arr = (_INPUT * 3)(move, down, up)
    sent = _SendInput(3, arr, ctypes.sizeof(_INPUT))
    if sent != 3:
        raise RuntimeError(
            f"SendInput sent {sent} of 3 mouse events "
            f"(win32 error {ctypes.get_last_error()})"
        )
    time.sleep(0.05)


# --------------------- focus guard primitives -------------------------------
# Read-only introspection used to prove, at every step, that input is going
# to the intended target and to nobody else.


def _foreground_hwnd() -> int:
    """The HWND the OS currently considers foreground (0 if none)."""
    return int(_GetForegroundWindow() or 0)


def _pid_of(hwnd: int) -> int:
    """PID owning `hwnd`, or 0 if it is not a live window."""
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    try:
        _GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value)


def _root_hwnd(hwnd: int) -> int:
    """Walk up to the top-level ancestor of `hwnd`."""
    if not hwnd:
        return 0
    try:
        root = _GetAncestor(wintypes.HWND(hwnd), _GA_ROOT)
        return int(root) if root else int(hwnd)
    except Exception:
        return int(hwnd)


def _class_of(hwnd: int) -> str:
    """Win32 window class name for `hwnd` ('' if unavailable)."""
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    try:
        n = _GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    except Exception:
        return ""
    return buf.value or "" if n > 0 else ""


def _focused_child_hwnd() -> int:
    """HWND that currently holds keyboard focus within the foreground
    window's UI thread (0 when nothing / idle / unavailable).

    GUITHREADINFO is the authoritative answer to "which control would a
    keystroke land in right now".
    """
    fg = _foreground_hwnd()
    if not fg:
        return 0
    try:
        tid = int(_GetWindowThreadProcessId(wintypes.HWND(fg), None))
        if not tid:
            return 0
        gti = _GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not _GetGUIThreadInfo(tid, ctypes.byref(gti)):
            return 0
        # flags bit0 (GUITHREADINFO_IDLE) means the queue is idle AND
        # hwndFocus/hwndCapture must be ignored.
        if gti.flags & _GUITHREADINFO_IDLE:
            return 0
        return int(gti.hwndFocus or 0)
    except Exception:
        return 0


def _window_from_point(x: int, y: int) -> int:
    """HWND under the virtual-screen point (x, y), 0 if none."""
    try:
        hwnd = _WindowFromPoint(wintypes.POINT(int(x), int(y)))
        return int(hwnd) if hwnd else 0
    except Exception:
        return 0


def _force_foreground(hwnd: int) -> bool:
    """Best-effort: make `hwnd`'s top-level frame the foreground window.

    SetForegroundWindow is frequently refused for background processes, so
    we also try the AttachThreadInput + SetForegroundWindow(+BringWindowToTop)
    dance that MSDN documents for exactly this case. Returns True only when
    the OS confirms this hwnd (or a descendant/ancestor of it) is foreground.
    """
    if not hwnd:
        return False
    target = _root_hwnd(hwnd)
    try:
        if _SetForegroundWindow(wintypes.HWND(target)):
            if _foreground_matches(target):
                return True
    except Exception:
        pass

    # AttachThreadInput fallback.
    attached = False
    try:
        fg = _foreground_hwnd()
        fg_tid = int(_GetWindowThreadProcessId(wintypes.HWND(fg), None)) if fg else 0
        cur_tid = int(_GetCurrentThreadId())
        tgt_tid = int(_GetWindowThreadProcessId(wintypes.HWND(target), None))
        if fg_tid and fg_tid != cur_tid:
            attached = bool(_AttachThreadInput(cur_tid, fg_tid, True))
        if tgt_tid and tgt_tid != cur_tid:
            attached = bool(_AttachThreadInput(cur_tid, tgt_tid, True)) or attached
        _BringWindowToTop(wintypes.HWND(target))
        _SetForegroundWindow(wintypes.HWND(target))
    except Exception:
        pass
    finally:
        if attached:
            try:
                cur_tid = int(_GetCurrentThreadId())
                _AttachThreadInput(cur_tid, int(_GetWindowThreadProcessId(wintypes.HWND(target), None)), False)
            except Exception:
                pass
    return _foreground_matches(target)


def _foreground_matches(hwnd: int) -> bool:
    """True when `hwnd` (or its ancestor/descendant) IS the foreground window."""
    if not hwnd:
        return False
    fg = _foreground_hwnd()
    if not fg:
        return False
    if fg == hwnd:
        return True
    return _root_hwnd(fg) == _root_hwnd(hwnd)


def _restore_foreground(hwnd: int) -> bool:
    """Hand the user's original foreground window back after an action.

    Used in a `finally` so it runs on success, refusal AND abort. Never
    restores if the user has already moved on to a different window (that
    would yank their focus around).
    """
    if not hwnd or not _foreground_hwnd():
        return False
    if _foreground_hwnd() == hwnd:
        return True  # already theirs
    return _force_foreground(hwnd)


def _hwnd_owns(hwnd: int, ancestor: int) -> bool:
    """True when `hwnd` is `ancestor` or lives under it."""
    if not hwnd or not ancestor:
        return False
    return _root_hwnd(hwnd) == _root_hwnd(ancestor)


def _target_hwnd(ctrl: uia.Control) -> int:
    """The control's own native HWND (0 for controls without one)."""
    try:
        return int(getattr(ctrl, "NativeWindowHandle", 0) or 0)
    except Exception:
        return 0


def _target_window_hwnd(ctrl: uia.Control) -> int:
    """Top-level HWND that owns `ctrl`, found by walking up the UIA tree.

    Needed because SendInput verification must compare against the *window*,
    not the leaf control: GetForegroundWindow always reports a top-level
    frame.
    """
    node = ctrl
    for _ in range(12):
        if node is None:
            break
        h = _target_hwnd(node)
        if h and _root_hwnd(h) == h:
            return h
        try:
            node = node.GetParentControl()
        except Exception:
            break
    # Fall back to the leaf's own HWND; if it has none we cannot verify.
    return _target_hwnd(ctrl)


# --------------------- focus-free window-message tier ----------------------
# These messages go straight to the control's HWND. The foreground window is
# never touched, so the user can keep typing into whatever they had in front.


def _send_msg(hwnd: int, msg: int, wparam: int = 0, lparam: Any = 0) -> int:
    """SendMessageTimeoutW wrapper. Returns the message result, or 0 on
    timeout/refusal. Never blocks longer than MESSAGE_TIMEOUT_MS.

    `lparam` accepts a ctypes buffer (whose ADDRESS is the LPARAM), an
    already-integer pointer, or 0. Note that the result of a WM_SETTEXT is
    the control's return value (1 on success), so callers that need to tell
    "success" from "failure" must check the returned int, not the timeout
    flag alone.
    """
    result = ctypes.c_size_t(0)
    if isinstance(lparam, ctypes.Array) or isinstance(
        lparam, ctypes._Pointer
    ):
        lp = ctypes.cast(lparam, ctypes.c_void_p)
    elif lparam:
        lp = ctypes.c_void_p(int(lparam))
    else:
        lp = ctypes.c_void_p(0)
    try:
        ok = _SendMessageTimeoutW(
            wintypes.HWND(hwnd), wintypes.UINT(msg),
            wintypes.WPARAM(wparam), lp,
            _SMTO_ABORTIFHUNG, _MESSAGE_TIMEOUT_MS, ctypes.byref(result),
        )
    except Exception:
        return 0
    if not ok:
        return 0
    return int(result.value) if result.value else 0


def _wm_get_text(hwnd: int) -> str:
    """WM_GETTEXT round-trip — read a Win32 control's text without focus."""
    length = _send_msg(hwnd, _WM_GETTEXTLENGTH)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    got = _send_msg(hwnd, _WM_GETTEXT, length + 1, buf)
    if got <= 0:
        return ""
    return buf.value or ""


def _wm_set_text(hwnd: int, text: str) -> bool:
    """WM_SETTEXT — write a Win32 control's text without focus."""
    buf = ctypes.create_unicode_buffer(text)
    return _send_msg(hwnd, _WM_SETTEXT, 0, buf) != 0


def _supports_wm_settext(hwnd: int) -> bool:
    """True when the control's class is a plain Win32 text window.

    WM_SETTEXT is honoured by Edit / RichEdit / Static / Window classes.
    It is NOT honoured by modern XAML/Document controls, which is exactly
    why we verify the write by reading the value back.
    """
    if not hwnd:
        return False
    return _class_of(hwnd) in (
        "Edit", "RICHEDIT50W", "RICHEDIT_CLASS", "Static", "Window",
    )


def _supports_bm_click(hwnd: int) -> bool:
    """True when BM_CLICK is meaningful for this control's class.

    Per MSDN, BM_CLICK is ignored by owner-drawn buttons, and it only
    exists for the Button family.
    """
    if not hwnd:
        return False
    if _class_of(hwnd) not in BUTTON_CLASSES:
        return False
    style = int(_GetWindowLongW(wintypes.HWND(hwnd), _GWL_STYLE) or 0)
    return (style & _BS_OWNERDRAW) != _BS_OWNERDRAW


def _do_bm_click(hwnd: int) -> bool:
    """Send BM_CLICK (and WM_COMMAND/BN_CLICKED to the parent, which is what
    an owner with a message-pump handler expects). Never changes focus."""
    cls = _class_of(hwnd)
    if cls not in BUTTON_CLASSES:
        return False
    parent = 0
    try:
        parent = int(_GetAncestor(wintypes.HWND(hwnd), 1) or 0)  # GA_PARENT
    except Exception:
        parent = 0
    # BM_CLICK synthesises the click and, for a real Button window, also
    # notifies the parent with WM_COMMAND/BN_CLICKED. Send the explicit
    # WM_COMMAND too for owners that only hook the notification.
    _send_msg(hwnd, _BM_CLICK)
    if parent:
        ctrl_id = _GetDlgCtrlID(wintypes.HWND(hwnd))
        if ctrl_id:
            _send_msg(parent, _WM_COMMAND, (ctrl_id << 16) | _BN_CLICKED, hwnd)
    return True


# --------------------- guarded typing / clicking ----------------------------


def _verify_typing_focus(
    target_win_hwnd: int,
    target_pid: int,
    ctrl: Optional[uia.Control] = None,
    chars_typed: int = 0,
) -> Optional[str]:
    """Return None when it is safe to send the next chunk, else the reason.

    Two independent proofs are required before ANY keystroke is emitted:
      1. GetForegroundWindow() resolves to the target's window, and
      2. the control that actually holds keyboard focus belongs to the
         target window/process (either the Win32 focus HWND or UIA's focused
         element agrees).
    """
    fg = _foreground_hwnd()
    if not fg:
        return (
            f"no foreground window at the {chars_typed}-character mark "
            f"(input aborted before any further keystroke)"
        )
    if not _foreground_matches(target_win_hwnd):
        return (
            f"foreground window changed to hwnd={fg} "
            f"({_class_of(fg) or 'unknown class'}) after {chars_typed} "
            f"characters; the target window is hwnd={target_win_hwnd}. "
            f"Aborting — keystrokes will NOT be delivered to the new window."
        )
    fg_pid = _pid_of(fg)
    if target_pid and fg_pid and fg_pid != target_pid:
        return (
            f"foreground window now belongs to pid={fg_pid}, not the "
            f"target pid={target_pid}, after {chars_typed} characters. "
            f"Aborting."
        )

    focus_hwnd = _focused_child_hwnd()
    focus_ok = False
    if focus_hwnd:
        focus_ok = (
            _hwnd_owns(focus_hwnd, target_win_hwnd)
            and (not target_pid or _pid_of(focus_hwnd) in (target_pid, 0))
        )
    if focus_ok:
        return None

    # Win32 focus unavailable (idle queue, or a UI framework that does not
    # publish one). Fall back to the UIA focused element.
    if ctrl is not None:
        try:
            focused = uia.GetFocusedControl()
            f_hwnd = int(getattr(focused, "NativeWindowHandle", 0) or 0)
            f_pid = int(getattr(focused, "ProcessId", 0) or 0)
            if f_pid and target_pid and f_pid == target_pid:
                return None
            if f_hwnd and _hwnd_owns(f_hwnd, target_win_hwnd):
                return None
        except Exception:
            pass
    return (
        f"could not confirm keyboard focus belongs to the target window "
        f"(hwnd={target_win_hwnd}, pid={target_pid}) after {chars_typed} "
        f"characters; focused control was hwnd={focus_hwnd or 0}. "
        f"Aborting to avoid typing into the wrong window."
    )


def _send_unicode_text_guarded(
    text: str,
    target_win_hwnd: int,
    target_pid: int,
    ctrl: Optional[uia.Control] = None,
    chunk_chars: int = TYPING_CHUNK_CHARS,
) -> int:
    """Type `text` one guarded chunk at a time.

    Before EVERY chunk (≤ `chunk_chars` characters, default 20) the focus
    is re-verified. The moment it fails we stop and raise FocusGuardAbort
    carrying the honest count of characters already delivered. We never
    continue typing into a window we can no longer prove is the target.
    """
    typed = 0
    for start in range(0, len(text), chunk_chars):
        piece = text[start:start + chunk_chars]

        # Test seam: lets the E2E steal the foreground mid-type.
        hook = globals().get("_MIDTYPE_INTERRUPT_HOOK")
        if callable(hook):
            hook(typed)

        reason = _verify_typing_focus(
            target_win_hwnd, target_pid, ctrl, chars_typed=typed
        )
        if reason is not None:
            raise FocusGuardAbort(
                reason, chars_typed=typed, phase="chunk_verify",
            )
        try:
            typed += _send_unicode_text(piece)
        except Exception as exc:
            raise FocusGuardAbort(
                f"SendInput failed after {typed} characters: {exc}",
                chars_typed=typed, phase="sendinput",
            ) from exc
        time.sleep(CHUNK_SETTLE_S)
    return typed


def _click_guarded(
    ctrl: uia.Control,
    target_win_hwnd: int,
    target_pid: int,
) -> tuple[int, int]:
    """Coordinate click with three execute-time checks.

    1. Re-resolve the control's rect NOW (never trust PLAN-time coords).
    2. WindowFromPoint(center) must belong to the target process.
    3. GetForegroundWindow() must be the target window, immediately before
       the click is sent.
    Any failure → FocusGuardAbort, before the cursor moves.
    """
    x, y = _center_of(ctrl)
    under = _window_from_point(x, y)
    if not under:
        raise FocusGuardAbort(
            f"no window found under the target control's center ({x}, {y}); "
            f"aborting before the click",
            phase="hit_test",
        )
    under_pid = _pid_of(under)
    if target_pid and under_pid and under_pid != target_pid:
        raise FocusGuardAbort(
            f"control center ({x}, {y}) resolves to hwnd={under} "
            f"(pid={under_pid}), which is NOT the target process "
            f"(pid={target_pid}). Aborting — the click would have gone to "
            f"the wrong window.",
            phase="hit_test",
        )
    fg = _foreground_hwnd()
    if not _foreground_matches(target_win_hwnd):
        raise FocusGuardAbort(
            f"foreground window is hwnd={fg}, not the target window "
            f"hwnd={target_win_hwnd}; aborting before the click",
            phase="foreground_verify",
        )
    _send_click_at(x, y)
    return x, y


# --------------------- action method selection ------------------------------
# The PLAN description must state the method so the user approves knowing
# exactly how the action will be performed.


def _center_of(ctrl: uia.Control) -> tuple[int, int]:
    """Center pixel of a control's bounding rectangle."""
    r = ctrl.BoundingRectangle
    return int(r.left + r.width() / 2), int(r.top + r.height() / 2)


def _has_writable_value_pattern(ctrl: uia.Control) -> bool:
    """True when the control exposes a writable ValuePattern."""
    if not getattr(ctrl, "IsValuePatternAvailable", False):
        return False
    try:
        pattern = ctrl.GetValuePattern()
        return bool(pattern.IsReadOnly is False)
    except Exception:
        # If we cannot read IsReadOnly, assume writable and let SetValue fail.
        return True


def _select_type_method(ctrl: uia.Control) -> str:
    """Pick how to type into this control.

    The ladder is ordered strictly by focus impact. SendInput is LAST and
    only reached when no focus-free method is viable for this control.

      1. 'value'  — UIA ValuePattern.SetValue. Works on background windows.
      2. 'wmtext' — WM_SETTEXT on the control's NativeWindowHandle, then
                    verified by reading the value back. Also focus-free.
      3. 'keyboard' — SendInput. Guarded: foreground is verified before
                    every 20-character chunk and restored afterwards.
    """
    if _has_writable_value_pattern(ctrl):
        return "value"
    if _supports_wm_settext(_target_hwnd(ctrl)):
        return "wmtext"
    return "keyboard"


def _select_click_method(ctrl: uia.Control) -> str:
    """Pick how to click this control.

      1. UIA patterns — 'invoke' | 'toggle' | 'select' | 'expand'.
         All work on background windows.
      2. 'bmclick'  — BM_CLICK / WM_COMMAND on the control's HWND.
         Focus-free, for plain Win32 Button-class controls.
      3. 'coordinate' — SendInput mouse click. Guarded last resort.
    """
    if getattr(ctrl, "IsInvokePatternAvailable", False):
        return "invoke"
    if getattr(ctrl, "IsTogglePatternAvailable", False):
        return "toggle"
    if getattr(ctrl, "IsSelectionItemPatternAvailable", False):
        return "select"
    if getattr(ctrl, "IsExpandCollapsePatternAvailable", False):
        return "expand"
    if _supports_bm_click(_target_hwnd(ctrl)):
        return "bmclick"
    return "coordinate"


# The SendInput methods need the target app physically in front. The plan
# description MUST carry this warning so the user knows to keep their hands
# off the keyboard and mouse for the few seconds the action runs.
SENDINPUT_WARNING = (
    "WARNING: this method needs the target app in front. "
    "Do not touch the keyboard or mouse during the few seconds it runs, "
    "and do not switch to another app — the action will abort if focus "
    "moves away, and it will never type into another window."
)

METHOD_LABEL = {
    "value": "via ValuePattern",
    "wmtext": "via WM_SETTEXT window message (no focus change)",
    "keyboard": "via guarded keyboard input into the focused control",
    "invoke": "via UIA Invoke",
    "toggle": "via UIA Toggle",
    "select": "via UIA SelectionItem",
    "expand": "via UIA ExpandCollapse",
    "bmclick": "via BM_CLICK window message (no focus change)",
    "coordinate": "via guarded coordinate click on the resolved control",
}

# Methods that touch the OS foreground window. Everything else is
# focus-free and runs against background windows untouched.
SENDINPUT_METHODS = frozenset({"keyboard", "coordinate"})

# Methods selected from the control's shape without a live desktop probe,
# exposed so the ladder order is unit-testable without a real UI.
def type_method_ladder(ctrl: uia.Control) -> List[str]:
    """The full type-method ladder for this control, best first."""
    ladder: List[str] = []
    if _has_writable_value_pattern(ctrl):
        ladder.append("value")
    if _supports_wm_settext(_target_hwnd(ctrl)):
        ladder.append("wmtext")
    ladder.append("keyboard")
    return ladder


def click_method_ladder(ctrl: uia.Control) -> List[str]:
    """The full click-method ladder for this control, best first."""
    ladder: List[str] = []
    if getattr(ctrl, "IsInvokePatternAvailable", False):
        ladder.append("invoke")
    if getattr(ctrl, "IsTogglePatternAvailable", False):
        ladder.append("toggle")
    if getattr(ctrl, "IsSelectionItemPatternAvailable", False):
        ladder.append("select")
    if getattr(ctrl, "IsExpandCollapsePatternAvailable", False):
        ladder.append("expand")
    if _supports_bm_click(_target_hwnd(ctrl)):
        ladder.append("bmclick")
    ladder.append("coordinate")
    return ladder


@dataclass
class ActionConfig:
    enabled: bool = True
    allowed_processes: List[str] = field(default_factory=lambda: ["notepad.exe"])
    deny_labels: List[str] = field(
        default_factory=lambda: [
            "delete",
            "format",
            "uninstall",
            "shut down",
            "pay now",
            "remove account",
        ]
    )

    @classmethod
    def load(cls) -> "ActionConfig":
        """Load from config/actions.json, creating defaults if missing."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(ACTION_CONFIG, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return cls(
                enabled=bool(raw.get("enabled", True)),
                allowed_processes=raw.get(
                    "allowed_processes", ["notepad.exe"]
                ),
                deny_labels=raw.get(
                    "deny_labels",
                    [
                        "delete",
                        "format",
                        "uninstall",
                        "shut down",
                        "pay now",
                        "remove account",
                    ],
                ),
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            # Create defaults on first run.
            default = cls()
            default._save()
            return default

    def _save(self) -> None:
        """Persist current settings to config/actions.json."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(ACTION_CONFIG, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "enabled": self.enabled,
                    "allowed_processes": self.allowed_processes,
                    "deny_labels": self.deny_labels,
                },
                f,
                indent=2,
            )


# --------------------- plan store -------------------------------------------

PLAN_TTL_SECONDS = 300  # 5 minutes


# Tokens handle plan binding; the TTL is shared with the token module.
PLAN_TTL_SECONDS = _tok.PLAN_TTL_SECONDS


def _jval(value: Any, default: str = "") -> Any:
    """Coerce a UIA/control attribute into a JSON-safe scalar.

    uiautomation returns strings and ints for the attributes we bind,
    but a mock or an odd control can return None or a non-primitive.
    Anything that is not str/int/float/bool becomes its ``str()`` so the
    token payload always serializes cleanly.
    """
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _build_binding(
    tool: str,
    process_name: str,
    window_title: str,
    control_name: str,
    control_type: Any,
    automation_id: str,
    hwnd: Any,
    args_hash: str,
) -> dict:
    """Build the canonical, cross-process-stable binding dictionary.

    Every field here is stable across server processes: the window title
    and process name come from the OS, the control identity comes from
    UIA's *stable* attributes (name + automation_id + control type), and
    the hwnd is the native window handle. Nothing here is a Python object
    identity or a UIA runtime id, both of which are meaningless in a
    different process.
    """
    try:
        hwnd_int = int(hwnd) if hwnd else 0
    except (TypeError, ValueError):
        hwnd_int = 0
    return {
        "tool": tool,
        "process": _jval(process_name),
        "window": _jval(window_title),
        "hwnd": hwnd_int,
        "control_name": _jval(control_name),
        "control_type": _jval(control_type),
        "automation_id": _jval(automation_id),
        "args_hash": _jval(args_hash),
    }


# --------------------- logging -----------------------------------------------

LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _log_event(
    event: str,  # "plan" | "execute" | "refuse"
    tool: str,
    process: str,
    window_title: str,
    control_name: str,
    detail: str = "",
) -> None:
    """Append one JSON line to logs/actions.log."""
    try:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "event": event,
            "tool": tool,
            "process": process,
            "window_title": window_title,
            "control_name": control_name,
            "detail": detail,
        }
        with open(ACTIONS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log.warning("could not write to actions.log: %r", exc)


# --------------------- gate checks ------------------------------------------


def _load_config() -> ActionConfig:
    return ActionConfig.load()


def _check_gate(
    tool: str,
    process_name: str,
    window_title: str,
    control_name: str,
) -> tuple[bool, str]:
    """Return (allowed, reason). reason is empty when allowed is True."""
    cfg = _load_config()

    # Rule 0: STOP_ACTIONS file
    if STOP_FILE.exists():
        return False, "STOP_ACTIONS file is present; all actions are blocked"

    # Rule 1: enabled flag
    if not cfg.enabled:
        return False, "actions are disabled in config/actions.json (enabled=false)"

    # Rule 2: allowlist
    if process_name.lower() not in [p.lower() for p in cfg.allowed_processes]:
        return (
            False,
            f"process {process_name!r} is not in allowed_processes "
            f"{cfg.allowed_processes!r}",
        )

    # Rule 3: deny labels
    name_lower = control_name.lower()
    for label in cfg.deny_labels:
        if label.lower() in name_lower:
            return (
                False,
                f"control name {control_name!r} contains deny_label {label!r}",
            )

    return True, ""


# --------------------- control resolution ------------------------------------

def _resolve_control(
    window: str,
    automation_id: Optional[str] = None,
    name: Optional[str] = None,
) -> tuple[Optional[uia.Control], str]:
    """Resolve exactly one control; return (control, window_title).

    Returns (None, error_reason) on failure.
    0 matches or 2+ matches → error.
    """
    _ui_mod.init()
    win = _ui_mod.find_window(window)
    if not win:
        return None, f"window not found: {window!r}"

    window_title = (win.Name or "").strip()

    # Build the search predicate.
    def _matches(ctrl: uia.Control) -> bool:
        if automation_id:
            aid = (ctrl.AutomationId or "").strip()
            if aid == automation_id:
                return True
        if name:
            nm = (ctrl.Name or "").strip()
            if nm == name:
                return True
        return False

    # BFS to find matching controls.
    matches: List[uia.Control] = []
    queue: List[tuple[uia.Control, int]] = [(win, 0)]
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
        if _matches(ctrl):
            matches.append(ctrl)
        for child in ctrl.GetChildren():
            queue.append((child, depth + 1))

    if len(matches) == 0:
        return None, (
            f"no control found with automation_id={automation_id!r} "
            f"and/or name={name!r}"
        )
    if len(matches) >= 2:
        return None, (
            f"ambiguous: {len(matches)} controls matched "
            f"automation_id={automation_id!r} and/or name={name!r}; "
            f"must resolve to exactly one"
        )

    return matches[0], window_title


def _process_name_from_window(window: str) -> str:
    """Get the process name for a window (for allowlist checks)."""
    _ui_mod.init()
    win = _ui_mod.find_window(window)
    if not win:
        return ""
    try:
        pid = int(win.ProcessId)
    except Exception:
        return ""
    try:
        exe_path = _ui_mod._process_exe_path(pid) or ""
        return exe_path.split("\\")[-1].lower()
    except Exception:
        return ""


# --------------------- click_control ----------------------------------------


def click_control(
    window: str,
    automation_id: Optional[str] = None,
    name: Optional[str] = None,
    plan_id: Optional[str] = None,
    _resolved_ctrl: Optional[uia.Control] = None,  # internal: pass pre-resolved control
    _resolved_title: str = "",  # internal: window title for resolved control
) -> dict:
    """Click a UI control via its UIA pattern (Invoke > Toggle > SelectionItem > ExpandCollapse).

    Two-phase:
    - Phase 1 (no plan_id): validates the target, checks gate, returns a plan.
    - Phase 2 (with plan_id): re-checks gate, verifies plan, performs the click.

    This tool ACTS on the user's real desktop. Always call without plan_id first,
    present the returned plan description to the user, and execute only after
    the user agrees. Never chain plan+execute silently.
    """
    _ui_mod.init()

    # Resolve the control first (needed for both phases).
    if _resolved_ctrl is not None:
        ctrl = _resolved_ctrl
        window_title = _resolved_title
    else:
        ctrl, window_title = _resolve_control(window, automation_id, name)
        if ctrl is None:
            _log_event(
                "refuse", "click_control", _process_name_from_window(window),
                window_title, automation_id or name or "", window
            )
            return {
                "ok": False,
                "error": "control_not_found",
                "message": window_title,
            }

    control_name = (ctrl.Name or "").strip()
    automation_id_val = (ctrl.AutomationId or "").strip()
    process_name = _process_name_from_window(window)

    # Gate check (both phases).
    allowed, reason = _check_gate(
        "click_control", process_name, window_title, control_name
    )
    if not allowed:
        _log_event(
            "refuse", "click_control", process_name, window_title,
            control_name, reason
        )
        return {"ok": False, "error": "gate_refused", "message": reason}

    # Phase 1: PLAN — no plan_id provided.
    if not plan_id:
        # Decide HOW the click will be performed, and say so in the plan.
        method = _select_click_method(ctrl)
        label = METHOD_LABEL[method]
        desc = (
            f"Click {label} in window {window_title!r} — "
            f"control: {control_name!r} "
            f"(automation_id={automation_id_val!r})"
        )
        if method == "bmclick":
            desc += (
                " (BM_CLICK goes straight to the control's HWND; the target "
                "window stays in the background — no focus change)"
            )
        if method in SENDINPUT_METHODS:
            desc = f"{desc} {SENDINPUT_WARNING}"
        # Build the token binding for this execute call.
        binding = _build_binding(
            tool="click_control",
            process_name=process_name,
            window_title=window_title,
            control_name=control_name,
            control_type=getattr(ctrl, "ControlTypeName", ""),
            automation_id=automation_id_val,
            hwnd=getattr(ctrl, "NativeWindowHandle", 0),
            args_hash=_tok._hash_args({}),
        )
        token = _tok.issue_token(
            tool="click_control",
            method=method,
            binding=binding,
        )
        plan_id_out = _tok.decode_token(token)["plan_id"]
        _log_event(
            "plan", "click_control", process_name, window_title,
            control_name, f"method={label} plan_id={plan_id_out}"
        )
        return {
            "ok": True,
            "phase": "plan",
            "plan_id": token,
            "description": desc,
            "method": method,
            "method_label": label,
            "expires_in_s": PLAN_TTL_SECONDS,
        }

    # Phase 2: EXECUTE — token provided.
    # verify_token checks the HMAC, TTL, consumed-on-disk, and binding.
    # expected_args carries what we resolved at EXECUTE time. NOTE: we
    # deliberately bind on the *resolved* window_title (not the raw
    # `window` argument) because the title is the stable, cross-process
    # identity of the target; the raw argument may be a fuzzy substring.
    resolved_method = _select_click_method(ctrl)
    expected_args = {
        "window": window_title,
        "automation_id": automation_id_val,
        "name": control_name,
    }
    vr = _tok.verify_token(plan_id, expected_args)

    # If the method changed between PLAN and EXECUTE, the token is still valid
    # but we must refuse rather than silently using the new method.
    if vr.ok and _tok.check_method_mismatch(
        vr.payload["method"], resolved_method
    ):
        _log_event(
            "refuse", "click_control", process_name, window_title,
            control_name,
            f"METHOD_MISMATCH: token={vr.payload['method']!r} resolved={resolved_method!r}",
        )
        return {
            "ok": False,
            "error": "plan_method_changed",
            "message": (
                f"control's method changed between PLAN and EXECUTE: "
                f"token approved {vr.payload['method']!r} but resolved "
                f"control now uses {resolved_method!r}. "
                f"Please re-plan and re-execute."
            ),
        }

    if not vr.ok:
        _log_event(
            "refuse", "click_control", process_name, window_title,
            control_name, f"token={vr.reason}: {vr.message}"
        )
        return {
            "ok": False,
            "error": vr.reason,
            "message": vr.message,
            "plan_id": plan_id,
        }

    # Token verified and consumed atomically by verify_token. Perform the click.
    # For the guarded SendInput method we capture the user's current
    # foreground window and hand it back on success AND on abort.
    original_fg = _foreground_hwnd() if resolved_method in SENDINPUT_METHODS else 0
    try:
        pattern_used = _do_click(
            ctrl, method=resolved_method, restore_foreground=original_fg
        )
        _log_event(
            "execute", "click_control", process_name, window_title,
            control_name, f"method={METHOD_LABEL[pattern_used]}"
        )
        return {
            "ok": True,
            "phase": "execute",
            "method": pattern_used,
            "pattern_used": pattern_used,
            "method_label": METHOD_LABEL[pattern_used],
            "control_name": control_name,
            "automation_id": automation_id_val,
        }
    except FocusGuardAbort as abort:
        _log_event(
            "refuse", "click_control", process_name, window_title,
            control_name, f"FOCUS_ABORT (phase={abort.phase}): {abort}",
        )
        return {
            "ok": False,
            "error": "focus_guard_abort",
            "message": str(abort),
            "method": resolved_method,
            "method_label": METHOD_LABEL[resolved_method],
            "aborted": True,
            "clicked": False,
            "control_name": control_name,
            "automation_id": automation_id_val,
        }
    except Exception as exc:
        _log_event(
            "refuse", "click_control", process_name, window_title,
            control_name, f"click failed: {exc}"
        )
        return {
            "ok": False,
            "error": "click_failed",
            "message": str(exc),
        }


# --------------------- type_text --------------------------------------------


def type_text(
    window: str,
    text: str,
    automation_id: Optional[str] = None,
    name: Optional[str] = None,
    plan_id: Optional[str] = None,
    _resolved_ctrl: Optional[uia.Control] = None,  # internal
    _resolved_title: str = "",  # internal
) -> dict:
    """Set the text of an editable control via UIA ValuePattern.

    Two-phase:
    - Phase 1 (no plan_id): validates the target, checks gate, returns a plan
      that includes the control's CURRENT value and the new text.
    - Phase 2 (with plan_id): re-checks gate, verifies plan, performs SetValue.

    WARNING: SetValue OVERWRITES the existing text. The plan description
    shows the current value so the user can decide if this is intended.

    This tool ACTS on the user's real desktop. Always call without plan_id first,
    present the returned plan description to the user, and execute only after
    the user agrees. Never chain plan+execute silently.
    """
    _ui_mod.init()

    # Resolve the control.
    if _resolved_ctrl is not None:
        ctrl = _resolved_ctrl
        window_title = _resolved_title
    else:
        ctrl, window_title = _resolve_control(window, automation_id, name)
        if ctrl is None:
            _log_event(
                "refuse", "type_text", _process_name_from_window(window),
                window_title, automation_id or name or "", window
            )
            return {
                "ok": False,
                "error": "control_not_found",
                "message": window_title,
            }

    control_name = (ctrl.Name or "").strip()
    automation_id_val = (ctrl.AutomationId or "").strip()
    process_name = _process_name_from_window(window)

    # Gate check (both phases).
    allowed, reason = _check_gate(
        "type_text", process_name, window_title, control_name
    )
    if not allowed:
        _log_event(
            "refuse", "type_text", process_name, window_title,
            control_name, reason
        )
        return {"ok": False, "error": "gate_refused", "message": reason}

    # Get current value (needed for plan description).
    current_value = _get_control_value(ctrl)

    # Phase 1: PLAN.
    if not plan_id:
        trunc_value = (current_value or "")[:200]
        trunc_text = text[:200]
        method = _select_type_method(ctrl)
        label = METHOD_LABEL[method]
        if method == "value":
            tail = "(SetValue overwrites the whole value)"
        elif method == "wmtext":
            tail = (
                "(WM_SETTEXT overwrites the whole value; the target window "
                "stays in the background — no focus change)"
            )
        else:
            tail = (
                "(no focus-free method is viable for this control; it will be "
                "brought to the front and the text typed into it at the "
                "caret, so existing text is NOT cleared. Typing is verified "
                "before every 20 characters and aborts if focus moves away.)"
            )
        desc = (
            f"Set text {label} of [{control_name!r}] in window "
            f"{window_title!r} from {trunc_value!r} -> {trunc_text!r} {tail}"
        )
        if method in SENDINPUT_METHODS:
            desc = f"{desc} {SENDINPUT_WARNING}"
        binding = _build_binding(
            tool="type_text",
            process_name=process_name,
            window_title=window_title,
            control_name=control_name,
            control_type=getattr(ctrl, "ControlTypeName", ""),
            automation_id=automation_id_val,
            hwnd=getattr(ctrl, "NativeWindowHandle", 0),
            args_hash=_tok._hash_args({"text": text}),
        )
        token = _tok.issue_token(
            tool="type_text",
            method=method,
            binding=binding,
        )
        plan_id_out = _tok.decode_token(token)["plan_id"]
        _log_event(
            "plan", "type_text", process_name, window_title,
            control_name,
            f"method={label} plan_id={plan_id_out} "
            f"current={trunc_value!r} new={trunc_text!r}"
        )
        return {
            "ok": True,
            "phase": "plan",
            "plan_id": token,
            "description": desc,
            "method": method,
            "method_label": label,
            "expires_in_s": PLAN_TTL_SECONDS,
            "current_value": trunc_value,
            "new_text": trunc_text,
        }

    # Phase 2: EXECUTE — token provided.
    resolved_method = _select_type_method(ctrl)
    expected_args = {
        "window": window_title,
        "automation_id": automation_id_val,
        "name": control_name,
        "text": text,
    }
    vr = _tok.verify_token(plan_id, expected_args)

    # Re-check method: if the control gained/lost a writable ValuePattern
    # between PLAN and EXECUTE, the resolved method may differ from the
    # token's method. The token is still valid but we must refuse rather
    # than silently changing the method.
    if vr.ok and _tok.check_method_mismatch(vr.payload["method"], resolved_method):
        _log_event(
            "refuse", "type_text", process_name, window_title,
            control_name,
            f"METHOD_MISMATCH: token={vr.payload['method']!r} resolved={resolved_method!r}",
        )
        return {
            "ok": False,
            "error": "plan_method_changed",
            "message": (
                f"control's type method changed between PLAN and EXECUTE: "
                f"token approved {vr.payload['method']!r} but resolved "
                f"control now uses {resolved_method!r}. "
                f"Please re-plan and re-execute."
            ),
        }

    if not vr.ok:
        _log_event(
            "refuse", "type_text", process_name, window_title,
            control_name, f"token={vr.reason}: {vr.message}"
        )
        return {
            "ok": False,
            "error": vr.reason,
            "message": vr.message,
            "plan_id": plan_id,
        }

    # Token verified and consumed atomically by verify_token. Perform the write.
    # For SendInput methods we capture the user's current foreground window
    # and hand it back afterwards, on success AND on abort.
    original_fg = _foreground_hwnd() if resolved_method in SENDINPUT_METHODS else 0
    try:
        chars = _do_type(
            ctrl, text, method=resolved_method,
            restore_foreground=original_fg,
        )
        _log_event(
            "execute", "type_text", process_name, window_title,
            control_name,
            f"method={METHOD_LABEL[resolved_method]} set={text!r}"
        )
        return {
            "ok": True,
            "phase": "execute",
            "method": resolved_method,
            "method_label": METHOD_LABEL[resolved_method],
            "chars_typed": chars,
            "control_name": control_name,
            "automation_id": automation_id_val,
            "new_value": text,
        }
    except FocusGuardAbort as abort:
        # The focus guard refused to deliver the remaining characters. This
        # is the honest outcome: report exactly how many landed, and prove
        # nothing was typed into any other window.
        _log_event(
            "refuse", "type_text", process_name, window_title,
            control_name,
            f"FOCUS_ABORT after {abort.chars_typed} chars "
            f"(phase={abort.phase}): {abort}",
        )
        return {
            "ok": False,
            "error": "focus_guard_abort",
            "message": str(abort),
            "method": resolved_method,
            "method_label": METHOD_LABEL[resolved_method],
            "chars_typed": abort.chars_typed,
            "requested_chars": len(text),
            "aborted": True,
            "delivered_to_other_window": False,
            "control_name": control_name,
            "automation_id": automation_id_val,
        }
    except Exception as exc:
        _log_event(
            "refuse", "type_text", process_name, window_title,
            control_name, f"type failed: {exc}"
        )
        return {
            "ok": False,
            "error": "type_failed",
            "message": str(exc),
        }


# --------------------- focus_window -------------------------------------------


def focus_window(window: str) -> dict:
    """Bring an allowlisted app's window to the foreground.

    No plan phase — just allowlist + STOP check + logging.

    If SetForegroundWindow fails (Windows may refuse foreground changes from
    a background process), returns an honest error, never fakes success.

    This tool ACTS on the user's real desktop. Always confirm with the user
    before calling this tool.
    """
    _ui_mod.init()

    win = _ui_mod.find_window(window)
    if not win:
        _log_event(
            "refuse", "focus_window", "", "", "",
            f"window not found: {window!r}"
        )
        return {
            "ok": False,
            "error": "window_not_found",
            "message": f"window not found: {window!r}",
        }

    window_title = (win.Name or "").strip()
    process_name = _process_name_from_window(window)

    # Gate checks (no plan, but same allowlist + STOP).
    allowed, reason = _check_gate(
        "focus_window", process_name, window_title, ""
    )
    if not allowed:
        _log_event(
            "refuse", "focus_window", process_name, window_title,
            "", reason
        )
        return {"ok": False, "error": "gate_refused", "message": reason}

    # Attempt SetForegroundWindow.
    try:
        hwnd = int(win.NativeWindowHandle)
    except Exception as exc:
        _log_event(
            "refuse", "focus_window", process_name, window_title,
            "", f"could not get hwnd: {exc}"
        )
        return {
            "ok": False,
            "error": "hwnd_error",
            "message": f"could not get hwnd: {exc}",
        }

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _SetForegroundWindow = _user32.SetForegroundWindow
    _SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _SetForegroundWindow.restype = ctypes.wintypes.BOOL

    result = _SetForegroundWindow(hwnd)
    if not result:
        err = ctypes.get_last_error()
        _log_event(
            "refuse", "focus_window", process_name, window_title,
            "", f"SetForegroundWindow failed, win32 error={err}"
        )
        return {
            "ok": False,
            "error": "foreground_refused",
            "message": (
                f"SetForegroundWindow({hwnd}) returned False; "
                f"Windows refused the foreground change (win32 error {err}). "
                f"This can happen when a background process requests foreground. "
                f"Try bringing the window to foreground manually."
            ),
        }

    _log_event(
        "execute", "focus_window", process_name, window_title,
        "", f"hwnd={hwnd}"
    )
    return {
        "ok": True,
        "window_title": window_title,
        "hwnd": hwnd,
    }


# --------------------- pattern helpers (private, no public API surface) --------


def _do_click(
    ctrl: uia.Control,
    method: Optional[str] = None,
    restore_foreground: int = 0,
) -> str:
    """Perform the click using exactly `method`. Returns the method used.

    `method` is the value chosen at PLAN time and re-verified by the caller;
    it is never silently upgraded here.

    For SendInput methods, `restore_foreground` is the user's original
    foreground HWND; it is handed back in a `finally` block so it runs on
    success AND on abort.
    """
    method = method or _select_click_method(ctrl)

    if method == "invoke":
        pattern = ctrl.GetInvokePattern()
        pattern.Invoke()
        return "invoke"
    if method == "toggle":
        pattern = ctrl.GetTogglePattern()
        pattern.Toggle()
        return "toggle"
    if method == "select":
        pattern = ctrl.GetSelectionItemPattern()
        pattern.Select()
        return "select"
    if method == "expand":
        pattern = ctrl.GetExpandCollapsePattern()
        pattern.Expand()
        return "expand"
    if method == "bmclick":
        # Focus-free: BM_CLICK straight at the control's HWND. The user's
        # foreground window is never touched.
        hwnd = _target_hwnd(ctrl)
        if not _do_bm_click(hwnd):
            raise RuntimeError(
                f"BM_CLICK was not accepted by control hwnd={hwnd}"
            )
        return "bmclick"
    if method == "coordinate":
        # Last resort. Re-resolve the rect, hit-test it, and verify the
        # foreground immediately before the click — then restore.
        target_win = _target_window_hwnd(ctrl)
        target_pid = _pid_of(target_win) or _pid_of(_target_hwnd(ctrl))
        try:
            if target_win:
                _force_foreground(target_win)
                time.sleep(0.1)
            _click_guarded(ctrl, target_win, target_pid)
        finally:
            if restore_foreground:
                _restore_foreground(restore_foreground)
        return "coordinate"

    raise RuntimeError(
        f"control {ctrl.Name!r} has unknown click method {method!r}"
    )


def _get_control_value(ctrl: uia.Control) -> Optional[str]:
    """Read the current ValuePattern value if available."""
    if getattr(ctrl, "IsValuePatternAvailable", False):
        try:
            pattern = ctrl.GetValuePattern()
            return pattern.CurrentValue or ""
        except Exception:
            pass
    return None


def _get_control_text_pattern(ctrl: uia.Control) -> Optional[str]:
    """Read the full document text via TextPattern (DocumentControl etc.)."""
    try:
        if getattr(ctrl, "IsTextPatternAvailable", False):
            pattern = ctrl.GetTextPattern()
            return pattern.DocumentRange.GetText(-1) or ""
    except Exception:
        pass
    return None


def _do_set_value(ctrl: uia.Control, text: str) -> int:
    """Set the control's text via ValuePattern.SetValue. Returns chars set."""
    if not getattr(ctrl, "IsValuePatternAvailable", False):
        raise RuntimeError(
            f"control {ctrl.Name!r} does not support ValuePattern"
        )
    pattern = ctrl.GetValuePattern()
    pattern.SetValue(text)
    return len(text)


def _do_wm_set_text(ctrl: uia.Control, text: str) -> int:
    """Set the control's text via WM_SETTEXT. Returns chars set.

    Focus-free: the message goes straight to the control's HWND and the
    foreground window is untouched. The write is VERIFIED by reading the
    value back; a mismatch is reported as a method failure so the caller
    refuses rather than silently escalating to SendInput.
    """
    hwnd = _target_hwnd(ctrl)
    if not hwnd:
        raise RuntimeError(
            f"control {ctrl.Name!r} has no NativeWindowHandle; "
            f"WM_SETTEXT cannot be used"
        )
    if not _wm_set_text(hwnd, text):
        raise RuntimeError(
            f"WM_SETTEXT to hwnd={hwnd} was rejected by the target window"
        )
    # Verify the write landed. A control that silently ignores WM_SETTEXT
    # must FAIL here, not escalate.
    readback = _wm_get_text(hwnd)
    if readback != text:
        raise RuntimeError(
            f"WM_SETTEXT verification failed: wrote {text!r} to hwnd={hwnd} "
            f"but reading it back returned {readback!r}. "
            f"Refusing to escalate to keyboard input."
        )
    return len(text)


def _do_type(
    ctrl: uia.Control,
    text: str,
    method: Optional[str] = None,
    restore_foreground: int = 0,
) -> int:
    """Type `text` into `ctrl` using exactly `method`. Returns chars typed.

    For the guarded SendInput path this raises FocusGuardAbort (carrying the
    honest partial character count) rather than ever continuing to type into
    a window we can no longer prove is the target.
    """
    method = method or _select_type_method(ctrl)

    if method == "value":
        return _do_set_value(ctrl, text)

    if method == "wmtext":
        # Focus-free tier: WM_SETTEXT straight at the control's HWND.
        return _do_wm_set_text(ctrl, text)

    if method == "keyboard":
        # Guarded last resort. Focus the target, VERIFY the OS agrees, then
        # type in ≤20-character chunks re-verifying before every chunk.
        target_win = _target_window_hwnd(ctrl)
        target_pid = _pid_of(target_win) or _pid_of(_target_hwnd(ctrl))
        try:
            if target_win:
                _force_foreground(target_win)
                time.sleep(0.1)
            _uia_set_focus(ctrl)
            time.sleep(0.15)  # let the provider move focus before we type
            reason = _verify_typing_focus(target_win, target_pid, ctrl, 0)
            if reason is not None:
                raise FocusGuardAbort(reason, chars_typed=0, phase="preflight")
            return _send_unicode_text_guarded(
                text, target_win, target_pid, ctrl=ctrl
            )
        finally:
            if restore_foreground:
                _restore_foreground(restore_foreground)

    raise RuntimeError(
        f"control {ctrl.Name!r} has unknown type method {method!r}"
    )
