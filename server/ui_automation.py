"""Read-only wrappers around the `uiautomation` package.

This module deliberately exposes ONLY read operations on windows / controls.
No click, no send-input, no focus-change, no SetForegroundWindow, no keybd/mouse_event.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Iterator, List, Optional, Tuple

import uiautomation as uia


def init() -> None:
    """Initialize UIA in the current thread (idempotent)."""
    uia.InitializeUIAutomationInCurrentThread()


# ---------- low-level Win32 helpers (read-only) -----------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_psapi = ctypes.WinDLL("Psapi.dll", use_last_error=True)


def _process_exe_path(pid: int) -> Optional[str]:
    """Return the full executable path for a PID using QueryFullProcessImageNameW."""
    if pid <= 0:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        # QueryFullProcessImageNameW(HANDLE hProcess, DWORD dwFlags,
        #                            LPWSTR lpExeName, PDWORD lpdwSize)
        buf = ctypes.create_unicode_buffer(2048)
        size = wintypes.DWORD(len(buf))
        ok = _kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buf,
            ctypes.byref(size),
        )
        if ok:
            return buf.value
    finally:
        _kernel32.CloseHandle(handle)
    return None


def _is_foreground(hwnd: int) -> bool:
    return ctypes.windll.user32.GetForegroundWindow() == hwnd


# ---------- window enumeration ----------------------------------------------


def iter_top_level_controls() -> Iterator[uia.WindowControl]:
    """Yield every visible, named top-level WindowControl under the desktop."""
    root = uia.GetRootControl()
    for ctrl in root.GetChildren():
        if not isinstance(ctrl, uia.WindowControl):
            continue
        if not ctrl.IsTopLevel():
            continue
        if ctrl.IsOffscreen:
            continue
        if ctrl.NativeWindowHandle == 0:
            continue
        yield ctrl


def list_windows() -> List[dict]:
    """List visible, named top-level windows with foreground marker."""
    out: List[dict] = []
    for ctrl in iter_top_level_controls():
        rect = ctrl.BoundingRectangle
        title = (ctrl.Name or "").strip()
        if not title:
            # Skip nameless invisible hosts.
            pass
        out.append(
            {
                "title": title,
                "process_name": _process_exe_path(ctrl.ProcessId).split("\\")[-1]
                if _process_exe_path(ctrl.ProcessId)
                else "",
                "pid": ctrl.ProcessId,
                "hwnd": int(ctrl.NativeWindowHandle),
                "x": rect.left,
                "y": rect.top,
                "w": rect.width(),
                "h": rect.height(),
                "is_minimized": bool(ctrl.IsMinimize),
                "is_foreground": _is_foreground(int(ctrl.NativeWindowHandle)),
            }
        )
    out.sort(key=lambda w: (not w["is_foreground"], w["title"].lower()))
    return out


def get_foreground_window() -> dict:
    """Return the foreground window with full exe path."""
    fg = uia.GetForegroundControl()
    rect = fg.BoundingRectangle
    hwnd = int(fg.NativeWindowHandle)
    pid = fg.ProcessId
    return {
        "title": (fg.Name or "").strip(),
        "process_name": _process_exe_path(pid).split("\\")[-1]
        if _process_exe_path(pid)
        else "",
        "exe_path": _process_exe_path(pid) or "",
        "pid": pid,
        "hwnd": hwnd,
        "x": rect.left,
        "y": rect.top,
        "w": rect.width(),
        "h": rect.height(),
        "is_minimized": bool(fg.IsMinimize),
        "is_foreground": True,
    }


# ---------- window lookup ----------------------------------------------------


def find_window(window: str) -> Optional[uia.WindowControl]:
    """Find a top-level window by hwnd (int) or fuzzy title match."""
    # hwnd?
    try:
        hwnd = int(window)
        ctrl = uia.ControlFromHandle(hwnd)
        if ctrl:
            return uia.WindowControl.CreateControlFromControl(ctrl)
    except (TypeError, ValueError):
        pass

    needle = (window or "").strip().lower()
    if not needle:
        return None

    best: Optional[uia.WindowControl] = None
    best_score = -1
    for w in iter_top_level_controls():
        title = (w.Name or "").lower()
        if needle in title:
            score = len(needle)  # exact substring is preferred
            if title == needle:
                score += 1000
            if _is_foreground(int(w.NativeWindowHandle)):
                score += 500
            if score > best_score:
                best = w
                best_score = score
    return best


# ---------- tree walking ----------------------------------------------------


def _rect_to_dict(rect) -> dict:
    return {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()}


def _walk(
    control: uia.Control,
    out: List[dict],
    depth: int,
    max_depth: int,
    budget: List[int],
) -> None:
    if budget[0] <= 0:
        return
    if depth > max_depth:
        return
    if control.IsOffscreen:
        return
    name = (control.Name or "").strip()
    if not name:
        return
    budget[0] -= 1
    out.append(
        {
            "control_type": control.ControlTypeName,
            "name": name,
            "automation_id": control.AutomationId or "",
            **_rect_to_dict(control.BoundingRectangle),
        }
    )
    for child in control.GetChildren():
        if budget[0] <= 0:
            break
        _walk(child, out, depth + 1, max_depth, budget)


def get_ui_tree(
    window: str,
    max_depth: int = 6,
    max_nodes: int = 300,
) -> dict:
    """Return a pruned UIA tree of `window` (hwnd or fuzzy title)."""
    win = find_window(window)
    if not win:
        raise ValueError(f"window not found: {window!r}")

    nodes: List[dict] = []
    budget = [max_nodes]
    _walk(win, nodes, 0, max_depth, budget)
    truncated = budget[0] <= 0
    return {
        "window_title": (win.Name or "").strip(),
        "pid": win.ProcessId,
        "hwnd": int(win.NativeWindowHandle),
        "nodes": nodes,
        "truncated": truncated,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
    }


# ---------- single control text --------------------------------------------


def get_control_text(window: str, automation_id_or_name: str) -> dict:
    """Find a control by automation_id or Name and return its text + rect."""
    win = find_window(window)
    if not win:
        raise ValueError(f"window not found: {window!r}")
    target = (automation_id_or_name or "").strip()
    if not target:
        raise ValueError("automation_id_or_name is required")

    # Search breadth-first up to a small budget
    queue: List[Tuple[uia.Control, int]] = [(win, 0)]
    seen = 0
    while queue:
        ctrl, depth = queue.pop(0)
        seen += 1
        if seen > 600:
            break
        if depth > 8:
            continue
        if ctrl.IsOffscreen:
            continue
        aid = (ctrl.AutomationId or "").strip()
        name = (ctrl.Name or "").strip()
        if aid == target or name == target:
            text = ctrl.Name or ctrl.GetWindowText() or ""
            return {
                "found": True,
                "control_type": ctrl.ControlTypeName,
                "automation_id": aid,
                "name": name,
                "text": text,
                **_rect_to_dict(ctrl.BoundingRectangle),
            }
        for c in ctrl.GetChildren():
            queue.append((c, depth + 1))
    return {
        "found": False,
        "reason": f"no control with automation_id or name == {target!r}",
        "window_title": (win.Name or "").strip(),
    }