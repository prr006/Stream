"""
Media engine — the decision layer between "what the file is" (media.probe)
and "how it should reach the screen".

Inspired by the Jellyfin/Emby Direct Play / Direct Stream / Transcode model,
ordered by MINIMUM intervention:

    1. direct-play       original bytes, browser decodes everything
    2. wasm-audio        original bytes; unsupported audio decoded in-browser
                         by WASM (client-side decoder) — nothing re-encoded
    3. container-remux   only the container is wrong (bitstreams copied)
    4. audio-remux       audio-only conversion to AAC (video never touched)
    5. transcode         full video transcode — LAST RESORT (not implemented)

`decide()` is a PURE function of (probe info, client capabilities) so the whole
matrix is unit-testable and the UI only renders the plan. Adding support for a
codec/container = extending the tables below — pages never branch on codecs.

Plan shape (JSON):

    {
      "mode": "wasm-audio",
      "implemented": true,
      "reasons": ["..."],                    # human-readable, shown in UI
      "steps": [{"stream": "container", "action": "source", ...}, ...],
      "playback": {"url": "/stream/<id>", "mime": "video/x-matroska"},
      "audio": {"trackIndex": 1, "codec": "ac3",
                "action": "wasm-decode", "module": "/static/ac3-audio.js"},
      "audioTracks": [ {...per-track chooser data...} ],
      "probe": {...summary echo for the info panel...},
      "subtitles": [...passthrough from /probe...],
      "alternatives": [{"mode": "audio-remux", "implemented": true, ...}]
    }
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Canonical names & knowledge tables
# --------------------------------------------------------------------------

# ffmpeg format_name → canonical container key
CONTAINER_ALIASES = {
    "matroska,webm": "mkv", "matroska": "mkv", "webm": "webm",
    "mov,mp4,m4a,3gp,3g2,mj2": "mp4", "mp4": "mp4", "mov": "mov",
    "avi": "avi", "mpegts": "ts", "mpeg": "mpeg", "flv": "flv",
    "ogg": "ogg", "asf": "asf",
}

# probe codec_name → canonical codec key
CODEC_ALIASES = {
    "h264": "h264", "avc1": "h264", "hevc": "hevc", "h265": "hevc",
    "vp8": "vp8", "vp9": "vp9", "av1": "av1", "av01": "av1",
    "mpeg4": "mpeg4", "mpeg2video": "mpeg2", "mpeg1video": "mpeg1",
    "aac": "aac", "mp4a": "aac", "mp3": "mp3", "opus": "opus",
    "vorbis": "vorbis", "flac": "flac", "ac3": "ac3", "eac3": "eac3",
    "dts": "dts", "dca": "dts", "truehd": "truehd", "mlp": "truehd",
}

# What the in-browser WASM decoder currently handles (ffmpeg.wasm ac3 engine).
WASM_DECODABLE_AUDIO = {"ac3", "eac3"}

# Codecs the server-side AAC remux can convert (essentially anything ffmpeg
# can decode; kept as a set so unsupported-by-ffmpeg outliers stay honest).
REMUXABLE_AUDIO = WASM_DECODABLE_AUDIO | {
    "aac", "mp3", "opus", "vorbis", "flac", "dts", "truehd", "pcm_s16le",
}

CONTAINER_MIME = {
    "mkv": "video/x-matroska", "webm": "video/webm", "mp4": "video/mp4",
    "mov": "video/quicktime", "ogg": "video/ogg",
}

# --------------------------------------------------------------------------
# Client capabilities
# --------------------------------------------------------------------------

# Conservative assumption for clients that don't report (server-side default):
# modern desktop browser, MP4/WebM only, mainstream codecs, WASM allowed.
DEFAULT_CAPABILITIES: dict = {
    "name": "server-default",
    "containers": {"mp4": True, "webm": True, "mkv": False, "avi": False,
                   "ts": False, "mov": True, "ogg": False, "mpeg": False,
                   "flv": False, "asf": False},
    "video": {"h264": True, "vp8": True, "vp9": True, "av1": True,
              "hevc": False, "mpeg4": False, "mpeg2": False, "mpeg1": False},
    "audio": {"aac": True, "mp3": True, "opus": True, "vorbis": True,
              "flac": True, "ac3": False, "eac3": False, "dts": False,
              "truehd": False},
    "features": {"wasmAudio": True, "mse": True},
}

_CAPS_BOOL_KEYS = {"containers", "video", "audio"}


def normalize_caps(payload: dict | None) -> dict:
    """Validate a browser-reported capability matrix; unknown keys default to
    False (never assume support the client didn't claim)."""
    if not isinstance(payload, dict):
        return dict(DEFAULT_CAPABILITIES)
    caps = {"name": str(payload.get("name") or "browser-report"),
            "features": dict(DEFAULT_CAPABILITIES["features"])}
    feats = payload.get("features") or {}
    for k in caps["features"]:
        caps["features"][k] = bool(feats.get(k, caps["features"][k]))
    for section in _CAPS_BOOL_KEYS:
        defaults = DEFAULT_CAPABILITIES[section]
        reported = payload.get(section) or {}
        caps[section] = {k: bool(reported.get(k, False)) for k in defaults}
    return caps


# --------------------------------------------------------------------------
# Normalization of probe output
# --------------------------------------------------------------------------

def container_key(format_name: str) -> str:
    return CONTAINER_ALIASES.get((format_name or "").lower(),
                                 (format_name or "unknown").split(",")[0])


def codec_key(name: str) -> str:
    return CODEC_ALIASES.get((name or "").lower(), name or "unknown")


# --------------------------------------------------------------------------
# The decision ladder
# --------------------------------------------------------------------------

MODES_BY_PRIORITY = ["direct-play", "wasm-audio", "container-remux",
                     "audio-remux", "transcode"]

# Adapters the shipped frontend/backend can actually execute right now.
# The ladder knows the full model; this set is what V1 wires end-to-end.
IMPLEMENTED_MODES = {"direct-play", "wasm-audio", "audio-remux"}


def _audio_track_plans(tracks: list[dict], caps: dict) -> list[dict]:
    out = []
    for t in tracks:
        codec = codec_key(t.get("codec"))
        native = bool(caps["audio"].get(codec))
        out.append({
            "index": t.get("index"),
            "codec": codec,
            "language": t.get("language") or "und",
            "title": t.get("title") or "",
            "nativePlayable": native,
            "wasmDecodable": (codec in WASM_DECODABLE_AUDIO
                              and caps["features"]["wasmAudio"]),
            "remuxable": codec in REMUXABLE_AUDIO,
        })
    return out


def decide(info: dict, caps: dict | None) -> dict:
    """Pure decision: probe info + client capabilities → playback plan."""
    caps = normalize_caps(caps)
    reasons: list[str] = []

    container = container_key(info.get("format_name", ""))
    container_ok = bool(caps["containers"].get(container))
    reasons.append(
        f"container '{info.get('format_name', '?')}' → '{container}': "
        + ("browser can parse it" if container_ok else "browser CANNOT parse it"))

    video = info.get("video")
    vcodec = codec_key(video["codec"]) if video else None
    video_ok = bool(vcodec and caps["video"].get(vcodec))
    if video:
        reasons.append(
            f"video '{video['codec']}': "
            + ("browser can decode" if video_ok else "browser CANNOT decode"))
    else:
        reasons.append("no video stream found (audio-only file)")

    audio_tracks = _audio_track_plans(info.get("audio", []), caps)

    # Default audio: first natively-playable, else first wasm-decodable,
    # else first track (will need conversion).
    chosen = next((t for t in audio_tracks if t["nativePlayable"]), None)
    chosen_action = "native"
    if chosen is None:
        chosen = next((t for t in audio_tracks if t["wasmDecodable"]), None)
        chosen_action = "wasm-decode"
    if chosen is None and audio_tracks:
        chosen = audio_tracks[0]
        chosen_action = "remux-aac"

    playback = {"url": f"/stream/{info['file_id']}",
                "mime": CONTAINER_MIME.get(container, "application/octet-stream")}

    plan_base = {
        "fileId": info["file_id"],
        "capabilities": caps["name"],
        "reasons": reasons,
        "audioTracks": audio_tracks,
        "subtitles": info.get("subtitles", []),
        "probe": {
            "container": info.get("format_name", ""),
            "duration": info.get("duration", 0),
            "video": video, "probe_engine": info.get("probe_engine", "?"),
            "playability": info.get("playability", {}),
        },
    }

    def make(mode: str) -> dict:
        p = dict(plan_base)
        p["mode"] = mode
        p["implemented"] = mode in IMPLEMENTED_MODES
        p["playback"] = playback
        return p

    chosen_mode: str

    if not video and not audio_tracks:
        p = make("unsupported")
        p["implemented"] = False
        reasons.append("no playable streams at all")
        p["alternatives"] = []
        return p

    # -- priority 1: Direct Play ------------------------------------------
    if container_ok and video_ok and (not audio_tracks or chosen_action == "native"):
        p = make("direct-play")
        p["steps"] = [
            {"stream": "container", "action": "source",
             "detail": f"/stream proxies original bytes of the .{container}"},
            {"stream": "video", "action": "native", "detail": vcodec},
            *( [{"stream": "audio", "action": "native",
                 "detail": f"{chosen['codec']} @ track {chosen['index']}"}]
               if chosen else [] ),
        ]
        p["audio"] = ({"trackIndex": chosen["index"], "codec": chosen["codec"],
                       "action": "native"} if chosen else None)
        reasons.insert(0, "everything playable natively — original bytes go "
                          "straight to <video> (Direct Play)")
        chosen_mode = "direct-play"

    # -- priority 2: original bytes + client-side WASM audio decode --------
    elif container_ok and video_ok and chosen_action == "wasm-decode":
        p = make("wasm-audio")
        p["steps"] = [
            {"stream": "container", "action": "source",
             "detail": "original bytes; mediabunny demuxes in-browser"},
            {"stream": "video", "action": "native", "detail": vcodec},
            {"stream": "audio", "action": "wasm-decode",
             "detail": f"{chosen['codec']} decoded by ffmpeg.wasm → Web Audio"},
        ]
        p["audio"] = {"trackIndex": chosen["index"], "codec": chosen["codec"],
                      "action": "wasm-decode", "module": "/static/ac3-audio.js"}
        reasons.insert(0, "video plays directly; the browser can't decode "
                          f"'{chosen['codec']}' so the WASM engine decodes it "
                          "from the SAME original bytes (no re-encode)")
        chosen_mode = "wasm-audio"

    # -- priority 3: container remux (bitstreams copied) --------------------
    elif not container_ok and video_ok and all(
            t["nativePlayable"] for t in audio_tracks):
        p = make("container-remux")
        p["steps"] = [
            {"stream": "container", "action": "remux",
             "detail": "rewrap into MP4/fMP4 — bitstreams COPIED, zero re-encode"},
            {"stream": "video", "action": "copy", "detail": vcodec},
            {"stream": "audio", "action": "copy",
             "detail": ", ".join(t["codec"] for t in audio_tracks) or "none"},
        ]
        p["audio"] = None
        reasons.insert(0, "codecs are fine — only the container is incompatible; "
                          "a copy-only remux is the minimum intervention")
        chosen_mode = "container-remux"

    # -- priority 4: audio-only conversion ----------------------------------
    elif container_ok and video_ok and chosen and chosen_action == "remux-aac" \
            and chosen.get("remuxable", True):
        p = make("audio-remux")
        p["steps"] = [
            {"stream": "container", "action": "source", "detail": container},
            {"stream": "video", "action": "copy", "detail": vcodec},
            {"stream": "audio", "action": "remux-aac",
             "detail": "server-side one-time AAC conversion; video bitstream untouched"},
        ]
        p["audio"] = {"trackIndex": chosen["index"], "codec": chosen["codec"],
                      "action": "remux-aac",
                      "startUrl": f"/remux/{info['file_id']}",
                      "statusUrl": f"/remux/{info['file_id']}/status",
                      "urlTemplate": f"/remux/{info['file_id']}.mkv"}
        reasons.insert(0, "container and video are fine; audio "
                          f"'{chosen['codec']}' isn't decodable in-browser and is "
                          "outside the WASM scope — one-time audio→AAC remux")
        chosen_mode = "audio-remux"

    else:
        # container bad AND something else bad → composed direct-stream …
        # ── or the last resort ──────────────────────────────────────────────
        p = make("transcode")
        p["steps"] = [
            {"stream": "video", "action": "transcode",
             "detail": "full re-encode — LAST RESORT, not implemented"},
        ]
        p["audio"] = None
        reasons.insert(0, "no combination of the cheaper modes can play this — "
                          "full video transcode is the last resort")
        chosen_mode = "transcode"

    # -- alternatives: any OTHER implemented mode that would also work ------
    alternatives = []
    for alt_mode in MODES_BY_PRIORITY:
        if alt_mode == chosen_mode or alt_mode not in IMPLEMENTED_MODES:
            continue
        if alt_mode == "direct-play":
            continue  # would have been chosen if it worked
        if alt_mode == "audio-remux" and container_ok and video_ok \
                and any(t["remuxable"] for t in audio_tracks):
            alternatives.append({
                "mode": "audio-remux",
                "implemented": True,
                "label": "AAC-remux fallback (server converts audio once)",
                "startUrl": f"/remux/{info['file_id']}",
                "statusUrl": f"/remux/{info['file_id']}/status",
                "urlTemplate": f"/remux/{info['file_id']}.mkv",
            })
    p["alternatives"] = alternatives
    p["implementedModes"] = sorted(IMPLEMENTED_MODES)
    return p
