/**
 * media/engine.js — the browser side of the media engine.
 *
 * Responsibilities (the ONLY media-layer surface pages may touch):
 *   detectCapabilities()          → capability matrix (see capabilities.js)
 *   MediaEngine.open(fileId, …)   → POST /media/{id}/plan, mount the right
 *                                   adapter, return a PlaybackSession.
 *
 * Pages never import adapters, never mention codecs, never set video.src
 * themselves. If the chosen mode can't run, the engine walks
 * plan.alternatives automatically; the page only renders what the plan says.
 */
import { detectCapabilities } from "./capabilities.js";
import { createAdapter, PlanUnavailable } from "./adapters.js";

export class MediaEngine {
  constructor({ base = "" } = {}) {
    this.base = base;
    this._caps = null;
    this._capsPromise = null;
  }

  /** Cached capability matrix (force=true to re-measure). */
  async caps(force = false) {
    if (this._caps && !force) return this._caps;
    if (!this._capsPromise || force) {
      this._capsPromise = detectCapabilities().then((c) => (this._caps = c), c);
    }
    return this._capsPromise;
  }

  /** Raw plan for a file (POSTs the capability matrix). */
  async plan(fileId) {
    const caps = await this.caps();
    const res = await fetch(`${this.base}/media/${encodeURIComponent(fileId)}/plan`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(caps),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(`plan failed: ${detail}`);
    }
    return res.json();
  }

  /**
   * Open a file for playback on <video>. Returns a PlaybackSession.
   * hooks: { onPlan, onStats, onDiag, onError, onRemuxState } — all optional.
   */
  async open(fileId, { video, hooks = {} } = {}) {
    if (!video) throw new Error("MediaEngine.open requires a <video> element");
    const plan = await this.plan(fileId);
    hooks.onPlan?.(plan);
    return this._mountWithFallback(fileId, plan, video, hooks);
  }

  async _mountWithFallback(fileId, plan, video, hooks) {
    const chain = [plan, ...(plan.alternatives || [])
      .filter((a) => a.implemented)
      .map((a) => ({ ...plan, mode: a.mode, implemented: true, audio: a,
                     reasons: [`alternative fallback after '${plan.mode}' failed`,
                               ...plan.reasons] }))];
    let lastErr = null;
    for (const candidate of chain) {
      const adapter = createAdapter(candidate.mode, {
        engine: this, plan: candidate, fileId,
      });
      try {
        const handle = await adapter.mount(video, hooks);
        return new PlaybackSession(this, fileId, candidate, adapter, handle, video, hooks);
      } catch (e) {
        lastErr = e;
        hooks.onError?.(`mode '${candidate.mode}' unavailable: ${e?.message || e}`);
        if (!(e instanceof PlanUnavailable)) break; // real failures don't cascade
      }
    }
    throw lastErr ?? new Error("no playable mode on this client");
  }

  /** Re-mount an existing session on a different mode (user-picked fallback). */
  async useAlternative(session, alt, hooks = {}) {
    const pseudo = { ...session.plan, mode: alt.mode, implemented: true, audio: alt,
                     reasons: [`user-selected fallback '${alt.mode}'`] };
    session.dispose();
    return this._mountWithFallback(session.fileId, pseudo, session.video, hooks);
  }
}

/** A live playback: one mounted adapter bound to a <video>. */
export class PlaybackSession {
  constructor(engine, fileId, plan, adapter, handle, video, hooks) {
    this.engine = engine;
    this.fileId = fileId;
    this.plan = plan;
    this.mode = plan.mode;
    this.adapter = adapter;
    this.handle = handle ?? {};
    this.video = video;
    this.hooks = hooks;
    this.disposed = false;
  }
  /** Wasm-audio only: 440 Hz tone through the exact output chain. */
  tone(seconds = 1.0) { this.handle.tone?.(seconds); }
  get subtitles() { return this.plan.subtitles ?? []; }
  get audioTracks() { return this.plan.audioTracks ?? []; }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    try { this.handle.dispose?.(); } catch {}
  }
}

export { detectCapabilities };
