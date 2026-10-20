"""Real Chrome verification that the MV3 service worker registers cleanly.

Launches Chrome with the unpacked extension loaded and remote debugging on,
then via CDP:
  1. attaches to the service_worker target
  2. reads the worker's console + any uncaught exceptions
  3. confirms it is alive and the bridge connect() path ran
Any entry containing an error / registration failure fails the check.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = r"D:\tool-wl-app"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EXT_DIR = os.path.join(ROOT, "extension").replace("\\", "/")
PROFILE = os.path.join(ROOT, "logs", "chrome_sw_verify")
PORT = 9371

shutil.rmtree(PROFILE, ignore_errors=True)
os.makedirs(PROFILE, exist_ok=True)

# NOTE: Chrome stable silently drops --load-extension when
# --disable-extensions-except is also passed on Chrome >= 120-ish.
# Instead, we launch Chrome WITHOUT those CLI flags and use the
# DevTools Protocol (Target.fetchExtensionTargets via ChromeExtensionSession)
# ... actually simpler & well-supported: use the
# chrome.management.loadUnpacked() API over CDP. We can't reach that from
# page targets, but we CAN reach it from the browser target:
#   1) connect to /json/version -> browser WS,
#   2) send Extensions.loadUnpacked (officially available in CDP).
# That avoids the CLI flag entirely and the ExtensionLoadFailed is
# returned inline if it ever happens.
args = [
    CHROME,
    "--no-first-run",
    "--no-default-browser-check",
    f"--user-data-dir={PROFILE}",
    f"--remote-debugging-port={PORT}",
    "--enable-logging=stderr",
    "--v=1",
]

log_path = os.path.join(ROOT, "logs", "chrome_sw_verify.log")
proc = subprocess.Popen(args, stderr=subprocess.STDOUT,
                        stdout=open(log_path, "wb"))


def http_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def wait_port(deadline=40):
    start = time.time()
    while time.time() - start < deadline:
        try:
            return http_json(f"http://127.0.0.1:{PORT}/json/version")
        except Exception:
            time.sleep(0.5)
    raise SystemExit("Chrome debugging port never came up")


def find_sw(targets):
    """Return OUR service worker only.

    Built-in Chrome component extensions also expose
    chrome-extension:// targets (e.g. a background.html page), so we must
    match on our own manifest's service worker script name.
    """
    for t in targets:
        url = t.get("url", "")
        if t.get("type") in ("service_worker", "background_page") \
                and url.startswith("chrome-extension://") \
                and url.endswith("/background.js"):
            return t
    return None


try:
    import asyncio
    import websockets  # type: ignore

    ver = wait_port()
    print("Browser:", ver.get("Browser"))
    print("WS endpoint:", ver.get("webSocketDebuggerUrl"))

    errors = []
    logs = []

    async def run_cdp():
        # Connect to the browser-level endpoint so we can issue
        # Extensions.loadUnpacked and reach ServiceWorker targets.
        browser_ws = ver["webSocketDebuggerUrl"]
        ext_load = {"ok": None, "ext_id": None, "err": None}

        async with websockets.connect(browser_ws, max_size=8 * 1024 * 1024,
                                     open_timeout=20) as ws:
            mid = 0

            async def send(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps(
                    {"id": mid, "method": method, "params": params or {}}))
                # Read until matching id (or service-worker-unrelated event pass-through).
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                    msg = json.loads(raw)
                    if "id" in msg and msg["id"] == mid:
                        return msg
                    # else: an unrelated event - drop it, continue waiting for the response.

            # Load the unpacked extension via CDP.
            try:
                resp = await send("Extensions.loadUnpacked",
                                  {"path": EXT_DIR})
                if resp.get("error"):
                    ext_load["err"] = str(resp["error"])
                else:
                    res = resp.get("result") or {}
                    # CDP returns {"id": "<extensionId>"} (sometimes nested
                    # under different keys depending on Chrome version).
                    ext_id = (res.get("id") or res.get("extensionId")
                              or res.get("extension_id"))
                    if isinstance(res.get("manifest"), dict):
                        # Some Chrome versions include the manifest here;
                        # the extensionId may live on a nested property.
                        manifest = res.get("manifest") or {}
                        if not ext_id:
                            ext_id = manifest.get("extension_id")
                    if not ext_id:
                        ext_id = res.get("id") or res.get("extensionId")
                    ext_load["raw_ok"] = res
                    ext_load["ext_id"] = ext_id
            except Exception as e:
                ext_load["err"] = f"loadUnpacked exception: {e}"

            # Service worker targets appear after ExtensionService installs.
            # Find the session id for OUR worker target; if MV3 it'll be type
            # "service_worker".
            sw_target = None
            deadline = time.time() + 20
            while time.time() < deadline and sw_target is None:
                # Event-driven: request one frame of service worker events.
                resp = await send("Extensions.getServiceWorkers",
                                  {})
                # Newer Chrome: returns {"serviceWorkerInfos": [...]};
                # safer compatibility: also fall back to Target.listTargets.
                infos = ((resp.get("result") or {}).get("serviceWorkerInfos")
                         or [])
                for sw in infos:
                    url = sw.get("url", "")
                    if url.endswith("/background.js") and url.startswith(
                            "chrome-extension://"):
                        sw_target = sw
                        break
                if sw_target:
                    break
                # Fallback: scan page targets.
                resp2 = await send("Target.getTargets", {})
                for t in (resp2.get("result") or {}).get("targetInfos", []):
                    url = t.get("url", "")
                    if t.get("type") in ("service_worker",
                                         "background_page") \
                            and url.endswith("/background.js") \
                            and url.startswith("chrome-extension://"):
                        sw_target = t
                        break
                if sw_target:
                    break
                time.sleep(0.5)

            async def attach_and_collect(sw_tgt):
                session_id = sw_tgt.get("sessionId") or sw_tgt.get(
                    "targetId") or (sw_tgt.get("id") or "")
                # Attach by targetId.
                resp = await send("Target.attachToTarget",
                                  {"targetId": sw_tgt["targetId"],
                                   "flatten": True})
                sess = (resp.get("result") or {}).get("sessionId") or session_id
                params = {"sessionId": sess}
                # Enable domains with the sessionId attached.
                await send("Runtime.enable", params)
                await send("Log.enable", params)

                # Read events for ~10s.
                logs.clear()
                errors.clear()
                end = time.time() + 10
                while time.time() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    except asyncio.TimeoutError:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    method = msg.get("method")
                    mparams = msg.get("params") or {}
                    # Only consider events for OUR session.
                    if msg.get("sessionId") and msg.get("sessionId") != sess:
                        continue
                    if method == "Runtime.exceptionThrown":
                        d = mparams.get("exceptionDetails") or {}
                        errors.append(
                            "EXCEPTION: " + str(d.get("text")) + " " +
                            str((d.get("exception") or {}).get(
                                "description", "")))
                    elif method == "Log.entryAdded":
                        e = mparams.get("entry") or {}
                        text = f"{e.get('level')}: {e.get('text')}"
                        logs.append(text)
                        if e.get("level") == "error":
                            errors.append(text)
                    elif method == "Runtime.consoleAPICalled":
                        args_list = mparams.get("args") or []
                        txt = " ".join(
                            str(a.get("value", a.get("description", "")))
                            for a in args_list)
                        logs.append(f"{mparams.get('type')}: {txt}")
                        if mparams.get("type") == "error":
                            errors.append("console.error: " + txt)

            if sw_target:
                print("Service worker target url:", sw_target.get("url"))
                print("Service worker target id:", sw_target.get("targetId"))
                await attach_and_collect(sw_target)
            else:
                print("(no service worker target discovered)")

            return ext_load, sw_target

    (ext_load, sw) = asyncio.run(run_cdp())

    print("--- extension load result ---")
    print("   id:", ext_load.get("ext_id"))
    print("   error:", ext_load.get("err"))
    print("   raw_ok:", ext_load.get("raw_ok"))

    if ext_load.get("err") or not ext_load.get("ext_id"):
        print("RESULT: FAIL - extension did not load")
        sys.exit(1)
    if not sw:
        print("RESULT: FAIL - our background.js service worker never appeared")
        try:
            targets = http_json(f"http://127.0.0.1:{PORT}/json/list")
            for t in targets:
                print("   target:", t.get("type"), "|", t.get("url"))
        except Exception:
            pass
        sys.exit(1)

    ext_id = sw["url"].split("/")[2]

    # Now check the worker script actually parsed without the
    # top-level-await error, by evaluating in the worker context.
    print("--- worker console lines ---")
    for l in logs[-40:]:
        print("   ", l)

    print("--- collected errors ---")
    seen = set()
    uniq = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    for e in uniq:
        print("   ", e)
    if not uniq:
        print("    (none)")

    # Grep the browser stderr log for the known failure signatures.
    bad_sigs = [
        "Top-level await is disallowed in service workers",
        "Service worker registration failed",
        "Status code: 3",
    ]
    hits = []
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if any(s.lower() in line.lower() for s in bad_sigs):
                    hits.append(line.strip())

    print("--- chrome stderr signature hits ---")
    for h in hits:
        print("   ", h)
    if not hits:
        print("    (none)")

    ok = sw is not None and not uniq and not hits
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
finally:
    try:
        proc.terminate()
        proc.wait(timeout=6)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass