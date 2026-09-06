/**
 * Browser-free integration test of the WASM AC3 audio pipeline.
 *
 * Exercises the REAL building blocks against the running POC server:
 *   1. unit: planSchedule / estimateMediaTime from frontend/ac3-audio.js
 *   2. demux: mediabunny UrlSource over http://127.0.0.1:PORT/stream/vid123
 *             (the existing range proxy) → AC3 packets only
 *   3. decode: vendored ffmpeg-core.wasm (same binary the browser loads)
 *   4. seek restart: getKeyPacket(t) cue re-anchor
 *   5. CLASS-LEVEL: Ac3WasmAudio full pump with fakes that behave like real
 *      WebAudio/HTMLVideoElement — verifies engine-level behaviour:
 *      - sources actually start, scheduled in-bounds, non-silent PCM
 *      - NO drift-watchdog re-anchor storm on a healthy playing clock
 *      - video STALL (waiting/readyState drop) freezes audio without flapping
 *      - seeked event re-anchors exactly once at the new position
 *      - clean dispose
 *
 * Usage:
 *   node tests/ac3_wasm_test.mjs <streamUrl> <vendorDir> <frontendDir> [wantSec] [seekT]
 * Prints a single line: RESULT={...json...}
 */
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [, , streamUrl, vendorDir, frontendDir, wantSecArg, seekTArg] = process.argv;
const wantSec = parseFloat(wantSecArg ?? "2.0");
const seekT = parseFloat(seekTArg ?? "20");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const out = { ok: false };

/* ---------------- fake WebAudio / video (behave like the real thing) ----- */

class FakeParam { constructor(v) { this.value = v; } setValueAtTime() {} }

class FakeAudioContext {
  constructor() {
    this.sampleRate = 48000;
    this.state = "suspended";
    this.destination = {};
    this._t0 = 0;                 // perfNow base (last resume)
    this._banked = 0;             // accumulated seconds before last resume
    this._started = [];
  }
  get currentTime() {
    return this.state === "running"
      ? this._banked + (performance.now() - this._t0) / 1000
      : this._banked;
  }
  async resume() {                     // real ADS: resume() on a running ctx is a no-op
    if (this.state === "running") return;
    this._t0 = performance.now(); this.state = "running";
  }
  async suspend() { this._banked = this.currentTime; this.state = "suspended"; }
  async close() { this._banked = this.currentTime; this.state = "closed"; }
  createGain() { return { gain: new FakeParam(1), connect() {} }; }
  createOscillator() {
    return { frequency: new FakeParam(0), connect() {}, start() {}, stop() {} };
  }
  createBuffer(chs, frames, rate) {
    const data = Array.from({ length: chs }, () => new Float32Array(frames));
    return {
      sampleRate: rate, numberOfChannels: chs, length: frames, _d: data,
      copyToChannel(arr, ch) { data[ch].set(arr); },
      getChannelData(ch) { return data[ch]; },
    };
  }
  createBufferSource() {
    const ctx = this;
    const src = {
      buffer: null, onended: null,
      connect() {},
      start(when = 0, offset = 0) {
        ctx._started.push({ when, offset, atNow: ctx.currentTime, buf: src.buffer });
      },
      stop() {},
    };
    return src;
  }
}

function makeFakeVideo() {
  const listeners = {};
  return {
    paused: true, muted: false, seeking: false, readyState: 4,
    _t: 0, _running: false, _t0: 0,
    get currentTime() {
      return this._running ? this._t + (performance.now() - this._t0) / 1000 : this._t;
    },
    seekTo(t) { this._t = t; if (this._running) this._t0 = performance.now(); },
    freeze() { this._t = this.currentTime; this._running = false; },       // stall
    unfreeze() { if (!this._running) { this._t0 = performance.now(); this._running = true; } },
    play() {
      this.unfreeze(); this.paused = false;
      this.fire("play"); this.fire("playing");
    },
    addEventListener(ev, fn) { (listeners[ev] ??= []).push(fn); },
    removeEventListener(ev, fn) {
      listeners[ev] = (listeners[ev] ?? []).filter((f) => f !== fn);
    },
    fire(ev) { (listeners[ev] ?? []).slice().forEach((f) => f()); },
  };
}

try {
  /* ---------- 1. pure scheduling helpers ---------- */
  const mod = await import(pathToFileURL(path.join(frontendDir, "ac3-audio.js")).href);
  const { planSchedule, estimateMediaTime } = mod;
  const u = [];
  const near = (a, b, tol = 1e-9) => Math.abs(a - b) <= tol;
  u.push(near(estimateMediaTime(10, 5, 20), 15));
  u.push(planSchedule({ anchorCtxT: 10, anchorMediaT: 5, chunkStart: 7, chunkDur: 2, nowCtx: 0 })
    .action === "play");
  let p = planSchedule({ anchorCtxT: 10, anchorMediaT: 5, chunkStart: 4, chunkDur: 2, nowCtx: 10 });
  u.push(p.action === "play" && near(p.offset, 1.03, 0.02));
  u.push(planSchedule({ anchorCtxT: 10, anchorMediaT: 5, chunkStart: 2, chunkDur: 2, nowCtx: 20 })
    .action === "skip");
  p = planSchedule({ anchorCtxT: 0, anchorMediaT: 0, chunkStart: 60, chunkDur: 2, nowCtx: 0 });
  u.push(p.action === "play" && near(p.when, 60));
  out.pureHelpersOk = u.every(Boolean);
  if (!out.pureHelpersOk) out.pureDetails = u;

  /* ---------- 2. demux via mediabunny over /stream ---------- */
  const mb = await import(
    pathToFileURL(path.join(vendorDir, "mediabunny/dist/bundles/mediabunny.min.mjs")).href);

  const source = new mb.UrlSource(streamUrl);
  let bytesTouched = 0;
  source.onread = (start, end) => { bytesTouched += end - start; };

  const input = new mb.Input({ source, formats: mb.ALL_FORMATS });
  const audioTracks = await input.getAudioTracks();
  out.audioCodecs = await Promise.all(audioTracks.map((t) => t.getCodec()));

  let track = null, codec = null;
  for (const t of audioTracks) {
    const c = await t.getCodec();
    if (/^(ac-?3|e-?ac-?3|ec-3)$/i.test(String(c).replace(/_/g, "-"))) { track = t; codec = c; break; }
  }
  if (!track) throw new Error(`no AC3 track; codecs=${JSON.stringify(out.audioCodecs)}`);
  out.codec = codec;

  const sink = new mb.EncodedPacketSink(track);
  const chunks = [];
  let total = 0, count = 0, firstTs = null, lastTs = null, prev = -1, monotonic = true;
  for await (const pkt of sink.packets()) {
    if (firstTs === null) firstTs = pkt.timestamp;
    lastTs = pkt.timestamp;
    if (pkt.timestamp < prev - 1e-6) monotonic = false;
    prev = pkt.timestamp;
    chunks.push(Buffer.from(pkt.data));
    total += pkt.data.byteLength; count++;
    if (lastTs - firstTs >= wantSec) break;
  }
  Object.assign(out, { packets: count, firstTs, lastTs, spanS: lastTs - firstTs,
                       monotonic, ac3Bytes: total, bytesTouched });

  /* ---------- 3. decode batch with the vendored wasm core ---------- */
  // emscripten worker-targeted glue probes self.location — shim for Node, and
  // hand it the wasm binary directly so no file:// fetch is attempted.
  globalThis.self = globalThis.self ?? { location: { href: pathToFileURL(vendorDir + "/").href } };
  const wasmPath = path.join(vendorDir, "ffmpeg-core/dist/esm/ffmpeg-core.wasm");
  const coreModule = await import(
    pathToFileURL(path.join(vendorDir, "ffmpeg-core/dist/esm/ffmpeg-core.js")).href);
  const core = await coreModule.default({ wasmBinary: fs.readFileSync(wasmPath) });

  core.FS.writeFile("/in.ac3", new Uint8Array(Buffer.concat(chunks)));
  const t0 = performance.now();
  const code = core.exec(                       // NOTE: exec is varargs, not array
    "-hide_banner", "-loglevel", "error",
    "-f", /e/i.test(String(codec)) ? "eac3" : "ac3", "-i", "/in.ac3",
    "-ac", "2", "-ar", "48000", "-f", "f32le", "/out.f32",
  );
  out.decMs = Math.round(performance.now() - t0);
  out.execCode = code;
  const pcm = core.FS.readFile("/out.f32");
  out.decodedSec = pcm.byteLength / 8 / 48000;
  const f32 = new Float32Array(pcm.buffer, pcm.byteOffset, pcm.byteLength / 4);
  let energy = 0, nRms = 0;
  for (let i = 0; i < f32.length; i += 101) { energy += f32[i] * f32[i]; nRms++; }
  out.rms = Math.sqrt(energy / nRms);

  /* ---------- 4. seek restart from t=seekT ---------- */
  const startPkt = await sink.getKeyPacket(seekT);
  if (startPkt) out.seekFirstTs = startPkt.timestamp;
  input.dispose();

  /* ---------- 5. class-level engine with realistic fakes ------------------ */
  const fakeCtx = new FakeAudioContext();
  const fakeVideo = makeFakeVideo();
  const classErrors = [];
  const logLines = [];
  const ctl = new mod.Ac3WasmAudio(fakeVideo, "vid123", {
    vendorBase: pathToFileURL(vendorDir).href,
    streamUrl,
    audioContextFactory: () => fakeCtx,
    coreModuleOverrides: { wasmBinary: fs.readFileSync(wasmPath) },
    log: (m) => logLines.push(String(m)),
    onStats: () => {},
    onError: (e) => classErrors.push(String(e)),
  });
  const resumeCalls = [];
  const origResume = ctl.resumeFrom.bind(ctl);
  ctl.resumeFrom = (t) => { resumeCalls.push(t); return origResume(t); };

  await ctl.init();
  out.classInit = { ctxStateAfterInit: fakeCtx.state, codec: ctl.codec };
  ctl.attach();

  /* 5a. healthy playing clock for 2.5 s — expect exactly ONE resumeFrom */
  fakeVideo.seekTo(0.05);
  fakeVideo.play();                       // fires 'play' → resumeFrom(0.05)
  await sleep(2500);
  let d0 = ctl.getDiagnostics();
  out.classPump = {
    ctxState: d0.ctxState,
    sampleRate: d0.sampleRate,
    packets: d0.packets,
    bytesFetched: d0.bytesFetched,
    buffersStarted: d0.buffersStarted,
    decodedS: d0.decodedS,
    rms: d0.rms,
    readyState: d0.readyState,
    neverScheduledPast: fakeCtx._started.every(
      (s) => Number.isFinite(s.when) && Number.isFinite(s.offset) &&
             s.when >= s.atNow - 1e-3),
    bufRate: fakeCtx._started[0]?.buf?.sampleRate ?? 0,
    bufChannels: fakeCtx._started[0]?.buf?.numberOfChannels ?? 0,
    nonSilent: fakeCtx._started.some(
      (s) => s.buf && s.buf.getChannelData(0).some((v) => Math.abs(v) > 0.01)),
    errors: classErrors,
    // THE regression guard for the re-anchor storm: exactly the one 'play'
    // resumeFrom may have happened; no drift re-anchors, bounded fetch.
    resumeCallsObserved: resumeCalls.length,
    reAnchorLogs: logLines.filter((l) => /re-anchor/i.test(l)).length,
    driftStreak: d0.driftStreak,
  };

  /* 5b. video STALL: 'waiting' + frozen clock ~0.9 s → no flapping */
  const buffersAtStall = ctl.buffersStarted;
  const packetsAtStall = ctl.packetsRead;
  fakeVideo.fire("waiting");
  fakeVideo.readyState = 2;
  fakeVideo.freeze();
  await sleep(900);
  const duringStall = {
    buffersDelta: ctl.buffersStarted - buffersAtStall,
    packetsDelta: ctl.packetsRead - packetsAtStall,
    resumeCalls: resumeCalls.length,
    suspended: fakeCtx.state !== "running",
  };
  fakeVideo.readyState = 4;
  fakeVideo.unfreeze();
  fakeVideo.fire("playing");
  await sleep(900);
  out.classStall = {
    ...duringStall,
    resumesAfterPlaying: resumeCalls.length,   // _resumeClock path: no pump restart
    buffersResumeAfter: ctl.buffersStarted > buffersAtStall + duringStall.buffersDelta,
    reAnchorLogsTotal: logLines.filter((l) => /re-anchor/i.test(l)).length,
  };

  /* 5c. seek via 'seeked' → exactly one more resumeFrom at the new time */
  const callsBeforeSeek = resumeCalls.length;
  fakeVideo.seeking = true; fakeVideo.fire("seeking");
  fakeVideo.seekTo(seekT);
  fakeVideo.seeking = false; fakeVideo.fire("seeked");
  await sleep(1500);
  out.classSeek = {
    resumeCallsDelta: resumeCalls.length - callsBeforeSeek,
    resumedAt: resumeCalls.at(-1),
    horizonAfterSeek: ctl.horizonMedia,
    buffersGrew: ctl.buffersStarted > 0,
    errors: classErrors,
  };
  ctl.dispose();
  out.classDisposed = ctl.disposed === true;
  out.resumeCallsTotal = resumeCalls;

  out.ok = out.pureHelpersOk && code === 0 && out.rms > 0 &&
           out.classPump.buffersStarted >= 1 && !classErrors.length;
} catch (e) {
  out.ok = false;
  out.error = String(e?.stack || e);
}
console.log("RESULT=" + JSON.stringify(out));
process.exit(out.ok ? 0 : 3);
