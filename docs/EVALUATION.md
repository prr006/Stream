# Open-source evaluation — browser media engines (recorded before writing
# any more custom WASM infrastructure)

**Verdict up front:** the two browser-side pieces we need — (a) demux/packet
access and (b) WASM codec decode — are already covered by existing,
actively-maintained open-source projects (**mediabunny** + **@ffmpeg/core**),
both vendored pinned since milestone 2b. The "media engine" itself — the
Direct Play ladder — is ~200 lines of policy; no open-source project offers
that as an embeddable library, so we own it (`backend/engine.py`). Details
per candidate below.

## 1. Jellyfin / Emby / Plex (reference architecture, not a library)

The Direct Play / Direct Stream / Transcode ladder is Jellyfin's model.
Takeaway adopted: *the server decides a plan from (media info × device
profile); clients execute the plan and report capabilities*. Their web
client's codec support matrix and per-track decision reasons inspired our
plan shape, but their code is entangled in a full server + UI monolith —
**not importable**. We re-implement the pattern minimally.

## 2. mediabunny 1.55.7 — ✅ CHOSEN (demux, packet access, future remux)

Zero-dependency, web-native MP4/MKV/WebM/MP3 reader-muxer.

* What we use **today (mode 2)**: `Input` + `UrlSource` over our range proxy
  (lazy Range reads — verified: 0.61 MB touched for a 2 s window of a 1 MB
  file), `EncodedPacketSink` for AC3 packet iteration, `getKeyPacket(t)`
  for cue-accurate seek restarts.
* What it unlocks **next (mode 3)**: real time MKV→fMP4 packet **copy**
  (no re-encode) muxed into MSE — i.e. true client-side Direct Stream:
  original bitstreams, original bytes off the network, only the container
  rebuilt. `Output` + `Mp4OutputFormat` + `StreamTarget` are designed for
  exactly this (its docs' canonical example).
* What it deliberately is not: a codec decoder (decode paths go through
  WebCodecs, which is exactly what stock Chromium lacks for AC3 — the
  hole our WASM layer fills).

## 3. @ffmpeg/core (ffmpeg.wasm) 0.12.10 — ✅ KEPT (WASM codec decode)

Full FFmpeg compiled to WASM; we drive `exec("ffmpeg -f ac3 -i … -f f32le")`
per ~2 s packet batch, main thread, single-threaded build (avoids
COOP/COEP isolation requirements that would break Drive range streaming).
Measured 27–40 ms per 2 s chunk (~75× realtime) — fine on the main thread.

* Pros: complete AC3/E-AC3 (and everything else) coverage; debugged in
  place; test-backed end to end against the real proxy.
* Cons: 32 MB wasm (one-time, range-cacheable), MEMFS round-trips, runs
  on the main thread.

## 4. libav.js 4.5.x (Yahweasel) — shortlisted, NOT adopted now

Emscripten builds of libavformat/libavcodec with a promise API identical in
or out of Web Workers (worker by default), modular variant builds, and
*ruthless* LGPL hygiene (ships sources alongside).

* Advantages over ffmpeg.wasm for us: streaming decode API without MEMFS
  file round-trips; worker-native (jank isolation); smaller payloads via
  per-feature variants.
* Blockers for our specific need: the **default variant covers Opus/FLAC/
  WAV/AAC-in-m4a — NOT AC3/E-AC3**. AC3 needs a custom variant build
  (their fragment system), i.e. new WASM build infra — exactly what we were
  told to evaluate before creating. LGPL compliance also means shipping
  sources on distribution.
* Decision: keep ffmpeg.wasm now; the decode backend sits behind one
  interface (`WasmAudioAdapter` → `Ac3WasmAudio`), so a libav.js swap
  (with a custom AC3 variant build) is a single-adapter change later,
  motivated only if profiling shows main-thread jank.

## 5. hls.js / Shaka — ❌ not applicable to the cheap rungs

Both are ABR players for HLS/DASH **transcoded** outputs — i.e. rung 5
(full transcode + segmenting). They solve a problem this architecture
explicitly defers, and introduce packaging (manifest/segments) that
contradicts "original bytes whenever possible". Revisit only if/when
mode 5 is built; MediaSource Extensions (native MSE) suffice for mode 3.

## 6. Codec capability detection — native web APIs, no library

`HTMLMediaElement.canPlayType` + `MediaSource.isTypeSupported` +
`MediaCapabilities.decodingInfo` (async refinement) in
`frontend/media/capabilities.js`. Static tables (jellyfin-style
browser profiles) are a fallback for clients that don't report; the server
treats unreported capabilities as *unsupported* and documents probes so the
matrix is honest. This matches how Jellyfin 10.9+ shifted from hardcoded
profiles toward measured support — with a fraction of the code.

## Sources

* Yahweasel/libav.js README + unpkg copy (variants, worker loading,
  default-variant codec set) — github.com/Yahweasel/libav.js
* Mediabunny docs/examples (Range-lazy UrlSource; Mp4Output/Muxer
  packet-copy remux example) — github.com/Vanilagy/mediabunny,
  webcodecsfundamentals.org
* ffmpegwasm/ffmpeg.wasm (core variants, threading requirements)
* Ecosystem context: news.ycombinator discussion on WASM A/V decoders
  (browser codec support hole motivation), artplayer.org's
  mediabunny-based remuxer demo (proof the client-remux path is real).
