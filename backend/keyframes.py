"""
keyframes — build the per-file keyframe TIME INDEX from a few Range reads.

Orchestrates boxparse over the file's actual container:
* mp4/mov → stss via the moov box
* mkv     → Cues element from the file tail

The index feeds the PlaybackDecision/segment-origin layer (P1): segments are
keyframe-aligned, and a static DASH SegmentTimeline can be generated BEFORE
any segment exists (docs/ARCHITECTURE.md §7.1).

Best-effort: any failure yields source="none" (uniform-grid fallback later),
never an exception.
"""
from __future__ import annotations

import boxparse
from engine import container_key

MAX_INDEX_TIME_S = 24 * 3600  # refuse absurd indexes


async def build_keyframe_index(info: dict, fetch, file_size: int) -> dict:
    """
    info  — normalized probe dict (needs format_name)
    fetch — async (start, end) -> bytes, inclusive, clamped to file size
    """
    out = {"source": "none", "count": 0, "times": [], "error": None,
           "moov_position": None, "fragmented": False, "encrypted": False}
    if not file_size:
        out["error"] = "no file size known"
        return out

    container = container_key(info.get("format_name", ""))
    try:
        if container in ("mp4", "mov"):
            r = await boxparse.analyze_mp4(fetch, file_size)
            out.update({k: r.get(k, out[k]) for k in
                        ("moov_position", "fragmented", "encrypted")})
            if r["count"] and r["times"][-1] < MAX_INDEX_TIME_S:
                out["source"] = r["source"]
                out["times"] = r["times"]
                out["count"] = r["count"]
            elif r["count"]:
                out["error"] = "implausible keyframe times (clock skew?)"
        elif container == "mkv":
            times = await boxparse.parse_mkvcues(fetch, file_size)
            if times and times[-1] < MAX_INDEX_TIME_S:
                out["source"] = "cues"
                out["times"] = times
                out["count"] = len(times)
    except Exception as e:  # noqa: BLE001 — the index is best-effort by design
        out["error"] = str(e)[:200]
        out["source"] = "none"
        out["times"] = []
        out["count"] = 0
    return out
