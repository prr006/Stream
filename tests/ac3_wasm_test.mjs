/**
 * Browser-free integration test of the WASM AC3 audio pipeline (V0).
 *
 * Exercises the REAL building blocks against the running POC server:
 *   1. unit: planSchedule / estimateMediaTime from frontend/ac3-audio.js
 *   2. demux: mediabunny UrlSource over http://127.0.0.1:PORT/stream/vid123
 *             (the existing range proxy) → AC3 packets only
 *   3. decode: vendored ffmpeg-core.wasm (same binary the browser loads)
 *   4. seek restart: sink.packets(t) from t=20 in a 40 s file
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

const out = { ok: false };
try {
  /* ---------- 1. pure scheduling helpers ---------- */
  const mod = await import(pathToFileURL(path.join(frontendDir, "ac3-audio.js")).href);
  const { planSchedule, estimateMediaTime } = mod;
  const u = [];
  const near = (a, b, tol = 1e-9) => Math.abs(a - b) <= tol;
  u.push(near(estimateMediaTime(10, 5, 20), 15));                       // 10s elapsed
  u.push(planSchedule({ anchorCtxT: 10, anchorMediaT: 5, chunkStart: 7, chunkDur: 2, nowCtx: 0 })
    .action === "play");                                                 // future chunk
  // chunk [4,6)s media-time: when = 10 + (4-5) = 9 < nowCtx(10)+lead → trim to now
  let p = planSchedule({ anchorCtxT: 10, anchorMediaT: 5, chunkStart: 4, chunkDur: 2, nowCtx: 10 });
  u.push(p.action === "play" && near(p.offset, 1.03, 0.02));             // straddles 'now' → trim
  u.push(planSchedule({ anchorCtxT: 10, anchorMediaT: 5, chunkStart: 2, chunkDur: 2, nowCtx: 20 })
    .action === "skip");                                                 // fully past
  p = planSchedule({ anchorCtxT: 0, anchorMediaT: 0, chunkStart: 60, chunkDur: 2, nowCtx: 0 });
  u.push(p.action === "play" && near(p.when, 60));                       // far future intact
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
  out.channels = await track.getNumberOfChannels().catch(() => null);

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
  const coreModule = await import(
    pathToFileURL(path.join(vendorDir, "ffmpeg-core/dist/esm/ffmpeg-core.js")).href);
  const core = await coreModule.default({
    wasmBinary: fs.readFileSync(path.join(vendorDir, "ffmpeg-core/dist/esm/ffmpeg-core.wasm")),
  });

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
  const startPkt = await sink.getKeyPacket(seekT);   // cues → EncodedPacket
  if (startPkt) out.seekFirstTs = startPkt.timestamp;

  out.ok = out.pureHelpersOk && code === 0 && out.rms > 0;
} catch (e) {
  out.ok = false;
  out.error = String(e?.stack || e);
}
console.log("RESULT=" + JSON.stringify(out));
process.exit(out.ok ? 0 : 3);
