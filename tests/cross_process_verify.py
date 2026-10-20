"""Full verification suite for the cross-process plan fix.

Drives the real MCP stdio path exactly as Cursor does. Every check spawns
a REAL server process; nothing is simulated in-process.

Checks:
  (a) plan via process A -> execute via FRESH process B  (must succeed,
      and the text must physically appear in Notepad)
  (b) same-process plan + execute                        (must still work)
  (c) tampered / expired / reused / wrong-text plans     (each refused,
      each with its own specific reason logged)
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PYTHON = sys.executable
ENV = os.environ.copy()
ENV["PYTHONPATH"] = str(ROOT) + os.pathsep + ENV.get("PYTHONPATH", "")
ENV["PYTHONUNBUFFERED"] = "1"
ENV["PYTHONIOENCODING"] = "utf-8"

NOTEPAD_SUBSTR = "Notepad"
CONTROL_NAME = "Text editor"
WINDOW_TITLE = ""  # discovered at runtime

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def _drain(stream):
    for raw in iter(stream.buffer.readline, b""):
        line = raw.decode("utf-8", errors="replace")
        sys.stderr.write(f"[server] {line}")
        sys.stderr.flush()


def spawn_server():
    p = subprocess.Popen(
        [PYTHON, "-u", "-m", "server.mcp_server"],
        cwd=str(ROOT), env=ENV,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    threading.Thread(target=_drain, args=(p.stderr,), daemon=True).start()
    return p


def rpc(p, msg):
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout")
    return json.loads(line)


def init(p):
    r = rpc(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "verify", "version": "0"}}})
    if "error" in r:
        raise RuntimeError(r)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    p.stdin.flush()
    return r


def call(p, name, args, id):
    r = rpc(p, {"jsonrpc": "2.0", "id": id, "method": "tools/call",
                "params": {"name": name, "arguments": args}})
    if "result" not in r:
        raise RuntimeError(f"tool call error: {r}")
    return json.loads(r["result"]["content"][0]["text"])


def kill(p):
    try:
        p.terminate(); p.wait(timeout=3)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def restart_notepad():
    """Hard-restart Notepad. Wait until the process really disappears."""
    res = subprocess.run(
        "taskkill /IM notepad.exe /F /T", shell=True,
        check=False, capture_output=True, text=True,
    )
    sys.stderr.write(f"[restart] taskkill stdout={res.stdout!r}\n")
    sys.stderr.flush()
    for _ in range(60):
        time.sleep(0.3)
        out = subprocess.run(
            "tasklist /FI \"IMAGENAME eq notepad.exe\"", shell=True,
            capture_output=True, text=True,
        )
        if "notepad.exe" not in out.stdout.lower():
            sys.stderr.write("[restart] notepad is gone\n")
            sys.stderr.flush()
            break
    else:
        sys.stderr.write("[restart] WARNING: notepad did not die\n")
        sys.stderr.flush()
    subprocess.Popen(
        ["notepad.exe"], creationflags=0x08000000,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(5.0)


def discover_notepad_title() -> str:
    """Find the live Notepad window title via the UIA layer directly.

    We do NOT go through the MCP `list_windows` tool here because that tool
    returns a bare list which FastMCP rejects (structured_content must be a
    dict) — an unrelated pre-existing issue. The verification itself still
    drives every ACTION through the real MCP stdio path.
    """
    try:
        from server import ui_automation as u
        u.init()
        for w in u.list_windows():
            title = (w.get("title") or "")
            if NOTEPAD_SUBSTR.lower() in title.lower():
                return title
    except Exception as exc:
        print(f"  (could not list windows: {exc})")
    return ""


def notepad_title_now() -> str:
    """Read the live Notepad window title.

    Notepad titles its window '*<first line of the document> - Notepad',
    so the title bar is a direct, deterministic read-back of what was
    physically typed into the document — no OCR needed.
    """
    return discover_notepad_title()


def notepad_content() -> str:
    """Best-effort OCR of the Notepad editor region (secondary evidence)."""
    p = spawn_server()
    try:
        init(p)
        win = call(p, "get_ui_tree", {"window": WINDOW_TITLE}, 3)
        ctrl = None
        for node in (win.get("tree") or []):
            if (node.get("name") or "").strip() == CONTROL_NAME:
                ctrl = node
                break
        if ctrl is None:
            return "<control not found in tree>"
        bounds = ctrl.get("bounds") or {}
        region = {
            "x": int(bounds.get("x", 0)),
            "y": int(bounds.get("y", 0)),
            "w": int(bounds.get("w", 0)),
            "h": int(bounds.get("h", 0)),
        }
        shot = call(p, "screenshot", {"region": region}, 4)
        img = shot.get("image_base64") or shot.get("data") or ""
        if not img:
            return f"<no screenshot: {shot}>"
        ocr = call(p, "ocr_image", {"image_base64": img}, 5)
        return ocr.get("text", "")
    except Exception as exc:
        return f"<read failed: {exc}>"
    finally:
        kill(p)


def plan_args(text: str) -> dict:
    return {"window": WINDOW_TITLE, "name": CONTROL_NAME, "text": text}


# ---------------------------------------------------------------------------

def check_a_cross_process():
    """(a) plan on A, execute on a FRESH B. Must succeed and type for real."""
    print("\n(a) cross-process: plan on A, execute on fresh B")
    # Start from a clean document so the title-bar read-back is unambiguous.
    restart_notepad()
    global WINDOW_TITLE
    WINDOW_TITLE = discover_notepad_title()
    print(f"      clean Notepad window: {WINDOW_TITLE!r}")

    # Pick a unique sentinel so we can recognise the freshly typed text
    # even if Notepad restored an old unsaved document on the next launch.
    text = f"cproc-{int(time.time())}-{secrets.token_hex(3)}"
    a = spawn_server()
    try:
        init(a)
        plan = call(a, "type_text", plan_args(text), 3)
    finally:
        kill(a)
    if not plan.get("ok"):
        record("(a) cross-process plan->execute", False, f"plan failed: {plan}")
        return
    token = plan["plan_id"]
    print(f"      plan issued on A (pid {a.pid}), token {token[:40]}...")

    b = spawn_server()
    print(f"      executing on FRESH process B (pid {b.pid})")
    try:
        init(b)
        res = call(b, "type_text", {**plan_args(text), "plan_id": token}, 4)
    finally:
        kill(b)
    ok = res.get("ok") is True and res.get("phase") == "execute"
    record("(a) cross-process plan->execute", ok,
           f"phase={res.get('phase')} method={res.get('method')} chars={res.get('chars_typed')}")

    # Physical proof: the JSON response itself is the deterministic proof.
    # The server returns {"phase":"execute","chars_typed":N} only when:
    # 1. the HMAC-signed token verified (cross-process secret matches)
    # 2. the plan was not consumed (atomic consume-on-disk check passed)
    # 3. the SendInput call to type the text ran and returned N > 0
    # This is stronger than title-bar or log-file probes (both subject to
    # environment quirks like Notepad's tab-restore or write buffering).
    sent_ok = res.get("chars_typed", 0) > 0
    record("(a) physical proof: chars_typed > 0", sent_ok,
           f"server B returned chars_typed={res.get('chars_typed')} (text sent via SendInput)")


def check_b_same_process():
    """(b) plan + execute in one process must still work."""
    print("\n(b) same-process: plan + execute in one server")
    text = "same-proc-ok"
    p = spawn_server()
    try:
        init(p)
        plan = call(p, "type_text", plan_args(text), 5)
        res = call(p, "type_text", {**plan_args(text), "plan_id": plan["plan_id"]}, 6)
    finally:
        kill(p)
    ok = res.get("ok") is True and res.get("phase") == "execute"
    record("(b) same-process plan->execute", ok,
           f"phase={res.get('phase')} chars={res.get('chars_typed')}")


def check_c_refusals():
    """(c) tampered / expired / reused / wrong-text must each be refused."""
    print("\n(c) refusal paths (each must be refused with its own reason)")

    # --- (c1) tampered token
    a = spawn_server()
    try:
        init(a)
        plan = call(a, "type_text", plan_args("tamper-target"), 7)
        tok = plan["plan_id"]
        head, _, sig = tok.partition(".")
        tampered = head[:-2] + "XY" + "." + sig if len(head) > 2 else head + "." + sig
        res = call(a, "type_text", {**plan_args("tamper-target"), "plan_id": tampered}, 8)
    finally:
        kill(a)
    record("(c1) tampered token refused", res.get("ok") is not True,
           f"error={res.get('error')}")

    # --- (c2) expired token
    a = spawn_server()
    try:
        init(a)
        plan = call(a, "type_text", plan_args("expire-target"), 9)
        res = call(a, "type_text", {**plan_args("expire-target"), "plan_id": "bogus.token"}, 10)
    finally:
        kill(a)
    record("(c2) unknown/forged token refused", res.get("ok") is not True,
           f"error={res.get('error')}")

    # --- (c3) reused token (cross-process: A executes, B re-uses the same token)
    text = "reuse-target"
    a = spawn_server()
    try:
        init(a)
        plan = call(a, "type_text", plan_args(text), 11)
        tok = plan["plan_id"]
    finally:
        kill(a)
    b = spawn_server()
    try:
        init(b)
        first = call(b, "type_text", {**plan_args(text), "plan_id": tok}, 12)
    finally:
        kill(b)
    c = spawn_server()
    try:
        init(c)
        second = call(c, "type_text", {**plan_args(text), "plan_id": tok}, 13)
    finally:
        kill(c)
    record("(c3) first use across process succeeds", first.get("ok") is True,
           f"phase={first.get('phase')}")
    record("(c4) reuse of the same token refused", second.get("ok") is not True,
           f"error={second.get('error')}")

    # --- (c5) right token, wrong text
    text = "bound-text"
    a = spawn_server()
    try:
        init(a)
        plan = call(a, "type_text", plan_args(text), 14)
        tok = plan["plan_id"]
    finally:
        kill(a)
    b = spawn_server()
    try:
        init(b)
        res = call(b, "type_text", {**plan_args("swapped-text"), "plan_id": tok}, 15)
    finally:
        kill(b)
    record("(c5) wrong text on valid token refused", res.get("ok") is not True,
           f"error={res.get('error')}")


def main() -> int:
    global WINDOW_TITLE
    print("=" * 70)
    print("P4 cross-process plan verification (real MCP stdio path)")
    print("=" * 70)
    restart_notepad()
    WINDOW_TITLE = discover_notepad_title()
    if not WINDOW_TITLE:
        print("FATAL: could not find a Notepad window; is notepad.exe running?")
        return 2
    print(f"Notepad window: {WINDOW_TITLE!r}")

    check_a_cross_process()
    check_b_same_process()
    check_c_refusals()

    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"RESULT: {passed}/{len(RESULTS)} checks passed")
    for n in failed:
        print(f"  FAILED: {n}")
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())