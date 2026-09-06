"""
Media-engine v2 tests: the decision ladder is a PURE function, so the entire
matrix is unit-tested directly; the /media/{id}/plan endpoint is exercised
through FastAPI's TestClient with a stubbed probe (no Drive, no ffmpeg).

v2 contract under test (docs/ARCHITECTURE.md §6):
  * rungs: direct-play → container-remux → audio-transcode → transcode
  * `wasm-audio` is GONE — no plan may ever select it or reference the
    in-browser decoder (global sweep asserts this across the matrix)
  * MKV is NEVER direct-played, even when the browser claims mkv support
  * fragmented MP4 is never direct-played
  * rungs 2–4 emit DASH playback ({type: dash, plan, url: /dash/…/manifest.mpd})
    with a stable plan id; `implemented` is honest while DASH_ORIGIN_ENABLED
    is off, and the alternatives fallback chain fills in once it is on
  * identical (file, client-class) plan requests hit the plan cache
    (decide() runs once)

Also parse/imports the POC frontend media layer with Node (file integrity;
the POC UI stays in place until the P1 Shaka player replaces it).

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

VALID_MODES = {"direct-play", "container-remux", "audio-transcode",
               "transcode", "unsupported"}

# --------------------------------------------------------------------------
# Capability fixtures
# --------------------------------------------------------------------------

def caps(name="test", **over):
    c = {
        "name": name,
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


CHROME = caps()                      # claims mkv + hevc (a "capable" browser)
CHROME_NO_WASM = caps("test-nw", features={"wasmAudio": False})
FIREFOX = caps("firefox", containers={"mkv": False}, video={"hevc": False})
MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


def probe(container="matroska,webm", video="hevc", audio=("ac3",), subs=(),
          fragmented=False):
    i = 0
    v = None
    if video:
        v = {"index": i, "codec": video, "language": "und", "title": "",
             "width": 1920, "height": 1080}
        i += 1
    audio_entries = []
    for codec in audio:
        audio_entries.append({"index": i, "codec": codec, "language": "und",
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
            "fragmented": fragmented, "probe_engine": "ffprobe",
            "playability": {}}


# --------------------------------------------------------------------------
# 1. Ladder unit tests
# --------------------------------------------------------------------------

def ladder_tests():
    # rung 1: everything native in a direct-playable container → Direct Play
    p = engine.decide(probe(container=MP4, video="h264", audio=("aac",)), CHROME)
    check("direct-play when all native", p["mode"] == "direct-play"
          and p["implemented"], p["mode"])
    check("direct-play sources original /stream bytes",
          p["playback"]["type"] == "range"
          and p["playback"]["url"] == "/stream/vid123")
    check("direct-play audio action native", p["audio"]["action"] == "native")
    check("direct-play steps ordered container→video→audio",
          [s["stream"] for s in p["steps"]] == ["container", "video", "audio"])

    # THE test file: MKV + HEVC + AC3 on a capable browser.
    # Video plays (HEVC ok) → rung 3: video copied, audio → AAC via DASH.
    p = engine.decide(probe(video="hevc", audio=("ac3",)), CHROME)
    check("mkv+hevc+ac3 on chrome → audio-transcode (not wasm, not transcode)",
          p["mode"] == "audio-transcode", p["mode"])
    check("audio-transcode playback is a DASH manifest",
          p["playback"]["type"] == "dash"
          and p["playback"]["url"] == "/dash/vid123/aac-1-und/manifest.mpd",
          json.dumps(p["playback"]))
    check("audio-transcode plan id is stable",
          p["playback"]["plan"] == "aac-1-und"
          and engine.dash_plan_id("aac", 1, "und") == "aac-1-und")
    check("audio-transcode keeps the AC3 track as the chosen one",
          p["audio"] == {"trackIndex": 1, "codec": "ac3",
                         "action": "convert-aac"}, json.dumps(p["audio"]))
    check("audio-transcode steps copy the video bitstream",
          any(s["stream"] == "video" and s["action"] == "copy"
              for s in p["steps"]))
    check("audio-transcode NOT implemented while DASH origin is off",
          p["implemented"] is False
          and p["implementedModes"] == ["direct-play"])

    # DTS is convertible server-side (it was never in WASM scope)
    p = engine.decide(probe(video="hevc", audio=("dts",)), CHROME)
    check("dts → audio-transcode (server-side AAC conversion)",
          p["mode"] == "audio-transcode", p["mode"])
    p = engine.decide(probe(video="hevc", audio=("dts",)), CHROME_NO_WASM)
    check("wasmAudio capability no longer changes the decision",
          p["mode"] == "audio-transcode", p["mode"])

    # multi-audio: prefer the natively-playable track (audio-track UX data)
    p = engine.decide(probe(container=MP4, video="hevc", audio=("ac3", "aac")),
                      CHROME)
    check("multi-audio (ac3+aac) on mp4 → direct-play picks the AAC track",
          p["mode"] == "direct-play" and p["audio"]["codec"] == "aac"
          and p["audio"]["trackIndex"] == 2, json.dumps(p["audio"]))
    tr = {t["codec"]: t for t in p["audioTracks"]}
    check("per-track capability flags exposed for the UI",
          not tr["ac3"]["nativePlayable"] and tr["aac"]["nativePlayable"],
          json.dumps(tr))

    # MKV is never direct-played, even when the browser claims mkv support:
    # all codecs native → rung 2 (copy-only remux), never rung 1.
    p = engine.decide(probe(video="vp9", audio=("opus",)), CHROME)
    check("mkv with all-native codecs + mkv-capable browser → NOT direct-play",
          p["mode"] == "container-remux", p["mode"])
    check("mkv remux is copy-only in the steps",
          all(s["action"] in ("remux", "copy") for s in p["steps"]))
    check("mkv reason names the unreliable-sniffing rule",
          any("never direct-played" in r for r in p["reasons"]),
          json.dumps(p["reasons"]))

    # fragmented MP4: not progressive-playable → remux even though all native
    p = engine.decide(probe(container=MP4, video="h264", audio=("aac",),
                            fragmented=True), CHROME)
    check("fragmented mp4 (all native) → NOT direct-play, remux instead",
          p["mode"] == "container-remux", p["mode"])

    # firefox can't do MKV or HEVC → rung 2 on vp9/opus
    p = engine.decide(probe(video="vp9", audio=("opus",)), FIREFOX)
    check("mkv+vp9+opus on firefox → container-remux (not direct)",
          p["mode"] == "container-remux", p["mode"])

    # container not browser-playable + all codecs native → remux, honest flag
    p = engine.decide(probe(container="avi", video="h264", audio=("aac",)),
                      CHROME)
    check("avi+h264+aac → container-remux, unimplemented while DASH is off",
          p["mode"] == "container-remux" and not p["implemented"], p["mode"])

    # rung 4: video codec itself blocked → full transcode, last resort
    p = engine.decide(probe(video="mpeg2video", audio=("aac",)), CHROME)
    check("mpeg2 video → transcode (last resort, honest flag)",
          p["mode"] == "transcode" and not p["implemented"], p["mode"])
    check("transcode playback is the h264aac DASH plan",
          p["playback"]["plan"] == "h264aac-1-und"
          and p["playback"]["url"] == "/dash/vid123/h264aac-1-und/manifest.mpd")

    # worst case: bad container AND blocked video → transcode with reasons
    p = engine.decide(probe(container="avi", video="mpeg2video",
                            audio=("ac3",)), CHROME)
    check("worst case → transcode with reasons chain",
          p["mode"] == "transcode" and len(p["reasons"]) >= 3)

    # empty file
    p = engine.decide(probe(video=None, audio=()), CHROME)
    check("no streams → unsupported, not implemented",
          p["mode"] == "unsupported" and not p["implemented"])

    # capabilities normalization: unreported keys must NOT default to support
    c = engine.normalize_caps({"containers": {"mkv": True}})
    check("unreported codecs default to unsupported",
          c["video"]["h264"] is False and c["audio"]["aac"] is False)

    # plan always carries the data the UI needs
    p = engine.decide(probe(subs=("subrip",)), CHROME)
    check("plan echoes subtitles + probe summary for pages",
          p["subtitles"][0]["url"].startswith("/subtitles/")
          and p["probe"]["container"] == "matroska,webm")

    # -- DASH origin ON: rungs 2–4 become implemented, fallback chain fills --
    old = engine.DASH_ORIGIN_ENABLED
    try:
        engine.DASH_ORIGIN_ENABLED = True
        p = engine.decide(probe(video="hevc", audio=("ac3",)), CHROME)
        check("DASH on: audio-transcode implemented",
              p["mode"] == "audio-transcode" and p["implemented"])
        check("DASH on: alternatives chain offers full transcode",
              any(a["mode"] == "transcode" and a["implemented"]
                  for a in p["alternatives"]),
              json.dumps(p["alternatives"]))
        check("DASH on: all four rungs implemented",
              p["implementedModes"] == sorted(
                  ["direct-play", "container-remux", "audio-transcode",
                   "transcode"]))
        p2 = engine.decide(probe(video="vp9", audio=("opus",)), CHROME)
        check("DASH on: mkv remux implemented + direct NOT an alternative",
              p2["mode"] == "container-remux" and p2["implemented"]
              and all(a["mode"] != "direct-play" for a in p2["alternatives"]))
    finally:
        engine.DASH_ORIGIN_ENABLED = old

    # -- global sweep: the WASM era is over everywhere ------------------------
    bad = []
    for container in ("matroska,webm", MP4, "avi"):
        for video in ("h264", "hevc", "vp9", "mpeg2video"):
            for audio in (("ac3",), ("aac",), ("ac3", "aac"), ("dts",), ()):
                for cc in (CHROME, CHROME_NO_WASM, FIREFOX, None):
                    p = engine.decide(probe(container, video, audio), cc)
                    if p["mode"] not in VALID_MODES:
                        bad.append((container, video, audio, cc, "mode",
                                    p["mode"]))
                        continue
                    a = p.get("audio")
                    if a and any(k in a for k in ("module", "startUrl",
                                                  "statusUrl")):
                        bad.append((container, video, audio, cc, "audio", a))
                    if any("wasm" in s.get("detail", "").lower()
                           for s in p.get("steps", [])):
                        bad.append((container, video, audio, cc, "steps", p))
    check("matrix sweep: no plan ever uses wasm-audio / wasm decoder refs",
          not bad, str(bad[:3]))


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

    async def fake_probe(file_id, token, drive_api_base):
        return probe()

    app_module.media.probe_media = fake_probe  # app shares the media module

    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)

    r = client.get("/media/vid123/plan")
    payload = r.json()
    check("GET plan 200", r.status_code == 200, r.text[:200])
    for key in ("mode", "implemented", "reasons", "steps", "playback",
                "audio", "audioTracks", "subtitles", "alternatives",
                "implementedModes"):
        check(f"plan has '{key}'", key in payload)
    check("server-default caps are conservative (mkv+hevc+ac3 → transcode)",
          payload["mode"] == "transcode", payload["mode"])
    check("server-default transcode is honest (DASH off → unimplemented)",
          payload["implemented"] is False)

    r = client.post("/media/vid123/plan", json=CHROME)
    payload = r.json()
    check("POST with chrome caps → audio-transcode",
          payload["mode"] == "audio-transcode", payload["mode"])
    check("posted plan named the reporting client",
          payload["capabilities"] == "test")
    check("posted plan is a DASH playback",
          payload["playback"]["type"] == "dash"
          and payload["playback"]["url"]
          == "/dash/vid123/aac-1-und/manifest.mpd")

    r = client.post("/media/vid123/plan", content=b"not json")
    check("POST with garbage body → falls back to defaults, still 200",
          r.status_code == 200 and r.json()["mode"] == "transcode")

    # -- plan cache: identical requests compute decide() exactly once --------
    real_decide = engine.decide
    calls = {"n": 0}

    def counting_decide(info, c):
        calls["n"] += 1
        return real_decide(info, c)

    engine.decide = counting_decide
    try:
        r1 = client.post("/media/vid123/plan", json=caps("cachetest-1"))
        r2 = client.post("/media/vid123/plan", json=caps("cachetest-1"))
        check("identical (file, client) requests → decide runs once",
              calls["n"] == 1 and r1.json() == r2.json(),
              f"calls={calls['n']}")
        r3 = client.post("/media/vid123/plan", json=caps("cachetest-2"))
        check("different client class → recomputed",
              calls["n"] == 2 and r3.json()["capabilities"] == "cachetest-2",
              f"calls={calls['n']}")
    finally:
        engine.decide = real_decide


# --------------------------------------------------------------------------
# 3. POC frontend media layer: parse + export integrity (node)
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
