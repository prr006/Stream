# MyStream architecture — media-engine redesign (milestone 3)

MyStream is a Google-Drive-backed web media server built around one rule:

> **The browser receives original bytes whenever it can play them; the system
> applies the *minimum* intervention needed — and every UI renders a plan,
> never a codec.**

## Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│ PRESENTATION  (frontend/index.html + app.js)                        │
│   thin pages: list files, render the plan badge/reasons/steps,      │
│   wire hooks. ZERO codec knowledge. Full library UX slots in here.  │
├─────────────────────────────────────────────────────────────────────┤
│ PLAYBACK SESSION  (frontend/media/)                                 │
│   engine.js        MediaEngine.open(fileId) → PlaybackSession       │
│   capabilities.js  measures THIS browser (canPlayType/MSE/MediaCap) │
│   adapters.js      one adapter per delivery mode, uniform contract  │
│   subtitles.js     SubtitleManager (track elements, showing modes)  │
├─────────────────────────────────────────────────────────────────────┤
│ DECISION          (backend/engine.py)                               │
│   decide(probe, caps) → plan — PURE, fully unit-tested              │
│   POST /media/{id}/plan   (browser posts measured caps)             │
├─────────────────────────────────────────────────────────────────────┤
│ DELIVERY          (backend/app.py + media.py, pre-existing)         │
│   /stream/{id} range proxy · /subtitles/*.vtt extraction ·          │
│   /remux/* AAC sidecar · on-disk caches                             │
├─────────────────────────────────────────────────────────────────────┤
│ INSPECTION        (backend/media.py, pre-existing)                  │
│   probe_media(): container/codec/stream inventory via ffprobe       │
├─────────────────────────────────────────────────────────────────────┤
│ STORAGE           Google Drive (read-only)                          │
└─────────────────────────────────────────────────────────────────────┘
```

## The decision ladder (ordered by minimum intervention)

| # | mode | what happens | status |
|---|------|--------------|--------|
| 1 | `direct-play` | original bytes → `<video>`; browser demuxes+decodes | ✅ shipped |
| 2 | `wasm-audio` | original bytes; browser-native video; AC3/E-AC3 demuxed in-browser (mediabunny) and decoded by ffmpeg.wasm → Web Audio | ✅ shipped |
| 3 | `container-remux` | codecs fine, container wrong → copy-only rewrap (planned: client-side MKV→fMP4 → MSE via mediabunny, or server repack) | 🚧 modeled, adapter stubbed (`PlanUnavailable`) |
| 4 | `audio-remux` | audio codec unsupported & out of WASM scope → one-time server audio→AAC; video bitstream copied | ✅ shipped |
| 5 | `transcode` | full video re-encode — absolute last resort | 🚧 modeled, deliberately unimplemented |

`decide()` returns the ladder answer *plus* the full `alternatives[]` list of
other implemented modes, so a page always has a working fallback control.

## Plan contract (server → page)

```jsonc
{
  "mode": "wasm-audio",
  "implemented": true,
  "reasons": ["…"],                  // why this mode — rendered verbatim
  "steps": [ {"stream":"container","action":"source","detail":"…"}, … ],
  "playback": {"url":"/stream/<id>","mime":"video/x-matroska"},
  "audio":  {"action":"wasm-decode","codec":"ac3","module":"/static/ac3-audio.js"},
  "audioTracks": [ {"index":1,"codec":"ac3","nativePlayable":false,
                    "wasmDecodable":true,"remuxable":true}, … ],
  "subtitles": [ … with VTT urls … ],
  "probe": { container, duration, video, probe_engine },
  "alternatives": [ {"mode":"audio-remux","implemented":true, "startUrl":…} ]
}
```

Capability matrix direction: **browser measures → POSTs → server decides.**
The server's `DEFAULT_CAPABILITIES` are only a conservative curl/debug fallback;
unreported keys normalize to *unsupported* (never assume).

## Extension points (no page edits required)

* new codec support claim → capability matrix only (`frontend/media/capabilities.js`)
* new decoder coverage  → `WASM_DECODABLE_AUDIO` (backend) + decoder module
* new delivery mode      → decision branch in `decide()` + one adapter
* new page (library, season browser, watch status) → consumes
  `plan`/`audioTracks`/subtitles — which already carry everything needed

## Roadmap mapping (streaming-site UX on top of this base)

* **Libraries / posters / seasons** → metadata layer keyed on the same
  `fileId`; pages still only call `engine.open(item.id, …)`.
* **Continue watching** → position beacons stored server-side per fileId;
  `PlaybackSession` already owns start/stop of playback.
* **Client-side container remux (mode 3 implementation)** → mediabunny
  packet copy (MKV→fMP4) into MSE — evaluated in docs/EVALUATION.md.
* **Track switching** → `audioTracks[]` already flags every track; the WASM
  engine needs a track-select argument (one-line constructor change).
* **Full transcode (mode 5)** → only after 1–4 are proven in practice.
