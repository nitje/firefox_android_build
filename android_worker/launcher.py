"""Android-Worker supervisor: use verified manager-staged Python code on shared volume.

The Linux toolchain image is stable; future manager/worker.py updates don't need
rebuilding the toolchain container. Worker exits only at a safe idle boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(os.getenv("SHARED_DIR", "/data/shared")) / "android" / "updates"
FALLBACK = pathlib.Path("/app/worker.py")
STOP = False
CHILD = None

def shutdown(signum, frame):
    global STOP
    STOP = True
    if CHILD and CHILD.poll() is None:
        CHILD.send_signal(signal.SIGTERM)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

def selected_worker():
    try:
        info = json.loads((ROOT / "worker_manifest.json").read_text(encoding="utf-8"))
        code = ROOT / "worker.py"
        data = code.read_bytes()
        version = re.search(rb'^VERSION = "([0-9.]+)"$', data, re.M)
        if (version and version.group(1).decode() == info.get("version")
                and hashlib.sha256(data).hexdigest() == info.get("sha256")):
            return code
    except (OSError, ValueError, TypeError):
        pass
    return FALLBACK

def main():
    global CHILD
    while not STOP:
        entry = selected_worker()
        print(f"ANDROID SUPERVISOR: starte {entry}", flush=True)
        CHILD = subprocess.Popen([sys.executable, "-u", str(entry)])
        rc = CHILD.wait()
        CHILD = None
        if STOP:
            break
        print(f"ANDROID SUPERVISOR: Worker beendet (Code {rc}); prüfe bereitgestellten Projektstand", flush=True)
        time.sleep(2 if rc == 0 else 5)

if __name__ == "__main__":
    main()
