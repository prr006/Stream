"""
Media probing and subtitle extraction helpers (ffmpeg/ffprobe based).

Design notes
------------
* We never transcode. Probing reads only the container headers (KBs) over HTTP
  Range requests; subtitle extraction demuxes the file and converts *text*
  subtitle tracks (subrip/ass/webvtt) to WebVTT. Image-based subtitle tracks
  (PGS/VobSub/DVB) are detected but reported as unsupported (they'd need OCR).
* ffmpeg/ffprobe read the Drive `alt=media` URL directly over HTTP; the
  `Authorization: Bearer` header is passed via `-headers`. Range seeking is
  native to ffmpeg's HTTP demuxer, which Drive honors with 206 responses.
* ffprobe (JSON output) is preferred when installed. If it's missing we fall
  back to parsing `ffmpeg -i` stderr, so an ffmpeg-only environment still
  works (e.g. the pip `imageio-ffmpeg` binary, which has no ffprobe).
"""
import asyncio
import json
import os
import re
import shutil
from pathlib import Path

from starlette.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

# ---------------------------------------------------------------------------
# Binary resolution (lazy so tests can run with only the bundled binary)
# ---------------------------------------------------------------------------

def ffmpeg_bin() -> str | None:
    if os.getenv("FFMPEG_BIN"):
        return os.environ["FFMPEG_BIN"]
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    try:  # bundled static binary from the pip package (no ffprobe included)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffprobe_bin() -> str | None:
    if os.getenv("FFPROBE_BIN"):
        return os.environ["FFPROBE_BIN"]
    return shutil.which("ffprobe")


class ProbeError(RuntimeError):
    pass


class ExtractError(RuntimeError):
    pass


class FFmpegMissingError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Codec classification (what Chrome/Edge can decode inside MKV/MP4 natively)
# ---------------------------------------------------------------------------

OK_VIDEO = {"h264", "vp8", "vp9", "av1", "mpeg4"}
RISKY_VIDEO = {"hevc", "mpeg2video", "mpeg1video", "vc1", "wmv3", "theora", "prores"}
OK_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le"}
RISKY_AUDIO = {"ac3", "eac3", "dts", "dca", "truehd", "mlp", "pcm_bluray"}

WEB_OK_SUBS = {"subrip", "ass", "ssa", "webvtt", "mov_text"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "xsub", "dvb_subtitle", "dvb_teletext"}


def media_url(drive_api_base: str, file_id: str) -> str:
    return f"{drive_api_base}/files/{file_id}?alt=media&acknowledgeAbuse=true"


def auth_header(token: str) -> str:
    # ffmpeg expects CRLF-terminated header block
    return f"Authorization: Bearer {token}\r\n"


def _safe_id(file_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", file_id)


async def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProbeError(f"command timed out after {timeout}s: {cmd[0]}")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

async def probe_media(file_id: str, token: str, drive_api_base: str) -> dict:
    """
    Return normalized media info:
      {file_id, format_name, duration, video, audio[], subtitles[], probe_engine}
    """
    url = media_url(drive_api_base, file_id)
    headers = auth_header(token)

    fb = ffprobe_bin()
    if fb:
        rc, out, err = await _run(
            [fb, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", "-headers", headers, url],
            timeout=90,
        )
        if rc == 0 and out.strip().startswith("{"):
            return normalize_ffprobe(json.loads(out), file_id)
        # fall through to the ffmpeg parser rather than dying outright

    ff = ffmpeg_bin()
    if not ff:
        raise FFmpegMissingError(
            "Neither ffprobe nor ffmpeg found. Install ffmpeg, or "
            "`pip install imageio-ffmpeg` for a bundled ffmpeg binary."
        )
    rc, _out, err = await _run(
        [ff, "-hide_banner", "-nostdin", "-headers", headers, "-i", url],
        timeout=90,
    )
    info = parse_ffmpeg_stderr(err, file_id)
    if info["video"] is None and not info["audio"] and not info["subtitles"]:
        raise ProbeError(f"could not parse any streams. ffmpeg said: {err[-400:]}")
    return info


def normalize_ffprobe(raw: dict, file_id: str) -> dict:
    fmt = raw.get("format", {})
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0.0

    info = {
        "file_id": file_id,
        "format_name": fmt.get("format_name", ""),
        "duration": duration,
        "video": None,
        "audio": [],
        "subtitles": [],
        "probe_engine": "ffprobe",
    }
    for s in raw.get("streams", []):
        codec = (s.get("codec_name") or "").lower()
        entry = {
            "index": s.get("index"),
            "codec": codec,
            "language": s.get("tags", {}).get("language") or "und",
            "title": s.get("tags", {}).get("title") or "",
        }
        if s.get("codec_type") == "video" and info["video"] is None:
            entry["width"] = s.get("width")
            entry["height"] = s.get("height")
            info["video"] = entry
        elif s.get("codec_type") == "audio":
            entry["channels"] = s.get("channel_layout") or s.get("channels")
            info["audio"].append(entry)
        elif s.get("codec_type") == "subtitle":
            _finalize_subtitle(entry)
            info["subtitles"].append(entry)
    return info


_STREAM_RE = re.compile(
    r"Stream #0:(\d+)(?:\(([^)]*)\))?: (Video|Audio|Subtitle): ([A-Za-z0-9_]+)(.*)")
_INPUT_RE = re.compile(r"Input #0,\s*([^,]+(?:,[^,]+)*?),\s*from")
_DURATION_RE = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_DIM_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_TITLE_RE = re.compile(r"^\s+title\s*:\s+(.*\S)\s*$")


def parse_ffmpeg_stderr(text: str, file_id: str) -> dict:
    """Fallback parser for `ffmpeg -i <url>` stderr output."""
    info = {
        "file_id": file_id,
        "format_name": "",
        "duration": 0.0,
        "video": None,
        "audio": [],
        "subtitles": [],
        "probe_engine": "ffmpeg-stderr",
    }
    current: dict | None = None
    for line in text.splitlines():
        m = _INPUT_RE.search(line)
        if m:
            info["format_name"] = m.group(1).strip()
            current = None
            continue
        m = _DURATION_RE.search(line)
        if m:
            h, mnt, s = m.groups()
            info["duration"] = int(h) * 3600 + int(mnt) * 60 + float(s)
            continue
        m = _STREAM_RE.search(line)
        if m:
            idx, lang, kind, codec, rest = m.groups()
            entry = {
                "index": int(idx),
                "codec": codec.lower(),
                "language": lang or "und",
                "title": "",
            }
            kind = kind.lower()
            if kind == "video" and info["video"] is None:
                dim = _DIM_RE.search(rest)
                if dim:
                    entry["width"], entry["height"] = int(dim.group(1)), int(dim.group(2))
                info["video"] = entry
            elif kind == "audio":
                ch = re.search(r"(mono|stereo|\d+\.\d+)", rest)
                if ch:
                    entry["channels"] = ch.group(1)
                info["audio"].append(entry)
            elif kind == "subtitle":
                _finalize_subtitle(entry)
                info["subtitles"].append(entry)
            else:
                current = None
                continue
            current = entry
            continue
        m = _TITLE_RE.match(line)
        if m and current is not None and not current.get("title"):
            current["title"] = m.group(1)
    return info


def _finalize_subtitle(entry: dict) -> None:
    codec = entry["codec"]
    entry["web_compatible"] = codec in WEB_OK_SUBS
    if codec in IMAGE_SUBS:
        entry["note"] = "image-based subtitles — cannot be converted to WebVTT without OCR"
    elif entry["web_compatible"]:
        entry["note"] = ""
    else:
        entry["note"] = f"unrecognized subtitle codec '{codec}' — extraction may fail"


# ---------------------------------------------------------------------------
# Playability assessment (server-side heuristic; browser re-checks too)
# ---------------------------------------------------------------------------

def assess_playability(info: dict) -> dict:
    warnings: list[str] = []
    fmt = info.get("format_name", "")

    if "matroska" in fmt or "webm" in fmt:
        browser_note = ("MKV/WebM plays natively in Chrome and Edge. "
                        "Safari and Firefox cannot play MKV in <video> — use Chrome/Edge.")
    elif any(t in fmt for t in ("mp4", "mov")):
        browser_note = "MP4/MOV container — plays natively in all modern browsers."
    else:
        browser_note = f"Unusual container '{fmt or 'unknown'}' — playback may fail."

    video = info.get("video")
    if video:
        vc = video["codec"]
        if vc in RISKY_VIDEO:
            warnings.append(
                f"Video codec '{vc}' is not decodable by most browsers (common for "
                "HEVC/MPEG-2 anime encodes). Direct play will fail — a future "
                "transcode/remux milestone would be needed.")
        elif vc not in OK_VIDEO:
            warnings.append(f"Video codec '{vc}' is untested — playback may fail.")

    for a in info.get("audio", []):
        ac = a["codec"]
        label = a.get("title") or a.get("language") or f"stream {a['index']}"
        if ac in RISKY_AUDIO:
            warnings.append(
                f"Audio '{label}' uses '{ac}', which browsers usually cannot decode — "
                "video may be silent (a cheap audio-only AAC remux can fix this later).")
        elif ac.startswith("pcm_"):
            warnings.append(f"Audio '{label}' uses raw PCM ('{ac}') — large and often unplayable.")

    for s in info.get("subtitles", []):
        if s["codec"] in IMAGE_SUBS:
            label = s.get("title") or s.get("language") or f"stream {s['index']}"
            warnings.append(f"Subtitle '{label}' is image-based ({s['codec']}) — shown as unsupported.")

    return {
        "container": fmt,
        "browser_note": browser_note,
        "video_blocked": any("Video codec" in w for w in warnings),
        # True when some audio track can't be decoded by browsers -> offer the
        # audio-only AAC remux endpoint as the cheap fix (video stays untouched).
        "needs_audio_remux": any(("silent" in w) or ("PCM" in w) for w in warnings),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Subtitle extraction -> WebVTT (cached on disk)
# ---------------------------------------------------------------------------

async def extract_subtitle(
    file_id: str,
    stream_index: int,
    token: str,
    drive_api_base: str,
    cache_dir: Path,
    timeout: float = 1800,
) -> Path:
    """
    Demux one subtitle track from the Drive-hosted file and convert to WebVTT.

    COST: MKV interleaves subtitle blocks with A/V, so the FIRST extraction of
    a (file, track) pair reads the whole file sequentially from Drive (server
    side, demux-only — no video decode). The result (~100 KB) is cached; every
    later request is served from cache instantly.
    """
    ff = ffmpeg_bin()
    if not ff:
        raise FFmpegMissingError(
            "ffmpeg not found. Install ffmpeg or `pip install imageio-ffmpeg`.")

    dest = Path(cache_dir) / f"{_safe_id(file_id)}_{stream_index}.vtt"
    if dest.exists():
        return dest

    cmd = [
        ff, "-hide_banner", "-nostdin", "-v", "warning",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-headers", auth_header(token),
        "-i", media_url(drive_api_base, file_id),
        "-map", f"0:{stream_index}",
        "-c:s", "webvtt", "-f", "webvtt", "pipe:1",
    ]
    rc, out, err = await _run(cmd, timeout)
    if rc != 0:
        raise ExtractError(f"ffmpeg exited {rc}: {err[-500:]}")
    if not out.strip().startswith("WEBVTT"):
        raise ExtractError(f"unexpected subtitle output: {out[:200]!r} {err[-300:].strip()}")

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, dest)  # atomic publish
    return dest


# ---------------------------------------------------------------------------
# Audio-only remux: copy video bitstream, convert audio to AAC
# ---------------------------------------------------------------------------

async def remux_audio_aac(
    file_id: str,
    token: str,
    drive_api_base: str,
    part_path: Path,
    final_path: Path,
    timeout: float = 1800,
) -> None:
    """
    Remux a Drive-hosted file into MKV with the video bitstream COPIED
    (-c:v copy: no re-encode of HEVC/etc.) and every audio track converted to
    AAC so browsers can play sound. Subtitle streams are copied too.

    Cost: one full sequential read of the file from Drive (demux + audio encode
    only — audio encode is negligible CPU). Output cached at final_path.
    Raises ExtractError on failure.
    """
    ff = ffmpeg_bin()
    if not ff:
        raise FFmpegMissingError(
            "ffmpeg not found. Install ffmpeg or `pip install imageio-ffmpeg`.")

    cmd = [
        ff, "-hide_banner", "-nostdin", "-v", "error",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-headers", auth_header(token),
        "-i", media_url(drive_api_base, file_id),
        "-map", "0:v:0", "-c:v", "copy",        # video bitstream untouched
        "-map", "0:a", "-c:a", "aac", "-b:a", "192k",  # ALL audio -> AAC
        "-map", "0:s?", "-c:s", "copy",         # keep embedded subtitle streams
        "-f", "matroska", "-y", str(part_path),
    ]
    rc, _out, err = await _run(cmd, timeout)
    if rc != 0:
        part_path.unlink(missing_ok=True)
        raise ExtractError(f"remux failed (ffmpeg exit {rc}): {err[-500:]}")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part_path, final_path)  # atomic publish


# ---------------------------------------------------------------------------
# Local file serving with HTTP Range support (for the on-disk remux cache)
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def ranged_file_response(path: Path, range_header: str | None, media_type: str) -> Response:
    """Serve a local file honoring `Range: bytes=...` (Starlette's FileResponse
    does not do range handling reliably across versions, so we do it ourselves)."""
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}

    if range_header:
        m = _RANGE_RE.fullmatch(range_header.strip())
        if not m or (not m.group(1) and not m.group(2)):
            return PlainTextResponse(
                "Malformed Range header", 416,
                {"Content-Range": f"bytes */{size}"})
        if m.group(1) == "":
            n = int(m.group(2))
            start, end = max(0, size - n), size - 1
        else:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
        if start > end or start >= size:
            return PlainTextResponse(
                "Range Not Satisfiable", 416,
                {"Content-Range": f"bytes */{size}"})

        length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)

        def gen():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(gen(), status_code=206,
                                 media_type=media_type, headers=headers)

    return FileResponse(path, media_type=media_type, headers=headers)
