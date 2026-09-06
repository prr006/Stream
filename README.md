# Stream POC

Minimal proof-of-concept: **Google Drive → local backend → browser `<video>` player**, with
HTTP Range request passthrough so playback can buffer and seek **without downloading the
whole file first**.

```
┌──────────────────────┐   Range: bytes=a-b   ┌───────────────────┐   alt=media    ┌──────────────┐
│ browser <video> tag  │ ───────────────────► │ FastAPI backend   │ ─────────────► │ Google Drive │
│  (frontend/index.html)│ ◄─── 206 Partial ─── │  /stream/{fileId} │ ◄── 206 ────── │  (1 account) │
└──────────────────────┘   Content + Range     └───────────────────┘                └──────────────┘
```

## Project structure

```
Stream/
├── backend/
│   ├── app.py            # OAuth flow + /files + range-aware /stream proxy (no Google SDK, plain HTTP)
│   ├── requirements.txt  # fastapi, uvicorn, httpx, python-dotenv
│   └── .env.example      # copy to .env and fill in your OAuth client credentials
├── frontend/
│   └── index.html        # file picker + <video> player + seek-test buttons
├── tests/
│   ├── fake_drive.py     # fake Drive upstream that serves 5 MiB with Range support
│   └── test_range_proxy.py  # end-to-end proof that Range passthrough is byte-exact
├── .gitignore            # ignores .env, token.json, .venv
└── token.json            # created automatically after you authorize (DO NOT COMMIT — already gitignored)
```

## 1. Google Cloud configuration (one-time, ~5 minutes)

1. **Create a project**: Go to https://console.cloud.google.com → project dropdown (top-left) → **New Project** (name e.g. `stream-poc`) → **Create**.
2. **Enable the Drive API**: In the new project, go to **APIs & Services → Library**, search **"Google Drive API"**, click **Enable**.
3. **Configure the OAuth consent screen**: **APIs & Services → OAuth consent screen**:
   - User type: **External** → Create.
   - App name: anything (e.g. `Stream POC`), plus your email as user-support and developer contact.
   - Scopes step: **Add or remove scopes** → add `.../auth/drive.readonly` (Google Drive API · read-only).
   - Test users (only shown while the app stays in "Testing" status): **add your own Gmail address** — otherwise Google will refuse to let you sign in.
4. **Create OAuth credentials**: **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs: add **exactly** `http://localhost:8000/auth/callback`
   - Create → copy the **Client ID** and **Client Secret**.
5. **(Recommended) Publish the app**: still on the OAuth consent screen, click **"Publish app"**.
   While the app is in "Testing" status, Google issues refresh tokens that **expire after 7 days**,
   so you'd have to re-authorize weekly. Publishing a personal app is fine: Google will show an
   *"unverified app"* warning when you sign in — since it's your own app, click **Advanced → Go to Stream POC (unsafe)**. No verification review is needed for a private app under 100 users.

## 2. Backend setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then edit .env:
#   GOOGLE_CLIENT_ID=....apps.googleusercontent.com
#   GOOGLE_CLIENT_SECRET=GOCSPX-...
#   OAUTH_REDIRECT_URI=http://localhost:8000/auth/callback
```

## 3. Run

```bash
# from the backend/ directory, with the venv active:
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## 4. Authorize your Drive account

1. Open http://localhost:8000
2. Click **Sign in with Google** → choose your account → (if you skipped step 1.5, click
   Advanced → proceed) → allow the read-only Drive scope.
3. You'll be redirected back; `token.json` now contains your access + refresh tokens.
   Access tokens (1 h) are refreshed automatically via the refresh token — you only repeat
   this step if you revoke access, change the OAuth client, or stayed in "Testing" > 7 days.

## 5. Test playback and seeking

1. Play from the start:
   - Open http://localhost:8000, click a video in the list (MP4 recommended for the first test).
   - **Open DevTools → Network** and watch `/stream/...` requests: you should see
     **status `206 Partial Content`** and a **`content-range`** response header.
2. Seeking without full download:
   - Press the **25% / 50% / 90%** buttons (or drag the seek bar).
   - Each seek triggers a *new* `/stream/...` request with a **`Range: bytes=…`** request header.
   - The status line next to the buttons shows the last media event and the buffered end time.
   - For the definitive check, note the **"Transferred"** total in DevTools: after jumping around
     a 2 GB file it should be only a few tens of MB, not gigabytes.
3. CLI equivalent:

   ```bash
   curl -si -H "Range: bytes=1000000-1000099" \
        http://localhost:8000/stream/<FILE_ID> -o /dev/null
   # expect:  HTTP/1.1 206 Partial Content
   #          content-range: bytes 1000000-1000099/<total-size>
   ```

## 6. Automated test (no Google credentials needed)

```bash
python tests/test_range_proxy.py
```

Spins up the fake Drive upstream and verifies full-body, bounded, open-ended and suffix Range
requests come back byte-exact with `206` + `Content-Range`, plus that the auth gate returns 401.

## Known limitations (POC scope)

- **Browser codec support applies.** The browser plays the file natively, so **MP4 (H.264 + AAC)**
  is the safe test target. MKV in `<video>` works in Chrome/Edge when the codecs are supported
  (H.264/VP9/AV1 + AAC/Opus), but not in Safari, and H.265/HEVC or AC3/DTS audio often won't
  decode anywhere without server-side remux/transcode (that's a later FFmpeg step — out of POC scope).
- **`HEAD` requests are not implemented.** Native `<video>` elements don't need them (they use
  range `GET`s), but some external players would.
- **Drive is not a video CDN.** Seek latency is typically a few hundred ms (a fresh range request
  per seek); sustained throughput is usually fine for 1080p but a very high-bitrate 4K remux may
  stutter. No adaptive bitrate here — the file's native bitrate is what you get.
- **Per-account download quota.** Google applies a (poorly documented, community-observed)
  ~750 GB/day per-account download cap; streaming does consume it. Fine for personal use —
  this is the reason the 6-account design exists later.
- **`acknowledgeAbuse=true`** is set automatically to bypass the virus-scan interstitial on large
  files; it only works for files the authorized account owns (or shared drives they organize).
- **Plaintext token storage** in `token.json`, localhost HTTP only, single user, no CORS, no TLS.
  Fine for local personal use; do not expose this to the internet as-is.
- If the OAuth app stays in **"Testing"**, refresh tokens **die after 7 days** → re-run sign-in.
  Publish the app (step 1.5) to avoid this.
