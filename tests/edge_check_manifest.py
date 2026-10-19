"""Validate the extension manifest using Chrome's manifest verifier.

Tries to load the extension and reports any errors.
"""

import subprocess
import os
import time
import urllib.request
import json

ROOT = r"D:\tool-wl-app"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.join(ROOT, "logs", "edge_manifest_check")
EXT_DIR = "D:/tool-wl-app/extension"
DEBUG_PORT = 9348

# Clean profile
import shutil
if os.path.exists(PROFILE):
    shutil.rmtree(PROFILE, ignore_errors=True)

# Log full output to a file for later inspection.
LOG = os.path.join(ROOT, "logs", "edge_manifest_check.log")

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
    "--enable-logging=stderr",
    "--v=1",
    "--log-level=0",
]

with open(LOG, "wb") as f:
    proc = subprocess.Popen(args, stderr=subprocess.STDOUT, stdout=f)
    time.sleep(8)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

# Print interesting lines.
print("=== LOG ===")
with open(LOG, encoding="utf-8", errors="replace") as f:
    for line in f:
        L = line.lower()
        if any(k in L for k in ["manifest", "tool-wl", "tool_wl", "unpack", "load_error", "error"]):
            if "task_manager" not in L and "render_process" not in L:
                print(line.rstrip())