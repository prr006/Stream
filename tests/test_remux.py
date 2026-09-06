"""
Tests for the audio-only AAC remux milestone (HEVC video copied, AC3 -> AAC).

Unit-ish part (no ffmpeg work):
  - local ranged-file endpoint: full GET, bounded/open/suffix ranges, 416s.

Integration part (auto-skipped if no ffmpeg binary):
  - synthesizes an MKV with **HEVC video + AC3 audio + one embedded SRT track**
    (falls back to h264 video if libx265 is unavailable — the remux logic is
    codec-agnostic), serves it through the fake Drive, then:
      POST /remux/{id}          -> background job starts
      GET  /remux/{id}/status   -> poll until "ready"
      GET  /remux/{id}.mkv      -> 200; re-probe the served bytes with ffmpeg
                                   and assert: video codec UNCHANGED (copied),
                                   audio codec == aac, subtitle stream kept.
      Range GET on the remux    -> 206 byte-exact slice

Run from the repo root:  python tests/test_remux.py
"""
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))

# --- Configure env BEFORE importing the app (it reads env at import time) ---
os.environ["DRIVE_API_BASE"] = "http://127.0.0.1:9011"   # distinct port per suite
os.environ["EXTRACT_TIMEOUT_S"] = "600"
_token_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
os.environ["TOKEN_FILE"] = _token_file.name
_token_file.close()
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="stream-poc-remux-cache-")

pathlib.Path(_token_file.name).write_text(json.dumps({
    "access_token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
    "expires_at": time.time() + 3600,
}))

import fake_drive  # noqa: E402
import media  # noqa: E402
import app as poc  # noqa: E402  (backend/app.py)
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FAKE_PORT = 9011
failures = 0


def check(label, cond, extra=""):
    global failures
    status = "PASS" if cond else "FAIL"
    if not cond:
        failures += 1
    print(f"[{status}] {label}" + (f"  -- {extra}" if extra and not cond else ""))


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


# ---------------------------------------------------------------------------
# Local ranged-file serving (uses the real endpoint + a pre-seeded cache file)
# ---------------------------------------------------------------------------

def ranged_file_tests(client):
    pattern = bytes(i % 256 for i in range(100_000))
    _part, final = poc._remux_paths("rangefile")
    final.write_bytes(pattern)
    try:
        r = client.get("/remux/rangefile.mkv")
        check("local full GET 200 + body", r.status_code == 200 and r.content == pattern)
        check("content-type is matroska",
              "video/x-matroska" in r.headers.get("content-type", ""))
        check("accept-ranges advertised", r.headers.get("accept-ranges") == "bytes")

        r = client.get("/remux/rangefile.mkv", headers={"Range": "bytes=1000-1999"})
        check("local range 206", r.status_code == 206, f"{r.status_code}")
        check("local range content-range",
              r.headers.get("content-range") == "bytes 1000-1999/100000")
        check("local range byte-exact", r.content == pattern[1000:2000])

        r = client.get("/remux/rangefile.mkv", headers={"Range": "bytes=99000-"})
        check("open range tail byte-exact",
              r.status_code == 206 and r.content == pattern[99_000:])

        r = client.get("/remux/rangefile.mkv", headers={"Range": "bytes=-500"})
        check("suffix range byte-exact",
              r.status_code == 206 and r.content == pattern[-500:])

        r = client.get("/remux/rangefile.mkv", headers={"Range": "bytes=500000-"})
        check("out-of-range -> 416", r.status_code == 416, f"{r.status_code}")

        r = client.get("/remux/rangefile.mkv", headers={"Range": "not-a-range"})
        check("malformed range -> 416", r.status_code == 416, f"{r.status_code}")

        r = client.get("/remux/never-built.mkv")
        check("missing remux -> 404", r.status_code == 404, f"{r.status_code}")
    finally:
        final.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Remux integration: HEVC + AC3 + SRT -> MKV with HEVC (copied) + AAC
# ---------------------------------------------------------------------------

SRT = """1
00:00:01,000 --> 00:00:02,500
Audio-remux fixture cue
"""


def generate_source_mkv(tmpdir: pathlib.Path) -> tuple[bytes, str]:
    """Returns (mkv_bytes, input_video_codec)."""
    ff = media.ffmpeg_bin()
    srt = tmpdir / "eng.srt"
    srt.write_text(SRT)
    out = tmpdir / "hevc_ac3.mkv"

    # Prefer a real HEVC fixture (user's exact case); fall back to h264 if the
    # local ffmpeg build lacks libx265 — the remux logic is codec-agnostic.
    video_args = ["libx265", "-preset", "ultrafast"]
    res = subprocess.run(
        [ff, "-hide_banner", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=duration=5:size=192x108:rate=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-f", "srt", "-i", str(srt),
         "-map", "0:v", "-map", "1:a", "-map", "2:0",
         "-c:v", *video_args, "-g", "10",
         "-c:a", "ac3", "-b:a", "96k",
         "-c:s", "srt",
         "-metadata:s:s:0", "language=eng",
         "-y", str(out)], capture_output=True, text=True, timeout=180)
    vcodec = "hevc"
    if res.returncode != 0:
        vcodec = "h264"
        res = subprocess.run(
            [ff, "-hide_banner", "-v", "error",
             "-f", "lavfi", "-i", "testsrc2=duration=5:size=192x108:rate=5",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
             "-f", "srt", "-i", str(srt),
             "-map", "0:v", "-map", "1:a", "-map", "2:0",
             "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "ac3", "-b:a", "96k",
             "-c:s", "srt",
             "-y", str(out)], capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(f"source mkv generation failed: {res.stderr[:800]}")
    return out.read_bytes(), vcodec


def parse_local_file(ff: str, data: bytes, tmpdir: pathlib.Path) -> dict:
    p = tmpdir / "downloaded.mkv"
    p.write_bytes(data)
    res = subprocess.run([ff, "-hide_banner", "-i", str(p)],
                         capture_output=True, text=True, timeout=60)
    return media.parse_ffmpeg_stderr(res.stderr, "local")


def integration(client):
    ff = media.ffmpeg_bin()
    if not ff:
        print("[SKIP] no ffmpeg binary available — skipping remux integration")
        return

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="stream-poc-remux-"))
    try:
        src, vcodec = generate_source_mkv(tmp)
    except Exception as e:
        print(f"[SKIP] could not synthesize HEVC/AC3 MKV: {e}")
        return
    print(f"       (fixture video codec: {vcodec}, {len(src)} bytes, AC3 audio + SRT subs)")

    fake_drive.DATA = src
    fake_drive.SIZE = len(src)

    # sanity: probe reports AC3 + needs audio remux
    r = client.get("/probe/vid123")
    check("probe 200 on AC3 source", r.status_code == 200, r.text[:200])
    if r.status_code == 200:
        info = r.json()
        check("probe flags AC3", any(a["codec"] == "ac3" for a in info["audio"]),
              str(info["audio"]))
        check("probe needs_audio_remux=True",
              info["playability"]["needs_audio_remux"] is True,
              str(info["playability"]))

    # start the background job
    r = client.post("/remux/vid123")
    check("remux start accepted", r.status_code == 200
          and r.json()["state"] in ("processing", "ready"), r.text[:200])

    # poll until ready
    deadline = time.time() + 180
    st = {}
    while time.time() < deadline:
        st = client.get("/remux/vid123/status").json()
        if st.get("state") in ("ready", "error"):
            break
        time.sleep(0.5)
    check("remux reached ready", st.get("state") == "ready",
          f"last status: {st}")
    if st.get("state") != "ready":
        return
    check("status exposes playable url", st.get("url") == "/remux/vid123.mkv")

    # second POST is a no-op (cached)
    r = client.post("/remux/vid123")
    check("re-POST returns ready (no duplicate job)",
          r.status_code == 200 and r.json()["state"] == "ready")

    # full fetch of remuxed file
    r = client.get("/remux/vid123.mkv")
    check("remuxed mkv served", r.status_code == 200 and len(r.content) > 10_000,
          f"{r.status_code} len={len(r.content)}")
    if r.status_code != 200:
        return

    info = parse_local_file(ff, r.content, tmp)
    check("video codec preserved (copied, NOT transcoded)",
          info["video"] and info["video"]["codec"] == vcodec,
          f"expected {vcodec}, got {info['video']}")
    check("audio converted AC3 -> AAC",
          info["audio"] and all(a["codec"] == "aac" for a in info["audio"]),
          str(info["audio"]))
    check("subtitle stream carried over",
          any(s["codec"] == "subrip" for s in info["subtitles"]),
          str(info["subtitles"]))

    # range fetch on the remuxed file
    r = client.get("/remux/vid123.mkv", headers={"Range": "bytes=64-127"})
    full = client.get("/remux/vid123.mkv").content
    check("remuxed range 206 byte-exact",
          r.status_code == 206 and r.content == full[64:128],
          f"{r.status_code}")


def main() -> int:
    global failures
    start_fake_drive()
    with TestClient(poc.app) as client:  # 'with' keeps the portal loop alive for the background task
        ranged_file_tests(client)
        integration(client)
    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All remux checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
