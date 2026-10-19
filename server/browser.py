"""Get the active tab info from Chrome / Edge / Firefox via UI Automation.

We deliberately use ONLY read-only APIs here (no clicks, no focus changes).
"""

from __future__ import annotations

import re
from typing import Optional

import uiautomation as uia

# A URL is either a full http(s)/ftp URL OR a bare hostname like
# "www.youtube.com/watch?v=...". We accept both shapes but reject
# arbitrary text.
_SCHEMED_URL = re.compile(r"^(https?|ftp)://", re.IGNORECASE)
_BARE_URL = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+(/|$|\?)",
    re.IGNORECASE,
)


def _looks_like_url(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    return bool(_SCHEMED_URL.match(text) or _BARE_URL.match(text))

# Process name -> browser label
_BROWSERS = {
    "chrome.exe": "chrome",
    "msedge.exe": "edge",
    "firefox.exe": "firefox",
    "brave.exe": "brave",
    "opera.exe": "opera",
    "vivaldi.exe": "vivaldi",
}

# Chrome / Edge / Brave / Opera share the same DOM. Firefox has its own.
_CHROMIUM_CLASS_PREFIXES = ("Chrome_WidgetWin_", "Chrome_RenderWidgetHostHWND")


def _all_windows_for(browser_exe: str):
    """Yield visible top-level windows belonging to a given browser process."""
    root = uia.GetRootControl()
    for child in root.GetChildren():
        if not isinstance(child, uia.WindowControl):
            continue
        if not child.IsTopLevel():
            continue
        if child.IsOffscreen:
            continue
        try:
            pid = child.ProcessId
        except Exception:
            continue
        # We must look up exe path; we use a tiny helper from ui_automation module.
        from server.ui_automation import _process_exe_path

        exe = _process_exe_path(pid) or ""
        name = exe.split("\\")[-1].lower()
        if name == browser_exe:
            yield child


def _find_address_bar(win: uia.WindowControl) -> Optional[uia.Control]:
    """Find the address bar / URL control inside a Chrome/Edge/Firefox window."""
    # Chrome: EditControl, automation_id == "addressAndSearchBar"
    # Edge:   EditControl, automation_id contains "address" or "urlBar"
    # Firefox: ComboBoxControl, automation_id == "urlbar-input"
    queue = [win]
    seen = 0
    while queue and seen < 2000:
        ctrl = queue.pop(0)
        seen += 1
        aid = (ctrl.AutomationId or "").lower()
        cls = (ctrl.ClassName or "").lower()
        if "address" in aid and "search" in aid:
            return ctrl
        if ctrl.ControlTypeName in ("EditControl", "ComboBoxControl") and (
            "url" in aid or "address" in aid
        ):
            return ctrl
        try:
            for c in ctrl.GetChildren():
                queue.append(c)
        except Exception:
            pass
    return None


def _find_tab_title(win: uia.WindowControl) -> str:
    """The window's Name is usually the active tab title (Chrome/Edge/Firefox)."""
    return (win.Name or "").strip()


def _get_url_from_control(ctrl: uia.Control) -> Optional[str]:
    """Try ValuePattern, then Name, then LegacyIAccessiblePattern.value.

    Returns the value only if it actually looks like a URL.
    """
    candidates: list[str] = []
    try:
        val = ctrl.GetValuePattern()
        if val:
            text = getattr(val, "Value", None) or getattr(val, "value", None)
            if text:
                candidates.append(text)
    except Exception:
        pass
    name = (ctrl.Name or "").strip()
    if name:
        candidates.append(name)
    try:
        leg = ctrl.GetLegacyIAccessiblePattern()
        if leg:
            v = leg.value
            if v:
                candidates.append(v)
    except Exception:
        pass
    for c in candidates:
        c = c.strip()
        if _looks_like_url(c):
            return c
    return None


def get_browser_tab_info() -> dict:
    """Return info of the active browser tab, if any."""
    try:
        uia.InitializeUIAutomationInCurrentThread()
    except Exception:
        pass

    # Foreground first
    fg_hwnd = ctypes_foreground_hwnd()
    fg_pid = 0
    try:
        fg = uia.ControlFromHandle(fg_hwnd)
        fg_pid = fg.ProcessId if fg else 0
    except Exception:
        pass

    # Try each known browser
    for browser_exe, label in _BROWSERS.items():
        wins = list(_all_windows_for(browser_exe))
        if not wins:
            continue

        # Prefer foreground window of that browser
        win = None
        for w in wins:
            if int(w.NativeWindowHandle) == fg_hwnd:
                win = w
                break
        if win is None:
            win = wins[0]

        url: Optional[str] = None
        try:
            ab = _find_address_bar(win)
            if ab:
                url = _get_url_from_control(ab)
        except Exception:
            url = None

        # Fallback: try every Edit control looking for a URL
        if not url:
            url = _fallback_url_from_edits(win)

        title = _find_tab_title(win)
        return {
            "supported": True,
            "browser": label,
            "title": title,
            "url": url or "",
            "hwnd": int(win.NativeWindowHandle),
            "pid": win.ProcessId,
        }

    return {
        "supported": False,
        "reason": "no supported browser window found (chrome/edge/firefox/brave/opera/vivaldi)",
    }


def _fallback_url_from_edits(win: uia.WindowControl) -> Optional[str]:
    queue = [win]
    seen = 0
    while queue and seen < 2000:
        ctrl = queue.pop(0)
        seen += 1
        try:
            if ctrl.ControlTypeName in ("EditControl", "ComboBoxControl"):
                v = _get_url_from_control(ctrl)
                if v and _looks_like_url(v):
                    return v.strip()
        except Exception:
            pass
        try:
            for c in ctrl.GetChildren():
                queue.append(c)
        except Exception:
            pass
    return None


def ctypes_foreground_hwnd() -> int:
    import ctypes

    return ctypes.windll.user32.GetForegroundWindow()