"""
Media-engine tests: the decision ladder is a PURE function, so the entire
matrix is unit-tested directly; the /media/{id}/plan endpoint is exercised
through FastAPI's TestClient with a stubbed probe (no Drive, no ffmpeg).

Also parse/imports the frontend media layer with Node (export integrity).

Run:  python tests/test_media_engine.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "backend"))

failures = []


def check(name, ok, extra=""):
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  -- {extra}" if (extra and not ok) else ""))
    if not ok:
        failures.append(name)


import engine  # noqa: E402  (backend/engine.py)

# --------------------------------------------------------------------------
# Capability fixtures
# --------------------------------------------------------------------------

def caps(**over):
    c = {
        "name": "test",
        "containers": {"mp4": True, "webm": True, "mkv": True, "mov": True,
                       "avi": False, "ts": False, "ogg": False, "mpeg": False,
                       "flv": False, "asf": False},
        "video": {"h264": True, "vp8": True, "vp9": True, "av1": True,
                  "hevc": True, "mpeg4": True, "mpeg2": False, "mpeg1": False},
        "audio": {"aac": True, "mp3": True, "opus": True, "vorbis": True,
                  "flac": True, "ac3": False, "eac3": False, "dts": False,
                  "truehd": False},
        "features": {"wasmAudio": True, "mse": True},
    }
    for k, v in over.items():
        if isinstance(v, dict):
            c.setdefault(k, {}).update(v)
        else:
            c[k] = v
    return c


CHROME = caps()
CHROME_NO_WASM = caps(features={"wasmAudio": False})
FIREFOX = caps(containers={"mkv": False}, video={"hevc": False})


def probe(container="matroska,webm", video="hevc", audio=("ac3",), subs=()):
    i = 0
    v = None
    if video:
        v = {"index": i, "codec": video, "language": "und", "title": "",
             "width": 1920, "height": 1080}
        i += 1
    audio_entries = []
    for codec in audio:
        audio_entries.append({"index": i, "codec": codec, "language": "eng",
                              "title": "", "channels": "5.1(side)"})
        i += 1
    sub_entries = []
    for codec in subs:
        sub_entries.append({"index": i, "codec": codec, "language": "eng",
                            "title": "", "web_compatible": True, "note": "",
                            "url": f"/subtitles/vid123/{i}.vtt"})
        i += 1
    return {"file_id": "vid123", "format_name": container, "duration": 3600.0,
            "video": v, "audio": audio_entries, "subtitles": sub_entries,
            "probe_engine": "ffprobe", "playability": {}}


# --------------------------------------------------------------------------
# 1. Ladder unit tests
# --------------------------------------------------------------------------

def ladder_tests():
    # priority 1: everything native → Direct Play with original bytes
    p = engine.decide(probe(video="h264", audio=("aac",)), CHROME)
    check("direct-play when all native", p["mode"] == "direct-play"
          and p["implemented"], p["mode"])
    check("direct-play sources original /stream bytes",
          p["playback"]["url"] == "/stream/vid123")
    check("direct-play audio action native", p["audio"]["action"] == "native")
    check("direct-play steps ordered container→video→audio",
          [s["stream"] for s in p["steps"]] == ["container", "video", "audio"])

    # priority 2: only audio blocked, wasm-decodable → WASM (still original bytes)
    p = engine.decide(probe(video="hevc", audio=("ac3",)), CHROME)
    check("mkv+hevc+ac3 on chrome → wasm-audio", p["mode"] == "wasm-audio"
          and p["implemented"], p["mode"])
    check("wasm plan keeps original /stream video", p["playback"]["url"] == "/stream/vid123")
    check("wasm plan points at the wasm module",
          p["audio"]["module"] == "/static/ac3-audio.js")
    check("wasm plan offers AAC-remux fallback",
          any(a["mode"] == "audio-remux" for a in p["alternatives"]))
    check("wasm reasons explain the choice", any("wasm" in r.lower() for r in p["reasons"]))

    # wasm disabled → fall to priority 4 (audio-only conversion)
    p = engine.decide(probe(video="hevc", audio=("ac3",)), CHROME_NO_WASM)
    check("ac3 with wasmAudio=false → audio-remux", p["mode"] == "audio-remux"
          and p["implemented"], p["mode"])
    check("audio-remux advertises start/status urls",
          p["audio"]["startUrl"].endswith("/vid123")
          and "/status" in p["audio"]["statusUrl"])

    # DTS: outside wasm scope today → audio-remux
    p = engine.decide(probe(video="hevc", audio=("dts",)), CHROME)
    check("dts (not in wasm scope) → audio-remux", p["mode"] == "audio-remux")

    # multi-audio: prefer the natively-playable track (audio-track UX data)
    p = engine.decide(probe(video="hevc", audio=("ac3", "aac")), CHROME)
    check("multi-audio picks native AAC track over AC3",
          p["mode"] == "direct-play" and p["audio"]["codec"] == "aac",
          json.dumps(p["audio"]))
    tr = {t["codec"]: t for t in p["audioTracks"]}
    check("per-track capability flags exposed for the UI",
          tr["ac3"]["wasmDecodable"] and not tr["ac3"]["nativePlayable"]
          and tr["aac"]["nativePlayable"])

    # priority 3: codecs fine, container not parseable → copy-only remux
    p = engine.decide(probe(container="avi", video="h264", audio=("aac",)), CHROME)
    check("avi+h264+aac → container-remux (correct, unimplemented adapter)",
          p["mode"] == "container-remux" and not p["implemented"], p["mode"])
    check("container-remux steps are copy-only",
          all(s["action"] in ("remux", "copy") for s in p["steps"]))

    # firefox can't do MKV at all → container remux (not direct play!)
    p = engine.decide(probe(container="matroska,webm", video="vp9", audio=("opus",)), FIREFOX)
    check("mkv+vp9+opus on firefox → NOT direct-play",
          p["mode"] == "container-remux", p["mode"])

    # priority 5: video codec itself blocked → last resort, honestly flagged
    p = engine.decide(probe(video="mpeg2video", audio=("aac",)), CHROME)
    check("mpeg2 video → transcode (last resort, unimplemented)",
          p["mode"] == "transcode" and not p["implemented"], p["mode"])

    # bad container AND blocked video → nothing cheap works
    p = engine.decide(probe(container="avi", video="mpeg2video", audio=("ac3",)), CHROME)
    check("worst case → transcode with reasons chain",
          p["mode"] == "transcode" and len(p["reasons"]) >= 3)

    # empty file
    p = engine.decide(probe(video=None, audio=()), CHROME)
    check("no streams → unsupported, not implemented",
          p["mode"] == "unsupported" and not p["implemented"])

    # capabilities normalization: unreported keys must NOT default to supported
    c = engine.normalize_caps({"containers": {"mkv": True}})
    check("unreported codecs default to unsupported",
          c["video"]["h264"] is False and c["audio"]["aac"] is False)
    check("wasmAudio feature survives normalization",
          c["features"]["wasmAudio"] is True)

    # plan always carries the data the future library UI needs
    p = engine.decide(probe(subs=("subrip",)), CHROME)
    check("plan echoes subtitles + probe summary for pages",
          p["subtitles"][0]["url"].startswith("/subtitles/")
          and p["probe"]["container"] == "matroska,webm")


# --------------------------------------------------------------------------
# 2. Endpoint shape via TestClient (stubbed probe, fabricated token)
# --------------------------------------------------------------------------

def endpoint_tests():
    tmp = tempfile.mkdtemp(prefix="engine-test-")
    tmp_path = Path(tmp)
    (tmp_path / "token.json").write_text(json.dumps({
        "access_token": "fake", "refresh_token": "fake",
        "expires_at": time.time() + 3600}))
    os.environ["TOKEN_FILE"] = str(tmp_path / "token.json")
    os.environ["CACHE_DIR"] = str(tmp_path / "cache")
    os.environ["DRIVE_API_BASE"] = "http://127.0.0.1:1/drive/v3"  # never hit

    import importlib
    import app as app_module
    importlib.reload(app_module)          # re-read env in case it was imported
    import media as media_module

    async def fake_probe(file_id, token, drive_api_base):
        return probe()

    app_module.media.probe_media = fake_probe
    media_module.probe_media = fake_probe

    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)

    r = client.get("/media/vid123/plan")
    payload = r.json()
    check("GET plan 200", r.status_code == 200, r.text[:200])
    for key in ("mode", "implemented", "reasons", "steps", "playback",
                "audio", "audioTracks", "subtitles", "alternatives", "probe"):
        check(f"plan has '{key}'", key in payload)
    check("server-default caps are conservative (mkv+hevc blocked → transcode)",
          payload["mode"] == "transcode", payload["mode"])

    r = client.post("/media/vid123/plan",
                    json=CHROME, )
    payload = r.json()
    check("POST with chrome caps → wasm-audio", payload["mode"] == "wasm-audio",
          payload["mode"])
    check("posted plan named the reporting client",
          payload["capabilities"] == "test")

    r = client.post("/media/vid123/plan", content=b"not json")
    check("POST with garbage body → falls back to defaults, still 200",
          r.status_code == 200 and r.json()["mode"] == "transcode")


# --------------------------------------------------------------------------
# 3. Frontend media layer: parse + export integrity (node)
# --------------------------------------------------------------------------

def frontend_tests():
    node = shutil.which("node")
    if not node:
        print("[SKIP] node not available — JS module checks skipped")
        return
    fdir = BASE / "frontend"
    files = ["media/capabilities.js", "media/engine.js", "media/adapters.js",
             "media/subtitles.js", "app.js", "ac3-audio.js"]
    for f in files:
        r = subprocess.run([node, "--input-type=module", "--check"],
                           input=(fdir / f).read_bytes(), capture_output=True)
        check(f"parse {f}", r.returncode == 0, r.stderr.decode()[:200])

    script = """
      const base = "file://" + process.argv[1] + "/";
      const caps = await import(base + "media/capabilities.js");
      const eng  = await import(base + "media/engine.js");
      const ad   = await import(base + "media/adapters.js");
      const sm   = await import(base + "media/subtitles.js");
      const ok =
        typeof caps.detectCapabilities === "function" &&
        typeof eng.MediaEngine === "function" &&
        typeof ad.createAdapter === "function" &&
        typeof ad.PlanUnavailable === "function" &&
        typeof sm.SubtitleManager === "function";
      for (const mode of ["direct-play","wasm-audio","container-remux","audio-remux","transcode","nope"]) {
        const a = ad.createAdapter(mode, {plan:{}, fileId:"x"});
        if (!a || typeof a.mount !== "function") throw new Error("adapter missing: " + mode);
      }
      const cu = await ad.createAdapter("transcode", {plan:{}}).mount().catch(e => e);
      if (!(cu instanceof ad.PlanUnavailable)) throw new Error("transcode must be PlanUnavailable");
      console.log(ok ? "EXPORTS_OK" : "EXPORTS_MISSING");
    """
    r = subprocess.run([node, "--input-type=module", "-e", script, str(fdir)],
                       capture_output=True, text=True, timeout=60)
    check("media-layer exports + all 5 adapters constructible",
          "EXPORTS_OK" in r.stdout, r.stderr[:300] + r.stdout[:100])


if __name__ == "__main__":
    ladder_tests()
    try:
        endpoint_tests()
    except Exception as e:  # pragma: no cover
        check("endpoint tests (TestClient import/run)", False, repr(e))
    frontend_tests()

    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("\nAll media-engine checks passed.")
