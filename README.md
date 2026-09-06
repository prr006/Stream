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
│   ├── app.py            # OAuth + /files + range-aware /stream proxy + /probe + /subtitles
│   ├── media.py          # ffprobe/ffmpeg stream detection + subtitle→WebVTT extraction
│   ├── requirements.txt  # fastapi, uvicorn, httpx, python-dotenv, imageio-ffmpeg
│   └── .env.example      # copy to .env and fill in your OAuth client credentials
├── frontend/
│   └── index.html        # file picker + <video> player + seek buttons + subtitle UI
├── tests/
│   ├── fake_drive.py        # fake Drive upstream that serves bytes with Range support
│   ├── test_range_proxy.py  # end-to-end proof that Range passthrough is byte-exact
│   └── test_subtitles.py    # MKV probe + subtitle extraction tests (real ffmpeg round-trip)
├── cache/                # created at runtime: probe JSON + extracted .vtt (gitignored)
├── .gitignore            # ignores .env, token.json, .venv, cache/
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

```bash
python tests/test_subtitles.py
```

Unit-tests the ffprobe/ffmpeg stream parsers, then synthesizes a **real MKV with two embedded
SRT tracks (eng + jpn)** using ffmpeg, serves it through the fake Drive, and verifies
`/probe` stream detection, `/subtitles/...vtt` extraction to WebVTT, the on-disk cache,
and the bad-stream-index error path.

## 7. Milestone 2: MKV playback + embedded subtitles

**Pipeline (deliberately transcode-free):**

```
browser <video>  -- Range: bytes=a-b -->  /stream/{fileId}  ------------>  Drive (unchanged, direct MKV passthrough)
browser <track>  --------------------->  /subtokens...      cached .vtt
any code path    --------------------->  /probe/{fileId}    ffprobe/ffmpeg reads only file headers
```

- **Video never leaves Drive's bytes untouched** — the MKV container is streamed through the
  existing range proxy and demuxed by the browser. Zero CPU, seeks stay instant.
  Requirement: **Chrome or Edge**, with codecs in {H.264, VP8/9, AV1} + {AAC, MP3, Opus, Vorbis, FLAC}.
- `GET /probe/{file_id}` detects container, video/audio codecs, and embedded subtitle tracks.
  Uses `ffprobe` JSON when installed; otherwise parses `ffmpeg -i` stderr (the bundled
  `imageio-ffmpeg` binary works as a last resort — it has no ffprobe).
- `GET /subtitles/{file_id}/{stream_index}.vtt` extracts one text subtitle track
  (SubRip/ASS/WebVTT/mov_text) to **WebVTT** via `ffmpeg -map 0:{index} -c:s webvtt`.
  Results cached in `cache/subs/`.
- **Image-based subtitles (PGS/VobSub) are detected and shown as unsupported** — converting them
  to text requires OCR (possible future add-on, e.g. SubtitleEdit / pgsrip).

**Subtitle rendering details that matter:** the `.vtt` endpoint serves `Content-Type: text/vtt`
and must NOT send `Content-Disposition: attachment` (Chrome otherwise downloads the payload
instead of parsing it as cues). The page controls rendering via the standard TextTracks API
(`track.mode = "showing" | "disabled"`). Open the **"Text-track diagnostics"** panel under the
player to see, per track: `readyState` (NONE/LOADING/LOADED/ERROR), current `mode`, parsed
`cues` count, first cue timestamp, and a **"jump to first cue"** button. Rule of thumb:
`LOADED + cues>0 + mode=showing` ⇒ cues should be visible at the right timestamp.

**The one real cost:** MKV interleaves subtitle packets with A/V data, so the *first* extraction
of a (file, track) pair reads the **entire file sequentially server-side** from Drive (demux
only, no decode — roughly the full file size against that account's daily Drive download quota).
After that, the ~100 KB `.vtt` is served from disk instantly. Probe calls only read the header.

### Audio-only AAC remux (HEVC/AC3 etc.)

When `/probe` reports an audio codec browsers can't decode (AC3, E-AC3, DTS, TrueHD, raw PCM —
`playability.needs_audio_remux: true`), the UI offers a one-click fix:

```
ffmpeg -i <drive-url> -map 0:v:0 -c:v copy -map 0:a -c:a aac -b:a 192k \
       -map 0:s? -c:s copy -f matroska cache/remux/<id>.mkv
```

- **Video bitstream is copied bit-exact (`-c:v copy`)** — HEVC is preserved as-is, zero video
  re-encode. Only audio is converted (negligible CPU). Subtitle streams are carried over.
- Endpoints: `POST /remux/{file_id}` (start background job), `GET /remux/{file_id}/status`
  (progress in MB written), `GET /remux/{file_id}.mkv` (**Range-capable** local file serving).
- The remux reads the file once from Drive, then playback is pure local disk:
  **seeking becomes instant and Drive-free**. The player keeps your position when swapping
  to the remuxed copy.
- Rejected alternative: live transmux stream (zero storage, but unknown duration ⇒ no seeking).

### How to test with one of YOUR MKV files

1. Install ffmpeg on your machine (optional but recommended — gives you ffprobe JSON probing):
   - Ubuntu/Debian: `sudo apt install ffmpeg` · macOS: `brew install ffmpeg` · Windows: winget.  
   If you skip this, the backend falls back to the pip-bundled `imageio-ffmpeg` binary.
2. `pip install -r backend/requirements.txt` (pulls `imageio-ffmpeg` + everything else).
3. Start the server (`uvicorn app:app --host 0.0.0.0 --port 8000 --reload` from `backend/`),
   authorize, and click your MKV file. **Use Chrome or Edge.**
4. The **detection panel** shows container, codecs, duration and warnings
   (e.g. HEVC → "won't decode", AC3/DTS → "audio may be silent", PGS → "unsupported").
5. Under **Subtitles**, check a track — the first fetch triggers server-side extraction
   (watch the uvicorn log; it reads the whole file once, so give a 4 GB file a minute or two).
   Subsequent toggles are instant (cached). The `.vtt` appears as standard browser-rendered cues.
6. Verify with curl:
   ```bash
   curl -s http://localhost:8000/probe/<FILE_ID> | python3 -m json.tool
   curl -si http://localhost:8000/subtitles/<FILE_ID>/2.vtt | head -12   # WEBVTT + cues
   ```
7. Seeking is unchanged — the video element still issues range `GET`s against `/stream/{id}`;
   DevTools Network shows `206 Partial Content`.

## Known limitations (POC scope)

- **MKV playback = Chrome/Edge only.** Safest codecs: H.264/VP9/AV1 video + AAC/Opus/MP3 audio.
  The `/probe` endpoint warns per-file: HEVC video only decodes where the OS/GPU provides an
  HEVC decoder (often OK on Windows); AC3/DTS/TrueHD audio plays silent **unless you build the
  AAC remux** (see "Audio-only AAC remux"); PGS/VobSub subtitles are image-based (need OCR).
  **Full video transcoding is still out of scope** — if HEVC itself won't decode on your
  machine, that needs a real transcode or HLS pipeline milestone.
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
