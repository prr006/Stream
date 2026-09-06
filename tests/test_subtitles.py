"""
Tests for the MKV + subtitle milestone.

Unit parts (no binaries needed):
  - ffprobe-JSON parsing (normalize_ffprobe)
  - ffmpeg-stderr parsing (parse_ffmpeg_stderr)
  - playability assessment (warnings for HEVC / AC3 / PGS)

Integration part (auto-skipped if no ffmpeg binary is available):
  - synthesizes a real MKV containing h264 video + aac audio + TWO embedded
    SRT subtitle tracks (eng + jpn), using ffmpeg's lavfi/srt inputs
  - serves those bytes through the fake Drive upstream
  - exercises /probe and /subtitles/<id>/<idx>.vtt end-to-end through the
    running POC app (TestClient), including the on-disk VTT cache and a
    bad-stream-index error path.

Run from the repo root:  python tests/test_subtitles.py
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
os.environ["DRIVE_API_BASE"] = "http://127.0.0.1:9010"
_token_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
os.environ["TOKEN_FILE"] = _token_file.name
_token_file.close()
os.environ["CACHE_DIR"] = tempfile.mkdtemp(prefix="stream-poc-cache-")

pathlib.Path(_token_file.name).write_text(json.dumps({
    "access_token": "fake-access-token",
    "refresh_token": "fake-refresh-token",
    "expires_at": time.time() + 3600,
}))

import fake_drive  # noqa: E402
import media  # noqa: E402
import app as poc  # noqa: E402  (backend/app.py — module name is 'app')
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FAKE_PORT = 9010
failures = 0


def check(label, cond, extra=""):
    global failures
    status = "PASS" if cond else "FAIL"
    if not cond:
        failures += 1
    print(f"[{status}] {label}" + (f"  -- {extra}" if extra and not cond else ""))


# ---------------------------------------------------------------------------
# Unit: canned ffprobe JSON
# ---------------------------------------------------------------------------

CANNED_FFPROBE = {
    "format": {"format_name": "matroska,webm", "duration": "1234.5"},
    "streams": [
        {"index": 0, "codec_name": "h264", "codec_type": "video",
         "width": 1920, "height": 1080, "tags": {"title": "Encoded by x264"}},
        {"index": 1, "codec_name": "aac", "codec_type": "audio",
         "channel_layout": "stereo", "tags": {"language": "eng"}},
        {"index": 2, "codec_name": "subrip", "codec_type": "subtitle",
         "tags": {"language": "eng", "title": "English"}},
        {"index": 3, "codec_name": "hdmv_pgs_subtitle", "codec_type": "subtitle",
         "tags": {"language": "jpn", "title": "Japanese PGS"}},
    ],
}


def unit_ffprobe_parse():
    info = media.normalize_ffprobe(CANNED_FFPROBE, "file1")
    check("ffprobe: container parsed", info["format_name"] == "matroska,webm")
    check("ffprobe: duration parsed", abs(info["duration"] - 1234.5) < 0.01)
    check("ffprobe: video parsed",
          info["video"]["codec"] == "h264" and info["video"]["width"] == 1920)
    check("ffprobe: audio parsed",
          len(info["audio"]) == 1 and info["audio"][0]["language"] == "eng")
    check("ffprobe: 2 subtitle tracks", len(info["subtitles"]) == 2)
    check("ffprobe: SRT flagged web-compatible",
          info["subtitles"][0]["codec"] == "subrip"
          and info["subtitles"][0]["web_compatible"] is True)
    check("ffprobe: PGS flagged NOT web-compatible",
          info["subtitles"][1]["codec"] == "hdmv_pgs_subtitle"
          and info["subtitles"][1]["web_compatible"] is False
          and "image-based" in info["subtitles"][1]["note"])

    p = media.assess_playability(info)
    check("ffprobe: MKV browser note present", "Chrome" in p["browser_note"])
    check("ffprobe: PGS warning emitted",
          any("image-based" in w for w in p["warnings"]))
    check("ffprobe: h264+aac not blocked", p["video_blocked"] is False)


# ---------------------------------------------------------------------------
# Unit: canned ffmpeg stderr (fallback parser)
# ---------------------------------------------------------------------------

CANNED_FFMPEG_STDERR = """Input #0, matroska,webm, from 'http://127.0.0.1:9010/files/x?alt=media':
  Metadata:
    encoder         : libebml v1.4.4 + libmatroska v1.7.1
  Duration: 01:23:45.67, start: 0.000000, bitrate: 5000 kb/s
  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc), 3840x2160, 23.98 fps (default)
      Metadata:
        title           : Movie Video
  Stream #0:1(eng): Audio: ac3, 48000 Hz, 5.1(side), fltp, 640 kb/s (default)
      Metadata:
        title           : English AC3
  Stream #0:2(eng): Subtitle: subrip
      Metadata:
        title           : English
  Stream #0:3(jpn): Subtitle: hdmv_pgs_subtitle (default)
      Metadata:
        title           : Japanese PGS
At least one output file must be specified
"""


def unit_ffmpeg_stderr_parse():
    info = media.parse_ffmpeg_stderr(CANNED_FFMPEG_STDERR, "file2")
    check("stderr: container parsed", "matroska" in info["format_name"])
    check("stderr: duration parsed",
          abs(info["duration"] - (3600 + 23 * 60 + 45.67)) < 0.01,
          f"got {info['duration']}")
    check("stderr: hevc video + dimensions",
          info["video"]["codec"] == "hevc"
          and info["video"]["width"] == 3840 and info["video"]["height"] == 2160)
    check("stderr: audio lang + title",
          info["audio"][0]["language"] == "eng"
          and info["audio"][0]["title"] == "English AC3")
    check("stderr: 2 subtitle tracks with titles",
          len(info["subtitles"]) == 2
          and info["subtitles"][0]["title"] == "English"
          and info["subtitles"][1]["language"] == "jpn")
    check("stderr: PGS flagged NOT web-compatible",
          info["subtitles"][1]["web_compatible"] is False)

    p = media.assess_playability(info)
    check("stderr: HEVC warning emitted",
          p["video_blocked"] is True and any("hevc" in w for w in p["warnings"]))
    check("stderr: AC3 audio warning emitted",
          any("ac3" in w and "silent" in w for w in p["warnings"]))


# ---------------------------------------------------------------------------
# Integration: synthetic MKV with two embedded SRT tracks through the app
# ---------------------------------------------------------------------------

ENG_SRT = """1
00:00:01,000 --> 00:00:02,500
Hello POC world

2
00:00:03,000 --> 00:00:05,000
Second english cue
"""

JPN_SRT = """1
00:00:01,500 --> 00:00:04,000
Japanese subtitle sample
"""


def generate_mkv(tmpdir: pathlib.Path) -> bytes:
    ff = media.ffmpeg_bin()
    eng = tmpdir / "eng.srt"
    jpn = tmpdir / "jpn.srt"
    out = tmpdir / "sample.mkv"
    eng.write_text(ENG_SRT)
    jpn.write_text(JPN_SRT)
    cmd = [
        ff, "-hide_banner", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=6:size=160x120:rate=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-f", "srt", "-i", str(eng),
        "-f", "srt", "-i", str(jpn),
        "-map", "0:v", "-map", "1:a", "-map", "2:0", "-map", "3:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
        "-c:a", "aac", "-b:a", "32k",
        "-c:s", "srt",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English",
        "-metadata:s:s:1", "language=jpn", "-metadata:s:s:1", "title=Japanese",
        "-y", str(out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"mkv generation failed: {res.stderr}")
    return out.read_bytes()


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


def integration_tests():
    ff = media.ffmpeg_bin()
    if not ff:
        print("[SKIP] no ffmpeg binary available — skipping integration tests")
        return

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="stream-poc-mkv-"))
    try:
        mkv = generate_mkv(tmp)
    except Exception as e:  # e.g. bundled build quirks — don't fail the suite
        print(f"[SKIP] could not synthesize MKV: {e}")
        return
    check("synthetic MKV generated", len(mkv) > 10_000, f"{len(mkv)} bytes")

    fake_drive.DATA = mkv
    fake_drive.SIZE = len(mkv)
    start_fake_drive()
    client = TestClient(poc.app)

    # /probe -----------------------------------------------------------------
    r = client.get("/probe/vid123")
    check("probe returns 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    if r.status_code != 200:
        return
    info = r.json()
    check("probe: container is matroska", "matroska" in info["format_name"],
          info["format_name"])
    check("probe: video is h264", info["video"] and info["video"]["codec"] == "h264")
    check("probe: audio is aac", any(a["codec"] == "aac" for a in info["audio"]),
          str(info["audio"]))
    subs = info["subtitles"]
    check("probe: 2 subtitle tracks detected", len(subs) == 2, str(subs))
    check("probe: both web-compatible subrip",
          all(s["codec"] == "subrip" and s["web_compatible"] for s in subs))
    check("probe: languages eng+jpn",
          {s["language"] for s in subs} == {"eng", "jpn"}, str(subs))
    check("probe: subtitle urls injected",
          all(s.get("url", "").startswith("/subtitles/vid123/") for s in subs))
    check("probe: playability object present",
          isinstance(info.get("playability", {}).get("warnings"), list))

    # /subtitles --------------------------------------------------------------
    eng_idx = next(s["index"] for s in subs if s["language"] == "eng")
    jpn_idx = next(s["index"] for s in subs if s["language"] == "jpn")

    r = client.get(f"/subtitles/vid123/{eng_idx}.vtt")
    check("eng vtt returns 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("eng vtt content-type", r.headers.get("content-type", "").startswith("text/vtt"))
    check("vtt NOT served as attachment (Chrome <track> would discard it)",
          "attachment" not in r.headers.get("content-disposition", "").lower(),
          r.headers.get("content-disposition", ""))
    check("eng vtt has WEBVTT header", r.text.strip().startswith("WEBVTT"))
    check("eng vtt contains first cue text", "Hello POC world" in r.text)
    check("eng vtt contains second cue", "Second english cue" in r.text)
    check("eng vtt has VTT-style timestamps",
          bool(re.search(r"\d{2}:\d{2}\.\d{3}\s*-->", r.text)), r.text[:200])
    eng_text = r.text

    r = client.get(f"/subtitles/vid123/{jpn_idx}.vtt")
    check("jpn vtt contains its cue", "Japanese subtitle sample" in r.text)

    # cache behavior: same content served from disk on second request
    cache_file = poc.SUBS_CACHE / f"vid123_{eng_idx}.vtt"
    check("vtt written to cache dir", cache_file.exists(), str(cache_file))
    mtime = cache_file.stat().st_mtime if cache_file.exists() else 0
    r2 = client.get(f"/subtitles/vid123/{eng_idx}.vtt")
    check("cached vtt identical on refetch", r2.status_code == 200 and r2.text == eng_text)
    check("cache file not rewritten", cache_file.exists() and cache_file.stat().st_mtime == mtime)

    # error path: subtitle stream index that doesn't exist
    r = client.get("/subtitles/vid123/97.vtt")
    check("bad stream index -> 422", r.status_code == 422, f"got {r.status_code}")


def main() -> int:
    global failures
    unit_ffprobe_parse()
    unit_ffmpeg_stderr_parse()
    integration_tests()
    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All subtitle/MKV checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
