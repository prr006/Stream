/**
 * media/adapters.js — one adapter per delivery mode.
 *
 * An adapter OWNS the <video> element while mounted. Contract:
 *   const handle = await adapter.mount(video, hooks)
 *   handle.dispose()   — mandatory; must leave <video> clean
 *   handle.tone?(s)    — optional diagnostics tone through the output chain
 *
 * hooks (all optional): onStats(wasm live stats), onDiag(wasm diagnostics),
 * onError(msg), onRemuxState({state, detail, url, size})
 *
 * Anything the engine can't actually run yet throws `PlanUnavailable` so the
 * engine can walk plan.alternatives. That stub behavior is deliberate:
 * the ladder models all five modes; adapters get filled in rung by rung.
 */

/** Thrown by adapters that model a mode but can't execute it yet. */
export class PlanUnavailable extends Error {}

export function createAdapter(mode, ctx) {
  switch (mode) {
    case "direct-play":    return new DirectPlayAdapter(ctx);
    case "wasm-audio":     return new WasmAudioAdapter(ctx);
    case "container-remux":return new ContainerRemuxAdapter(ctx);
    case "audio-remux":    return new AudioRemuxAdapter(ctx);
    case "transcode":      return new TranscodeAdapter(ctx);
    default:               return new UnsupportedAdapter(ctx, mode);
  }
}

/* ------------------------------------------------------------------ */
/* 1. Direct Play — original bytes straight into <video>               */
/* ------------------------------------------------------------------ */

class DirectPlayAdapter {
  constructor({ plan }) { this.plan = plan; }
  async mount(video) {
    video.muted = false;
    video.src = this.plan.playback.url;
    return { dispose: () => { video.pause?.(); video.removeAttribute("src"); video.load?.(); } };
  }
}

/* ------------------------------------------------------------------ */
/* 2. Client-side WASM decode — original video bytes + WASM-decoded    */
/*    audio from the same bytes (wraps frontend/ac3-audio.js).         */
/* ------------------------------------------------------------------ */

class WasmAudioAdapter {
  constructor({ plan, fileId }) { this.plan = plan; this.fileId = fileId; }
  async mount(video, hooks) {
    const mod = await import("/static/ac3-audio.js");
    const ctl = new mod.Ac3WasmAudio(video, this.fileId, {
      log: (m) => console.log("[ac3wasm]", m),
      onStats: (s) => hooks.onStats?.(s),
      onError: (m) => hooks.onError?.(m),
    });
    await ctl.init();
    ctl.attach();
    video.muted = false;
    video.src = this.plan.playback.url;   // HEVC video + subs play natively
    video.muted = true;                    // WASM replaces the audio channel
    if (!video.paused) ctl.resumeFrom(video.currentTime);
    const diagTimer = setInterval(() => hooks.onDiag?.(ctl.getDiagnostics()), 500);
    ctl.testTone(0.8);                     // audible proof the chain is alive
    return {
      tone: (s) => ctl.testTone(s),
      dispose: () => {
        clearInterval(diagTimer);
        try { ctl.dispose(); } catch {}
        video.muted = false;
      },
    };
  }
}

/* ------------------------------------------------------------------ */
/* 3. Container remux — modeled (rung 3 of the ladder) but the         */
/*    client-side MKV→fMP4→MSE remuxer is a FUTURE milestone.          */
/* ------------------------------------------------------------------ */

class ContainerRemuxAdapter {
  constructor({ plan }) { this.plan = plan; }
  async mount() {
    throw new PlanUnavailable(
      "container remux is on the roadmap (client-side MKV→fMP4 via "
      + "mediabunny into MSE, or a server-side copy-only repack); "
      + "the engine selected it as the minimum intervention but the "
      + "adapter is not built yet");
  }
}

/* ------------------------------------------------------------------ */
/* 4. Audio-only AAC remux — existing server sidecar (video bitstream  */
/*    copied, audio converted once, cached on disk).                   */
/* ------------------------------------------------------------------ */

class AudioRemuxAdapter {
  constructor({ plan }) { this.plan = plan; this.timer = null; }
  async mount(video, hooks) {
    const audio = this.plan.audio;
    if (!audio?.statusUrl) throw new PlanUnavailable("malformed audio-remux plan: missing urls");
    video.muted = false;
    video.src = this.playUrlOrBlank(video);
    const emit = (st) => hooks.onRemuxState?.(st);
    const poll = async () => {
      try { emit(await (await fetch(audio.statusUrl)).json()); }
      catch (e) { emit({ state: "error", detail: String(e) }); }
    };
    const start = async () => {
      const r = await fetch(audio.startUrl, { method: "POST" });
      emit(await r.json());
    };
    const playRemuxed = () => {
      const resume = Number.isFinite(video.currentTime) ? video.currentTime : 0;
      const wasPlaying = !video.paused;
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = Math.min(resume, video.duration || resume);
        if (wasPlaying) video.play().catch(() => {});
      }, { once: true });
      video.src = audio.urlTemplate;
      video.load();
    };
    await poll();
    this.timer = setInterval(poll, 3000);
    return {
      remux: { start, playRemuxed },
      dispose: () => clearInterval(this.timer),
    };
  }
  playUrlOrBlank(video) {
    return this.plan.playback?.url ?? video.currentSrc ?? "";
  }
}

/* ------------------------------------------------------------------ */
/* 5. Full transcode — LAST RESORT, deliberately not implemented.      */
/* ------------------------------------------------------------------ */

class TranscodeAdapter {
  constructor({ plan }) { this.plan = plan; }
  async mount() {
    throw new PlanUnavailable(
      "full video transcode is the ladder's last resort and is not "
      + "implemented in this build — no cheaper mode could play this file");
  }
}

class UnsupportedAdapter {
  constructor(ctx, mode) { this.mode = mode; }
  async mount() {
    throw new PlanUnavailable(`engine mode '${this.mode}' is not playable`);
  }
}
