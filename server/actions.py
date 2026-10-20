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

Logging: every plan / execute / refusal is appended as one JSON line to
logs/actions.log (created automatically).
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import uiautomation as uia

# Re-use the init() from ui_automation so we have a sane UIA apartment.
from server import ui_automation as _ui_mod

_log = logging.getLogger("tool_wl_app.actions")

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
_SetForegroundWindow.restype = ctypes.wintypes.BOOL

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
    """Pick how to type into this control: 'value' or 'keyboard'."""
    return "value" if _has_writable_value_pattern(ctrl) else "keyboard"


def _select_click_method(ctrl: uia.Control) -> str:
    """Pick how to click this control: 'invoke' | 'toggle' | 'select' |
    'expand' | 'coordinate'."""
    if getattr(ctrl, "IsInvokePatternAvailable", False):
        return "invoke"
    if getattr(ctrl, "IsTogglePatternAvailable", False):
        return "toggle"
    if getattr(ctrl, "IsSelectionItemPatternAvailable", False):
        return "select"
    if getattr(ctrl, "IsExpandCollapsePatternAvailable", False):
        return "expand"
    return "coordinate"


METHOD_LABEL = {
    "value": "via ValuePattern",
    "keyboard": "via keyboard input into the focused control",
    "invoke": "via UIA Invoke",
    "toggle": "via UIA Toggle",
    "select": "via UIA SelectionItem",
    "expand": "via UIA ExpandCollapse",
    "coordinate": "via coordinate click on the resolved control",
}


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


@dataclass
class ActionPlan:
    plan_id: str
    tool: str  # "click_control" | "type_text"
    process_name: str
    window_title: str
    control_name: str
    automation_id: str
    # For type_text only:
    current_value: Optional[str] = None
    new_text: Optional[str] = None
    # How the action will be performed ("value" / "keyboard" / "invoke" /
    # "toggle" / "select" / "expand" / "coordinate"). Bound at PLAN time so
    # EXECUTE cannot silently escalate to a different method.
    method: str = "value"
    created_at: float = field(default_factory=time.time)
    consumed: bool = False

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > PLAN_TTL_SECONDS

    def is_valid_for(
        self,
        tool: str,
        automation_id: str,
        control_name: str,
        new_text: Optional[str] = None,
        method: Optional[str] = None,
    ) -> bool:
        return (
            not self.consumed
            and not self.is_expired()
            and self.tool == tool
            and self.automation_id == automation_id
            and self.control_name == control_name
            and (new_text is None or self.new_text == new_text)
            and (method is None or self.method == method)
        )


_plans: Dict[str, ActionPlan] = {}


def _gc_plans() -> None:
    """Remove expired or consumed plans to prevent unbounded growth."""
    global _plans
    expired = [k for k, p in _plans.items() if p.is_expired() or p.consumed]
    for k in expired:
        del _plans[k]


def _store_plan(plan: ActionPlan) -> str:
    _gc_plans()
    _plans[plan.plan_id] = plan
    return plan.plan_id


def _consume_plan(plan_id: str) -> Optional[ActionPlan]:
    plan = _plans.get(plan_id)
    if plan:
        plan.consumed = True
    return plan


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
        plan_uuid = str(uuid.uuid4())[:16]
        plan = ActionPlan(
            plan_id=plan_uuid,
            tool="click_control",
            process_name=process_name,
            window_title=window_title,
            control_name=control_name,
            automation_id=automation_id_val,
            method=method,
        )
        _store_plan(plan)
        _log_event(
            "plan", "click_control", process_name, window_title,
            control_name, f"method={label}"
        )
        return {
            "ok": True,
            "phase": "plan",
            "plan_id": plan_uuid,
            "description": desc,
            "method": method,
            "method_label": label,
            "expires_in_s": PLAN_TTL_SECONDS,
        }

    # Phase 2: EXECUTE — plan_id provided.
    existing_plan = _plans.get(plan_id)
    if existing_plan is None:
        _log_event(
            "refuse", "click_control", process_name, window_title,
            control_name, f"plan_id {plan_id!r} not found"
        )
        return {
            "ok": False,
            "error": "plan_not_found",
            "message": f"plan_id {plan_id!r} not found or already consumed",
        }

    # Re-select the method and require it to match the plan. If the control
    # changed under us (pattern appeared/vanished), refuse rather than
    # silently doing something the user did not approve.
    method = _select_click_method(ctrl)

    if not existing_plan.is_valid_for(
        "click_control", automation_id_val, control_name, method=method
    ):
        reason = "plan expired, consumed, or arguments/method mismatch"
        _log_event(
            "refuse", "click_control", process_name, window_title,
            control_name, reason
        )
        return {"ok": False, "error": "plan_invalid", "message": reason}

    # Perform the click using exactly the approved method.
    try:
        pattern_used = _do_click(ctrl, method=method)
        _consume_plan(plan_id)
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
        else:
            tail = (
                "(the control has no writable ValuePattern; this control will "
                "be focused and the text will be typed into it at the caret, "
                "so existing text is NOT cleared)"
            )
        desc = (
            f"Set text {label} of [{control_name!r}] in window "
            f"{window_title!r} from {trunc_value!r} -> {trunc_text!r} {tail}"
        )
        plan_uuid = str(uuid.uuid4())[:16]
        plan = ActionPlan(
            plan_id=plan_uuid,
            tool="type_text",
            process_name=process_name,
            window_title=window_title,
            control_name=control_name,
            automation_id=automation_id_val,
            current_value=trunc_value,
            new_text=trunc_text,
            method=method,
        )
        _store_plan(plan)
        _log_event(
            "plan", "type_text", process_name, window_title,
            control_name,
            f"method={label} current={trunc_value!r} new={trunc_text!r}"
        )
        return {
            "ok": True,
            "phase": "plan",
            "plan_id": plan_uuid,
            "description": desc,
            "method": method,
            "method_label": label,
            "expires_in_s": PLAN_TTL_SECONDS,
            "current_value": trunc_value,
            "new_text": trunc_text,
        }

    # Phase 2: EXECUTE.
    existing_plan = _plans.get(plan_id)
    if existing_plan is None:
        _log_event(
            "refuse", "type_text", process_name, window_title,
            control_name, f"plan_id {plan_id!r} not found"
        )
        return {
            "ok": False,
            "error": "plan_not_found",
            "message": f"plan_id {plan_id!r} not found or already consumed",
        }

    method = _select_type_method(ctrl)
    if not existing_plan.is_valid_for(
        "type_text", automation_id_val, control_name, text, method=method
    ):
        reason = "plan expired, consumed, or text/method mismatch"
        _log_event(
            "refuse", "type_text", process_name, window_title,
            control_name, reason
        )
        return {"ok": False, "error": "plan_invalid", "message": reason}

    # Perform the write using exactly the approved method.
    try:
        chars = _do_type(ctrl, text, method=method)
        _consume_plan(plan_id)
        _log_event(
            "execute", "type_text", process_name, window_title,
            control_name,
            f"method={METHOD_LABEL[method]} set={text!r}"
        )
        return {
            "ok": True,
            "phase": "execute",
            "method": method,
            "method_label": METHOD_LABEL[method],
            "chars_typed": chars,
            "control_name": control_name,
            "automation_id": automation_id_val,
            "new_value": text,
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


def _do_click(ctrl: uia.Control, method: Optional[str] = None) -> str:
    """Perform the click using exactly `method`. Returns the method used.

    `method` is the value chosen at PLAN time and re-verified by the caller;
    it is never silently upgraded here.
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
    if method == "coordinate":
        # Fallback: physical click at the center of the resolved control's
        # bounding rectangle. Never used while a UIA pattern is available.
        x, y = _center_of(ctrl)
        _send_click_at(x, y)
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


def _do_type(ctrl: uia.Control, text: str, method: Optional[str] = None) -> int:
    """Type `text` into `ctrl` using exactly `method`. Returns chars typed."""
    method = method or _select_type_method(ctrl)

    if method == "value":
        return _do_set_value(ctrl, text)

    if method == "keyboard":
        # Fallback: focus this exact control via UIA, then type with
        # SendInput KEYEVENTF_UNICODE into whatever now has keyboard focus.
        # NOTE: this appends at the caret — it does NOT clear existing text.
        _uia_set_focus(ctrl)
        time.sleep(0.15)  # let the provider move focus before we type
        return _send_unicode_text(text)

    raise RuntimeError(
        f"control {ctrl.Name!r} has unknown type method {method!r}"
    )
