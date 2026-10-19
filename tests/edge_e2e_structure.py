"""Manual end-to-end test for get_page_structure.

Same as edge_e2e.py but exercises get_page_structure instead of get_page_text.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = r"D:\tool-wl-app"
sys.path.insert(0, ROOT)
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.join(ROOT, "logs", "edge_e2e_profile_struct")
EXT_DIR = "D:/tool-wl-app/extension"
DEBUG_PORT = 9361
WS_PORT = 57532  # must match the extension's BRIDGE_URL_DEFAULT

URLS = [
    "https://example.com/",
    "https://en.wikipedia.org/wiki/Web_browser",
    "https://github.com/anthropics/anthropic-cookbook",
]

import websockets  # type: ignore


async def drive_page(cdp_url, url, wait_s=4.0):
    async with websockets.connect(cdp_url, max_size=8 * 1024 * 1024) as cdp:
        await send(cdp, "Page.enable")
        await send(cdp, "Runtime.enable")
        await send(cdp, "Page.navigate", {"url": url})
        await asyncio.sleep(wait_s)


async def send(ws, method, params=None):
    msg_id = int(time.time() * 1000) % (2**31)
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = await ws.recv()
        m = json.loads(raw)
        if m.get("id") == msg_id:
            return m


async def main():
    from server import browser_dom
    bridge = browser_dom.DOMBridge(host="127.0.0.1", port=WS_PORT)
    browser_dom._bridge = bridge
    started = await bridge.start()
    if not started:
        print("FAIL: bridge failed to start")
        sys.exit(2)
    print(f"[bridge] listening on ws://127.0.0.1:{WS_PORT}/")

    if os.path.exists(PROFILE):
        import shutil
        shutil.rmtree(PROFILE, ignore_errors=True)
    args = [
        EDGE, "--headless=new", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={PROFILE}", f"--remote-debugging-port={DEBUG_PORT}",
        "--enable-extensions", f"--load-extension={EXT_DIR}",
        f"--disable-extensions-except={EXT_DIR}",
    ]
    proc = subprocess.Popen(args, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    print("[edge] launched")
    await asyncio.sleep(8)

    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5)
        targets = json.loads(req.read().decode())
        page_target = None
        for t in targets:
            u = t.get("url", "")
            if t.get("type") == "page" and u.startswith("http") and "msn.com" not in u:
                page_target = t
                break
        if page_target is None:
            new = urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{DEBUG_PORT}/json/new?about:blank",
                    method="PUT"),
                timeout=5)
            page_target = json.loads(new.read().decode())
        cdp_url = page_target["webSocketDebuggerUrl"]

        summary = []
        for url in URLS:
            print(f"\n=== Testing {url} ===")
            await drive_page(cdp_url, url)
            try:
                reply = await bridge.request("get_page_structure", {"max_items": 200},
                                              timeout_s=8.0)
                if reply.get("ok"):
                    items = reply.get("data", {}).get("items", [])
                    counts = {}
                    for it in items:
                        k = it.get("kind", "?")
                        counts[k] = counts.get(k, 0) + 1
                    print(f"[bridge] get_page_structure OK: {len(items)} items")
                    print(f"[bridge] by kind: {counts}")
                    # Print first heading and first link.
                    headings = [it for it in items if it.get("kind") == "heading"]
                    links = [it for it in items if it.get("kind") == "link"]
                    if headings:
                        h = headings[0]
                        print(f"  first heading: H{h.get('level')}: {(h.get('text') or '')[:60]}")
                    if links:
                        l = links[0]
                        print(f"  first link: {(l.get('text') or '')[:60]} -> {l.get('href','')[:60]}")
                    summary.append({"url": url, "items": len(items), "counts": counts,
                                    "ok": True})
                else:
                    print(f"[bridge] error: {reply.get('error')} - {reply.get('message','')[:120]}")
                    summary.append({"url": url, "ok": False, "error": reply.get("error")})
            except Exception as exc:
                print(f"[bridge] EXC: {exc!r}")
                summary.append({"url": url, "ok": False, "error": str(exc)})

        print()
        print("=" * 60)
        print("SUMMARY")
        for s in summary:
            print(f"  {s['url']:60s} ok={s.get('ok')} items={s.get('items','-')}")
        print("=" * 60)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        await bridge.stop()
        browser_dom._bridge = None


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    asyncio.run(main())