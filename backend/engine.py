"""
Media engine v2 — the decision layer between "what the file is" (media.probe)
and "how it should reach the screen".

Jellyfin/Plex-style Direct Play / Direct Stream / Transcode model, ordered by
MINIMUM intervention (docs/ARCHITECTURE.md §6):

    1. direct-play       original bytes, browser decodes everything
    2. container-remux   only the container is wrong (bitstreams copied)
    3. audio-transcode   video plays, audio doesn't → video copied, audio → AAC
    4. transcode         full video transcode — LAST RESORT

v2 changes vs the POC engine (milestone 3, see docs/ARCHITECTURE-poc-m3.md):
  * `wasm-audio` is REMOVED from the ladder (client-side WASM decode was a
    browser workaround; the server-side audio-only transcode is the correct
    minimum operation and is cacheable — §13 of the architecture doc).
  * rungs 2–4 emit DASH/CMAF plans (static manifest URLs with a stable
    `plan` id) instead of the whole-file remux job; the segment origin that
    serves them lands in P1 (DASH_ORIGIN_ENABLED gates the `implemented` flag
    so plans stay honest during the transition).
  * MKV is NEVER direct-played, even when a browser claims mkv support
    (Chrome's MKV sniffing is build/codec dependent and invisible to
    canPlayType — exactly the instability the WASM hack existed for).
  * Fragmented MP4 (moof in body) is not direct-playable either.

`decide()` remains a PURE function of (probe info, client capabilities) so the
whole matrix is unit-testable and the UI only renders the plan.

Plan shape (JSON) — contract preserved from the POC (pages render plans,
never codecs):

    {
      "mode": "audio-transcode",
      "implemented": true,
      "reasons": ["..."],
      "steps": [{"stream": "container", "action": "…", …}, …],
      "playback": {"type": "dash", "url": "/dash/<id>/<plan>/manifest.mpd",
                   "plan": "aac-1-und"},
      "audio": {"trackIndex": 1, "codec": "ac3", "action": "convert-aac"},
      "audioTracks": [ …per-track chooser data… ],
      "subtitles": [ …passthrough from /probe… ],
      "probe": { …summary echo… },
      "alternatives": [ {"mode": "transcode", "implemented": true, …}, … ],
      "implementedModes": [ … ]
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

# Legacy reference only (P0): the in-browser WASM decoder's old coverage.
# No longer used by the ladder — kept so old plans/captures stay explainable.
WASM_DECODABLE_AUDIO = {"ac3", "eac3"}

# Audio codecs the server-side AAC conversion handles.
CONVERTIBLE_AUDIO = WASM_DECODABLE_AUDIO | {
    "aac", "mp3", "opus", "vorbis", "flac", "dts", "truehd", "pcm_s16le",
}

CONTAINER_MIME = {
    "mkv": "video/x-matroska", "webm": "video/webm", "mp4": "video/mp4",
    "mov": "video/quicktime", "ogg": "video/ogg",
}

# Containers the browser will NOT direct-play, even when canPlayType claims
# otherwise (Chrome's MKV sniffing is unreliable; see module docstring).
NO_DIRECT_CONTAINERS = {"mkv"}

# --------------------------------------------------------------------------
# Client capabilities
# --------------------------------------------------------------------------

# Conservative assumption for clients that don't report (server-side default):
# modern desktop browser, MP4/WebM only, mainstream codecs.
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
# The decision ladder (v2)
# --------------------------------------------------------------------------

MODES_BY_PRIORITY = ["direct-play", "container-remux", "audio-transcode",
                     "transcode"]

# The DASH segment origin (P1) serves the remux/audio/transcode rungs.
# Until it is enabled, those plans are emitted but flagged implemented=False
# so clients (and the /plan debug surface) stay honest.
DASH_ORIGIN_ENABLED: bool = False


def implemented_modes() -> set[str]:
    modes = {"direct-play"}
    if DASH_ORIGIN_ENABLED:
        modes |= {"container-remux", "audio-transcode", "transcode"}
    return modes


def dash_plan_id(config: str, track_index, lang: str) -> str:
    """Stable per-(config, audio track, language) id → segment URL namespace
    (shared across sessions/viewers; see docs §7.1)."""
    return f"{config}-{track_index if track_index is not None else 'x'}-{lang or 'und'}"


def _dash_playback(file_id: str, plan_id: str) -> dict:
    return {"type": "dash", "plan": plan_id,
            "url": f"/dash/{file_id}/{plan_id}/manifest.mpd"}


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
            # legacy field (pre-v2 WASM era) — kept for plan compatibility
            "wasmDecodable": codec in WASM_DECODABLE_AUDIO
            and caps["features"]["wasmAudio"],
            "convertible": codec in CONVERTIBLE_AUDIO,
        })
    return out


def decide(info: dict, caps: dict | None) -> dict:
    """Pure decision: probe info + client capabilities → playback plan."""
    caps = normalize_caps(caps)
    reasons: list[str] = []

    container = container_key(info.get("format_name", ""))
    container_reported = bool(caps["containers"].get(container))
    fragmented = bool(info.get("fragmented"))
    container_direct = (container_reported
                        and container not in NO_DIRECT_CONTAINERS
                        and not fragmented)
    if container in NO_DIRECT_CONTAINERS and container_reported:
        reasons.append(f"container '{container}': browser claims support, but "
                       "MKV sniffing is unreliable — never direct-played "
                       "(copy-only remux instead)")
    else:
        reasons.append(
            f"container '{info.get('format_name', '?')}' → '{container}': "
            + ("direct-playable" if container_direct else "NOT directly playable"))
    if fragmented:
        reasons.append("fragmented MP4 (moof in body) — not progressive-"
                       "playable; remux required")

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

    # Default audio: first natively-playable track, else the first track that
    # can be server-converted (or the first, for the transcode rung).
    chosen = next((t for t in audio_tracks if t["nativePlayable"]), None)
    if chosen is None and audio_tracks:
        chosen = next((t for t in audio_tracks if t["convertible"]),
                      audio_tracks[0])
    chosen_action = "native" if (chosen and chosen["nativePlayable"]) \
        else "convert-aac"

    file_id = info["file_id"]

    def playback_for(mode: str) -> dict:
        if mode == "direct-play":
            return {"type": "range", "url": f"/stream/{file_id}",
                    "mime": CONTAINER_MIME.get(container,
                                               "application/octet-stream")}
        if mode == "container-remux":
            plan = dash_plan_id("copy", chosen["index"] if chosen else None,
                                chosen["language"] if chosen else "und")
            return _dash_playback(file_id, plan)
        if mode == "audio-transcode":
            plan = dash_plan_id("aac", chosen["index"] if chosen else None,
                                chosen["language"] if chosen else "und")
            return _dash_playback(file_id, plan)
        plan = dash_plan_id("h264aac", chosen["index"] if chosen else None,
                            chosen["language"] if chosen else "und")
        return _dash_playback(file_id, plan)

    plan_base = {
        "fileId": file_id,
        "capabilities": caps["name"],
        "reasons": reasons,
        "audioTracks": audio_tracks,
        "subtitles": info.get("subtitles", []),
        "probe": {
            "container": info.get("format_name", ""),
            "containerKey": container,
            "duration": info.get("duration", 0),
            "size": info.get("size", 0),
            "video": video,
            "probe_engine": info.get("probe_engine", "?"),
            "playability": info.get("playability", {}),
            "keyframes": (info.get("keyframes") or {}).get("source", "none"),
        },
    }

    def make(mode: str) -> dict:
        p = dict(plan_base)
        p["mode"] = mode
        p["implemented"] = mode in implemented_modes()
        p["playback"] = playback_for(mode)
        # POC contract: "audio" = chosen-track chooser data (or null).
        actions = {"direct-play": "native", "container-remux": "copy",
                   "audio-transcode": "convert-aac", "transcode": "transcode-aac"}
        p["audio"] = ({"trackIndex": chosen["index"], "codec": chosen["codec"],
                       "action": actions[mode]} if chosen else None)
        return p

    def steps_for(mode: str) -> list[dict]:
        if mode == "direct-play":
            return [
                {"stream": "container", "action": "source",
                 "detail": f"/stream proxies original bytes of the .{container}"},
                {"stream": "video", "action": "native", "detail": vcodec},
                *([{"stream": "audio", "action": "native",
                    "detail": f"{chosen['codec']} @ track {chosen['index']}"}]
                  if chosen else []),
            ]
        if mode == "container-remux":
            return [
                {"stream": "container", "action": "remux",
                 "detail": "copy-only repack to DASH/CMAF — zero re-encode"},
                {"stream": "video", "action": "copy", "detail": vcodec},
                {"stream": "audio", "action": "copy",
                 "detail": ", ".join(t["codec"] for t in audio_tracks) or "none"},
            ]
        if mode == "audio-transcode":
            return [
                {"stream": "container", "action": "remux",
                 "detail": "DASH/CMAF (segments)"} if not container_direct
                else {"stream": "container", "action": "source",
                      "detail": container},
                {"stream": "video", "action": "copy",
                 "detail": f"{vcodec} bitstream untouched"},
                {"stream": "audio", "action": "convert-aac",
                 "detail": (f"{chosen['codec']} → AAC once (cached)"
                            if chosen else "none")},
            ]
        return [
            {"stream": "video", "action": "transcode",
             "detail": "full re-encode (H.264) — LAST RESORT"},
            {"stream": "audio", "action": "convert-aac",
             "detail": "AAC"},
        ]

    if not video and not audio_tracks:
        p = make("unsupported")
        p["implemented"] = False
        p["steps"] = []
        reasons.append("no playable streams at all")
        p["alternatives"] = []
        p["implementedModes"] = sorted(implemented_modes())
        return p

    # -- rung 1: Direct Play ----------------------------------------------
    if container_direct and video_ok and (not audio_tracks
                                          or chosen_action == "native"):
        chosen_mode = "direct-play"
        reasons.insert(0, "everything playable natively — original bytes go "
                          "straight to <video> (Direct Play)")

    # -- rung 2: container remux (all streams OK, container wrong) ---------
    elif video_ok and all(t["nativePlayable"] for t in audio_tracks):
        chosen_mode = "container-remux"
        reasons.insert(0, "codecs play natively — only the container is "
                          "incompatible; a copy-only remux is the minimum "
                          "intervention (no re-encode)")

    # -- rung 3: audio-only transcode (video OK, audio needs conversion) ---
    elif video_ok and audio_tracks and chosen_action == "convert-aac":
        chosen_mode = "audio-transcode"
        reasons.insert(0,
                       f"video '{vcodec}' plays natively and is copied "
                       f"untouched; audio '{chosen['codec']}' is not "
                       "decodable in this browser — one-time AAC conversion "
                       "(cached; this replaces any manual 'audio fix')")

    # -- rung 4: full transcode (video itself unplayable) ------------------
    else:
        chosen_mode = "transcode"
        reasons.insert(0, "the video codec itself is not decodable in this "
                          "browser — full transcode (H.264 + AAC), the last "
                          "resort")

    p = make(chosen_mode)
    p["steps"] = steps_for(chosen_mode)

    # -- alternatives: other implemented rungs that would also work ---------
    # (client fallback chain if the chosen rung fails at runtime, e.g. an MSE
    #  codec surprise; ordered cheapest-first)
    candidates = []
    if video_ok and all(t["nativePlayable"] for t in audio_tracks):
        candidates.append("container-remux")
    if video_ok and audio_tracks:
        candidates.append("audio-transcode")
    if video:
        candidates.append("transcode")
    implemented = implemented_modes()
    alternatives = []
    for m in candidates:
        if m == chosen_mode or m not in implemented:
            continue
        alt_playback = playback_for(m)
        alternatives.append({
            "mode": m,
            "implemented": True,
            "label": {
                "container-remux": "copy-only remux (no re-encode)",
                "audio-transcode": "video copy + AAC audio",
                "transcode": "full transcode (H.264 + AAC)",
            }[m],
            "playback": alt_playback,
        })
    p["alternatives"] = alternatives
    p["implementedModes"] = sorted(implemented_modes())
    return p
