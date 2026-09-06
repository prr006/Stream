/**
 * ac3-audio.js — browser-side AC3/E-AC3 playback for the Drive streaming POC.
 *
 * Pipeline (all in-page, zero server changes, original MKV untouched):
 *
 *   /stream/{fileId}  (existing range proxy)
 *        │  HTTP Range reads (lazy; UrlSource.onread tracks bytes)
 *        ▼
 *   mediabunny  EncodedPacketSink   → raw AC3 packets
 *        │  batches of ~64 packets ≈ 2 s
 *        ▼
 *   ffmpeg-core.wasm  exec('ffmpeg -f ac3 -i …' → f32le 48 kHz stereo PCM)
 *        │  Float32 PCM (+RMS/peak telemetry)
 *        ▼
 *   Web Audio  AudioBufferSourceNode scheduling, anchored mediaTime↔ctxTime
 *        │  drift watchdog vs video.currentTime
 *        ▼
 *   speakers
 *
 * V0 scope: sequential packet flow (cue-indexed restart on seek via
 * getKeyPacket(t)), bounded decode-ahead horizon, first AC3/E-AC3 track,
 * playbackRate = 1 only.
 *
 * Debuggability: every pipeline stage reports into getDiagnostics() and the
 * onStats/onError callbacks; a 440 Hz testTone() drives the exact same
 * output chain to isolate WebAudio output from AC3 decoding.
 *
 * No top-level imports/side effects → fully unit/integration testable in
 * Node with a fake AudioContext (see tests/ac3_wasm_test.mjs).
 */

export const CFG = {
  sampleRate: 48000,
  batchPackets: 64,        // AC3 frame = 32 ms → 64 packets ≈ 2.05 s
  maxHorizonS: 30,         // stop decoding ahead beyond this (bounded buffering)
  refillHorizonS: 18,
  driftHardS: 0.150,
  driftSoftS: 0.045,
  minLeadS: 0.03,
  watchdogMs: 250,
  primeDelayS: 0.20,
};

/* ------------------------------------------------------------------ *
 *  Pure scheduling math (unit-tested)                                 *
 * ------------------------------------------------------------------ */

export function estimateMediaTime(anchorCtxT, anchorMediaT, nowCtx, rate = 1) {
  return anchorMediaT + (nowCtx - anchorCtxT) * rate;
}

/**
 * Decide how to schedule a PCM chunk covering [chunkStart, chunkStart+chunkDur).
 * Returns {action:"skip"} or {action:"play", when, offset, end} where `when`
 * is a ctx.currentTime-based start and `offset` trims past audio (seconds).
 */
export function planSchedule(args) {
  const {
    anchorCtxT, anchorMediaT, chunkStart, chunkDur,
    nowCtx, rate = 1, minLead = CFG.minLeadS,
  } = args;
  let when = anchorCtxT + (chunkStart - anchorMediaT) / rate;
  const end = when + chunkDur / rate;
  if (end <= nowCtx + 1e-6) return { action: "skip" };
  if (when < nowCtx + minLead) {
    const offsetMedia = (nowCtx + minLead - when) * rate;
    if (offsetMedia >= chunkDur) return { action: "skip" };
    return { action: "play", when: nowCtx + minLead, offset: offsetMedia,
             end: nowCtx + minLead + (chunkDur - offsetMedia) / rate };
  }
  return { action: "play", when, offset: 0, end };
}

/* ------------------------------------------------------------------ */

const AC3ISH = /^(ac-?3|e-?ac-?3|ec-3)$/i;

export class Ac3WasmAudio {
  /**
   * @param {HTMLVideoElement} video  master clock (plays muted)
   * @param {string} fileId           Drive file id → backend /stream endpoint
   * @param {object} opts  { vendorBase, streamUrl, log, onStats, onError,
   *                         audioContextFactory, coreModuleOverrides }
   */
  constructor(video, fileId, opts = {}) {
    this.video = video;
    this.fileId = fileId;
    this.vendorBase = (opts.vendorBase ?? "/static/vendor").replace(/\/$/, "");
    this.streamUrl = opts.streamUrl ?? `/stream/${fileId}`;
    this.log = opts.log ?? ((m) => console.log("[ac3wasm]", m));
    this.onStats = opts.onStats ?? (() => {});
    this.onError = opts.onError ?? ((e) => console.error("[ac3wasm]", e));
    // Testability seams (Node): injected fake AudioContext + preloaded wasm.
    this._ctxFactory = opts.audioContextFactory ?? null;
    this._coreOverrides = opts.coreModuleOverrides ?? {};

    this.ctx = null; this.core = null; this.input = null; this.sink = null;
    this.codec = null; this.ffmt = "ac3";
    this.disposed = false;
    this.gen = 0;
    this._iter = null;             // current async iterator (for cancel)
    this.scheduled = new Set();
    this.horizonMedia = -Infinity;
    this.anchorCtxT = 0; this.anchorMediaT = 0;
    this.packetsRead = 0; this.bytesFetched = 0;
    this.decodedMediaS = 0;
    this.lastRms = 0; this.lastPeak = 0; this.lastDecMs = 0;
    this.buffersStarted = 0; this.nextStartLeadS = null;
    this.eof = false; this.err = null;
    this._watchdog = null;
    this._handlers = [];
    this._driftStreak = 0;         // consecutive polls beyond driftHardS
    this._lastWaitingAt = -1e9;    // last 'waiting'/'seeking' (stall guard)
  }

  /* ---------------- setup (runs on the Enable click = user gesture) ------ */

  async init() {
    const [mb, coreMod] = await Promise.all([
      import(`${this.vendorBase}/mediabunny/dist/bundles/mediabunny.min.mjs`),
      import(`${this.vendorBase}/ffmpeg-core/dist/esm/ffmpeg-core.js`),
    ]);
    this._mb = mb;

    this.ctx = this._ctxFactory
      ? this._ctxFactory()
      : new (window.AudioContext)({ sampleRate: CFG.sampleRate });
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);        // (6) chain proves below
    // init() is reached from the Enable button (a user gesture) → resume is legal.
    if (this.ctx.state === "suspended") { try { await this.ctx.resume(); } catch {} }
    this.log(`AudioContext: state=${this.ctx.state} sampleRate=${this.ctx.sampleRate}Hz, ` +
             `gain(1.0) → destination ✓`);

    this.log("loading ffmpeg-core.wasm (~32 MB, one-time)…");
    this.core = await coreMod.default(this._coreOverrides);
    this.log("wasm runtime ready");

    const source = new mb.UrlSource(this.streamUrl);
    source.onread = (start, end) => { this.bytesFetched += end - start; };  // (1) fetch proof
    this.input = new mb.Input({ source, formats: mb.ALL_FORMATS });
    const tracks = await this.input.getAudioTracks();
    const infos = await Promise.all(tracks.map(async (t) => ({ t, c: await t.getCodec() })));
    const hit = infos.find(({ c }) => AC3ISH.test(String(c).replace(/_/g, "-")));
    if (!hit) {
      throw new Error("no AC3/E-AC3 audio track found " +
        `(audio tracks: ${JSON.stringify(infos.map((i) => i.c))})`);
    }
    this.track = hit.t; this.codec = hit.c;
    this.ffmt = /e/i.test(String(this.codec)) ? "eac3" : "ac3";
    this.sink = new mb.EncodedPacketSink(this.track);
    this.log(`audio track: ${this.codec} — demux via mediabunny, decode via ffmpeg.wasm`);
  }

  /** Wire to the video element; audio pump starts on 'play'. */
  attach() {
    const v = this.video;
    const on = (ev, fn) => { v.addEventListener(ev, fn); this._handlers.push([ev, fn]); };
    on("play",    () => this.resumeFrom(v.currentTime));
    on("seeking", () => { this._lastWaitingAt = performance.now();
                          this._suspend(); this._stopScheduled(); });
    on("seeked",  () => { if (!v.paused) this.resumeFrom(v.currentTime); });
    on("waiting", () => { this._lastWaitingAt = performance.now(); this._suspend(); });
    on("playing", () => { if (!v.paused) this._resumeClock(); });
    on("pause",   () => this._suspend());
    on("ended",   () => { this._suspend(); this._stopScheduled(); });
    on("emptied", () => this.dispose());
    this._watchdog = setInterval(() => this._checkDrift(), CFG.watchdogMs);
  }

  /* ---------------- clock / anchor ---------------- */

  _anchor(mediaT, extraLead = CFG.primeDelayS) {
    // The prime lead buys decode headroom. Crucially it must NOT shift the
    // audio timeline: anchor the media clock extraLead AHEAD too, so
    // mediaNow() ≈ video.currentTime from the first millisecond (chunks in
    // [mediaT, mediaT+lead) simply get trimmed by planSchedule). Anchoring at
    // bare mediaT used to bake in a permanent +lead offset → the drift
    // watchdog saw constant ~200 ms error → re-anchor storm → silence.
    this.anchorMediaT = mediaT + extraLead;
    this.anchorCtxT = this.ctx.currentTime + extraLead;
  }
  _mediaNow() {
    return estimateMediaTime(this.anchorCtxT, this.anchorMediaT, this.ctx.currentTime);
  }

  /* ---------------- pump (demux → decode → schedule) ---------------- */

  async resumeFrom(t) {
    if (this.disposed || !this.core) return;
    if (this.ctx.state !== "running") await this.ctx.resume();
    this._stopScheduled();
    this.gen += 1;
    const g = this.gen;
    // Cancel any still-running previous pump iterator (avoid interleaved reads).
    if (this._iter) { const it = this._iter; this._iter = null;
                      try { await it.return?.(); } catch (_) {} }
    this._anchor(t);
    this.horizonMedia = t;
    this.eof = false; this.err = null;
    this.log(`audio pump start @ ${t.toFixed(2)}s (ctx.state=${this.ctx.state})`);
    this._pump(t, g).catch((e) => {
      if (this.disposed || g !== this.gen) return;
      this.err = String(e?.message || e);
      this.onError(this.err);               // surface to the UI, not just console
      this.log(`pump error: ${this.err}`);
    });
  }

  _concat(parts, total) {            // ← was missing in the first cut (root cause)
    const out = new Uint8Array(total);
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.byteLength; }
    return out;
  }

  async _pump(fromT, g) {
    let iterable;
    if (fromT > 0) {
      const startPkt = await this.sink.getKeyPacket(fromT);
      if (!startPkt) {                 // seek target beyond last audio packet
        this.eof = true;
        this.log("seek target beyond last audio packet — no audio from here");
        return;
      }
      iterable = this.sink.packets(startPkt);
    } else {
      iterable = this.sink.packets();  // from the beginning (V0 sequential)
    }
    const it = iterable[Symbol.asyncIterator]();
    if (g === this.gen) this._iter = it;

    let batch = [], batchBytes = 0, batchStart = null, n = 0;
    const flush = async () => {
      if (n && !this.disposed && g === this.gen) {
        await this._decodeSchedule(this._concat(batch, batchBytes), batchStart, g);
      }
      batch = []; batchBytes = 0; n = 0; batchStart = null;
    };

    for (;;) {
      const res = await it.next();
      if (res.done) break;
      if (this.disposed || g !== this.gen) { try { await it.return?.(); } catch (_) {} return; }
      const pkt = res.value;
      await this._gateHorizon(g);
      if (batchStart === null) batchStart = pkt.timestamp;
      batch.push(pkt.data); batchBytes += pkt.data.byteLength; n++;
      this.packetsRead++;
      // Each packet holds whole AC3 frames; concatenated packets form a valid
      // AC3 elementary stream — no JS frame splitting needed.
      if (n >= CFG.batchPackets) await flush();
    }
    await flush();
    if (g === this.gen && !this.disposed) { this.eof = true; this.log("audio pump reached EOF"); }
    this._iter = null;
  }

  async _gateHorizon(g) {
    while (!this.disposed && g === this.gen &&
           this.horizonMedia - this._mediaNow() > CFG.maxHorizonS) {
      await new Promise((r) => setTimeout(r, CFG.watchdogMs));
    }
  }

  async _decodeSchedule(bytes, mediaStart, g) {
    // Per-generation MEMFS paths — a stale pump of a previous generation can
    // never corrupt this generation's decode buffers.
    const inPath = `/in_${g}.ac3`, outPath = `/out_${g}.f32`;
    const t0 = performance.now();
    this.core.FS.writeFile(inPath, bytes);
    const code = this.core.exec(                    // NOTE: exec is varargs
      "-hide_banner", "-loglevel", "error",
      "-f", this.ffmt, "-i", inPath,
      "-ac", "2", "-ar", String(CFG.sampleRate),   // (3) fixed f32le stereo 48k
      "-f", "f32le", outPath,
    );
    let pcmBytes = new Uint8Array(0);
    try { if (code === 0) pcmBytes = this.core.FS.readFile(outPath); }
    finally {
      try { this.core.FS.unlink(inPath); } catch (_) {}
      try { this.core.FS.unlink(outPath); } catch (_) {}
    }
    this.lastDecMs = performance.now() - t0;
    if (this.disposed || g !== this.gen) return;
    if (code !== 0 || pcmBytes.byteLength === 0) {
      if (code !== 0) { this.err = `ffmpeg decode exit ${code}`; this.onError(this.err); }
      return;
    }

    // (2) PCM verification: frames + RMS/peak telemetry
    const frames = pcmBytes.byteLength / 8;          // f32 stereo = 8 B/frame
    const dur = frames / CFG.sampleRate;
    const f32 = new Float32Array(pcmBytes.buffer, pcmBytes.byteOffset, frames * 2);
    const L = new Float32Array(frames), R = new Float32Array(frames);
    let sum = 0, peak = 0;
    for (let i = 0; i < frames; i++) {
      const l = f32[2 * i], r = f32[2 * i + 1];
      L[i] = l; R[i] = r;
      if (!(i & 63)) { const e = l * l + r * r; sum += e;
                       if (Math.abs(l) > peak) peak = Math.abs(l); }
    }
    this.lastRms = Math.sqrt(sum / Math.ceil(frames / 64));
    this.lastPeak = peak;

    const plan = planSchedule({
      anchorCtxT: this.anchorCtxT, anchorMediaT: this.anchorMediaT,
      chunkStart: mediaStart, chunkDur: dur, nowCtx: this.ctx.currentTime,
    });
    if (plan.action === "skip") { this._emitStats(); return; }

    const buf = this.ctx.createBuffer(2, frames, CFG.sampleRate);
    buf.copyToChannel(L, 0); buf.copyToChannel(R, 1);
    const src = this.ctx.createBufferSource();
    src.buffer = buf; src.connect(this.gain);        // (6) node → gain → destination
    try {
      src.start(plan.when, plan.offset);             // (5) start() call recorded
    } catch (e) { this.onError(`source.start failed: ${e}`); return; }
    src.onended = () => this.scheduled.delete(src);
    this.scheduled.add(src);
    this.buffersStarted++;
    this.nextStartLeadS = plan.when - this.ctx.currentTime;   // (7) past-schedule proof
    this.horizonMedia = Math.max(this.horizonMedia, mediaStart + dur);
    this.decodedMediaS += dur;
    this._emitStats();
  }

  _emitStats() {
    this.onStats({
      decodedS: this.decodedMediaS, packets: this.packetsRead,
      horizonS: this.horizonMedia - this._mediaNow(),
      decMs: Math.round(this.lastDecMs),
      rms: this.lastRms, peak: this.lastPeak,
      buffersStarted: this.buffersStarted, eof: this.eof,
    });
  }

  /* ---------------- video-clock following ---------------- */

  _stopScheduled() {
    for (const s of this.scheduled) { try { s.stop(); } catch (_) {} }
    this.scheduled.clear();
  }
  async _suspend() { if (this.ctx?.state === "running") await this.ctx.suspend(); }
  async _resumeClock() {
    if (this.disposed || !this.ctx) return;
    await this.ctx.resume();
    const v = this.video.currentTime;
    if (Math.abs(v - this.anchorMediaT) > CFG.driftSoftS) this._anchor(v, CFG.minLeadS * 3);
  }

  _checkDrift() {
    if (this.disposed || !this.video || this.video.paused ||
        this.ctx?.state !== "running" || !this.scheduled.size) return;
    const v = this.video;
    // Stall-safety: a frozen video clock (network stall / seek in flight) is
    // NOT audio drift. Re-anchoring here would flap the whole pipeline —
    // cancel pump → re-decode → cancel → … (~every 250 ms) — which is exactly
    // the failure mode this watchdog previously caused. Require sustained
    // evidence instead: 3 consecutive out-of-tolerance polls (~750 ms) with
    // the video demonstrably playing (readyState ≥ HAVE_FUTURE_DATA).
    if (v.seeking || v.readyState < 3 ||
        performance.now() - this._lastWaitingAt < 1500) {
      this._driftStreak = 0;
      return;
    }
    const diff = v.currentTime - this._mediaNow();
    if (Math.abs(diff) > CFG.driftHardS) {
      if (++this._driftStreak < 3) return;
      this._driftStreak = 0;
      this.log(`drift ${(diff * 1000).toFixed(0)} ms sustained ≥750 ms → hard re-anchor`);
      this.resumeFrom(v.currentTime);
    } else if (Math.abs(diff) > CFG.driftSoftS) {
      this._driftStreak = 0;
      this.anchorMediaT += diff * 0.5;             // glide toward the video clock
    } else {
      this._driftStreak = 0;
    }
  }

  /* ---------------- diagnostics & tone ---------------------------------- */

  /** Full stage-by-stage snapshot for the live diagnostics line. */
  getDiagnostics() {
    const estT = this.ctx ? this._mediaNow() : null;
    return {
      ctxState: this.ctx?.state ?? "n/a",
      sampleRate: this.ctx?.sampleRate ?? 0,
      pcmFormat: "f32le 2ch 48000Hz",
      videoT: this.video?.currentTime ?? null,
      audioEstT: estT,
      driftMs: (this.video && estT != null)
        ? (this.video.currentTime - estT) * 1000 : null,
      scheduledPending: this.scheduled.size,
      buffersStarted: this.buffersStarted,
      nextStartLeadS: this.nextStartLeadS,
      horizonS: estT != null ? this.horizonMedia - estT : null,
      rms: this.lastRms, peak: this.lastPeak,
      decodedS: this.decodedMediaS,
      packets: this.packetsRead,
      bytesFetched: this.bytesFetched,
      eof: this.eof, error: this.err,
      driftStreak: this._driftStreak,
      readyState: this.video?.readyState ?? null,
      videoMuted: this.video?.muted ?? null,
      gainValue: this.gain?.gain?.value ?? null,
    };
  }

  /**
   * 440 Hz sine through the SAME output chain (local gain → master gain →
   * destination). If this is audible while AC3 is silent, WebAudio/browser
   * output is proven fine and the fault is upstream (demux/decode).
   */
  testTone(seconds = 1.5) {
    if (!this.ctx || !this.gain) return;
    this.ctx.resume?.();
    const osc = this.ctx.createOscillator();
    const post = this.ctx.createGain();
    post.gain.value = 0.25;
    osc.frequency.value = 440;
    osc.connect(post); post.connect(this.gain);
    osc.start();
    osc.stop(this.ctx.currentTime + seconds);
    this.log(`test tone: 440 Hz for ${seconds}s through the WASM audio chain ` +
             `(ctx.state=${this.ctx.state})`);
  }

  dispose() {
    this.disposed = true;
    this.gen += 1;
    clearInterval(this._watchdog);
    for (const [ev, fn] of this._handlers) this.video.removeEventListener(ev, fn);
    this._handlers = [];
    this._stopScheduled();
    try { this.input?.dispose(); } catch (_) {}
    try { this.ctx?.close(); } catch (_) {}
    this.log("disposed");
  }
}
