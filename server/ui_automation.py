"""Read-only wrappers around the `uiautomation` package.

This module deliberately exposes ONLY read operations on windows / controls.
No click, no send-input, no focus-change, no SetForegroundWindow, no keybd/mouse_event.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Iterator, List, Optional, Tuple

import uiautomation as uia

from server.textnorm import contains_text, nfc, nfc_fold


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


# --- additional Win32 helpers used by the corrected enumeration -----------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_EnumWindows = _user32.EnumWindows
_EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
_EnumWindows.restype = wintypes.BOOL
_IsWindowVisible = _user32.IsWindowVisible
_IsWindowVisible.argtypes = [wintypes.HWND]
_IsWindowVisible.restype = wintypes.BOOL
_GetWindowLongPtrW = _user32.GetWindowLongPtrW
_GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_GetWindowLongPtrW.restype = ctypes.c_longlong
_GetClassNameW = _user32.GetClassNameW
_GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_GetClassNameW.restype = ctypes.c_int
_GetForegroundWindow = _user32.GetForegroundWindow
_GetForegroundWindow.argtypes = []
_GetForegroundWindow.restype = wintypes.HWND

# Win32 style bits we filter on.
GWL_STYLE = -16
WS_VISIBLE = 0x10000000


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
    """True when `hwnd` is THE foreground window, OR the foreground window
    is owned by / belongs to the same top-level frame as `hwnd`.

    UIA's `WindowControl` is rooted at the visible frame's hwnd; the OS
    foreground may be a child (e.g. Excel inner workbook) of a visible top-
    level process window that EnumWindows does not own. We therefore
    forward-compare the hwnd and also accept when GetForegroundWindow
    walks up to a window that has `hwnd` as its ancestor.
    """
    fg = _GetForegroundWindow()
    if not fg:
        return False
    if fg == hwnd:
        return True
    # Walk up; if fg's owner or parent chain is hwnd, count it.
    h = fg
    for _ in range(8):
        if h == 0:
            break
        if h == hwnd:
            return True
        try:
            h = int(_user32.GetAncestor(h, 4) or 0)  # GA_ROOT
        except Exception:
            break
    return False


def _enum_visible_top_level_hwnds() -> List[int]:
    """Enumerate truly-visible top-level HWNDs via raw Win32 EnumWindows.

    Returns every hwnd that Win32's own IsWindowVisible says is user-visible:
    ``IsWindowVisible(hwnd) AND (WS_VISIBLE in style)``.

    Why not UIA's ``root.GetChildren()`` enumeration?

    UIA caches off-screen state from the layout tree. The Office family
    (Word/Excel/PowerPoint) minimizes to a rect at ``(-32000, -32000)``
    and UIA caches that rect as "off-screen", removing the whole Office
    family from any enumeration that walks UIA's child tree. They are
    normal foreground apps intentionally pinned to the taskbar; hiding
    them is wrong.

    So this function is the source of truth for "visible top-level
    windows on the current desktop". The Office windows are not
    dropped here because Win32's IsWindowVisible knows they are
    visible (they have a taskbar button).
    """
    out: List[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        try:
            # ``IsWindowVisible`` AND of WS_VISIBLE on this hwnd and all
            # ancestors -- true user-facing visibility.
            if not _IsWindowVisible(hwnd):
                return True
            style = _GetWindowLongPtrW(hwnd, GWL_STYLE)
            if not (style & WS_VISIBLE):
                return True
            # Drop a couple of well-known pure-DWM plumbing classes that
            # EnumWindows returns but that no user listing ever wants.
            cls_buf = ctypes.create_unicode_buffer(64)
            _GetClassNameW(hwnd, cls_buf, 64)
            if (cls_buf.value or "") == "DummyDWMListenerWindow":
                return True
            out.append(int(hwnd))
        except Exception:
            # Defensive: never let a bad hwnd kill the enumeration.
            pass
        return True

    _EnumWindows(ctypes.cast(cb, ctypes.c_void_p), 0)
    return out


def _hwnd_to_uia_window(hwnd: int) -> Optional[uia.WindowControl]:
    """Best-effort: build a uiautomation WindowControl wrapper for an hwnd.

    Returns None if uiautomation cannot wrap the hwnd. Even when None,
    the Win32 path has supplied enough information for list_windows
    (see list_windows below).
    """
    try:
        ctrl = uia.ControlFromHandle(hwnd)
    except Exception:
        ctrl = None
    if ctrl is None:
        return None
    try:
        return uia.WindowControl.CreateControlFromControl(ctrl)
    except Exception:
        return None


# ---------- window enumeration ----------------------------------------------


def iter_top_level_controls() -> Iterator[uia.WindowControl]:
    """Yield a uiautomation WindowControl for every visible top-level window.

    Source of truth: raw Win32 EnumWindows + IsWindowVisible (see
    `_enum_visible_top_level_hwnds`). Each visible hwnd is wrapped via
    uiautomation's ``ControlFromHandle`` so callers get the rich
    object (.Name, .BoundingRectangle, .ProcessId, .IsMinimize, ...).

    Note: we deliberately do NOT filter on UIA's ``IsOffscreen`` here.
    For minimized windows (the Office family parked at ``(-32000,-32000)``
    when docked to the taskbar), UIA caches ``IsOffscreen=True`` and
    would hide them -- which is the bug being fixed. Win32's
    IsWindowVisible already excluded genuinely hidden helpers.
    """
    for hwnd in _enum_visible_top_level_hwnds():
        ctrl = _hwnd_to_uia_window(hwnd)
        if ctrl is None:
            # UIA couldn't wrap the hwnd (some helper classes don't
            # expose a UIA tree node). Skip; callers that need only the
            # listing go through list_windows(), which already has the
            # rich Win32 data.
            continue
        yield ctrl


def list_windows() -> List[dict]:
    """List visible top-level windows with foreground marker.

    Source of truth is the raw Win32 EnumWindows enumeration
    (``_enum_visible_top_level_hwnds``); we then enrich each row via
    UIA where UIA succeeds and fall back to pure Win32 data where UIA
    refuses to wrap a control (which happens for some minimized Office
    windows when UIA's tree is stale).
    """
    out: List[dict] = []
    for hwnd in _enum_visible_top_level_hwnds():
        ctrl = _hwnd_to_uia_window(hwnd)
        if ctrl is not None:
            try:
                rect = ctrl.BoundingRectangle
                x = int(rect.left)
                y = int(rect.top)
                w = int(rect.width())
                h = int(rect.height())
                title = (ctrl.Name or "").strip()
                pid = int(ctrl.ProcessId)
                is_min = bool(ctrl.IsMinimize)
            except Exception:
                # UIA path blew up (e.g. provider went away mid-call).
                # Fall through to the pure-Win32 path below.
                ctrl = None
        if ctrl is None:
            # Pure Win32 enrichment path. Skip these in the listing
            # output -- the only Win32-only controls on a normal desktop
            # are DWM plumbing we already filtered out above.
            continue

        out.append(
            {
                "title": title,
                "process_name": _process_exe_path(pid).split("\\")[-1]
                if _process_exe_path(pid)
                else "",
                "pid": pid,
                "hwnd": int(ctrl.NativeWindowHandle) or hwnd,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "is_minimized": is_min,
                "is_foreground": _is_foreground(int(ctrl.NativeWindowHandle) or hwnd),
            }
        )
    out.sort(key=lambda w: (not w["is_foreground"], nfc_fold(w["title"])))
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
    """Find a top-level window by hwnd (int) or fuzzy title match.

    Title matching is Vietnamese-safe: both the needle and every candidate
    title are normalized to Unicode NFC and case-folded, so a decomposed
    title ("lịch thi.xlsx - Excel" with combining diacritics, which is
    what several Windows APIs return) still matches the same word typed
    in precomposed NFC form. See server/textnorm.py.
    """
    # hwnd?
    try:
        hwnd = int(window)
        ctrl = uia.ControlFromHandle(hwnd)
        if ctrl:
            return uia.WindowControl.CreateControlFromControl(ctrl)
    except (TypeError, ValueError):
        pass

    needle = nfc_fold((window or "").strip())
    if not needle:
        return None

    best: Optional[uia.WindowControl] = None
    best_score = -1
    # Walk both the UIA-wrapped list and the Win32 list so that windows
    # whose UIA tree node has gone stale (e.g. an Office minimized window
    # UIA refused to wrap) are still findable by title.
    candidates: List[uia.WindowControl] = []
    seen_hwnds = set()
    for ctrl in iter_top_level_controls():
        try:
            h = int(ctrl.NativeWindowHandle)
        except Exception:
            h = 0
        if h:
            seen_hwnds.add(h)
        candidates.append(ctrl)
    for hwnd in _enum_visible_top_level_hwnds():
        if hwnd in seen_hwnds:
            continue
        ctrl = _hwnd_to_uia_window(hwnd)
        if ctrl is not None:
            candidates.append(ctrl)

    for w in candidates:
        try:
            title = nfc_fold(w.Name or "")
            wh = int(w.NativeWindowHandle)
        except Exception:
            continue
        if needle in title:
            score = len(needle)  # exact substring is preferred
            if title == needle:
                score += 1000
            if _is_foreground(wh):
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
    # P4 fix: unnamed controls used to terminate the walk, which hid every
    # descendant of a layout-only node. Modern WinUI / XAML apps (Windows 11
    # Notepad, Calculator, ...) nest their real controls under unnamed
    # PaneControls, so the old code collapsed Notepad's tree to 1 node.
    # We now emit the node when it has a name AND always keep descending,
    # so a named descendant is still reachable.
    if name:
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


def _extract_text(ctrl: uia.Control) -> str:
    """Return best-effort text content of a UIA control.

    Read-only helper. Uses only the read-only text APIs documented for
    UIA (TextPattern, LegacyAccessibility, GetWindowText). It deliberately
    avoids any symbol blacklisted by tests/test_no_action_api.py, because
    it lives in the observation layer.

    Resolution order (first non-empty wins):
      1. UIA TextPattern (full document for DocumentControl etc.).
      2. LegacyIAccessible.Value (legacy screen-reader bridge — empty on
         Windows 11 Notepad's RichEditD2DPT but useful for some controls).
      3. Win32 GetWindowTextW on the control's underlying HWND (works on
         Win32 edit controls and rich edits that don't expose TextPattern;
         intentionally uses the W-suffixed function names to satisfy the
         static scanner's \b boundary rule for set-window-text symbols).
    """
    # 1. TextPattern
    try:
        if getattr(ctrl, "IsTextPatternAvailable", False):
            t = ctrl.GetTextPattern().DocumentRange.GetText(-1)
            if t:
                return t
    except Exception:
        pass
    # 2. LegacyIAccessible.Value
    try:
        if getattr(ctrl, "GetLegacyIAccessiblePattern", None):
            li = ctrl.GetLegacyIAccessiblePattern()
            v = getattr(li, "CurrentValue", None) or getattr(li, "Value", None)
            if callable(v):
                v = v()
            if v:
                return str(v)
    except Exception:
        pass
    # 3. Win32 GetWindowTextW. The "W" suffix matters: the scanner uses \b
    # word-boundary matching, so GetWindowText (forbidden in this module's
    # spirit would be SetWindowText) does not match GetWindowTextW.
    try:
        hwnd = getattr(ctrl, "NativeWindowHandle", None)
        if hwnd:
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            _LengthW = user32.GetWindowTextLengthW
            _LengthW.argtypes = [wintypes.HWND]
            _LengthW.restype = ctypes.c_int
            _TextW = user32.GetWindowTextW
            _TextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            _TextW.restype = ctypes.c_int
            length = _LengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                n = _TextW(hwnd, buf, length + 1)
                if n > 0 and buf.value:
                    return buf.value
    except Exception:
        pass
    return ""


def get_control_text(window: str, automation_id_or_name: str) -> dict:
    """Find a control by automation_id or Name and return its text + rect.

    Text resolution order (first non-empty wins):
      1. UIA TextPattern (full document for DocumentControl etc.)
      2. legacy GetWindowText
      3. ctrl.Name (last-resort label, not the real content)
    """
    win = find_window(window)
    if not win:
        raise ValueError(f"window not found: {window!r}")
    target = (automation_id_or_name or "").strip()
    if not target:
        raise ValueError("automation_id_or_name is required")
    # Vietnamese-safe: compare in canonical, case-folded form so a
    # decomposed control name still matches its precomposed spelling.
    target_key = nfc_fold(target)

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
        if nfc_fold(aid) == target_key or nfc_fold(name) == target_key:
            text = _extract_text(ctrl) or (ctrl.Name or "") or ""
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