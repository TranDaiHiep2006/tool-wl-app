"""Measure get_page_text and get_page_structure output sizes on real pages.

Outputs JSON with per-URL byte sizes so the REPORT can quote real numbers.
"""

import asyncio
import json
import os
import subprocess
import sys
import urllib.request

ROOT = r"D:\tool-wl-app"
sys.path.insert(0, ROOT)
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.join(ROOT, "logs", "edge_measure_profile")
EXT_DIR = "D:/tool-wl-app/extension"
DEBUG_PORT = 9362
WS_PORT = 57532

URLS = [
    ("example", "https://example.com/"),
    ("wikipedia_web_browser", "https://en.wikipedia.org/wiki/Web_browser"),
    ("github_anthropic_cookbook", "https://github.com/anthropics/anthropic-cookbook"),
]

import websockets  # type: ignore


async def send(ws, method, params=None):
    msg_id = int(time.time() * 1000) % (2**31)
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = await ws.recv()
        m = json.loads(raw)
        if m.get("id") == msg_id:
            return m


async def drive(cdp_url, url):
    async with websockets.connect(cdp_url, max_size=8 * 1024 * 1024) as cdp:
        await send(cdp, "Page.enable")
        await send(cdp, "Page.navigate", {"url": url})
        await asyncio.sleep(4)


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

    results = []
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

        for label, url in URLS:
            print(f"\n--- {label}: {url}")
            await drive(cdp_url, url)

            # get_page_text
            try:
                r = await bridge.request("get_page_text", {"max_chars": 8000}, timeout_s=10.0)
                text = (r.get("data") or {}).get("text", "")
                size_bytes = len(text.encode("utf-8"))
                size_kb = size_bytes / 1024.0
                print(f"  get_page_text:    {len(text):>5d} chars   {size_kb:.2f} KB   "
                      f"truncated={r.get('data',{}).get('truncated',False)}")
            except Exception as e:
                print(f"  get_page_text ERR: {e!r}")
                text = ""
                size_bytes = 0
                size_kb = 0.0

            # get_page_structure
            try:
                r = await bridge.request("get_page_structure", {"max_items": 200}, timeout_s=10.0)
                items = (r.get("data") or {}).get("items", [])
                # Approximate size of the serialized items.
                size_bytes_struct = len(json.dumps(items, ensure_ascii=False).encode("utf-8"))
                size_kb_struct = size_bytes_struct / 1024.0
                print(f"  get_page_structure: {len(items):>3d} items   {size_kb_struct:.2f} KB")
            except Exception as e:
                print(f"  get_page_structure ERR: {e!r}")
                items = []
                size_bytes_struct = 0
                size_kb_struct = 0.0

            results.append({
                "label": label,
                "url": url,
                "text_chars": len(text),
                "text_bytes": size_bytes,
                "text_kb": round(size_kb, 2),
                "structure_items": len(items),
                "structure_bytes": size_bytes_struct,
                "structure_kb": round(size_kb_struct, 2),
            })
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        await bridge.stop()
        browser_dom._bridge = None

    print()
    print("=" * 70)
    print("MEASUREMENT RESULTS")
    print("=" * 70)
    for r in results:
        print(f"  {r['label']:30s} text={r['text_kb']:>7.2f}KB  struct={r['structure_kb']:>7.2f}KB")

    # Save JSON.
    out_path = os.path.join(ROOT, "logs", "browser_dom_measure.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    import time
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    asyncio.run(main())