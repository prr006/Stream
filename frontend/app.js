/**
 * app.js — the thin page layer. Owns DOM only; every media decision comes
 * from the MediaEngine (POST /media/{id}/plan). This file contains ZERO
 * codec/container branching: it renders the plan's mode, reasons, steps and
 * alternatives, and forwards hook events into visible rows.
 */
import { MediaEngine } from "/static/media/engine.js";
import { SubtitleManager } from "/static/media/subtitles.js";

const $ = (s) => document.querySelector(s);
const video = $("#player");
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const el = (tag, text) => { const e = document.createElement(tag); e.textContent = text; return e; };

const engine = new MediaEngine();
let session = null;

const subMgr = new SubtitleManager(video, { onChange: renderDiag });

const MODE_BADGE = {
  "direct-play":     ["#0f9d58", "DIRECT PLAY"],
  "wasm-audio":      ["#0b57d0", "DIRECT + WASM AUDIO"],
  "container-remux": ["#9334e6", "REMUX (container)"],
  "audio-remux":     ["#e8710a", "AUDIO REMUX"],
  "transcode":       ["#b3261e", "TRANSCODE (last resort)"],
};

/* ---------------- file list / auth ---------------- */

async function main() {
  const status = await (await fetch("/auth/status")).json();
  if (!status.authorized) {
    $("#banner").innerHTML = 'Not authorized. <a href="/auth/login">Sign in with Google</a>.';
    return;
  }
  $("#banner").className = "ok";
  $("#banner").innerHTML = 'Authorized with Google Drive. (<a href="/auth/logout">log out</a>)';

  const res = await fetch("/files");
  if (res.status === 401) {
    $("#banner").innerHTML = 'Session expired. <a href="/auth/login">Re-authorize</a>.';
    return;
  }
  const data = await res.json();
  const all = data.files || [];
  render(all);
  $("#filter").addEventListener("input", (e) => {
    const q = e.target.value.toLowerCase();
    render(all.filter(f => f.name.toLowerCase().includes(q)));
  });
}

function render(files) {
  $("#files").innerHTML = files.length
    ? files.map(f => {
        const size = f.size ? (f.size / 1048576).toFixed(1) + " MB" : "?";
        return `<li><a href="#" data-id="${f.id}">${escapeHtml(f.name)}</a>
                <small>${escapeHtml(f.mimeType)} &middot; ${size}</small></li>`;
      }).join("")
    : "<li><small>No video files found in this Drive account.</small></li>";
  document.querySelectorAll("#files a").forEach(a =>
    a.addEventListener("click", (e) => { e.preventDefault(); playFile(a.dataset.id, a.textContent); }));
}

/* ---------------- open a file through the media engine ---------------- */

async function playFile(id, name) {
  if (session) { session.dispose(); session = null; }
  subMgr.clear();
  $("#now-playing").textContent = name;
  ["#plan", "#info", "#subs"].forEach(s => { $(s).hidden = true; });
  const planBox = $("#plan");
  planBox.hidden = false;
  planBox.replaceChildren(el("span", "Analyzing file + measuring this browser…"));

  try {
    session = await engine.open(id, { video, hooks: {
      onPlan: renderPlan,
      onStats: renderWasmStats,
      onDiag: renderWasmDiag,
      onError: (m) => { $("#wasm-error").textContent = m; console.error("[media]", m); },
      onRemuxState: renderRemuxState,
    }});
  } catch (e) {
    planBox.replaceChildren();
    const w = el("span", "No working playback mode: " + (e?.message || e));
    w.className = "warn";
    planBox.append(w);
    return;
  }
  $("#now-playing").textContent = `${name}  ·  ${session.mode}`;
  video.play().catch(() => {});
  renderInfo(session.plan);
  renderSubtitles(session.plan.subtitles);
}

/* ---------------- plan panel (mode badge, reasons, steps, fallbacks) ---- */

function renderPlan(plan) {
  const box = $("#plan");
  box.hidden = false;
  box.replaceChildren();

  const [color, label] = MODE_BADGE[plan.mode] ?? ["#444", plan.mode.toUpperCase()];
  const badge = el("b", label);
  badge.style.cssText = `color:#fff;background:${color};padding:.1rem .5rem;border-radius:4px;font-size:.8rem`;
  box.append(badge, el("span", " "));

  // wasm extras live in the plan panel when that mode is active
  const wasmBits = $("#wasm-bits");
  wasmBits.hidden = plan.mode !== "wasm-audio";

  const steps = el("div", "");
  steps.style.cssText = "margin-top:.35rem";
  for (const s of plan.steps ?? []) {
    const chip = el("code", `${s.stream}: ${s.action}`);
    chip.title = s.detail || "";
    chip.style.marginRight = ".3rem";
    steps.append(chip);
  }
  box.append(steps);

  const reasons = document.createElement("ul");
  reasons.style.cssText = "margin:.35rem 0 .2rem 1.1rem;font-size:.8rem;color:#555";
  for (const r of plan.reasons ?? []) reasons.append(el("li", r));
  box.append(reasons);

  const implWarn = el("div", "");
  implWarn.className = "warn";
  implWarn.textContent = plan.implemented
    ? "" : "This mode is the correct minimum intervention but its adapter is not built yet.";
  if (implWarn.textContent) box.append(implWarn);

  const alts = (plan.alternatives ?? []).filter(a => a.implemented);
  if (alts.length) {
    const row = el("div", "Fallbacks: ");
    row.style.marginTop = ".3rem";
    for (const a of alts) {
      const b = el("button", a.label || a.mode);
      b.onclick = () => useFallback(a);
      row.append(b);
    }
    box.append(row);
  }
}

async function useFallback(alt) {
  if (!session) return;
  const id = session.fileId;
  const old = session;
  try {
    session = await engine.useAlternative(old, alt, {
      onPlan: renderPlan, onStats: renderWasmStats, onDiag: renderWasmDiag,
      onError: (m) => { $("#wasm-error").textContent = m; },
      onRemuxState: renderRemuxState,
    });
    renderPlan(session.plan);
    renderInfo(session.plan);
    video.play().catch(() => {});
  } catch (e) {
    $("#wasm-error").textContent = "fallback failed: " + (e?.message || e);
    session = old;
  }
}

/* ---------------- wasm-audio panel bits (mode-conditioned) ------------- */

function renderWasmStats(s) {
  $("#wasm-stats").textContent =
    `decoded ${s.decodedS.toFixed(1)}s · packets ${s.packets} · horizon ${s.horizonS.toFixed(1)}s` +
    ` · decode ${s.decMs} ms/chunk · rms ${s.rms.toFixed(3)} peak ${s.peak.toFixed(3)}` +
    (s.eof ? " · EOF" : "");
}

function renderWasmDiag(d) {
  const fmtN = (x) => x == null ? "?" : Number(x).toFixed(2) + "s";
  $("#wasm-diag").textContent =
    `ctx=${d.ctxState} ${d.sampleRate}Hz | pcm=${d.pcmFormat} | ` +
    `video.t=${fmtN(d.videoT)} audio≈${fmtN(d.audioEstT)} ` +
    `drift=${d.driftMs == null ? "?" : d.driftMs.toFixed(0) + "ms"}(streak ${d.driftStreak}) ` +
    `vrs=${d.readyState} | ` +
    `buffers: started=${d.buffersStarted} pending=${d.scheduledPending} ` +
    `nextStartLead=${d.nextStartLeadS == null ? "?" : d.nextStartLeadS.toFixed(2) + "s"} | ` +
    `horizon=${d.horizonS == null ? "?" : d.horizonS.toFixed(1) + "s"} | ` +
    `rms=${d.rms.toFixed(3)} peak=${d.peak.toFixed(3)} decoded=${d.decodedS.toFixed(1)}s | ` +
    `pkts=${d.packets} fetched=${(d.bytesFetched / 1048576).toFixed(2)}MB | ` +
    `video.muted=${d.videoMuted} gain=${d.gainValue}${d.eof ? " | EOF" : ""}`;
}

$("#wasm-tone").addEventListener("click", () => session?.tone?.(1.5));

/* ---------------- info (probe echo) + audio tracks ---------------------- */

function renderInfo(plan) {
  const box = $("#info");
  box.hidden = false;
  box.replaceChildren();
  const p = plan.probe ?? {};
  const dur = p.duration ? (p.duration / 60).toFixed(0) + " min" : "?";
  box.append(el("div", ""));
  box.firstChild.innerHTML =
    `<b>Container:</b> <code>${escapeHtml(p.container || "?")}</code> · ${dur} ` +
    `· probed via ${escapeHtml(p.probe_engine || "?")}`;
  if (p.video) {
    box.append(el("div", "")).lastChild.innerHTML =
      `<b>Video:</b> <code>${escapeHtml(p.video.codec)}</code> ${p.video.width || "?"}×${p.video.height || "?"}`;
  }
  const audioRow = el("div", "");
  audioRow.innerHTML = "<b>Audio tracks:</b> " +
    (plan.audioTracks?.length ? "" : "none");
  box.append(audioRow);
  for (const t of plan.audioTracks ?? []) {
    const line = el("div", "");
    line.style.cssText = "font-size:.85rem;margin-left:.8rem";
    const flags = [
      t.nativePlayable ? "native" : null,
      t.wasmDecodable ? "wasm" : null,
      "remuxable" in t && t.remuxable ? "aac-remux" : null,
    ].filter(Boolean).join(" · ") || "no path";
    line.innerHTML = `<code>${escapeHtml(t.codec)}</code> ${escapeHtml(t.language)} ` +
      `${escapeHtml(t.title)} — <small>${flags}</small>`;
    box.append(line);
  }
  const warn = plan.probe?.playability?.warnings ?? [];
  for (const w of warn) {
    const d = el("div", "⚠ " + w); d.className = "warn"; box.append(d);
  }
}

/* ---------------- remux (audio-remux mode state machine) ---------------- */

function renderRemuxState(st) {
  const box = $("#remux");
  box.hidden = false;
  box.replaceChildren();
  box.append(el("b", "Audio→AAC remux: "));
  if (st.state === "ready") {
    box.append(el("span", `ready (${((st.size || 0) / 1048576).toFixed(0)} MB, cached). `));
    const btn = el("button", "Play remuxed version (restores sound)");
    btn.onclick = () => session?.handle?.remux?.playRemuxed();
    box.append(btn);
  } else if (st.state === "processing") {
    box.append(el("span", `Building remux… ${st.detail || ""}`));
  } else if (st.state === "error") {
    box.append(el("span", `Remux failed: ${st.detail} `));
    const btn = el("button", "Retry");
    btn.onclick = () => session?.handle?.remux?.start();
    box.append(btn);
  } else {
    box.append(el("span", "Build a one-time AAC remux (video copied bit-exact): "));
    const btn = el("button", "Build AAC remux");
    btn.onclick = () => session?.handle?.remux?.start();
    box.append(btn);
  }
}

/* ---------------- subtitles (rows over SubtitleManager) ----------------- */

function renderSubtitles(subs) {
  const box = $("#subs");
  box.hidden = false;
  const entries = subMgr.mount(subs);
  const frag = document.createDocumentFragment();
  frag.append(el("h2", "3. Subtitles (embedded tracks → WebVTT)"));
  if (!entries.length) {
    frag.append(el("em", "No embedded subtitle tracks detected."));
    box.replaceChildren(frag); return;
  }
  for (const e of entries) {
    if (!e.compatible) {
      const d = el("div", `✗ ${e.label} (${e.codec} — ${e.note || "unsupported"})`);
      d.className = "unsupported"; frag.append(d); continue;
    }
    const row = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = e.enabled;
    cb.addEventListener("change", () => subMgr.setEnabled(e.index, cb.checked));
    const note = el("small", e.loadedNote); note.style.color = "#777";
    const jumpBtn = el("button", "jump to first cue");
    jumpBtn.style.display = "none";
    row.append(cb, document.createTextNode(` ${e.label} (${e.codec}, ${e.lang})`), note, jumpBtn);
    frag.append(row);
    // reflect manager state into the row on every change
    const orig = subMgr.onChange;
    subMgr.onChange = () => {
      orig();
      note.textContent = e.loadedNote;
      jumpBtn.style.display = e.hasCues ? "" : "none";
      jumpBtn.onclick = () => subMgr.jumpToFirstCue(e.index);
    };
  }
  box.replaceChildren(frag);
}

/* ---------------- diagnostics ---------------- */

function renderDiag() { $("#diag").textContent = subMgr.diagText(); }
["loadedmetadata", "seeked", "play"].forEach(ev => video.addEventListener(ev, renderDiag));

/* ---------------- seek buttons + event status line --------------------- */

document.querySelectorAll("[data-jump]").forEach(btn =>
  btn.addEventListener("click", () => {
    if (Number.isFinite(video.duration) && video.duration > 0) {
      video.currentTime = video.duration * parseFloat(btn.dataset.jump);
    }
  }));

for (const ev of ["loadedmetadata","seeking","seeked","waiting","canplay","progress","stalled","error"]) {
  video.addEventListener(ev, () => {
    const bufferedEnd = video.buffered.length
      ? video.buffered.end(video.buffered.length - 1).toFixed(1) : "0.0";
    $("#status").textContent =
      `event: ${ev} | now ${video.currentTime.toFixed(1)}s | buffered to ${bufferedEnd}s`;
    if (ev === "error" && video.error) {
      $("#status").textContent += ` (media error code ${video.error.code}: ${video.error.message || "unknown"})`;
    }
  });
}

main();
