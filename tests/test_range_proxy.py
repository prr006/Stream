"""
End-to-end test of the streaming proxy WITHOUT real Google credentials.

Runs the POC app (via FastAPI TestClient) pointed at a fake 'Drive' upstream
and verifies that Range requests pass through correctly:
  - full GET          -> 200 + exact bytes
  - bytes=a-b         -> 206 + Content-Range + exact slice
  - bytes=a-  (open)  -> 206 + exact tail slice
  - bytes=-n (suffix) -> 206 + last n bytes
  - /files            -> passthrough JSON
  - no token file     -> 401 from the auth gate

Run from the repo root:  python tests/test_range_proxy.py
"""
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))

# --- Configure before importing the app (env is read at import time) --------
os.environ["DRIVE_API_BASE"] = "http://127.0.0.1:9010"
_token_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
os.environ["TOKEN_FILE"] = _token_file.name
_token_file.close()

# Seed a "valid" token so the POC believes it's authorized (it never talks to
# real Google in this test, so the value is irrelevant).
pathlib.Path(_token_file.name).write_text(json.dumps({
    "access_token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
    "expires_at": time.time() + 3600,
}))

import uvicorn  # noqa: E402
import fake_drive  # noqa: E402
import app as poc  # noqa: E402  (backend/app.py — module name is 'app')
from fastapi.testclient import TestClient  # noqa: E402

FAKE_PORT = 9010


def start_fake_drive():
    config = uvicorn.Config(fake_drive.app, host="127.0.0.1", port=FAKE_PORT,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", FAKE_PORT), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("fake drive upstream did not start")


def main() -> int:
    start_fake_drive()
    client = TestClient(poc.app)
    data, size = fake_drive.DATA, fake_drive.SIZE
    failures = 0

    def check(label, cond, extra=""):
        nonlocal failures
        status = "PASS" if cond else "FAIL"
        if not cond:
            failures += 1
        print(f"[{status}] {label}" + (f"  -- {extra}" if extra and not cond else ""))

    # 1. Full body, no Range -> 200 and byte-identical
    r = client.get("/stream/vid123")
    check("full GET returns 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    check("full body is byte-identical", r.content == data)
    check("accept-ranges header present", r.headers.get("accept-ranges") == "bytes")

    # 2. Bounded range -> 206 + Content-Range + exact slice
    r = client.get("/stream/vid123", headers={"Range": "bytes=1000000-1000099"})
    check("range GET returns 206", r.status_code == 206, f"got {r.status_code}")
    check("content-range echoed", r.headers.get("content-range") == f"bytes 1000000-1000099/{size}",
          r.headers.get("content-range", "<missing>"))
    check("range body matches slice", r.content == data[1_000_000:1_000_100])

    # 3. Open-ended range -> 206 + exact tail
    r = client.get("/stream/vid123", headers={"Range": "bytes=4194304-"})
    check("open range returns 206", r.status_code == 206, f"got {r.status_code}")
    check("open range body matches", r.content == data[4_194_304:])

    # 4. Suffix range -> 206 + last N bytes
    r = client.get("/stream/vid123", headers={"Range": "bytes=-500"})
    check("suffix range returns 206", r.status_code == 206, f"got {r.status_code}")
    check("suffix range body matches", r.content == data[-500:])

    # 5. /files passthrough
    r = client.get("/files")
    check("/files returns list", r.status_code == 200 and r.json()["files"][0]["id"] == "vid123",
          f"{r.status_code} {r.text[:200]}")

    # 6. Auth gate: no token file -> 401
    original = poc.TOKEN_FILE
    poc.TOKEN_FILE = pathlib.Path("/tmp/definitely-not-a-token-file.json")
    r = client.get("/stream/vid123")
    check("missing token -> 401", r.status_code == 401, f"got {r.status_code}")
    poc.TOKEN_FILE = original

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed: range-passthrough proxy works end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
