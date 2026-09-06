"""
WASM AC3 audio POC — orchestrated integration test (no browser in CI).

Setup:
  - synthesize a 40 s MKV: H.264/ultrafast video + AC3 (96 kbps, 48 kHz) sine
  - serve it through the fake Drive
  - boot the REAL POC FastAPI app (uvicorn thread) so Node talks to the actual
    /stream/{id} range endpoint (auth gate + Google-token refresh code path
    included, backed by a fake token file)
  - run tests/ac3_wasm_test.mjs (Node) which uses the SAME vendored mediabunny
    + ffmpeg-core.wasm the browser will use

Asserts: AC3 track discovery, packet ordering, partial (range) reading,
~2 s batch decoding to non-silent PCM, decode speed, and seek restart.
Also grep the POC /stream path for the AC3 audio actually flowing through it.

Run from repo root:  python tests/test_wasm_audio.py
"""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))

os.environ["DRIVE_API_BASE"] = "http://127.0.0.1:9012"
_token_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
os.environ["TOKEN_FILE"] = _token_file.name
_token_file.close()
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="stream-poc-wasm-cache-")

pathlib.Path(_token_file.name).write_text(json.dumps({
    "access_token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
    "expires_at": time.time() + 3600,
}))

import fake_drive  # noqa: E402
import media  # noqa: E402  (backend/media.py)
import app as poc  # noqa: E402  (backend/app.py)
import uvicorn  # noqa: E402

FAKE_PORT, POC_PORT = 9012, 9013
failures = 0


def check(label, cond, extra=""):
    global failures
    status = "PASS" if cond else "FAIL"
    if not cond:
        failures += 1
    print(f"[{status}] {label}" + (f"  -- {extra}" if extra and not cond else ""))


def serve(app_module_app, port):
    config = uvicorn.Config(app_module_app, host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server on :{port} did not start")


def generate_mkv(tmpdir: pathlib.Path) -> bytes:
    ff = media.ffmpeg_bin()
    out = tmpdir / "h264_ac3_40s.mkv"
    res = subprocess.run(
        [ff, "-hide_banner", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=duration=40:size=192x108:rate=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=40:sample_rate=48000",
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
         "-c:a", "ac3", "-b:a", "96k",
         "-y", str(out)],
        capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(f"mkv generation failed: {res.stderr[:600]}")
    return out.read_bytes()


def main() -> int:
    global failures

    if not shutil.which("node"):
        print("[SKIP] node.js not available — cannot run browserless wasm test")
        return 0
    if not media.ffmpeg_bin():
        print("[SKIP] no ffmpeg binary available")
        return 0
    vendor = ROOT / "frontend" / "vendor"
    if not (vendor / "mediabunny").exists() or not (vendor / "ffmpeg-core").exists():
        print("[SKIP] frontend/vendor missing — run: python backend/fetch_vendor.py")
        return 0

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="stream-poc-wasm-"))
    try:
        mkv = generate_mkv(tmp)
    except Exception as e:
        print(f"[SKIP] could not synthesize MKV: {e}")
        return 0
    fake_drive.DATA = mkv
    fake_drive.SIZE = len(mkv)
    print(f"       fixture: {len(mkv)} bytes (40 s, h264 + ac3 96 kbps)")

    serve(fake_drive.app, FAKE_PORT)
    serve(poc.app, POC_PORT)
    stream_url = f"http://127.0.0.1:{POC_PORT}/stream/vid123"

    # sanity: gate works, then real range read works, before Node runs
    import urllib.request
    req = urllib.request.Request(stream_url, headers={"Range": "bytes=0-63"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        head = resp.read()
    check("poc /stream 206 + byte-exact through the full chain",
          resp.status == 206 and head == mkv[:64], f"status={resp.status}")

    node_script = ROOT / "tests" / "ac3_wasm_test.mjs"
    res = subprocess.run(
        ["node", str(node_script), stream_url, str(vendor),
         str(ROOT / "frontend"), "2.0", "20"],
        capture_output=True, text=True, timeout=300)
    if res.returncode != 0 or "RESULT=" not in res.stdout:
        print(res.stdout[-2000:])
        print(res.stderr[-2000:])
        check("node wasm test executed", False, f"rc={res.returncode}")
        return 1
    result = json.loads(res.stdout.split("RESULT=")[1].strip().splitlines()[0])
    print(f"       node result: {json.dumps({k: v for k, v in result.items() if k != 'ok'})}")

    check("pure scheduling helpers", result.get("pureHelpersOk") is True,
          str(result.get("pureDetails")))
    check("AC3 track discovered", str(result.get("codec", "")).lower() in ("ac3", "ac-3"),
          str(result.get("audioCodecs")))
    check("packet timestamps monotonic", result.get("monotonic") is True)
    check(f"demuxed ~2s span", 1.5 < result.get("spanS", 0) < 3.5,
          f"span={result.get('spanS')}")
    check("wasm decode exit 0", result.get("execCode") == 0)
    check("decoded duration matches span",
          abs(result.get("decodedSec", 0) - (result["spanS"])) < 0.6,
          f"decoded={result.get('decodedSec')} span={result.get('spanS')}")
    check("decoded PCM is non-silent (sine wave)", result.get("rms", 0) > 0.05,
          f"rms={result.get('rms')}")
    check("decode is fast (<2s per ~2s chunk, worst case CI)",
          result.get("decMs", 99999) < 2000, f"{result.get('decMs')}ms")
    check("seek restart lands near target",
          abs(result.get("seekFirstTs", -999) - 20) < 2.0,
          f"seekFirstTs={result.get('seekFirstTs')}")
    check("partial read only (range streaming, not full download)",
          result.get("bytesTouched", 10**12) < len(mkv),
          f"touched={result.get('bytesTouched')} size={len(mkv)}")

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All WASM-audio checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
