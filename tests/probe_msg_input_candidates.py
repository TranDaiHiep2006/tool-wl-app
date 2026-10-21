"""P5 Section B1 — probe message-based character-input candidates on THIS
machine's Notepad.

Problem being solved
--------------------
Windows 11 Notepad's document control (class `RichEditD2DPT`) genuinely
rejects `WM_SETTEXT`: the send returns non-zero but the control does not
take the text, so the P4 ladder falls through all the way to `SendInput`.
`SendInput` follows the *global* foreground focus, which is exactly the
focus-safety hazard P4 round 3 was about.

If any *message-based* method can write into such a control, we get a
focus-free tier that never touches the foreground at all — strictly
better than SendInput.

Candidates probed (in the order they would join the ladder):
  1. WM_CHAR sequence  — send each character as a WM_CHAR message to the
     control HWND. Pure message input, no clipboard, no focus.
  2. EM_REPLACESEL     — replace the current selection. Works on
     RichEdit-family controls; needs a caret/selection.
  3. WM_PASTE          — put the text on the clipboard and paste it.
     Requires clipboard save + restore.

Each candidate is verified by READING THE TEXT BACK. A candidate only
joins the production ladder if read-back proves it actually wrote.

Run:
    python tests/probe_msg_input_candidates.py

This is NOT part of the default pytest run — it drives a real Notepad.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

import uiautomation as uia  # noqa: E402

from server import ui_automation as _ui_mod  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C
WM_CHAR = 0x0102
WM_PASTE = 0x0302
EM_REPLACESEL = 0x00C2

SMTO_ABORTIFHUNG = 0x0002
MESSAGE_TIMEOUT_MS = 2000

_SendMessageTimeoutW = user32.SendMessageTimeoutW
_SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
_SendMessageTimeoutW.restype = wintypes.BOOL

_OPENCLIPBOARD = user32.OpenClipboard
_OPENCLIPBOARD.argtypes = [wintypes.HWND]
_OPENCLIPBOARD.restype = wintypes.BOOL


def _open_clipboard_with_retry(max_attempts: int = 20, sleep_s: float = 0.05) -> bool:
    """Open the clipboard, retrying if another app is holding it.

    Windows apps occasionally hold the clipboard open for a short while
    after a paste (RichEditD2DPT does this — one of the candidate
    receiver apps). The retry is bounded and sleeps briefly between
    attempts, so the script never hangs.
    """
    for _ in range(max_attempts):
        if _OPENCLIPBOARD(None):
            return True
        time.sleep(sleep_s)
    return False
_GETCLIPBOARDDATA = user32.GetClipboardData
_GETCLIPBOARDDATA.argtypes = [wintypes.UINT]
_GETCLIPBOARDDATA.restype = wintypes.HANDLE
_SETCLIPBOARDDATA = user32.SetClipboardData
_SETCLIPBOARDDATA.argtypes = [wintypes.UINT, wintypes.HANDLE]
_SETCLIPBOARDDATA.restype = wintypes.HANDLE
_EMPTYCLIPBOARD = user32.EmptyClipboard
_EMPTYCLIPBOARD.argtypes = []
_EMPTYCLIPBOARD.restype = wintypes.BOOL
_CLOSE_CLIPBOARD = user32.CloseClipboard
_CLOSE_CLIPBOARD.argtypes = []
_CLOSE_CLIPBOARD.restype = wintypes.BOOL

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def send_msg(hwnd: int, msg: int, wparam: int = 0, lparam=None) -> int:
    result = ctypes.c_size_t(0)
    if isinstance(lparam, ctypes.Array) or isinstance(lparam, ctypes._Pointer):
        lp = ctypes.cast(lparam, ctypes.c_void_p)
    elif lparam:
        lp = ctypes.c_void_p(int(lparam))
    else:
        lp = ctypes.c_void_p(0)
    ok = _SendMessageTimeoutW(
        wintypes.HWND(hwnd), wintypes.UINT(msg), wintypes.WPARAM(wparam), lp,
        SMTO_ABORTIFHUNG, MESSAGE_TIMEOUT_MS, ctypes.byref(result),
    )
    if not ok:
        return 0
    return int(result.value) if result.value else 0


def wm_get_text(hwnd: int) -> str:
    length = send_msg(hwnd, WM_GETTEXTLENGTH)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    got = send_msg(hwnd, WM_GETTEXT, length + 1, buf)
    if got <= 0:
        return ""
    return buf.value or ""


def wm_set_text(hwnd: int, text: str) -> bool:
    buf = ctypes.create_unicode_buffer(text)
    return send_msg(hwnd, WM_SETTEXT, 0, buf) != 0


def class_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value or ""


# ---------------- clipboard save / restore ----------------

def clipboard_read_text() -> str | None:
    """Current clipboard text, or None if it holds no text."""
    if not _open_clipboard_with_retry():
        return None
    try:
        h = _GETCLIPBOARDDATA(CF_UNICODETEXT)
        if not h:
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return None
        try:
            size = kernel32.GlobalSize(h)
            ctypes.memmove  # noqa
            raw = ctypes.string_at(ptr, size)
            return raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
        finally:
            kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalUnlock(h)
    finally:
        _CLOSE_CLIPBOARD()


def clipboard_set_text(text: str) -> None:
    if not _open_clipboard_with_retry():
        raise RuntimeError("could not open clipboard for writing")
    try:
        _EMPTYCLIPBOARD()
        data = text + "\x00"
        size = len(data.encode("utf-16-le"))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data.encode("utf-16-le"), size)
        kernel32.GlobalUnlock(handle)
        _SETCLIPBOARDDATA(CF_UNICODETEXT, handle)
    finally:
        _CLOSE_CLIPBOARD()


# ---------------- the candidates ----------------

def _clean(hwnd: int) -> None:
    """Put the control into a known-empty state before probing."""
    wm_set_text(hwnd, "")
    time.sleep(0.05)


def candidate_wm_set_text(hwnd: int, text: str) -> tuple[bool, str]:
    _clean(hwnd)
    ok = wm_set_text(hwnd, text)
    time.sleep(0.05)
    back = wm_get_text(hwnd)
    print(f"      (WM_SETTEXT returned {ok})")
    return (ok and back == text), back


def candidate_wm_char(hwnd: int, text: str) -> tuple[bool, str]:
    """Send each UTF-16 code unit as a WM_CHAR message."""
    _clean(hwnd)
    for ch in text:
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            unit = int.from_bytes(raw[i:i + 2], "little")
            send_msg(hwnd, WM_CHAR, unit, 0)
    time.sleep(0.1)
    back = wm_get_text(hwnd)
    return (back == text), back


def candidate_em_replacesel(hwnd: int, text: str) -> tuple[bool, str]:
    """EM_REPLACESEL with wParam=1 (replace the selection)."""
    _clean(hwnd)
    buf = ctypes.create_unicode_buffer(text)
    send_msg(hwnd, EM_REPLACESEL, 1, buf)
    time.sleep(0.1)
    back = wm_get_text(hwnd)
    return (back == text), back


def candidate_wm_paste(hwnd: int, text: str) -> tuple[bool, str]:
    """Save the clipboard, set it, paste into the control, restore."""
    saved = clipboard_read_text()
    try:
        clipboard_set_text(text)
        _clean(hwnd)
        send_msg(hwnd, WM_PASTE, 0, 0)
        time.sleep(0.15)
        back = wm_get_text(hwnd)
    finally:
        # The user's clipboard MUST come back, pass or fail.
        if saved is not None:
            try:
                clipboard_set_text(saved)
            except Exception:
                pass
    return (back == text), back


CANDIDATES = [
    ("WM_SETTEXT", candidate_wm_set_text),
    ("WM_CHAR sequence", candidate_wm_char),
    ("EM_REPLACESEL", candidate_em_replacesel),
    ("WM_PASTE (clipboard save+restore)", candidate_wm_paste),
]

# P5 Section A's acceptance sentence — the real target for this probe.
VIETNAMESE = "Hà Nội những ngày đầu đông, sương mù giăng kín lối cũ."


def find_notepad_edit_control(timeout_s: float = 15.0):
    """Locate Notepad's editable document control.

    Windows 11 Notepad does NOT expose an EditControl — the editable
    surface is a ``DocumentControl`` named "Text editor" whose Win32 class
    is RichEditD2DPT. That class is the whole reason this probe exists:
    it is the control that rejects WM_SETTEXT.
    """
    _ui_mod.init()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        win = _ui_mod.find_window("Notepad")
        if win:
            queue = [(win, 0)]
            while queue:
                ctrl, depth = queue.pop(0)
                if depth > 6:
                    continue
                try:
                    ctype = ctrl.ControlTypeName
                    hwnd = int(ctrl.NativeWindowHandle or 0)
                    if ctype == "DocumentControl" and hwnd:
                        return ctrl, hwnd
                except Exception:
                    pass
                try:
                    for c in ctrl.GetChildren():
                        queue.append((c, depth + 1))
                except Exception:
                    pass
        time.sleep(0.4)
    return None, 0


def main() -> int:
    probe_text = "HA NOI 123"
    print("=" * 72)
    print("P5 B1 — message-input candidate probe on this machine's Notepad")
    print("=" * 72)

    print("launching Notepad...")
    proc = subprocess.Popen(["notepad.exe"])
    try:
        ctrl, hwnd = find_notepad_edit_control()
        if not hwnd:
            print("FAIL: could not find a Notepad Edit control within timeout")
            return 2
        cls = class_of(hwnd)
        print(f"Notepad edit control: hwnd={hwnd} class={cls!r}")
        print()

        results = {}
        for name, fn in CANDIDATES:
            try:
                ok, back = fn(hwnd, probe_text)
            except Exception as exc:
                ok, back = False, f"<exception: {exc}>"
            results[name] = ok
            verdict = "WORKS" if ok else "does NOT work"
            print(f"  {name:38s} -> {verdict}")
            print(f"      read-back: {back!r}")

        print()
        print("-" * 72)
        print("SUMMARY (a candidate joins the ladder ONLY if read-back proves it):")
        for name, ok in results.items():
            print(f"  {name:38s} : {'VERIFIED' if ok else 'REJECTED'}")

        # --- Vietnamese round-trip through each candidate -------------
        # Message-based input must carry composed Vietnamese exactly;
        # a mojibake here is the whole reason SendInput/IME was a risk.
        print()
        print("-" * 72)
        print("VIETNAMESE round-trip per candidate (exact NFC equality required):")
        print(f"  target: {VIETNAMESE!r}")
        for name, fn in CANDIDATES:
            try:
                ok, back = fn(hwnd, VIETNAMESE)
                exact = back == VIETNAMESE
            except Exception as exc:
                ok, back = False, f"<exception: {exc}>"
                exact = False
            mark = "EXACT" if exact else ("mangled" if ok else "FAILED")
            print(f"  {name:38s} : {mark}")
            if not exact:
                print(f"      got: {back!r}")

        # Clipboard restoration is a hard requirement for WM_PASTE: the
        # user's clipboard must survive the action untouched.
        print()
        print("-" * 72)
        print("clipboard save/restore check (WM_PASTE must not eat user data):")
        sentinel = "USER_CLIPBOARD_SENTINEL_42"
        clipboard_set_text(sentinel)
        read_back_before = clipboard_read_text()
        print(f"  set sentinel        -> {read_back_before!r}")
        ok_paste, _ = candidate_wm_paste(hwnd, "PASTE_PROBE")
        time.sleep(0.15)  # let the OS release the clipboard before re-reading
        after = clipboard_read_text()
        print(f"  after WM_PASTE probe -> {after!r}")
        restored = after == sentinel
        if restored:
            verdict = "YES"
        else:
            verdict = (
                "NO - WM_PASTE is UNSAFE and must not join the ladder"
            )
        print(f"  clipboard restored  : {verdict}")
        return 0 if restored else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())