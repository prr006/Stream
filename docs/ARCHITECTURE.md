# MyStream — Playback Architecture (v2)

**Status:** PROPOSAL v2 — revised after inspection of the working POC
(branch `arena/01a07368-stream`). **No new code written yet.**

v1 (this file, earlier revision) assumed an empty repo. v2 is grounded in the
actual POC: what is preserved verbatim, what is evolved, what is replaced, and
what is removed.

Target: *"I click an episode and it just plays properly."* No codec, container,
subtitle, or transcoding decisions are ever exposed to the user. The source
file in Google Drive is **never modified**.

---

## 0. Decisions at a glance

| # | Decision | Choice |
|---|----------|--------|
| 1 | Overall architecture | **DASH/CMAF segment origin** for all generated tiers + Direct Play passthrough (Option A, §14) |
| 2 | Backend stack | **Keep the POC's Python/FastAPI backend** (proven Drive auth + range proxy + tests) — *one confirmation requested, §18*; frontend = new TypeScript app |
| 3 | Web player | **Shaka Player (headless) + thin custom TS control bar**; Direct Play mode = plain `<video>+<track>` (no MSE) under the same controls |
| 4 | Streaming protocols | Direct Play = progressive over HTTP Range (POC `/stream/{id}`, unchanged). Generated tiers = **DASH, CMAF (fMP4) segments, static VOD MPD** |
| 5 | FFmpeg strategy | One-time `ffprobe` per file version (extends POC probe) + keyframe index; per-segment/per-session FFmpeg jobs, copy-first; AAC audio-only tier; NVENC/VideoToolbox-assisted full transcode (server has GPU) |
| 6 | Drive ↔ FFmpeg | POC's `/stream/{id}` range proxy **gains a chunked byte-range cache (RangeCache)**; FFmpeg reads through the loopback URL; single-flight + backoff + quota self-metering |
| 7 | Mode selection | Evolve the POC's **pure `decide()` engine** (same contract: reasons/steps/alternatives) — implement the remux / audio-only / transcode rungs as DASH plans; demote `wasm-audio` to legacy |
| 8 | Caching | POC's probe/subs caches kept; remux whole-file cache replaced by **LRU segment cache** + LRU raw-Drive-byte cache; nothing written to Drive; `--no-cache` = POC parity |
| 9 | WASM AC3 / AAC-remux panel | WASM AC3 → isolated legacy, deleted after E2E proof; the AAC-remux UI panel disappears (becomes the automatic audio-only tier) |

---

## 1. Product & goals

MyStream is a personal anime/movie/series streaming site backed by Google
Drive: browse (titles → seasons → episodes), click **Play**, get a full
player experience — seeking, fullscreen, ±10s, volume/mute, PiP, playback
speed, subtitles, audio-track selection, episode navigation.

**Hard constraints**
1. Storage = Google Drive; no permanent local copy of sources (bounded,
   evictable caches OK — §12).
2. Source files are never modified.
3. No codec logic in the frontend; the frontend asks for a playback session
   and executes the plan (the POC already follows this contract — §2).
4. The proven POC Drive auth + range-streaming code is **preserved, not
   re-invented**.

**Non-goals (v1):** multi-user, DRM, live TV, DVR, casting, multi-rendition
ABR ladders (single source per episode; Phase 3 option).

---

## 2. Current state — the POC (branch `arena/01a07368-stream`)

Inspected and assessed. It is a working, test-backed FastAPI + vanilla-JS
prototype:

| Component | File(s) | Status | Disposition |
|---|---|---|---|
| Google OAuth (user OAuth, offline access, refresh-token persistence) | `backend/app.py` | ✅ proven | **Keep verbatim** |
| `/stream/{file_id}` range proxy (Drive `alt=media`, 206 passthrough, `acknowledgeAbuse`) | `backend/app.py` | ✅ proven ("the POC") | **Keep; add RangeCache layer behind it** (§11) |
| `/files` listing | `backend/app.py` | ✅ | Keep; extend into Library (§10.3) |
| Probe: `ffprobe` JSON over HTTP Range w/ bearer `-headers`, ffmpeg-stderr fallback, disk cache | `backend/media.py` | ✅ proven | **Keep; extend** with keyframe index, moov position, fragmentation, Drive version (§4) |
| Subtitle extraction → WebVTT (whole-file MKV demux, atomic publish, disk cache) + `/subtitles/{id}/{i}.vtt` | `backend/media.py`, `app.py` | ✅ proven (fixed the Chrome `<track>` gotcha) | **Keep; add cue-aware fast path + lazy-prefix** (§9) |
| Decision engine: pure `decide(probe, caps)` ladder (direct-play → wasm-audio → container-remux → audio-remux → transcode) with reasons/steps/alternatives | `backend/engine.py` | ✅ unit-tested | **Evolve**: implement remux/audio/transcode rungs as DASH plans; demote wasm-audio (§6) |
| Audio-only AAC remux: background **whole-file** job → cached MKV + status polling + range-serve | `app.py`, `media.py` | ✅ proven, but first play waits for the ENTIRE file to be built | **Replaced** by segment origin (§7, §14) |
| Capability measurement: `canPlayType` + `MediaSource` + `MediaCapabilities` (per-codec probe strings) | `frontend/media/capabilities.js` | ✅ | **Keep & extend** (HW signal, mp4 AC-3 probe) (§5) |
| `MediaEngine.open()` → plan → adapter chain with automatic fallback | `frontend/media/engine.js` | ✅ | Keep the pattern; re-implement in TS on the new player |
| Adapters: direct-play ✅, wasm-audio ✅, container-remux 🚧 stub, audio-remux ✅ (polling), transcode 🚧 stub | `frontend/media/adapters.js` | — | Replaced by two-transport player (§10); remux/transcode stubs become real |
| WASM AC3 player (mediabunny demux + ffmpeg.wasm → WebAudio, drift watchdog) | `frontend/ac3-audio.js` + vendored libs | ✅ proven, but client-side workaround | **Demote to legacy, then remove** (§13) |
| Diagnostic UI (plan panel, remux panel, WASM stats) | `frontend/app.js`, `index.html` | prototype-grade | **Replaced** by production player + library UI (§10) |
| Test harness: fake Drive upstream, range-proxy/remux/subtitles/engine tests | `tests/` | ✅ | **Keep; extend** to segment origin & MPD |
| POC docs (architecture + open-source evaluation) | `docs/` | — | Superseded by this doc; the evaluation's "revisit Shaka when mode 5 is built" note is exactly where we are now |

**The POC already has ~80% of the control plane this design needs** (probe,
pure decision engine, measured capabilities, plan contract, subtitle
pipeline). What is missing — and what this architecture adds — is:
(a) a *delivery* layer that makes remux/audio/transcode **seekable,
instant-to-first-byte, and cacheable** (segment origin), (b) the full
transcode rung, (c) a production player + library UX.

---

## 3. High-level architecture

```
                      ┌──────────────────────────────────────────┐
                      │           WEB PLAYER (NEW, TS)           │
                      │  Shaka (headless) + custom control bar   │
                      │   · range mode: <video src> + <track>    │
                      │   · dash mode: Shaka/MSE on same element │
                      │  capabilities.js (kept, extended)        │
                      │  MediaEngine (kept pattern, TS)          │
                      └─────────────────────────────────────────┘
                                       │  POST /media/{id}/plan (measured caps)
                                       │  GET ranges | MPD | segments | vtt
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                     APP SERVER (POC FastAPI — extended)                     │
│                                                                             │
│  ┌──────────┐ ┌──────────────┐ ┌────────────────┐ ┌──────────────────────┐ │
│  │ Library  │ │ MediaProbe   │ │ PlaybackDecision│ │ StreamOutput         │ │
│  │ (new:    │→│ (POC probe + │→│ (POC engine.py, │ │ (POC routes kept:    │ │
│  │  catalog,│ │  + keyframes,│ │  evolved)       │ │  /stream /subtitles; │ │
│  │  naming) │ │  + version)  │ │                 │ │  NEW: /dash/… MPD,   │ │
│  └──────────┘ └──────┬───────┘ └────────────────┘ │       init, segments)│ │
│                      │                             └──────────┬───────────┘ │
│                      │        ┌───────────────────────────────▼──────────┐   │
│                      └───────▶│        MediaPipeline (NEW)               │   │
│                               │  4 tiers · segment model · job manager   │   │
│                               │  (workers, coalescing, LRU segment cache)│   │
│                               └──────────────────┬───────────────────────┘   │
│                                                  │ child ffmpeg/ffprobe      │
│                               ┌──────────────────▼───────────────────────┐   │
│                               │ DRIVE GATEWAY (POC /stream + NEW)        │   │
│                               │  POC: OAuth, token refresh, 206 proxy    │   │
│                               │  NEW: RangeCache (sparse LRU, 8MB chunks,│   │
│                               │       single-flight, backoff, quota meter│   │
│                               └──────────────────┬───────────────────────┘   │
└──────────────────────────────────────────────────┼───────────────────────────┘
                                                   ▼
                                     Google Drive (read-only source)
```

Pipeline: **MediaProbe → PlaybackDecision → MediaPipeline → StreamOutput →
WebPlayer** (plus Library, Drive Gateway, Job Manager as supporting modules).

---

## 4. MediaProbe (extends POC `probe_media`)

The POC probe (ffprobe over Range with bearer header, cached per file) is
kept. Extensions, all cheap (few KB–MB of Range reads, no full-file scan):

- **Keyframe index** — what makes seeking O(1) in generated tiers:
  - **MP4**: parse `stss/stts/stco` from the `moov` box (1–2 Range reads of
    the moov bytes; minimal box parser ~100 lines, or a small library).
  - **MKV**: parse the `Cues` element from the file tail (1–2 Range reads) —
    cues ARE the keyframe list; written by mkvmerge/ffmpeg by default.
  - **MKV without cues** (rare): uniform-grid fallback with per-segment
    base-time correction; optional background sequential index pass.
- **Container details**: moov position (start/end), `fragmented` flag
  (fmp4 with `moof` in body → not direct-playable), `encrypted` flag
  (CENC/CBCS → hard-reject "DRM").
- **Stream details**: video profile/level/pixFmt/fps; audio sample_rate,
  channel layout, bitrate; subtitle codec + format class (text vs image).
- **Drive version**: `md5Checksum` + `sizeChangeTime` from the files API →
  cache key for probe/index/VTT/segments; re-upload invalidates everything.

Stored in SQLite (the POC's JSON cache files migrate into it; both formats
remain readable during transition).

### External subtitles
The Library matches `.srt/.ass/.vtt` sidecars by filename; they join
`MediaInfo.subtitles` (`embedded: false`, own Drive id) and flow through the
same extraction pipeline (tiny files — instant).

---

## 5. Client capabilities (keeps POC `capabilities.js`, extends)

The POC's approach is exactly right and stays: **the browser measures itself
(canPlayType + MediaSource.isTypeSupported + MediaCapabilities.decodingInfo
refinement) and POSTs the matrix; the server decides.** Static tables lie in
2026 — verified:

1. **HEVC is hardware/OS-conditional** (Chrome/Edge need system codecs —
   Windows "HEVC Video Extensions"; Linux usually lacks them; a 2026
   1M-session dataset found HEVC "nearly absent" in real-world Edge even
   though docs say supported).
2. **AC-3 is disabled by default in stock Chromium builds** (platform
   decoders exist behind the `enable_platform_ac3_eac3_audio` build flag);
   Firefox has none; Safari plays AC-3 in MP4.

Extensions to the POC matrix:
- `MediaCapabilities.decodingInfo` → expose **`powerEfficient`** as the HW
  signal (hevc.hw) so the plan can state "HW" in the debug chip.
- Add `audio/mp4; codecs="ac-3"` / `"ec-3"` probe variants (MSE polarity —
  Chromium may report "maybe" via MFT; the POC already has this
  refinement for ac3/eac3).
- Keep the POC's mkv/webm/mp4 container probes (Chrome's flaky MKV sniffing
  will still report `mkv: true` in some builds — the decision engine
  deliberately does NOT direct-play MKV on that claim; §6.2).

`DEFAULT_CAPABILITIES` (server-side curl/debug fallback) stays conservative;
unreported keys normalize to *unsupported* (POC behavior, kept).

---

## 6. PlaybackDecision (evolves POC `engine.py`)

### 6.1 What stays
- The **pure function** `decide(probe_info, caps) → plan` and its unit tests.
- The **plan contract**: `mode, implemented, reasons[], steps[], playback,
  audio, audioTracks[], subtitles[], alternatives[]` — pages render plans,
  never codecs. This is kept so the POC frontend keeps working during
  migration.
- Capability normalization (`normalize_caps`).

### 6.2 What changes

**New ladder order** (wasm-audio demoted out of the ladder, §13):

```
0. encrypted / no-video            → unplayable (explicit reason)
1. DIRECT-PLAY                     container native (mp4/m4v; NOT mkv — see note)
                                   AND video OK AND audio OK AND subs natively
                                   exposed  → playback.type="range" (/stream/{id})
2. CONTAINER-REMUX                 video OK AND audio OK, container wrong
                                   → playback.type="dash", planHash="copy"
                                   (video copy, audio copy, vtt rendition)
3. AUDIO-TRANSCODE                 video OK, audio not
                                   → playback.type="dash", planHash="aac"
                                   (video copy, audio → AAC)  ← was "audio-remux"
4. TRANSCODE                       video itself unplayable
                                   → playback.type="dash", planHash="h264aac"
                                   (h264 + aac; NVENC/VideoToolbox when GPU)
5. unplayable (explicit reason)
```

- **MKV note:** the POC observes Chrome/Edge *can* sometimes play MKV in
  `<video>` (that's why the POC's wasm-audio mode worked on Chrome MKV).
  But it is build- and codec-dependent, invisible to `canPlayType`, and
  exactly the instability that produced the WASM hack. v1 rule: **MKV is
  never direct-played** — it takes rung 2 (a copy-only remux, which is cheap
  and identical-quality). This makes behavior consistent across browsers.
  (A capability flag can relax this later with data.)
- `playback` becomes `{type: "range"|"dash", url}` (range: the POC
  `/stream/{id}`; dash: `/dash/{id}/{planHash}/manifest.mpd`).
- **`alternatives[]` kept as the robustness chain**: if a dash session fails
  to start (MSE/codec surprise), the client retries the next implemented
  alternative (e.g., `aac` rung failed → `h264aac`).
- **Plan cache** per (file_version, client_class, language); client_class =
  coarse UA bucket. The POC re-probes-per-plan stays; plans themselves are
  now memoized (probe stays the expensive part, and it's cached).
- Every plan keeps **human-readable reasons + per-stream steps** (POC
  contract) → the debug chip.

### 6.3 Walkthrough — the real test file (MKV + HEVC + AC-3 + SRT)

| Viewer | Video | Audio | Decision |
|---|---|---|---|
| **Chrome/Edge with HW HEVC** | HEVC HW ✓ | AC-3 ✗ (stock Chromium) | **Rung 3 — AUDIO-TRANSCODE**: HEVC bitstream copied untouched; AC-3 → AAC 5.1; SRT → WebVTT rendition. First segment in ~1–2 s; rest generated ahead + cached. This is the automatic replacement for BOTH the WASM AC3 hack and the "Audio Fix" button. |
| **Safari (mac/iOS)** | HEVC ✓ | AC-3 ✓ | **Rung 2 — CONTAINER-REMUX**: MKV → CMAF with HEVC copy + AC-3 copy. (If E2E shows Safari MSE+AC-3-fMP4 trouble, the alternatives chain drops it to rung 3 — the engine already carries both plans.) |
| **Chrome/Edge without HW HEVC** (Linux; Windows w/o extension — the common in-the-wild case) | ✗ | ✗ | **Rung 4 — TRANSCODE** (H.264 + AAC; GPU PC → NVENC real-time), clearly labelled in the chip. |
| **Firefox** | conditional (134+/system) | ✗ | Rung 3 or 4 per the measured report. |

Seeking, subtitles, audio selection all work in every rung (§8, §9, §10).

---

## 7. MediaPipeline (NEW — replaces the POC's whole-file remux job)

The POC's proven `remux_audio_aac` does the right *codec* thing (video copy,
audio→AAC) but the wrong *delivery* thing: it builds the **entire** file
(~5–10 min for a 15 GB source at Drive read speed) before the first byte is
playable. The segment origin fixes that while keeping the same ffmpeg
primitives.

### 7.1 The segment model (shared by rungs 2–4)

- Segment n = frames keyframe n → keyframe n+1 (copy tiers) or exactly 4 s
  with forced keyframes (transcode tier).
- **Static VOD MPD generated at decision time** from probe + keyframe index
  (`SegmentTemplate` + explicit `SegmentTimeline`; total duration known →
  full seek bar, no growing-playlist semantics).
- URLs: `/dash/{id}/{planHash}/init.mp4`, `v_{n}.mp4`, `a_{n}.mp4`,
  `vtt_{lang}.mp4`. `planHash` = hash of the codec config → URLs stable
  across sessions/browsers → the segment cache is **shared**: viewer #2 and
  every later viewer get instant playback.

Production mechanisms (two, complementary):
1. **Prefetch worker** (per active session): one continuous ffmpeg writing
   self-contained fMP4 segments via the `segment` muxer
   (`-segment_time 4 -segment_format fmp4`; cuts on keyframes when copying)
   — reads the source **forward exactly once**, N segments (3) ahead of the
   playhead; a seek re-targets the worker at the target keyframe (cheap:
   RangeCache warm, moov/cues cached).
2. **On-demand job** (seeks/misses): single-shot
   `ffmpeg -ss {kf} -i {loopback_url} -t {dur} … -f fmp4 seg_n.mp4`,
   atomic rename into cache. Jobs coalesced by (file, plan, segment);
   global worker cap protects CPU.

Input is always the loopback `http://127.0.0.1:{port}/stream/{file_id}` —
never a Drive URL in ffmpeg args (the POC's `-headers Bearer` pattern is
retired for pipeline jobs: the token stays in the gateway; quota is
accountable; the RangeCache serves all readers).

### 7.2 Per-tier ffmpeg specs

Audio: normalize 48 kHz; keep channel layout (5.1→5.1; >6 ch → 5.1).

| Rung | Video | Audio | Subs |
|---|---|---|---|
| 1 Direct Play | — | — | VTT via `<track>` (POC endpoint) |
| 2 Remux | `-c:v copy` | `-c:a copy` | WebVTT DASH rendition |
| 3 Audio-transcode | `-c:v copy` (HEVC bitstream preserved) | `-c:a aac -b:a 256k` (5.1) / 192k (stereo) | WebVTT rendition |
| 4 Transcode | `-c:v h264_nvenc -preset p5` / `h264_videotoolbox` (GPU PC; CPU fallback `libx264 -preset veryfast -crf 21 -profile:v high -level 4.1 -pix_fmt yuv420p -force_key_frames expr:'gte(t,n_forced*4)'`) | `-c:a aac` | WebVTT rendition |

- Video & audio are **separate DASH AdaptationSets** — the video
  representation in rung 3 is literally the original HEVC bitstream in CMAF
  (the same model Plex uses for "Direct Stream, audio only").
- Multi-audio sources: playable tracks become separate audio representations
  (player track selector); a track needing conversion is converted per-track.
- HDR10/10-bit in rung 4: tone-map to SDR 8-bit (v1); HDR stays available in
  rungs 1–3 for HEVC-capable clients.
- "Quality selection": v1 = one rendition per rung; ladders = Phase 3 (DASH
  extends naturally).

---

## 8. StreamOutput (POC routes kept + new DASH routes)

| Route | Status | Purpose |
|---|---|---|
| `GET /stream/{file_id}` | **POC, unchanged API** | Range/206 from Drive via gateway. Serves Direct Play browsers, all FFmpeg/ffprobe reads, probes. |
| `GET/POST /media/{file_id}/plan` | **POC, evolved response** | The one entry point: browser posts measured caps → plan (mode + playback URL + tracks + reasons + alternatives). |
| `GET /probe/{file_id}` | POC, unchanged | Debug/inspection. |
| `GET /subtitles/{file_id}/{i}.vtt` | **POC, kept** | Extracted WebVTT (inline, not attachment — the POC's Chrome gotcha fix preserved). |
| `GET /dash/{file_id}/{planHash}/manifest.mpd` | NEW | Static VOD MPD (memoized per planHash). |
| `GET /dash/{file_id}/{planHash}/init.mp4` | NEW | ftyp+moov init (generated once per planHash). |
| `GET /dash/{file_id}/{planHash}/{v|a}_{n}.mp4` | NEW | Cache hit → serve; miss → on-demand job → serve; 404 past the end. |
| `GET /files`, `/auth/*` | POC | Listing/auth (extended by Library, §10.3). |
| `GET /api/debug/sessions` | NEW | Ops: quota meter, RangeCache hit-rate, workers, per-segment latency. |
| `POST /remux/{file_id}` + status + `.mkv` | POC | **Retired** (replaced by rungs 2–4). Kept read-only during transition, then removed. |

**Seeking**
- Range mode: native browser Range (POC behavior, unchanged).
- Dash mode: `seek(t)` → SegmentTimeline → segment n → 1 GET. Cache hit:
  instant; miss: one job (~0.3–2 s copy tiers; 1–5 s transcode 1080p;
  GPU: ~real-time). **O(1) in position**, unlike process-restart pipelines.

---

## 9. Subtitles (keeps POC pipeline, adds speed + coverage)

- **Embedded text (SRT/ASS/WebVTT)**: never shown raw (raw SRT in MKV/MP4 is
  not browser-renderable). POC's extract-to-WebVTT pipeline (cached, atomic)
  is **kept**. Improvements:
  - **MKV with cues**: fetch subtitle events at their cue offsets (small
    ranges, ~KB–MB total) instead of the whole-file sequential read —
    first-extraction cost drops from "whole file" to "hundreds of KB".
  - **MKV without cues**: lazy-prefix — serve the first ~30 min (head
    read), background job extends to the end; VTT ETag bumps, player
    re-fetches transparently. (POC's whole-file read remains the fallback.)
  - **MP4**: subtitle sample table in `moov` → fetch only the subtitle
    sample bytes (KBs). ~Free.
- **External `.srt/.ass/.vtt` sidecars**: Library matches by filename;
  selectable tracks; same pipeline (instant — tiny files).
- **Delivery**: range mode → native `<track>` (POC SubtitleManager rules
  preserved); dash mode → **WebVTT DASH rendition** (Shaka renders; no
  overlay library).
- **Multiple tracks**: all exposed with language labels; player selector
  (on/off + track + size).
- **Image-based (PGS/VobSub)**: POC already detects + marks unsupported —
  **kept**. v1: listed with a "bitmap — not available" tag, no silent
  conversion. On-demand **burn-in** (transcode-tier `subtitles=` filter,
  `*burn` plan variant, cached) is a Phase-3 operation. WEB-DL anime —
  this corpus — is overwhelmingly SRT/ASS, so this path is rare.

---

## 10. WebPlayer (NEW — replaces the diagnostic UI)

### 10.1 Player library — **Shaka Player (headless)**

The POC's own evaluation deferred Shaka: *"revisit only if/when mode 5
[full transcode] is built."* That condition is now met, and the product
requirements (production player, robust seeking, audio-track selection,
captions in a segmented pipeline) are exactly Shaka's domain:

- Reference-grade DASH+HLS over MSE; first-class WebVTT in MSE mode;
  audio-track switching from DASH representations; ABR with explicit
  override; headless mode → UI stays fully ours.
- Apache-2.0, Google-maintained, **used in production by Jellyfin's web
  client** for exactly this workload.
- Runner-up: Video.js + http-streaming (better stock UI; DASH is a
  second-class adapter there; skin fights the target look). hls.js/dash.js
  would lock the protocol. Shaka plays DASH *and* HLS, so nothing is locked.

### 10.2 Two transports, one `<video>`, one control bar

- **Range mode** (Direct Play): plain `video.src = /stream/{id}` + `<track>`
  — **no MSE**, exactly the POC's proven path.
- **Dash mode**: Shaka loads the MPD on the same element.
- Custom control bar (TS, ~400–600 lines, both modes): play/pause, seek bar
  with buffered/drag, **±10 s**, volume/mute, **fullscreen**, **PiP**,
  **playback speed**, **subtitles selector**, **audio-track selector**
  (when >1 representation), **quality** (when a ladder exists),
  **next/prev episode + auto-continue**, keyboard shortcuts, skip-intro
  (Phase 2), and the **debug chip** (dev toggle: mode + per-stream verdicts +
  cache hit-rate + segment latency) — the POC's MODE_BADGE concept, kept as
  a developer affordance, hidden from the user surface.
- Error surfaces show the plan's human-readable reason (POC contract).

### 10.3 Library layer (new; the "streaming site" front)

- Drive listing (POC `/files`) + **naming parser** (anime patterns:
  `Show S01E02 …`, `[Group] Show - 01 [1080p …].mkv`, folder-as-season) →
  titles → seasons → episodes, with configurable regex + manual override.
- Metadata/artwork: Phase-2 option (e.g., Jikan) — no proprietary assets.
- Continue-watching: position beacons per fileId (the POC's roadmap note,
  now scheduled Phase 2).
- The player is reached from any catalog node with one call:
  `MediaEngine.open(fileId)` — the POC's contract, unchanged.

---

## 11. Drive integration (POC gateway + RangeCache)

The POC's gateway (OAuth login/callback/refresh, `/files`, `/stream` 206
proxy with `accept-encoding: identity`) is **kept verbatim as the public
API**. Behind it, one new layer:

**RangeCache** — sparse local file per `(file_id, version)`, **8 MB chunk
granularity**, single-flight (concurrent requests for one chunk share a
fetch), LRU eviction, backoff+jitter on 403/429, per-chunk timeout, and a
self-meter of quota units.

Why it matters:
- FFmpeg's HTTP client issues its own small Range requests (tens of KB–MB).
  Coalesced into 8 MB chunks, a 15 GB read ≈ **1,900 Drive requests total**,
  and *every later read (seek, second viewer, re-probe) costs zero Drive
  egress*.
- Direct Play browsers benefit too: after one watch, the next watch of the
  same file is fully local (instant start, 0 Drive egress).
- One throttle/cache/auth path for ALL readers (browser, ffprobe, ffmpeg
  jobs, subtitle extraction).

**Quota model (verified, current — May 2026 quota-unit overhaul):**
1,000,000 units/min per project, 325,000/min per user; media download ≈ 200
units/request (we count every ranged media request at 200 conservatively)
→ ~1,600 media req/min per user; egress 1 TB/day per project before charges.
Historical per-file daily download caps have also been reported. The
RangeCache makes total Drive egress ≈ *file size × versions*, independent of
play count — safely under any of these. The gateway self-throttles.

**Invariants:** Drive is read-only (no uploads/writes/edits); file version
= `md5Checksum` + `sizeChangeTime` invalidates probe/index/VTT/segments;
`--no-cache` mode = pure POC passthrough (parity testing, low-disk boxes).

---

## 12. Caching strategy

| Layer | Content | Lifetime | Eviction | POC precursor |
|---|---|---|---|---|
| L0 | MediaInfo + keyframe index + plans | Persistent per file version | version change | `PROBE_CACHE` (migrated to SQLite) |
| L1 | WebVTT | Persistent per file version | version change | `SUBS_CACHE` (kept) |
| L2 | **RangeCache** — raw Drive bytes, 8 MB chunks | LRU, default **200 GB** (GPU-PC local disk; configurable; 0 = POC parity) | LRU by file | — (new) |
| L3 | **Segments** — generated fMP4 per (file, planHash) | LRU, default **100 GB** (configurable) | LRU, per-file sub-quotas | `REMUX_DIR` whole-file cache (replaced) |
| L4 | Promotion — whole-file pre-render of heavy transcodes for often-played titles | LRU | LRU | — (Phase 3, Plex-style) |

No permanent converted copy exists in any tier — every derived byte is an
evictable cache entry keyed to the source version. This satisfies
*"avoid a permanent converted copy unless caching is useful"*: here caching
is demonstrably useful (removes repeated Drive egress + quota spend +
transcode CPU; shares work across viewers).

---

## 13. WASM AC3 & AAC-remux panel — disposition

- **`wasm-audio` rung: demoted from the decision ladder.** Rationale
  (unchanged from v1, now concretely testable against the POC):
  - The only major browsers that play HEVC video **and** AC-3 natively are
    Safari — where no decoding work of any kind is needed.
  - Every stock Chromium build that can play the HEVC **cannot** play AC-3 →
    the correct minimal operation is the **server-side audio-only AAC
    transcode** (video copied): robust sync/seek, subtitles via the same
    pipeline, and **cacheable** (the POC's WASM path re-pays decode cost on
    every playback and needed two dedicated fix commits — silent playback,
    drift watchdog — precisely the class of bugs a standard pipeline avoids).
  - For MKV sources the WASM path required a full JS demuxer (mediabunny)
    — exactly the browser-side workaround this architecture eliminates.
  - **Action:** `engine.py` stops emitting `wasm-audio`; `ac3-audio.js` +
    vendored mediabunny/ffmpeg.wasm move to an isolated `legacy/` branch;
    deleted at Phase-1 exit once the AAC rung passes E2E on the real file
    (browsers × seeking × subtitles × audio continuity).
- **AAC-remux panel (`AudioRemuxAdapter` + remux status UI): removed from
  the UI.** Becomes rung 3, selected automatically, visible only in the
  debug chip. The `/remux/*` endpoints are retired after the transition.

---

## 14. Architecture comparison (required)

All options share the decision engine, gateway, and player. They differ in
**how generated output is produced and served**.

### Option A — DASH/CMAF segment origin *(selected, §7)*
Static VOD MPD from probe + keyframe index; segments generated on demand +
prefetch worker; LRU segment cache shared across viewers.

### Option B — whole-file pre-generation + cached file *(the POC's current model; the classic Jellyfin/Plex variant serves the growing file live)*
Background ffmpeg builds the entire converted file (POC: cached MKV; classic:
growing temp MP4 served live, seek = process restart with `-ss`).

### Option C — client-side (WASM/mediabunny)
Browser demuxes/decodes the original stream; server stays a dumb range proxy
(the POC's wasm-audio rung, plus mediabunny client-remux as proposed in the
POC's EVALUATION.md).

| Criterion | A: segment origin | B: whole-file / live | C: client-side |
|---|---|---|---|
| **First playable byte** | **~1–2 s** (one segment) | **minutes** (whole file) / instant if fully cached | instant (client decodes live) |
| Seeking | **O(1)** — fetch one segment (cacheable) | Whole-file: O(1) from disk once built; live variant: restart + re-read penalty ∝ work | In-buffer OK; JS demux+decode |
| Caching / reuse | **Cross-session, cross-viewer** LRU segments | Whole file (large, all-or-nothing) | None (per-playback rework) |
| Drive egress | Each source byte **≤1×** (RangeCache + forward workers) | Full read per build; N builds (multiple planHashes) = N reads | Full read per viewer |
| Server CPU | Bounded, shareable; GPU transcode real-time | Same ffmpeg, but duplicated per build/viewer (live variant) | ~0 |
| Client CPU/battery | 0 (native decode of copied bitstreams) | 0 | **High** (JS/WASM decode, main thread) |
| Concurrency | Scales via shared cache + worker cap | 1 build per (file, config); viewers then fine | Client-bound |
| A/V sync risk | None (standard fMP4 pipeline) | None | Proven fragile (POC's 2 fix commits) |
| New code | Medium & bounded: MPD generator + keyframe index + job manager | **Low** (POC already has it) | Low server, high client, unbounded client bugs |
| Maturity | Shaka+DASH proven (Jellyfin); CMAF standard | Proven (Jellyfin/Plex, POC) | Proven fragile for this corpus |
| Multi-audio / ladders | **First-class** (AdaptationSets) | Awkward (one output file) | No |
| "No permanent copies" | Evictable LRU ✓ | Whole-file cache = large, coarse | n/a |

**Why A:**
1. **First-byte latency is the product** ("click an episode and it just
   plays"). B (as the POC builds it) makes the first viewing of a 15 GB file
   wait for a full copy-speed build from Drive (≈5–10 min); A plays in ~1–2 s
   and caches the rest in the background.
2. **Remote source inverts B's economics.** Jellyfin/Plex read local disk
   with unlimited random access — pre-generation and restart-seeks are cheap
   there. On Drive, every rebuild is quota + egress + minutes; A's forward
   workers read each byte at most once, seeks cost one small job.
3. **Caching becomes genuinely useful** (your stated condition) only with
   addressable output: segments survive sessions and are shared across
   viewers; a whole file is all-or-nothing and plan-specific.
4. **B's low-code advantage is already paid** — the POC built it, and its UX
   (remux panel, status polling, wait-for-whole-file) is exactly what the
   product must eliminate. A's new code is bounded: a static MPD template
   generator, a keyframe index parser, and a job manager around plain ffmpeg.

**Why DASH (CMAF), not HLS:** track separation makes "video copy + audio
transcode" and multi-audio first-class AdaptationSets; a **static complete
VOD manifest** is possible (probe + keyframe index know everything up front)
— no growing-playlist/live semantics; Shaka handles it in production (and
plays HLS too, so the CMAF segments double as an HLS fallback if ever
needed).

**Why not C (including mediabunny client-remux):** it is the architecture
being retired. It fails the HEVC gap (no JS HEVC decoder of consequence),
multiplies client CPU/battery, prevents server caching, and reintroduces
per-browser workarounds. Mediabunny's client-remux idea stays recorded in
the POC evaluation as a "serverless mode" for hypothetical lightweight
deployments — not for this product on a GPU PC.

---

## 15. CPU / storage / bandwidth (server: local PC **with GPU**)

| Mode | Server CPU/GPU | Client bandwidth | Drive egress (1st / later) |
|---|---|---|---|
| Direct Play | ~0 | = source bitrate (1080p HEVC WEB-DL ≈ 10–20 Mbps; 4K ≈ 40–60) | file size / **0** |
| Remux | 3–8% of 1 core (copy) | = source bitrate | file size / 0 |
| Audio-transcode | ~5–10% of 1 core (AAC 5.1) | = source bitrate (video unchanged) | file size / 0 |
| Full transcode 1080p | **NVENC/VideoToolbox: real-time, a few % of GPU** (CPU fallback 1–2 cores) | ≈ 8–15 Mbps (CRF ~21) | file size / 0 |
| Full transcode 4K | real-time on GPU; CPU = degraded (documented) | ≈ 20–35 Mbps | file size / 0 |

- **Storage (defaults, configurable):** RangeCache 200 GB + segment cache
  100 GB + metadata/VTT < 1 GB (local PC disk). `--no-cache` → ~0 (POC
  parity).
- **Latency to first frame:** Direct Play ≈ instant (moov/tail fetch);
  Remux/AAC ≈ 1–2 s; Transcode ≈ 1–5 s CPU / ~1–3 s GPU. Mid-playback stalls:
  none expected in copy tiers; 3-segment prefetch hides transcode latency.
- **Quota:** ≈200 units per 8 MB chunk; a 15 GB first play ≈ 1,900 requests
  spread over the playback — under the ~1,600/min user ceiling with
  self-throttling + backoff as the safety net.

---

## 16. Biggest technical risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | **Drive throttling** (403/429, per-file daily caps) | Stalls on cold plays | RangeCache (each byte ≤1×), 8 MB coalescing, single-flight, backoff, self-quota meter; `--no-cache` passthrough is a working degenerate state |
| 2 | **HEVC device gaps** (Windows w/o extension; Edge fleet data; Linux) | Transcode instead of direct play | Measured capability report gates every decision; conservative defaults; debug chip explains why; GPU transcode makes the fallback pleasant |
| 3 | **MKV without cues + long SRT** | Inaccurate grid / late subs | Cues are the norm in real MKVs; fallback uniform grid + base-time correction; cue-aware extraction, else lazy-prefix VTT with ETag extension; background index pass |
| 4 | **AAC rung: segment-boundary audio glitches** (encoder delay/edit lists) | Tiny pops at seeks | CMAF edit-list handling (Shaka consumes them); real-file E2E is a Phase-1 exit criterion; fallback: 2048-sample overlap padding |
| 5 | **Safari + MSE + HEVC/AC-3 CMAF** edge cases | Rung 2 broken on Apple | Shaka is the proven path; explicit Phase-1 E2E on Safari; the plan's alternatives chain already carries rung 3 as the fallback; HLS emission (same segments) as a last resort |
| 6 | **FFmpeg-HTTP quirks** vs our gateway (range merging, retries, moov-at-end) | Probe/transcode failures | Gateway fully under our control (logged, shapeable); all E2E runs against real Drive files from day one; `fake_drive.py` covers unit-level |
| 7 | **Quota-model drift** (Google changed models May 2026) | Silent degradation | Self-metering + alerts at 80%; passthrough mode |
| 8 | **Naming-based catalog fragility** | Wrong episode mapping | Configurable regexes + manual override; metadata enrichment optional (Phase 2) |
| 9 | **Transition risk**: POC frontend must keep working until the new TS player is ready | Broken playback during migration | Plan contract kept stable (§6.2); new player ships as a separate page, swapped in at the route level; POC UI retained until Phase-1 exit |

---

## 17. Phased plan & acceptance

- **P0 — Import + foundation** (backend)
  1. Import the POC (branch `arena/01a07368-stream`) into this branch —
     verbatim, tests green first.
  2. RangeCache behind `/stream` (configurable size; `--no-cache` parity).
  3. MediaInfo v2: keyframe index (MP4 stss / MKV cues), moov position,
     fragmentation, encryption, Drive version → SQLite.
  4. Engine v2: rungs 1–4 with dash playback URLs + planHash + alternatives;
     wasm-audio demoted; plan cache.
  **Acceptance:** `POST /media/{id}/plan` returns the correct rung for the
  real MKV test file per measured caps (unit tests against the POC's fixture
  style + real-file probes); `/stream` behavior identical to POC
  (existing range tests pass unmodified).
- **P1 — Segment origin + production player**
  DASH routes (MPD generator, init, segment jobs, worker, LRU cache);
  Shaka headless + custom TS control bar (range + dash modes); cue-aware /
  lazy-prefix VTT; **real-file E2E matrix** (Chrome, Safari × rungs × seek /
  subtitles / audio continuity / PiP / speed). **Exit criteria:** the WASM
  AC3 path is removed; the AAC-remux panel is gone; *the real test file
  plays with one click in Chrome (rung 3) and Safari (rung 2), subtitles +
  seeking working, no user-facing codec decisions.*
- **P2 — Full transcode + polish**
  Rung 4 with NVENC/VideoToolbox; prefetch tuning; library catalog (naming
  parser, seasons/episodes), continue-watching, skip-intro; session debug
  UI; quota monitor; LRU tuning.
- **P3 — Optional**
  Promotion/pre-render cache, quality ladders + ABR, multi-audio switching
  UI, HDR handling options, PGS burn-in, metadata/artwork enrichment.

---

## 18. Open items

- **Q4 (one confirmation left) — backend language.** The POC is
  **Python/FastAPI** with a working test suite; my earlier stack proposal
  said Node/TS and you selected it — but your hard constraint is to
  *preserve the working Drive auth + range-streaming code*, and porting
  proven OAuth/range/probe/subtitle code (+ its tests) to Node is pure
  re-validation risk with zero playback benefit. **Recommendation: keep the
  FastAPI backend** (extend it in place); use **TypeScript for the new
  frontend**, where the production investment actually goes. Confirm or
  override.
- Q1–Q3 resolved by your answers: POC = branch `arena/01a07368-stream`
  (imported at P0) · test file = **MKV** (acceptance: Safari → rung 2,
  Chrome → rung 3) · server = **local PC with GPU** (NVENC/VideoToolbox;
  200/100 GB cache defaults).
