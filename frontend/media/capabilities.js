/**
 * media/capabilities.js — measure what THIS browser can actually play.
 *
 * Static tables lie (HEVC depends on hardware, MKV sniffing varies by build),
 * so we ask the platform: HTMLMediaElement.canPlayType + MediaSource, with a
 * MediaCapabilities decodingInfo refinement where available. The result is
 * POSTed to /media/{id}/plan — the server never guesses.
 *
 * Keeping every probe string HERE is the point: pages and even the backend
 * decision layer deal only in canonical container/codec names.
 */

const CONTAINER_PROBES = {
  mkv:  ["video/x-matroska", 'video/x-matroska; codecs="h264"'],
  webm: ["video/webm", 'video/webm; codecs="vp9"'],
  mp4:  ["video/mp4", 'video/mp4; codecs="avc1.42E01E"'],
  mov:  ["video/quicktime"],
  ts:   ["video/mp2t"],
  avi:  ["video/x-msvideo"],
  ogg:  ["video/ogg"],
  mpeg: ["video/mpeg"],
  flv:  ["video/x-flv"],
  asf:  ["video/x-ms-asf"],
};

const VIDEO_PROBES = {
  h264:  ['video/mp4; codecs="avc1.42E01E"', 'video/x-matroska; codecs="h264"'],
  hevc:  ['video/mp4; codecs="hev1.1.6.L93.B0"',
          'video/mp4; codecs="hvc1.1.6.L93.B0"',
          'video/x-matroska; codecs="hevc"'],
  vp8:   ['video/webm; codecs="vp8"'],
  vp9:   ['video/webm; codecs="vp09.00.10.08"'],
  av1:   ['video/mp4; codecs="av01.0.04M.08"'],
  mpeg4: ['video/mp4; codecs="mp4v.20.8"'],
  mpeg2: ['video/mpeg; codecs="mp2v"'],
  mpeg1: ["video/mpeg"],
};

const AUDIO_PROBES = {
  aac:    ['audio/mp4; codecs="mp4a.40.2"', 'video/mp4; codecs="mp4a.40.2"'],
  mp3:    ['audio/mpeg'],
  opus:   ['audio/ogg; codecs="opus"', 'audio/webm; codecs="opus"'],
  vorbis: ['audio/ogg; codecs="vorbis"'],
  flac:   ['audio/flac', 'audio/ogg; codecs="flac"'],
  ac3:    ['audio/ac3', 'video/x-matroska; codecs="ac3"'],
  eac3:   ['audio/eac3', 'audio/mp4; codecs="ec-3"'],
  dts:    ['audio/vnd.dts', 'video/x-matroska; codecs="dts"'],
  truehd: ['audio/vnd.truehd'],
};

const browserName = () => {
  const ua = navigator.userAgent;
  const m = ua.match(/(Firefox|Edg|Chrome|Safari)\/(\d+)/);
  return m ? `${m[1].toLowerCase()}-${m[2]}` : "unknown";
};

const ok = (v) => v === "probably" || v === "maybe";

async function decodingInfoRefine(kind, contentType) {
  // MediaCapabilities is the least-lie-prone API; use it when present and
  // disagreeing with canPlayType="". Failure → keep canPlayType's answer.
  try {
    if (!navigator.mediaCapabilities?.decodingInfo) return null;
    const res = await Promise.race([
      navigator.mediaCapabilities.decodingInfo({
        type: "file",
        [kind]: { contentType, ...(kind === "video"
          ? { width: 1920, height: 1080, bitrate: 8e6, framerate: 24 }
          : { bitrate: 320e3, channels: 6 }) },
      }),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 1500)),
    ]);
    return !!res?.supported && (kind === "audio" ? !!res.smooth || !!res.supported : true);
  } catch {
    return null;
  }
}

/** Measure the capability matrix the plan endpoint understands. */
export async function detectCapabilities() {
  const probeEl = document.createElement("video");
  const can = (mime) => { try { return ok(probeEl.canPlayType(mime)); } catch { return false; } };
  const anyOf = (mimes) => mimes.some(can);

  const containers = {};
  for (const [k, mimes] of Object.entries(CONTAINER_PROBES)) containers[k] = anyOf(mimes);

  const video = {}, audio = {};
  const refineJobs = [];
  for (const [k, mimes] of Object.entries(VIDEO_PROBES)) {
    video[k] = anyOf(mimes);
    if ("MediaSource" in window &&
        mimes.some((m) => { try { return MediaSource.isTypeSupported(m); } catch { return false; } }))
      video[k] = true;
    if (!video[k]) {
      refineJobs.push((async () => {
        const d = await decodingInfoRefine("video", mimes[mimes.length - 1]);
        if (d === true) video[k] = true;
      })());
    }
  }
  for (const [k, mimes] of Object.entries(AUDIO_PROBES)) {
    audio[k] = anyOf(mimes)
      || (can('audio/x-matroska; codecs="' + k + '"'));
  }
  // AC3/E-AC3 polarity deserves the expensive check: some Chromium builds
  // report "maybe" via MFT system decoders.
  for (const k of ["ac3", "eac3"]) {
    if (!audio[k]) {
      refineJobs.push((async () => {
        const d = await decodingInfoRefine("audio", AUDIO_PROBES[k][0]);
        if (d === true) audio[k] = true;
      })());
    }
  }
  await Promise.allSettled(refineJobs);

  return {
    name: browserName(),
    containers, video, audio,
    features: {
      mse: "MediaSource" in window,
      wasmAudio: typeof WebAssembly === "object",
    },
  };
}
