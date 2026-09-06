/**
 * ac3-audio.js — browser-side AC3/E-AC3 playback for the Drive streaming POC.
 *
 * Pipeline (all in-page, zero server changes, original MKV untouched):
 *
 *   /stream/{fileId}  (existing range proxy)
 *        │  HTTP Range reads (lazy)
 *        ▼
 *   mediabunny  UrlSource + EncodedPacketSink   → raw AC3 packets
 *        │  batches of ~2 seconds
 *        ▼
 *   ffmpeg-core.wasm  exec('ffmpeg -f ac3 -i …' → f32le 48 kHz stereo PCM)
 *        │  Float32 PCM
 *        ▼
 *   Web Audio  AudioBufferSourceNode scheduling, anchored mediaTime↔ctxTime
 *        │  drift watchdog vs video.currentTime
 *        ▼
 *   speakers
 *
 * V0 scope: sequential packet flow (restarted on seek via mediabunny's
 * cue-indexed packets(fromT) when available), bounded decode-ahead horizon,
 * single AC3/E-AC3 track, playbackRate = 1 only.
 *
 * The module has NO top-level imports/side effects so Node.js can unit-test
 * the pure scheduling helpers (planSchedule / estimateMediaTime) and drive a
 * full demux+decode integration run without a browser.
 */

export const CFG = {
  sampleRate: 48000,
  batchPackets: 64,        // AC3 frame = 32 ms → 64 packets ≈ 2.05 s of audio
  maxHorizonS: 30,         // stop decoding ahead beyond this (bounded buffering)
  refillHorizonS: 18,      // resume decoding below this
  driftHardS: 0.150,       // |video - audio estimate| above ⇒ full re-anchor
  driftSoftS: 0.045,       // between soft..hard ⇒ nudge anchor (glide)
  minLeadS: 0.03,          // never schedule into the past / immediate present
  watchdogMs: 250,
  primeDelayS: 0.20,       // initial scheduling lead after (re)anchor
};

/* ------------------------------------------------------------------ *
 *  Pure scheduling math (unit-tested in Node)                         *
 * ------------------------------------------------------------------ */

/** Estimate media time from the anchor pair using the AudioContext clock. */
export function estimateMediaTime(anchorCtxT, anchorMediaT, nowCtx, rate = 1) {
  return anchorMediaT + (nowCtx - anchorCtxT) * rate;
}

/**
 * Decide how to schedule a PCM chunk whose media interval is
 * [chunkStart, chunkStart+chunkDur).
 * Returns {action:'skip'} or {action:'play', when, offset, end}:
 *   when   = ctx.currentTime-based start time
 *   offset = seconds into the chunk to start from (dropping past audio)
 *   end    = ctx time when the audible part ends (horizon bookkeeping)
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
    const offsetMedia = (nowCtx + minLead - when) * rate; // trim the past part
    if (offsetMedia >= chunkDur) return { action: "skip" };
    const playDur = chunkDur - offsetMedia;
    return { action: "play", when: nowCtx + minLead, offset: offsetMedia,
             end: nowCtx + minLead + playDur / rate };
  }
  return { action: "play", when, offset: 0, end };
}

/* ------------------------------------------------------------------ *
 *  Player                                                             *
 * ------------------------------------------------------------------ */

const AC3ISH = /^(ac-?3|e-?ac-?3|ec-3)$/i;

export class Ac3WasmAudio {
  /**
   * @param {HTMLVideoElement} video  master clock (plays muted)
   * @param {string} fileId           Drive file id (backend /stream endpoint)
   * @param {object} opts             { vendorBase, log, onStats }
   */
  constructor(video, fileId, opts = {}) {
    this.video = video;
    this.fileId = fileId;
    this.vendorBase = opts.vendorBase ?? "/static/vendor";
    this.log = opts.log ?? ((m) => console.log("[ac3wasm]", m));
    this.onStats = opts.onStats ?? (() => {});
    this.streamUrl = opts.streamUrl ?? `/stream/${fileId}`;

    this.ctx = null; this.core = null; this.input = null; this.sink = null;
    this.codec = null; this.ffmt = "ac3";
    this.disposed = false;
    this.gen = 0;                  // pump generation; bump = invalidate loop
    this.scheduled = new Set();    // live AudioBufferSourceNodes
    this.horizonMedia = -Infinity; // media-time end of scheduled audio
    this.anchorCtxT = 0; this.anchorMediaT = 0;
    this.decodedMediaS = 0; this.packetsRead = 0;
    this.eof = false;
    this._watchdog = null;
    this._handlers = [];
    this._startT = 0;
  }

  /* ---------------- setup ---------------- */

  async init() {
    const [mb, coreMod] = await Promise.all([
      import(`${this.vendorBase}/mediabunny/dist/bundles/mediabunny.min.mjs`),
      import(`${this.vendorBase}/ffmpeg-core/dist/esm/ffmpeg-core.js`),
    ]);
    this._mb = mb;

    this.ctx = new (window.AudioContext)({ sampleRate: CFG.sampleRate });
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);

    this.log("loading ffmpeg-core.wasm (~32 MB, one-time)…");
    this.core = await coreMod.default();
    this.log("wasm runtime ready");

    this.input = new mb.Input({
      source: new mb.UrlSource(this.streamUrl),
      formats: mb.ALL_FORMATS,
    });
    const tracks = await this.input.getAudioTracks();
    for (const t of tracks) {
      const c = (await t.getCodec()) || "";
      if (AC3ISH.test(c.replace(/_/g, "-"))) { this.track = t; this.codec = c; break; }
    }
    if (!this.track) {
      throw new Error("no AC3/E-AC3 audio track found in this file " +
        `(audio tracks: ${await Promise.all(tracks.map(async t => await t.getCodec()))})`);
    }
    this.ffmt = /e/i.test(this.codec) ? "eac3" : "ac3";
    this.channels = await this.track.getNumberOfChannels().catch(() => 0);
    this.sink = new mb.EncodedPacketSink(this.track);
    this.log(`audio track: ${this.codec}, ${this.channels || "?"}ch — demux via mediabunny, decode via ffmpeg.wasm`);
  }

  /** Wire to the video element and idle until first 'play'. */
  attach() {
    const v = this.video;
    const on = (ev, fn) => { v.addEventListener(ev, fn); this._handlers.push([ev, fn]); };

    on("play", () => this.resumeFrom(v.currentTime));
    on("seeking", () => { this._seekTarget = v.currentTime; this._suspend(); this._stopScheduled(); });
    on("seeked", () => { if (!v.paused) this.resumeFrom(v.currentTime); });
    on("waiting", () => this._suspend());
    on("playing", () => { if (!v.paused) this._resumeClock(); });
    on("pause", () => this._suspend());
    on("ended", () => { this._suspend(); this._stopScheduled(); });
    on("emptied", () => this.dispose());

    this._watchdog = setInterval(() => this._checkDrift(), CFG.watchdogMs);
  }

  /* ---------------- clock / anchor ---------------- */

  _anchor(mediaT, extraLead = CFG.primeDelayS) {
    this.anchorMediaT = mediaT;
    this.anchorCtxT = this.ctx.currentTime + extraLead;
  }
  _mediaNow() {
    return estimateMediaTime(this.anchorCtxT, this.anchorMediaT, this.ctx.currentTime);
  }

  /* ---------------- pump (demux → decode → schedule) ---------------- */

  async resumeFrom(t) {
    if (this.disposed || !this.core) return;
    await this.ctx.resume();
    this._stopScheduled();
    this.gen += 1;
    const g = this.gen;
    this._anchor(t);
    this.horizonMedia = t;
    this.eof = false;
    this.log(`audio pump start @ ${t.toFixed(2)}s`);
    this._pump(t, g).catch((e) => {
      if (!this.disposed) this.log(`pump error: ${e?.message || e}`);
    });
  }

  async _pump(fromT, g) {
    // EncodedPacketSink.packets() takes an EncodedPacket as cursor, not a
    // timestamp; getKeyPacket(t) resolves t → packet (audio: every packet is
    // "key", and MKV cues make this cue-indexed, i.e. one range read).
    let startPkt = null;
    if (fromT > 0) startPkt = await this.sink.getKeyPacket(fromT);
    const iterable = startPkt ? this.sink.packets(startPkt) : this.sink.packets();

    let batch = [], batchBytes = 0, batchStart = null, n = 0;

    for await (const pkt of iterable) {
      if (this.disposed || g !== this.gen) return;   // stale pump (seek/reset)
      await this._gateHorizon(g);
      if (batchStart === null) batchStart = pkt.timestamp;
      batch.push(pkt.data); batchBytes += pkt.data.byteLength; n++;
      this.packetsRead++;
      // Each mediabunny packet is one or more whole AC3 frames; concatenated
      // packets form a valid AC3 elementary stream — no JS frame splitting.
      if (n >= CFG.batchPackets) {
        await this._decodeSchedule(this._concat(batch, batchBytes), batchStart, g);
        batch = []; batchBytes = 0; n = 0; batchStart = null;
      }
    }
    if (n && !this.disposed && g === this.gen) {
      await this._decodeSchedule(this._concat(batch, batchBytes), batchStart, g);
    }
    if (g === this.gen) { this.eof = true; this.log("audio pump reached EOF"); }
  }

  /** Keep decode-ahead bounded: wait while scheduled horizon is far ahead. */
  async _gateHorizon(g) {
    while (!this.disposed && g === this.gen &&
           this.horizonMedia - this._mediaNow() > CFG.maxHorizonS) {
      await new Promise((r) => setTimeout(r, CFG.watchdogMs));
    }
  }

  async _decodeSchedule(bytes, mediaStart, g) {
    const t0 = performance.now();
    this.core.FS.writeFile("/in.ac3", bytes);
    const code = this.core.exec(                    // NOTE: exec is varargs
      "-hide_banner", "-loglevel", "error",
      "-f", this.ffmt, "-i", "/in.ac3",
      "-ac", "2", "-ar", String(CFG.sampleRate),
      "-f", "f32le", "/out.f32",
    );
    let pcmBytes = new Uint8Array(0);
    try { if (code === 0) pcmBytes = this.core.FS.readFile("/out.f32"); }
    finally {
      try { this.core.FS.unlink("/in.ac3"); this.core.FS.unlink("/out.f32"); } catch (_) {}
    }
    const decMs = performance.now() - t0;
    if (this.disposed || g !== this.gen || code !== 0 || pcmBytes.byteLength === 0) {
      if (code !== 0) this.log(`ffmpeg decode failed (code ${code})`);
      return;
    }

    const frames = pcmBytes.byteLength / 8;  // f32 stereo = 8 bytes/frame
    const dur = frames / CFG.sampleRate;
    const f32 = new Float32Array(pcmBytes.buffer, pcmBytes.byteOffset, frames * 2);
    const L = new Float32Array(frames), R = new Float32Array(frames);
    for (let i = 0; i < frames; i++) { L[i] = f32[2 * i]; R[i] = f32[2 * i + 1]; }

    const plan = planSchedule({
      anchorCtxT: this.anchorCtxT, anchorMediaT: this.anchorMediaT,
      chunkStart: mediaStart, chunkDur: dur, nowCtx: this.ctx.currentTime,
    });
    if (plan.action === "skip") return;

    const buf = this.ctx.createBuffer(2, frames, CFG.sampleRate);
    buf.copyToChannel(L, 0); buf.copyToChannel(R, 1);
    const src = this.ctx.createBufferSource();
    src.buffer = buf; src.connect(this.gain);
    try { src.start(plan.when, plan.offset); } catch { return; }
    src.onended = () => this.scheduled.delete(src);
    this.scheduled.add(src);
    this.horizonMedia = Math.max(this.horizonMedia, mediaStart + dur);
    this.decodedMediaS += dur;
    this.onStats({
      decodedS: this.decodedMediaS, packets: this.packetsRead,
      horizonS: this.horizonMedia - this._mediaNow(),
      decMsPer2SChunk: Math.round(decMs), eof: this.eof,
    });
  }

  _stopScheduled() {
    for (const s of this.scheduled) { try { s.stop(); } catch (_) {} }
    this.scheduled.clear();
  }
  async _suspend() { if (this.ctx?.state === "running") await this.ctx.suspend(); }
  async _resumeClock() {
    if (this.disposed || !this.ctx) return;
    await this.ctx.resume();
    // Small nudge to current playhead; big corrections go through watchdog.
    const v = this.video.currentTime;
    if (Math.abs(v - this.anchorMediaT) > CFG.driftSoftS) this._anchor(v, CFG.minLeadS * 3);
  }

  _checkDrift() {
    if (this.disposed || !this.video || this.video.paused ||
        this.ctx?.state !== "running" || !this.scheduled.size) return;
    const diff = this.video.currentTime - this._mediaNow();
    if (Math.abs(diff) > CFG.driftHardS) {
      this.log(`drift ${(diff * 1000).toFixed(0)} ms > ${CFG.driftHardS * 1000} ms → hard re-anchor`);
      this.resumeFrom(this.video.currentTime);
    } else if (Math.abs(diff) > CFG.driftSoftS) {
      this.anchorMediaT += diff * 0.5; // glide
    }
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
