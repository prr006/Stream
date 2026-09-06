"""
Minimal Google Drive video streaming POC.

Flow:
  1. /auth/login     -> Google consent screen (scope: drive.readonly, offline access)
  2. /auth/callback  -> exchange code for tokens, persist refresh token to token.json
  3. /files          -> list the user's video files
  4. /stream/{id}    -> proxy bytes from Drive, forwarding HTTP Range headers
                        so the browser <video> element can buffer and seek.

No Google client library is used; everything is plain HTTP via httpx so the
mechanics (token refresh, alt=media download, Range passthrough) stay visible.
"""
import asyncio
import json
import os
import time
import urllib.parse
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import media

BACKEND_DIR = Path(__file__).resolve().parent
BASE_DIR = BACKEND_DIR.parent

load_dotenv(BACKEND_DIR / ".env")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")

# Overridable for tests (lets us point at a fake "Drive" upstream).
DRIVE_API_BASE = os.getenv("DRIVE_API_BASE", "https://www.googleapis.com/drive/v3")
TOKEN_FILE = Path(os.getenv("TOKEN_FILE", str(BASE_DIR / "token.json")))

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"

FRONTEND_DIR = BASE_DIR / "frontend"

# On-disk caches (drive file id keyed). Subtitles are tiny; probe json a few KB.
CACHE_DIR = Path(os.getenv("CACHE_DIR", str(BASE_DIR / "cache")))
PROBE_CACHE = CACHE_DIR / "probe"
SUBS_CACHE = CACHE_DIR / "subs"
REMUX_DIR = CACHE_DIR / "remux"
for _d in (PROBE_CACHE, SUBS_CACHE, REMUX_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Subtitle extraction reads the whole file server-side on first request.
EXTRACT_TIMEOUT_S = int(os.getenv("EXTRACT_TIMEOUT_S", "1800"))

app = FastAPI(title="Stream POC")

_locks: dict[str, asyncio.Lock] = {}

# Background audio-remux job state (single-process POC; rebuilt on restart
# from the presence of cached files).
REMUX_JOBS: dict[str, dict] = {}


def _lock(key: str) -> asyncio.Lock:
    """Per-resource async lock so we never run duplicate ffmpeg extractions."""
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def _remux_paths(file_id: str) -> tuple[Path, Path]:
    safe = media._safe_id(file_id)
    return REMUX_DIR / f"{safe}.part", REMUX_DIR / f"{safe}.mkv"


# --------------------------------------------------------------------------
# Token storage / refresh
# --------------------------------------------------------------------------

def load_tokens() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    return json.loads(TOKEN_FILE.read_text())


def save_tokens(tokens: dict) -> None:
    # Plaintext is fine for a single-user local POC; do NOT do this multi-user.
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))


async def get_access_token() -> str | None:
    """Return a valid access token, transparently refreshing it if expired."""
    tokens = load_tokens()
    if not tokens:
        return None
    if tokens.get("expires_at", 0) - 30 > time.time():
        return tokens["access_token"]

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return None

    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
    if resp.status_code != 200:
        return None

    data = resp.json()
    tokens["access_token"] = data["access_token"]
    tokens["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    save_tokens(tokens)
    return tokens["access_token"]


def require_config() -> None:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            500,
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set. "
            "Copy backend/.env.example to backend/.env and fill them in.",
        )


# --------------------------------------------------------------------------
# OAuth flow
# --------------------------------------------------------------------------

@app.get("/auth/login")
def auth_login():
    require_config()
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # request a refresh token
        "prompt": "consent",        # force consent so we ALWAYS get a refresh token
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.get("/auth/callback")
async def auth_callback(code: str = "", error: str = ""):
    require_config()
    if error:
        raise HTTPException(400, f"Google returned an error: {error}")

    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": OAUTH_REDIRECT_URI,
        })
    if resp.status_code != 200:
        raise HTTPException(400, f"Token exchange failed: {resp.text}")

    tokens = resp.json()
    # With prompt=consent a refresh_token is normally (re)issued; keep the old
    # one as a fallback if Google withholds it.
    tokens.setdefault("refresh_token", (load_tokens() or {}).get("refresh_token", ""))
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600) - 60
    save_tokens(tokens)
    return RedirectResponse("/")


@app.get("/auth/status")
def auth_status():
    tokens = load_tokens()
    return {
        "authorized": bool(tokens and (tokens.get("access_token") or tokens.get("refresh_token")))
    }


@app.get("/auth/logout")
def auth_logout():
    TOKEN_FILE.unlink(missing_ok=True)
    return {"authorized": False}


# --------------------------------------------------------------------------
# Drive API
# --------------------------------------------------------------------------

async def require_token() -> str:
    token = await get_access_token()
    if not token:
        raise HTTPException(401, "Not authorized with Google. Visit /auth/login")
    return token


@app.get("/files")
async def list_videos():
    """List video files visible to the authorized account."""
    token = await require_token()
    params = {
        "q": "mimeType contains 'video/' and trashed = false",
        "fields": "files(id,name,mimeType,size,modifiedTime)",
        "pageSize": 200,
        "orderBy": "modifiedTime desc",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DRIVE_API_BASE}/files",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30.0,
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Drive error: {resp.text[:500]}")
    return resp.json()


@app.get("/stream/{file_id}")
async def stream_video(file_id: str, request: Request):
    """
    Range-aware streaming proxy.

    The browser <video> element sends `Range: bytes=start-end` requests as the
    user seeks/buffers. We forward that header to Drive's alt=media endpoint
    and stream the (206 Partial Content) response back verbatim.
    """
    token = await require_token()

    upstream_headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Encoding": "identity",  # never gzip: Range must map to raw bytes
    }
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=30.0),
    )
    req = client.build_request(
        "GET",
        f"{DRIVE_API_BASE}/files/{file_id}",
        # alt=media           -> download content rather than metadata
        # acknowledgeAbuse    -> bypass the large-file virus-scan interstitial
        params={"alt": "media", "acknowledgeAbuse": "true"},
        headers=upstream_headers,
    )
    upstream = await client.send(req, stream=True)

    if upstream.status_code >= 400:
        detail = (await upstream.aread()).decode("utf-8", "replace")[:1000]
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(upstream.status_code, f"Drive error: {detail}")

    passthrough = {}
    for name in ("content-type", "content-length", "content-range", "etag", "last-modified"):
        if name in upstream.headers:
            passthrough[name] = upstream.headers[name]
    passthrough["accept-ranges"] = "bytes"
    passthrough["cache-control"] = "no-cache"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=1024 * 256):
                yield chunk
        finally:
            # Runs on completion OR when the browser cancels the request
            # (which happens constantly on every seek).
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=passthrough)


# --------------------------------------------------------------------------
# Media probing / subtitle extraction (MKV milestone)
# --------------------------------------------------------------------------

@app.get("/probe/{file_id}")
async def probe_file(file_id: str):
    """
    Inspect container + streams via ffprobe/ffmpeg (reads only file headers).
    Result is cached per file id; subtitle entries get a ready-to-use VTT url.
    """
    token = await require_token()
    cache = PROBE_CACHE / f"{media._safe_id(file_id)}.json"

    async with _lock(f"probe:{file_id}"):
        if cache.exists():
            info = json.loads(cache.read_text())
        else:
            try:
                info = await media.probe_media(file_id, token, DRIVE_API_BASE)
            except media.FFmpegMissingError as e:
                raise HTTPException(503, str(e))
            except media.ProbeError as e:
                raise HTTPException(502, f"probe failed: {e}")
            cache.write_text(json.dumps(info))

    for s in info["subtitles"]:
        s["url"] = f"/subtitles/{file_id}/{s['index']}.vtt" if s["web_compatible"] else None
    info["playability"] = media.assess_playability(info)
    return info


@app.get("/subtitles/{file_id}/{stream_index}.vtt")
async def get_subtitles(file_id: str, stream_index: int):
    """
    Extract one embedded subtitle track as WebVTT (cached after first call).

    First extraction reads the whole file sequentially from Drive server-side
    (MKV interleaves subtitle blocks), then the ~100KB result is cached.
    """
    token = await require_token()
    async with _lock(f"sub:{file_id}:{stream_index}"):
        try:
            path = await media.extract_subtitle(
                file_id, stream_index, token, DRIVE_API_BASE,
                SUBS_CACHE, timeout=EXTRACT_TIMEOUT_S,
            )
        except media.FFmpegMissingError as e:
            raise HTTPException(503, str(e))
        except media.ExtractError as e:
            raise HTTPException(422, str(e))
    # IMPORTANT: no Content-Disposition: attachment here — Chrome treats an
    # attachment-<track> response as a download instead of WebVTT cues, which
    # makes fetched-but-invisible subtitles. Default (inline) is required.
    return FileResponse(path, media_type="text/vtt")


# --------------------------------------------------------------------------
# Audio-only AAC remux (HEVC/other video bitstream copied, audio remixed)
# --------------------------------------------------------------------------

@app.post("/remux/{file_id}")
async def remux_start(file_id: str):
    """Kick off a background audio->AAC remux (idempotent; cached)."""
    token = await require_token()
    part, final = _remux_paths(file_id)

    if final.exists():
        REMUX_JOBS[file_id] = {"state": "ready", "detail": "already cached"}
        return {"state": "ready", "url": f"/remux/{file_id}.mkv"}

    async with _lock(f"remux:{file_id}"):
        job = REMUX_JOBS.get(file_id)
        if job and job.get("state") == "processing":
            return {"state": "processing", "detail": job.get("detail", "")}
        if final.exists():  # completed while we waited on the lock
            REMUX_JOBS[file_id] = {"state": "ready", "detail": ""}
            return {"state": "ready", "url": f"/remux/{file_id}.mkv"}

        REMUX_JOBS[file_id] = {"state": "processing", "detail": "starting"}

        async def runner():
            try:
                await media.remux_audio_aac(
                    file_id, token, DRIVE_API_BASE, part, final,
                    timeout=EXTRACT_TIMEOUT_S)
                REMUX_JOBS[file_id] = {"state": "ready", "detail": "complete"}
            except Exception as e:  # noqa: BLE001 - surfaced via status endpoint
                part.unlink(missing_ok=True)
                REMUX_JOBS[file_id] = {"state": "error", "detail": str(e)[:500]}

        asyncio.create_task(runner())
    return {"state": "processing"}


@app.get("/remux/{file_id}/status")
def remux_status(file_id: str):
    """Poll remux progress: none | processing (bytes written) | ready | error."""
    part, final = _remux_paths(file_id)
    if final.exists():
        return {"state": "ready", "detail": "",
                "url": f"/remux/{file_id}.mkv",
                "size": final.stat().st_size}
    job = REMUX_JOBS.get(file_id)
    if job and job.get("state") == "processing":
        written = part.stat().st_size if part.exists() else 0
        return {"state": "processing",
                "detail": f"{written / 1048576:.1f} MB written so far"}
    if job:
        return job
    return {"state": "none", "detail": "not started"}


@app.get("/remux/{file_id}.mkv")
def remux_file(file_id: str, request: Request):
    """Serve the cached remux with Range support (local disk = instant seeks)."""
    _part, final = _remux_paths(file_id)
    if not final.exists():
        raise HTTPException(
            404, "Remux not built. POST /remux/{file_id} first, "
                 "then poll /remux/{file_id}/status.")
    return media.ranged_file_response(
        final, request.headers.get("range"), "video/x-matroska")


# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


# Static mount AFTER API routes: serves ac3-audio.js and vendored browser libs
# (frontend/vendor/ is gitignored — regenerate with `python backend/fetch_vendor.py`).
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR), check_dir=False),
          name="static")

