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
import json
import os
import time
import urllib.parse
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

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

app = FastAPI(title="Stream POC")


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
# Frontend
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
