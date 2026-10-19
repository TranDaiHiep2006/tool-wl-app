"""Manual end-to-end test.

Boots Edge with the tool-wl-app extension loaded, navigates to a real
page, and exercises the bridge:

  1. starts a WebSocket bridge on 127.0.0.1:57532 that pretends to be the
     MCP server side
  2. launches Edge with --load-extension pointing at D:/tool-wl-app/extension
  3. navigates to a real page via the DevTools Protocol
  4. sends a get_page_text request over the WebSocket bridge
  5. the extension's content script (running in the real page) extracts DOM
  6. the response travels back over the bridge and we verify it

This is the same path Cursor Agent will hit when it calls the tool.
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
PROFILE = os.path.join(ROOT, "logs", "edge_e2e_profile_main")
EXT_DIR = "D:/tool-wl-app/extension"
DEBUG_PORT = 9360
WS_PORT = 57532

# Two URLs we exercise per the prompt's "2-3 real pages" requirement.
# Pick a news article and a GitHub page.
URLS = [
    "https://example.com/",
    "https://en.wikipedia.org/wiki/Web_browser",
    "https://github.com/anthropics/anthropic-cookbook",
]

import websockets  # type: ignore


async def drive_page(cdp_url, url, wait_s=4.0):
    """Navigate the page and return document text via JS evaluation."""
    async with websockets.connect(cdp_url, max_size=8 * 1024 * 1024) as cdp:
        await send(cdp, "Page.enable")
        await send(cdp, "Runtime.enable")
        await send(cdp, "Page.navigate", {"url": url})
        await asyncio.sleep(wait_s)

        # Get full body innerText.
        text_resp = await send(cdp, "Runtime.evaluate", {
            "expression": """
                (function() {
                    var root = document.body || document.documentElement;
                    if (!root) return '';
                    // Strip boilerplate-ish nodes.
                    var skip = new Set(['SCRIPT','STYLE','NOSCRIPT','IFRAME','SVG','CANVAS']);
                    function walk(el, parts) {
                        if (!el || skip.has(el.tagName)) return;
                        for (var c = el.childNodes, i = 0; i < c.length; i++) {
                            var n = c[i];
                            if (n.nodeType === 3) {
                                var t = (n.nodeValue || '').replace(/\\s+/g, ' ').trim();
                                if (t) parts.push(t);
                            } else if (n.nodeType === 1) {
                                if (n.tagName === 'BR') parts.push('\\n');
                                else walk(n, parts);
                            }
                        }
                        if (/^(P|DIV|SECTION|ARTICLE|MAIN|HEADER|FOOTER|NAV|ASIDE|H[1-6]|LI|BLOCKQUOTE)$/.test(el.tagName)) parts.push('\\n');
                    }
                    var parts = [];
                    walk(root, parts);
                    var s = parts.join(' ').replace(/\\s+/g, ' ').trim();
                    return s.slice(0, 8000);
                })()
            """,
            "returnByValue": True,
        })
        text = text_resp.get("result", {}).get("result", {}).get("value", "")
        return text


async def send(ws, method, params=None):
    msg_id = int(time.time() * 1000) % (2**31)
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = await ws.recv()
        m = json.loads(raw)
        if m.get("id") == msg_id:
            return m


async def main():
    # Start WS bridge — this is the same code that the MCP server runs.
    from server import browser_dom
    bridge = browser_dom.DOMBridge(host="127.0.0.1", port=WS_PORT)
    browser_dom._bridge = bridge
    started = await bridge.start()
    if not started:
        print("FAIL: bridge failed to start")
        sys.exit(2)
    print(f"[bridge] listening on ws://127.0.0.1:{WS_PORT}/")

    # Launch Edge with our extension.
    if os.path.exists(PROFILE):
        import shutil
        shutil.rmtree(PROFILE, ignore_errors=True)
    args = [
        EDGE,
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={DEBUG_PORT}",
        "--enable-extensions",
        f"--load-extension={EXT_DIR}",
        f"--disable-extensions-except={EXT_DIR}",
    ]
    proc = subprocess.Popen(args, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    print("[edge] launched")
    await asyncio.sleep(8)

    try:
        # Find a usable page target.
        req = urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5)
        targets = json.loads(req.read().decode())
        page_target = None
        for t in targets:
            u = t.get("url", "")
            if t.get("type") == "page" and u.startswith("http") and "msn.com" not in u:
                page_target = t
                break
        if page_target is None:
            # Create a new tab via PUT.
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{DEBUG_PORT}/json/new?about:blank",
                method="PUT",
            )
            new = urllib.request.urlopen(req2, timeout=5)
            page_target = json.loads(new.read().decode())
        cdp_url = page_target["webSocketDebuggerUrl"]
        print(f"[cdp] using page target: {page_target.get('url')[:80]}")

        # Exercise each URL via get_page_text.
        summary = []
        for url in URLS:
            print(f"\n=== Testing {url} ===")
            # 1. Drive the page via CDP (simulates user opening the URL).
            js_text = await drive_page(cdp_url, url)
            print(f"[cdp] raw innerText: {len(js_text)} chars")

            # 2. Send get_page_text through the bridge — the extension's
            #    content script will run inside the same Edge process and
            #    extract the DOM. Verify the response.
            try:
                reply = await bridge.request("get_page_text", {"max_chars": 8000},
                                              timeout_s=8.0)
                if reply.get("ok"):
                    text = reply.get("data", {}).get("text", "")
                    print(f"[bridge] get_page_text OK: {len(text)} chars")
                    print(f"[bridge] preview: {text[:200]!r}")
                    summary.append({"url": url, "text_chars": len(text),
                                    "ok": True})
                else:
                    print(f"[bridge] get_page_text error: {reply.get('error')} - {reply.get('message', '')[:120]}")
                    summary.append({"url": url, "ok": False, "error": reply.get("error")})
            except Exception as exc:
                print(f"[bridge] get_page_text EXC: {exc!r}")
                summary.append({"url": url, "ok": False, "error": str(exc)})

        print()
        print("=" * 60)
        print("SUMMARY")
        for s in summary:
            print(f"  {s['url']:60s} ok={s.get('ok')} chars={s.get('text_chars','-')}")
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
    # Force UTF-8 output (Windows console often defaults to cp1252).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    asyncio.run(main())