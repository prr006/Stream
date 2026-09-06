"""
boxparse — minimal container indexers for the playback decision engine.

Goal (docs/ARCHITECTURE.md §4): a KEYFRAME TIME INDEX for each media file,
built from a handful of HTTP Range reads — never a full-file scan:

* MP4: walk the top-level box list (headers only), fetch the `moov` box
  (KBs–few MB), read the video track's stss (sync samples) + stts (durations)
  → keyframe PTS list. Also reports moov position, fragmentation (moof) and
  CENC encryption markers.
* MKV: fetch the file tail, locate the Cues element, parse CuePoints →
  keyframe times (the video track = the one with the most cue points).

Both functions take an injected async fetcher:
    fetch(start, end) -> bytes        # inclusive, clamped to file size
so they are unit-testable against a local byte array and, in production,
backed by the Drive gateway.

These are best-effort indexers: any failure yields source="none" and the
engine falls back to a uniform segment grid (P1) — a missing index must
never block playback.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------
# MP4
# --------------------------------------------------------------------------

_BOGUS_SIZE_LIMIT = 1 << 40  # a box larger than 1 TB is a parse failure


def _box_header(data: bytes, i: int) -> tuple[int, bytes, int] | None:
    """At offset i → (box_size, box_type, header_len). None if malformed."""
    if i + 8 > len(data):
        return None
    size = int.from_bytes(data[i:i + 4], "big")
    btype = data[i + 4:i + 8]
    if size == 1:  # 64-bit size follows
        if i + 16 > len(data):
            return None
        size = int.from_bytes(data[i + 8:i + 16], "big")
        return size, btype, 16
    if size == 0:  # box runs to end of data
        size = len(data) - i
        return size, btype, 8
    if size < 8:
        return None
    return size, btype, 8


def _walk(data: bytes, start: int = 0, end: int | None = None) -> list[tuple[int, int, bytes]]:
    """Yield (offset, size, type) for boxes in data[start:end] (no nesting)."""
    if end is None:
        end = len(data)
    out = []
    i = start
    while i + 8 <= end:
        h = _box_header(data, i)
        if h is None:
            break
        size, btype, _ = h
        if size > _BOGUS_SIZE_LIMIT or i + size > end:
            break
        out.append((i, size, btype))
        i += size
    return out


def _sub(data: bytes, i: int, j: int, name: bytes) -> tuple[int, int] | None:
    for off, size, btype in _walk(data, i, j):
        if btype == name:
            hdr = 16 if data[off] == 0 and int.from_bytes(data[off:off + 4], "big") == 1 else 8
            return off, off + size
    return None


def _stts_durations(data: bytes, i: int, j: int) -> list[int]:
    """stts table → per-sample duration list (may be large; anime GOPs keep
    entries few). Returns [] on parse failure."""
    try:
        k = i + 8
        if data[k:k + 4] != b"\x00\x00\x00\x00":  # version+flags (assume v0)
            pass
        n = int.from_bytes(data[k + 4:k + 8], "big")
        if n > 10_000:  # absurd; refuse to build a 10k-entry table from bad data
            return []
        durations: list[int] = []
        p = k + 8
        for _ in range(n):
            cnt = int.from_bytes(data[p:p + 4], "big")
            dur = int.from_bytes(data[p + 4:p + 8], "big")
            if cnt > 10_000_000:
                return []
            durations.extend([dur] * cnt)
            p += 8
        return durations
    except (IndexError, ValueError):
        return []


def _stss_numbers(data: bytes, i: int, j: int) -> list[int]:
    try:
        n = int.from_bytes(data[i + 12:i + 16], "big")
        if n == 0 or n > 10_000_000:
            return []
        out = []
        p = i + 16
        for _ in range(n):
            out.append(int.from_bytes(data[p:p + 4], "big"))
            p += 4
        return out
    except (IndexError, ValueError):
        return []


def parse_mp4_keyframes(moov: bytes) -> dict:
    """
    moov bytes (including its own header) → {"times": [pts...], "count": n}.
    times are seconds (decode time of each sync sample).
    """
    result = {"times": [], "count": 0}
    # find the first video track (walk starts AFTER the moov's own header)
    trak_found = None
    for off, size, btype in _walk(moov, 8):
        if btype != b"trak":
            continue
        mdia = _sub(moov, off + 8, off + size, b"mdia")
        if not mdia:
            continue
        hdlr = _sub(moov, mdia[0] + 8, mdia[1], b"hdlr")
        if not hdlr:
            continue
        handler = moov[hdlr[0] + 16:hdlr[0] + 20]  # after version/flags+pre_defined
        if handler != b"vide":
            continue
        trak_found = (off, off + size, mdia)
        break
    if trak_found is None:
        return result

    _, _, (m0, m1) = trak_found
    mdhd = _sub(moov, m0 + 8, m1, b"mdhd")
    timescale = 1
    if mdhd:
        # v0: header(8)+version(1)+flags(3)+ctime(4)+mtime(4)+timescale(4)
        ts_at = mdhd[0] + 20
        timescale = int.from_bytes(moov[ts_at:ts_at + 4], "big") or 1

    minf = _sub(moov, m0 + 8, m1, b"minf")
    if not minf:
        return result
    stbl = _sub(moov, minf[0] + 8, minf[1], b"stbl")
    if not stbl:
        return result
    stts = _sub(moov, stbl[0] + 8, stbl[1], b"stts")
    stss = _sub(moov, stbl[0] + 8, stbl[1], b"stss")
    if not stts:
        return result
    durations = _stts_durations(moov, stts[0], stts[1])
    if not durations:
        return result
    sync = _stss_numbers(moov, stss[0], stss[1]) if stss else []
    if not sync:
        # no stss → every sample is a sync sample; too dense to be useful
        return result

    # dts of sample n (1-based) = sum(durations[0..n-2])
    dts: list[float] = [0.0]
    acc = 0
    for d in durations:
        acc += d
        dts.append(acc / timescale)
    times = []
    for n in sync:
        if 1 <= n <= len(dts):
            times.append(round(dts[n - 1], 4))
    result["times"] = times
    result["count"] = len(times)
    return result


async def analyze_mp4(fetch, file_size: int) -> dict:
    """
    Top-level walk (headers only) + moov fetch → keyframe times + container
    details. fetch(start, end) is inclusive.
    """
    out = {"source": "none", "times": [], "count": 0,
           "moov_position": None, "fragmented": False, "encrypted": False}
    head = await fetch(0, 7)
    if len(head) < 8:
        return out

    moov_span = None
    moov_pos = None
    seen_mdat = False
    offset = 0
    while offset + 8 <= file_size:
        hbytes = await fetch(offset, offset + 15)
        if len(hbytes) < 8:
            break
        h = _box_header(hbytes, 0)
        if h is None:
            break
        size, btype, _ = h
        if size > _BOGUS_SIZE_LIMIT or offset + size > file_size:
            break
        if btype == b"moof":
            out["fragmented"] = True
        if btype == b"mdat":
            seen_mdat = True
        if btype == b"moov" and moov_span is None:
            moov_span = (offset, offset + size)
            moov_pos = "end" if seen_mdat else "start"
        offset += size

    if moov_span is None:
        # moov at the end: fetch a tail window and scan backwards for a box
        # whose (header, size) reaches exactly the file end.
        for window in (128 * 1024, 1 * 1024 * 1024, 8 * 1024 * 1024):
            w = min(window, file_size)
            base = file_size - w
            data = await fetch(base, file_size - 1)
            found = False
            i = len(data) - 8
            while i >= 0:
                h = _box_header(data, i)
                if h is None:
                    i -= 4
                    continue
                size, btype, _ = h
                if btype == b"moov" and i + size == len(data):
                    moov_span = (base + i, file_size)
                    moov_pos = "end"
                    found = True
                    break
                i -= 4
            if found:
                break
            if w == file_size:
                break
    if moov_span is None:
        return out

    out["moov_position"] = moov_pos or "end"
    if moov_span[1] - moov_span[0] > 512 * 1024 * 1024:
        return out  # implausible moov — don't fetch
    moov = await fetch(moov_span[0], moov_span[1] - 1)
    if not moov:
        return out
    out["encrypted"] = (b"sinf" in moov) or (b"encv" in moov)
    parsed = parse_mp4_keyframes(moov)
    if parsed["count"]:
        out["source"] = "stss"
        out["times"] = parsed["times"]
        out["count"] = parsed["count"]
    return out


# --------------------------------------------------------------------------
# MKV (EBML)
# --------------------------------------------------------------------------

CUES_ID = 0x1C53BB6B
CUES_ID_BYTES = b"\x1c\x53\xbb\x6b"
CUEPOINT_ID = 0xBB
CUETIME_ID = 0xB3
CUE_TRACK_POSITIONS_ID = 0xB7
CUE_TRACK_ID = 0xF7          # IETF v4 "CueTrack" (the track number)
CUE_TRACK_ID_LEGACY = 0xB0   # pre-IETF "CueTrackNumber"
CUE_CLUSTER_POS_ID = 0xF1    # CueClusterPosition (absolute Segment Position)

INFO_ID_BYTES = b"\x15\x49\xa9\x66"
TIMESTAMP_SCALE_ID = 0x2AD7B1  # TimestampScale (ns per tick; default 1 ms)
TRACKS_ID_BYTES = b"\x16\x54\xae\x6b"
TRACK_ENTRY_ID = 0xAE
TRACK_NUMBER_ID = 0xD7
TRACK_TYPE_ID = 0x83
TRACK_TYPE_VIDEO = 1

_TIMESCALE_NS_DEFAULT = 1_000_000  # 1 ms — the spec default


def _ebml_id(data: bytes, i: int) -> tuple[int, int, int]:
    """
    EBML element ID at i → (id, id_len, next_i).

    ID length follows the same leading-zeros rule as vints but is capped at
    4 bytes: 1xxxxxxx → 1, 01xxxxxx → 2, 001xxxxx → 3, 0001xxxx → 4.
    (Reference: ebmlite.decodeIDLength.)
    """
    if i >= len(data) or data[i] == 0:
        raise ValueError("bad element id")
    b = data[i]
    if b >= 0x80:
        length = 1
    elif b >= 0x40:
        length = 2
    elif b >= 0x20:
        length = 3
    elif b >= 0x10:
        length = 4
    else:
        raise ValueError("element id too long")
    if i + length > len(data):
        raise ValueError("element id out of bounds")
    return int.from_bytes(data[i:i + length], "big"), length, i + length


def _vint(data: bytes, i: int) -> tuple[int, int]:
    """
    EBML vint at i → (value, next_i).

    Length = number of leading zero bits + 1 (the marker 1-bit starts the
    value): 1xxxxxxx → 1 byte, 01xxxxxx → 2, 001xxxxx → 3, …, 0x01 → 8.
    (Reference: ebmlite.decodeIntLength.)
    """
    if i >= len(data) or data[i] == 0:
        raise ValueError("bad vint")
    b = data[i]
    if b >= 0x80:
        length = 1
    elif b >= 0x40:
        length = 2
    elif b >= 0x20:
        length = 3
    elif b >= 0x10:
        length = 4
    elif b >= 0x08:
        length = 5
    elif b >= 0x04:
        length = 6
    elif b >= 0x02:
        length = 7
    else:
        length = 8
    if i + length > len(data):
        raise ValueError("vint out of bounds")
    val = b & (0xFF >> length)
    for j in range(1, length):
        val = (val << 8) | data[i + j]
    return val, i + length


def _parse_tracks(head: bytes) -> dict[int, int]:
    """
    head of the file → {TrackNumber: TrackType} (1=video, 2=audio,
    17=subtitle …). {} when the Tracks element isn't in the window.
    """
    for m in re.finditer(re.escape(TRACKS_ID_BYTES), head):
        c = m.start()
        try:
            esize, j = _vint(head, c + 4)
            end = j + esize
            if end > len(head):
                continue
            out: dict[int, int] = {}
            k = j
            while k + 2 < end:
                tid, tlen, k2 = _ebml_id(head, k)
                tsize, k3 = _vint(head, k2)
                tend = k3 + tsize
                if tend > end:
                    break
                if tid == TRACK_ENTRY_ID:
                    num = typ = None
                    p = k3
                    while p + 2 < tend:
                        eid, elen, p2 = _ebml_id(head, p)
                        esz, p3 = _vint(head, p2)
                        if p3 + esz > tend:
                            break
                        if eid == TRACK_NUMBER_ID:
                            num = int.from_bytes(head[p3:p3 + esz], "big")
                        elif eid == TRACK_TYPE_ID:
                            typ = int.from_bytes(head[p3:p3 + esz], "big")
                        p = p3 + esz
                    if num is not None and typ is not None:
                        out[num] = typ
                k = tend
            if out:
                return out
        except (ValueError, IndexError):
            continue
    return {}


def _parse_timescale(head: bytes) -> int:
    """Info/TimestampScale (ns per tick); spec default 1 000 000."""
    for m in re.finditer(re.escape(INFO_ID_BYTES), head):
        c = m.start()
        try:
            esize, j = _vint(head, c + 4)
            end = j + esize
            if end > len(head):
                continue
            k = j
            while k + 2 < end:
                eid, elen, k2 = _ebml_id(head, k)
                esz, k3 = _vint(head, k2)
                if k3 + esz > end:
                    break
                if eid == TIMESTAMP_SCALE_ID:
                    v = int.from_bytes(head[k3:k3 + esz], "big")
                    if 1 <= v <= 10**9:
                        return v
                k = k3 + esz
            break  # the first Info is enough
        except (ValueError, IndexError):
            continue
    return _TIMESCALE_NS_DEFAULT


def _try_parse_cues(data: bytes, track_map: dict[int, int],
                    timescale_ns: int) -> tuple[list[float] | None,
                                                list[int | None] | None]:
    """
    Look for a Cues element inside `data` (a tail window of the file, whose
    last byte is the file's last byte). Walks its children generically so
    unknown/leading elements are skipped rather than fatal.

    → (video keyframe times in seconds, cluster offsets for each keyframe)
    or (None, None). Cluster offsets power the cue-aware subtitle fast path.
    """
    candidates = [m.start() for m in re.finditer(re.escape(CUES_ID_BYTES), data)]
    for c in reversed(candidates):  # prefer the one at the file tail
        try:
            esize, j = _vint(data, c + 4)
            end = j + esize
            if end > len(data) or end < j + 4:
                continue
            # A real Cues block ends at (or within a few bytes of) the file
            # end in files written by standard muxers; this rejects e.g. a
            # SeekHead SeekID reference copy of the ID elsewhere in the file.
            if end < len(data) - 4:
                continue

            cue_tracks: dict[int, list[tuple[int, int | None]]] = {}
            k = j
            while k + 2 < end:
                pid, idlen, k2 = _ebml_id(data, k)
                psize, k3 = _vint(data, k2)
                pend = k3 + psize
                if pend > end:
                    break
                if pid == CUEPOINT_ID:
                    # A CuePoint may carry several CueTrackPositions — one
                    # per track that has a cue at this time (e.g. video
                    # keyframe + subtitle cue at the same second). Each is
                    # recorded independently.
                    t: int | None = None
                    pairs: list[tuple[int | None, int | None]] = []
                    m = k3
                    while m + 2 < pend:
                        eid, elen, m2 = _ebml_id(data, m)
                        esz, m3 = _vint(data, m2)
                        if m3 + esz > pend:
                            break
                        if eid == CUETIME_ID:
                            t = int.from_bytes(data[m3:m3 + esz], "big")
                        elif eid == CUE_TRACK_POSITIONS_ID:
                            track = cluster = None
                            q = m3
                            while q + 2 < m3 + esz:
                                tid, tlen, q2 = _ebml_id(data, q)
                                tsz, q3 = _vint(data, q2)
                                if q3 + tsz > m3 + esz:
                                    break
                                if tid in (CUE_TRACK_ID, CUE_TRACK_ID_LEGACY):
                                    track = int.from_bytes(
                                        data[q3:q3 + tsz], "big")
                                elif tid == CUE_CLUSTER_POS_ID:
                                    cluster = int.from_bytes(
                                        data[q3:q3 + tsz], "big")
                                q = q3 + tsz
                            pairs.append((track, cluster))
                        m = m3 + esz
                    if t is not None and 0 <= t < 10**7 and pairs:
                        for track, cluster in pairs:
                            # Some muxers omit the track number; group
                            # those cues under one default track.
                            cue_tracks.setdefault(
                                track if track is not None else 0,
                                []).append((t, cluster))
                k = pend
            if not cue_tracks:
                continue
            # Video track = TrackType video when the track map is known;
            # otherwise the track with the most cue points.
            video = [t for t in cue_tracks
                     if track_map.get(t) == TRACK_TYPE_VIDEO]
            if not video:
                video = [max(cue_tracks, key=lambda t: len(cue_tracks[t]))]
            pts = sorted(cue_tracks[video[0]])
            if not pts:
                continue
            times = [round(x * timescale_ns / 1e9, 4) for x, _ in pts]
            clusters = [c for _, c in pts]
            return times, clusters
        except (ValueError, IndexError):
            continue
    return None, None


async def parse_mkvcues(fetch, file_size: int,
                        max_window: int = 16 * 1024 * 1024
                        ) -> tuple[list[float] | None, list[int | None] | None]:
    """
    Fetch a small head window (Tracks + Info) and the file tail in expanding
    windows until the Cues element is found.
    Returns (video keyframe times in seconds, cluster offsets), or (None, None).
    """
    head = await fetch(0, min(file_size, 256 * 1024) - 1)
    track_map = _parse_tracks(head) if head else {}
    timescale = _parse_timescale(head) if head else _TIMESCALE_NS_DEFAULT

    window = min(file_size, 4 * 1024 * 1024)
    while True:
        base = file_size - window
        data = await fetch(base, file_size - 1)
        times, clusters = _try_parse_cues(data, track_map, timescale)
        if times is not None:
            return times, clusters
        if window >= file_size or window >= max_window:
            return None, None
        window = min(file_size, window * 4)
