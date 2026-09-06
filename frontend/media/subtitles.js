/**
 * media/subtitles.js — SubtitleManager: owns <track> elements and their
 * showing/disabled modes. Extracted from index.html unchanged in behavior
 * (the same rules that made embedded MKV subs render are preserved verbatim:
 * element must be in the DOM, mode must be driven explicitly, and we
 * re-assert mode on every load/play event).
 *
 * The page renders plain checkboxes; this manager owns every media truth.
 */

const READY = ["NONE", "LOADING", "LOADED", "ERROR"];
const fmtT = (s) => Number.isFinite(s) ? s.toFixed(1) + "s" : "?";

export class SubtitleManager {
  constructor(video, { onChange } = {}) {
    this.video = video;
    this.onChange = onChange ?? (() => {});
    this.entries = [];     // { index, label, codec, lang, note, trackEl, compatible }
    this._wired = false;
  }

  _wire() {
    if (this._wired) return;
    this._wired = true;
    ["loadedmetadata", "seeked", "play"].forEach((ev) =>
      this.video.addEventListener(ev, () => this.onChange()));
  }

  clear() {
    this.video.querySelectorAll("track").forEach((t) => t.remove());
    this.entries = [];
    this.onChange();
  }

  /** subs: plan.subtitles (entries already carry `url` from the backend). */
  mount(subs) {
    this.clear();
    this._wire();
    let firstCompatible = true;
    for (const t of subs ?? []) {
      const label = t.title || t.language || `stream ${t.index}`;
      const entry = {
        index: t.index, codec: t.codec, label,
        lang: (t.language && t.language !== "und") ? t.language : "en",
        compatible: !!t.web_compatible,
        note: t.web_compatible ? "" : (t.note || "unsupported"),
        loadedNote: "", trackEl: null, hasCues: false,
        enabled: false,
      };
      if (t.web_compatible && t.url) {
        const trackEl = document.createElement("track");
        trackEl.kind = "subtitles";
        trackEl.src = t.url;
        trackEl.srclang = entry.lang;
        trackEl.label = label;
        if (firstCompatible) trackEl.default = true;
        this.video.appendChild(trackEl);   // MUST be in the DOM to load
        entry.trackEl = trackEl;
        entry.enabled = firstCompatible;
        entry.loadedNote = firstCompatible
          ? " (extracting/first load can take a while…)" : " (off)";
        firstCompatible = false;

        const apply = () => {
          trackEl.track.mode = entry.enabled ? "showing" : "disabled";
          this.onChange();
        };
        trackEl.addEventListener("load", () => {
          const cues = trackEl.track.cues;
          entry.hasCues = !!(cues && cues.length);
          entry.loadedNote = entry.enabled ? "" : " (loaded, off)";
          apply();   // re-assert after load (some builds reset mode)
        });
        trackEl.addEventListener("error", () => {
          entry.loadedNote = " (track ERROR — likely bad headers/parse)";
          this.onChange();
        });
        entry._apply = apply;
        apply();
      }
      this.entries.push(entry);
    }
    setTimeout(() => this.onChange(), 1500);
    return this.entries;
  }

  setEnabled(index, on) {
    const e = this.entries.find((x) => x.index === index);
    if (!e?.trackEl) return;
    e.enabled = on;
    e.loadedNote = "";
    e._apply?.();
  }

  jumpToFirstCue(index) {
    const e = this.entries.find((x) => x.index === index);
    const cues = e?.trackEl?.track?.cues;
    if (!cues?.length) return;
    this.video.currentTime = cues[0].startTime + 0.05;
    if (!e.enabled) this.setEnabled(index, true);
    this.video.play().catch(() => {});
  }

  /** Human-readable diagnostics (same content the old inline page rendered). */
  diagText() {
    const lines = [];
    lines.push(`video.src               : ${this.video.currentSrc || "(none)"}`);
    lines.push(`media time/duration     : ${fmtT(this.video.currentTime)} / ${fmtT(this.video.duration)}`);
    lines.push(`video.textTracks.length : ${this.video.textTracks.length}`);
    const trackEls = [...this.video.querySelectorAll("track")];
    if (this.video.textTracks.length) trackEls.forEach((elm, i) => {
      const tr = elm.track;
      const cues = tr.cues ? tr.cues.length : 0;
      const first = cues ? fmtT(tr.cues[0].startTime) : "-";
      lines.push(
        `[${i}] ${elm.label || elm.srclang}: readyState=${elm.readyState} (${READY[elm.readyState]}), ` +
        `mode=${tr.mode}, cues=${cues}, first-cue@${first}, src=${elm.src}`);
    });
    if (!trackEls.length) lines.push("(no <track> elements attached)");
    lines.push("");
    lines.push("How to read: readyState LOADED + cues>0 + mode=showing => cues SHOULD render.");
    lines.push("readyState ERROR => fetch ok or not, but parsing/header/CORS failed (check Network).");
    return lines.join("\n");
  }
}
