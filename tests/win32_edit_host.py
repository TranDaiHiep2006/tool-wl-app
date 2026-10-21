"""Real Win32 host window with a genuine 'Edit' control — for E2E only.

Runs as a SUBPROCESS so it has its own message pump (SendMessageTimeout
needs a live message loop to answer) and so the test can observe exactly
what text each window received via cross-process WM_GETTEXT.

Usage:
    python tests/win32_edit_host.py <window-title>

Prints one JSON line on stdout once the window is ready:
    {"hwnd": <top-level>, "edit_hwnd": <edit>, "pid": <pid>, "title": ...}

Keeps running (message pump) until killed.

NOTE: this file deliberately creates windows/messages. It is a TEST
fixture, never imported by server/.
"""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_BORDER = 0x00800000
CW_DEFAULT = 0

WM_DESTROY = 0x0002
WM_CHAR = 0x0102
WM_SETTEXT = 0x000C

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
    wintypes.WPARAM, wintypes.LPARAM,
)

user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
user32.DefWindowProcW.restype = ctypes.c_longlong


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


GWLP_WNDPROC = -4


def _subclass(hwnd, proc: WNDPROC) -> int:
    """Swap a window procedure, handling 32/64-bit pointer width."""
    u = user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        fn = u.SetWindowLongPtrW
    else:
        fn = u.SetWindowLongW
    fn.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    fn.restype = ctypes.c_ssize_t
    addr = ctypes.cast(proc, ctypes.c_void_p).value
    return int(fn(wintypes.HWND(hwnd), GWLP_WNDPROC, addr) or 0)


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else "ToolWl Edit Host"

    # Count every WM_CHAR that lands on the Edit, so we can prove exactly how
    # many characters were physically delivered to THIS window.
    counters = {"chars": 0}

    def _host_proc(h, msg, wp, lp):
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(h, msg, wp, lp)

    hproc = WNDPROC(_host_proc)
    hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
    wc = WNDCLASS()
    wc.style = 0x0003
    wc.lpfnWndProc = hproc
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
    wc.hbrBackground = gdi32.GetStockObject(5)   # COLOR_BTNFACE+1
    wc.lpszClassName = "ToolWlEditHostV1"
    user32.RegisterClassW(ctypes.byref(wc))

    host = user32.CreateWindowExW(
        0, "ToolWlEditHostV1", title,
        WS_OVERLAPPEDWINDOW, 120, 140, 560, 240,
        None, None, hinst, None,
    )
    if not host:
        print(json.dumps({"error": "host window failed",
                          "winerr": ctypes.get_last_error()}), flush=True)
        return 2
    edit = user32.CreateWindowExW(
        0, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_BORDER | 0x00800000,
        10, 10, 520, 170,
        wintypes.HWND(host), None, hinst, None,
    )
    if not edit:
        print(json.dumps({"error": "edit control failed",
                          "winerr": ctypes.get_last_error()}), flush=True)
        return 2

    # Subclass the Edit so WM_CHAR is counted and can be queried via a
    # custom message from the test process.
    BM_COUNT = 0x0400 + 100  # WM_APP+100

    def _edit_proc(h, msg, wp, lp):
        # Count every WM_CHAR physically delivered to THIS window. The default
        # Edit proc also inserts the character, so the Edit's own text (read
        # cross-process via WM_GETTEXT) is an independent record of exactly
        # what was typed here.
        if msg == WM_CHAR:
            counters["chars"] += 1
        elif msg == BM_COUNT:
            return counters["chars"]
        return user32.DefWindowProcW(h, msg, wp, lp)

    eproc = WNDPROC(_edit_proc)
    _subclass(edit, eproc)
    # Re-apply the styles we need (SetWindowLongPtr can reset some state).
    user32.ShowWindow(wintypes.HWND(edit), 5)  # SW_SHOW

    user32.ShowWindow(wintypes.HWND(host), 5)
    user32.UpdateWindow(wintypes.HWND(host))

    print(json.dumps({
        "ok": True,
        "hwnd": int(host),
        "edit_hwnd": int(edit),
        "pid": ctypes.windll.kernel32.GetCurrentProcessId(),
        "title": title,
        "bm_count_msg": BM_COUNT,
    }), flush=True)

    # Message pump — this is what makes SendMessageTimeout answerable
    # cross-process.
    msg = wintypes.MSG()
    while True:
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r in (0, -1):
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())