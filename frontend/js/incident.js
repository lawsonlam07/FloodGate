// Incident page.
//  - verified_board: header, Key facts panel, Key/All timeline toggle, secondary tag filters,
//    compact timeline items that expand into the full card (log, sources, voting, report, tags).
//  - official_warning: a simple warning view (headline, severity, validity, area, official text, source).
import * as api from "./api.js";
import { createLive, ribbonText, animDelay, setPausedChip } from "./live.js";
import { renderPhotos } from "./photos.js";
import {
  initTheme,
  esc, ago, clock, icon, sevChip, countChips, statusChip, heldChip, STATUS, STATUS_ORDER, KIND_ICON, STANCE,
  CATEGORY_LABEL, lsGet, lsSet, setLive, startAgoTicker, toast, isWarning, rangeText, untilText, validText, isEnded, capturedText, updatedChecked, onResume,
} from "./ui.js";

const incidentId = new URLSearchParams(location.search).get("id");
const VOTES_KEY = "fg-votes";
const state = { incident: null, items: [], tags: { status: [], category: [], place: [] }, keyFacts: null, contextChecks: [] };
const filter = { status: new Set(), category: new Set(), place: new Set() };
const view = { mode: "key", expanded: new Set(), filtersOpen: false };

// ---------------------------------------------------------------- live update feedback
const liveFx = createLive({
  getIncident: (id) => (id === incidentId ? state.incident : null),
  inFocus: (id) => id === incidentId,                    // other incidents' updates go to the bell only
});
const changes = new Map();          // item_id → change (from liveFx.onItem) for ribbons / glow
const kfKnown = new Map();          // section → Set(item_id) already shown
const kfFresh = new Map();          // `${section}:${item_id}` → when it appeared or changed
let kfReady = false;
const changeAge = (id) => (changes.has(id) ? Date.now() - changes.get(id).at : Infinity);
const GLOW_CLASS = { false: "glow-false", verified: "glow-ok", held: "glow-held" };
const RIBBON_CLASS = { false: "ribbon-false", verified: "ribbon-ok", held: "ribbon-held" };

const $ = (id) => document.getElementById(id);
$("back").href = api.link("index.html");

// ---------------------------------------------------------------- header

function renderHeader() {
  const inc = state.incident;
  if (!inc) return;
  document.title = `${inc.name} · FloodGate`;
  if (isWarning(inc)) { $("inc-head").innerHTML = ""; return; }
  const sources = (inc.sources || []).map((s) =>
    `<a href="${esc(s.url)}" target="_blank" rel="noopener" class="chip chip-tag hover:text-ink" title="${esc(capturedText(s) || s.url)}">${icon(KIND_ICON[s.kind] || "public", "text-[14px]")}${esc(s.name)}</a>`).join(" ");
  $("inc-head").innerHTML = `
  <div class="card accent-sev${esc(inc.severity)} p-4">
    <div class="flex items-center justify-between gap-2 flex-wrap">
      <div class="flex items-center gap-2">${sevChip(inc.severity, inc.severity_label)}
        <span class="label text-faint">${esc(inc.status)}</span></div>
      ${updatedChecked(inc, "label text-faint")}
    </div>
    <h1 class="mt-2 text-2xl sm:text-3xl font-extrabold tracking-tight leading-tight">${esc(inc.name)}</h1>
    <p class="mt-2 text-sm sm:text-base text-muted">${esc(inc.summary)}</p>
    <div class="mt-3 label text-faint flex items-center gap-1">${icon("location_on", "text-[14px]")}${esc(inc.area?.label || "")}
      ${(inc.hazards || []).length ? ` · ${esc(inc.hazards.join(", ").replace(/_/g, " "))}` : ""}</div>
    <div class="mt-3 flex flex-wrap gap-1.5">${countChips(inc.counts)}</div>
    ${sources ? `<details class="mt-3 pt-3 border-t border-line"><summary class="label flex items-center justify-between">Watched sources · ${(inc.sources || []).length}<span class="material-symbols-outlined chev text-faint">expand_more</span></summary><div class="mt-2 flex flex-wrap gap-1.5">${sources}</div></details>` : ""}
  </div>`;
}

// ---------------------------------------------------------------- key facts

const KF_SECTIONS = [
  { key: "confirmed", title: "Confirmed", icon: "verified", cls: "text-ok", border: "border-ok/40" },
  { key: "what_to_do", title: "What to do", icon: "campaign", cls: "text-authority", border: "border-authority/40", note: "official instructions only" },
  { key: "not_true", title: "Not true", icon: "block", cls: "text-critical", border: "border-critical/40" },
  { key: "dont_act_yet", title: "Don't act on yet", icon: "pan_tool", cls: "text-triage", border: "border-triage/40", note: "unconfirmed reassurance" },
  { key: "developing", title: "Developing", icon: "trending_up", cls: "text-triage", border: "border-triage/40", note: "not yet confirmed" },
];

function kfLine(section, f) {
  const href = `#${encodeURIComponent(f.item_id)}`;
  let body = `<span>${esc(f.title)}</span>`;
  if (section === "not_true") {
    body = `<span><span class="struck">${esc(f.title)}</span>${f.debunked_by ? `<span class="block mono text-[11px] text-critical/90 mt-0.5">debunked by ${esc(f.debunked_by)}</span>` : ""}</span>`;
  } else if (section === "developing") {
    body = `<span>${esc(f.title)}<span class="ml-1.5 align-middle">${statusChip(f.status || "unverified")}</span>${f.origins ? `<span class="ml-1 mono text-[11px] text-faint">${esc(f.origins)} origins</span>` : ""}</span>`;
  }
  const bullet = section === "what_to_do" ? icon("arrow_right", "text-authority text-[18px] -ml-1") : `<span class="mt-[7px] w-1.5 h-1.5 rounded-full bg-current opacity-70 shrink-0"></span>`;
  const fresh = kfFresh.has(`${section}:${f.item_id}`) ? Date.now() - kfFresh.get(`${section}:${f.item_id}`) : Infinity;
  const flash = fresh < 2500 ? `kf-fresh" style="${animDelay(fresh)}` : "";
  const dot = fresh < 60000 ? `<span class="kf-dot ml-auto" title="new or changed"></span>` : "";
  return `<li><a href="${href}" data-goto="${esc(f.item_id)}" class="flex items-start gap-2 py-1.5 px-1 -mx-1 rounded text-sm leading-snug text-ink hover:bg-elevated ${flash}">${bullet}${body}${dot}</a></li>`;
}

function trackKeyFacts() {
  const kf = state.keyFacts || {};
  for (const s of KF_SECTIONS) {
    const now = new Set((kf[s.key] || []).map((f) => f.item_id));
    const before = kfKnown.get(s.key) || new Set();
    for (const id of now) {
      if ((kfReady && !before.has(id)) || changeAge(id) < 60000) {
        if (!kfFresh.has(`${s.key}:${id}`)) kfFresh.set(`${s.key}:${id}`, Date.now());
      }
    }
    kfKnown.set(s.key, now);
  }
  kfReady = true;
}

function renderKeyFacts() {
  trackKeyFacts();
  const kf = state.keyFacts;
  const el = $("key-facts");
  const sections = kf ? KF_SECTIONS.filter((s) => (kf[s.key] || []).length) : [];
  if (!sections.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  el.innerHTML = `
  <div class="card p-4">
    <div class="flex items-baseline justify-between gap-2">
      <h2 id="kf-h" class="text-base font-bold tracking-tight flex items-center gap-2 whitespace-nowrap">${icon("summarize", "text-authority")}Key facts</h2>
      <span class="label text-faint text-right">by verification rules<span class="hidden sm:inline"> · tap to see evidence</span></span>
    </div>
    <div class="mt-2 grid gap-3 sm:grid-cols-2">
      ${sections.map((s) => `
      <div class="rounded border ${s.border} bg-canvas/40 p-3 ${s.key === "confirmed" || s.key === "what_to_do" ? "" : ""}">
        <div class="label ${s.cls} flex items-center gap-1.5 whitespace-nowrap">${icon(s.icon, "text-[16px]")}${esc(s.title)} <span class="tnum opacity-80">${kf[s.key].length}</span></div>
        ${s.note ? `<div class="mono text-[10.5px] text-faint mt-0.5">${esc(s.note)}</div>` : ""}
        <ul class="mt-1.5 ${s.cls}">${kf[s.key].map((f) => kfLine(s.key, f)).join("")}</ul>
      </div>`).join("")}
    </div>
  </div>`;
  el.querySelectorAll("[data-goto]").forEach((a) => {
    a.onclick = (e) => { e.preventDefault(); goTo(a.dataset.goto); history.replaceState(null, "", `#${encodeURIComponent(a.dataset.goto)}`); };
  });
}

// Make sure an item is visible (switch to All / clear filters if needed), expand it and scroll to it.
function goTo(itemId) {
  const it = state.items.find((x) => x.id === itemId);
  if (!it) return;
  if (!inView(it)) {
    if (view.mode === "key" && it.importance !== "key") setMode("all", false);
    if (!passes(it)) clearFilters(false);
  }
  view.expanded.add(itemId);
  renderFilters(); renderTimeline();
  const li = document.getElementById(itemId);
  if (li) { li.scrollIntoView({ block: "start", behavior: "smooth" }); li.querySelector("article")?.classList.add("flash"); }
}

// ---------------------------------------------------------------- Context.dev checks (v0.5)

const CTX_CHIP = { changed: "chip-corroborated", no_change: "chip-tag", baseline: "chip-tag", failed: "chip-contradicted", skipped: "chip-tag" };
let ctxOpen = false;

function renderContextChecks() {
  const el = $("ctx-checks");
  const list = state.contextChecks || [];
  if (!list.length || isWarning(state.incident)) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const latest = list[0];
  const chip = (c) => c.disagreement ? `<span class="chip chip-unverified">review</span>` : `<span class="chip ${CTX_CHIP[c.status] || "chip-tag"}">${esc(String(c.status).replace("_", " "))}</span>`;
  el.innerHTML = `
  <details class="card" ${ctxOpen ? "open" : ""}>
    <summary class="px-4 py-3 flex items-center justify-between gap-2">
      <span class="label flex items-center gap-2">${icon("travel_explore", "text-authority text-[17px]")}Context.dev checks <span class="chip chip-tag tnum">${list.length}</span></span>
      <span class="flex items-center gap-2 min-w-0"><span class="hidden sm:inline-flex">${chip(latest)}</span>${ago(latest.at, "label text-faint")}${icon("expand_more", "chev text-muted")}</span>
    </summary>
    <ol class="border-t border-line divide-y divide-line">
      ${list.map((c) => `
      <li class="px-4 py-2.5">
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span class="mono text-[11px] text-faint">${clock(c.at)}</span>
          ${chip(c)}
          <span class="text-xs text-muted">${esc(c.source_name || c.source_id)}</span>
          <span class="ml-auto mono text-[10.5px] text-faint">${c.importance ? `${esc(c.importance)} importance · ` : ""}${c.confidence != null ? `${Math.round(c.confidence * 100)}% conf · ` : ""}${c.latency_ms != null ? `${(c.latency_ms / 1000).toFixed(1)} s` : ""}</span>
        </div>
        ${c.title ? `<div class="mt-1 text-sm font-semibold leading-snug">${esc(c.title)}</div>` : ""}
        ${c.summary ? `<div class="text-xs text-muted">${esc(c.summary)}</div>` : ""}
        ${c.disagreement ? `<div class="mt-1 text-xs text-triage">Context.dev saw no meaningful change, but our pipeline extracted ${esc(c.pipeline_new_claims ?? "some")} new claims. Review.</div>` : ""}
      </li>`).join("")}
    </ol>
  </details>`;
  el.querySelector("details").addEventListener("toggle", (e) => { ctxOpen = e.target.open; });
}

// ---------------------------------------------------------------- spoken bulletin (v0.10, ElevenLabs)
// The script is built by rules on the server from the key facts (no AI wording). Audio is generated only when someone
// presses Listen, and cached per script, so each state of the board is voiced at most once per language.

const LANG_NAME = { en: "English", de: "Deutsch", fr: "Français", nl: "Nederlands", pl: "Polski", sv: "Svenska",
  cs: "Čeština", pt: "Português", it: "Italiano", es: "Español", el: "Ελληνικά" };
const bul = { lang: null, data: null, busy: false, error: null, open: false, playingId: null };
const audio = new Audio();
audio.preload = "none";
for (const ev of ["play", "pause", "ended"]) audio.addEventListener(ev, () => renderBulletin());

async function loadBulletin() {
  try {
    bul.data = await api.getBulletin(incidentId, bul.lang || "en");
    bul.lang = bul.data.lang;
  } catch (err) {
    console.warn("bulletin unavailable", err);
    bul.data = null;
  }
  renderBulletin();
}

function bulletinNote(d) {
  if (isWarning(state.incident)) return "The official warning text, voiced by ElevenLabs.";
  return d.verbatim ? "The officials' own sentences, read word for word. Voiced by ElevenLabs."
                    : "The board's lines above, put together by rules (no AI wording). Voiced by ElevenLabs.";
}

function renderBulletin() {
  const el = $("bulletin");
  const d = bul.data;
  if (!d || !d.lines?.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const langs = d.languages || ["en"];
  const playing = !audio.paused && bul.playingId != null;
  const stale = playing && bul.playingId !== d.id;
  const btn = bul.busy ? [icon("progress_activity", "animate-spin"), "Voicing…"]
    : playing ? [icon("pause"), "Pause"] : [icon("play_arrow"), "Listen"];
  el.innerHTML = `
  <div class="card p-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 class="text-base font-bold tracking-tight flex items-center gap-2">${icon("radio", "text-authority")}Spoken bulletin</h2>
      ${langs.length > 1 ? `<div class="inline-flex rounded border border-line overflow-hidden" role="group" aria-label="Bulletin language">
        ${langs.map((l, i) => `<button type="button" data-lang="${esc(l)}" aria-pressed="${l === d.lang}"
          class="px-2.5 min-h-[32px] text-xs font-semibold ${i ? "border-l border-line" : ""} ${l === d.lang ? "bg-elevated text-ink" : "text-muted hover:text-ink"}">${esc(LANG_NAME[l] || l)}</button>`).join("")}
      </div>` : ""}
    </div>
    <div class="mt-3 flex items-center gap-3">
      <button type="button" id="bul-play" class="btn shrink-0" ${bul.busy || (!d.can_voice && !d.audio && !playing) ? "disabled" : ""}>${btn.join("")}</button>
      <span class="text-xs text-muted leading-snug">${esc(bulletinNote(d))}</span>
    </div>
    ${stale ? `<p class="mt-2 text-xs text-triage">The board changed while this was playing. Press again for the new bulletin.</p>` : ""}
    ${bul.error ? `<p class="mt-2 text-xs text-critical">${esc(bul.error)}</p>` : ""}
    <details class="mt-3" ${bul.open ? "open" : ""}>
      <summary class="label cursor-pointer flex items-center gap-1">${icon("expand_more", "chev text-muted")}Transcript · ${esc(d.chars)} characters</summary>
      <div class="mt-2 text-sm leading-relaxed" lang="${esc(d.lang)}">
        ${d.lines.map((l) => l.kind === "heading" ? `<div class="mt-2 font-semibold">${esc(l.text)}</div>`
          : `<div class="${l.kind === "fact" ? "pl-3 border-l border-line" : "text-muted"}">${esc(l.text)}</div>`).join("")}
      </div>
    </details>
  </div>`;
}

async function playBulletin() {
  const d = bul.data;
  if (!d) return;
  if (!audio.paused && bul.playingId === d.id) { audio.pause(); return; }
  if (bul.playingId === d.id && audio.src) { audio.play(); return; }   // resume the same bulletin
  bul.busy = true; bul.error = null; renderBulletin();
  try {
    const r = d.audio ? { audio: d.audio } : await api.voiceBulletin(incidentId, d.lang);
    if (!r.audio) throw new Error("Mock mode: audio comes from the real backend only.");
    d.audio = r.audio;
    bul.playingId = d.id;
    audio.src = r.audio;
    await audio.play();
  } catch (err) {
    bul.error = `Could not play the bulletin: ${err.message}`;
  } finally {
    bul.busy = false;
    renderBulletin();
  }
}

$("bulletin").addEventListener("click", (e) => {
  const lang = e.target.closest("[data-lang]")?.dataset.lang;
  if (lang && lang !== bul.lang) { bul.lang = lang; bul.error = null; loadBulletin(); return; }
  if (e.target.closest("#bul-play")) playBulletin();
});
$("bulletin").addEventListener("toggle", (e) => { if (e.target.tagName === "DETAILS") bul.open = e.target.open; }, true);

// ---------------------------------------------------------------- over time (v0.10, history in Tiger Data)

const hist = { data: null, open: false, at: 0 };
const LAYERS = [
  ["verified", "var(--ok)", 0.85, "Verified"], ["corroborated", "var(--authority)", 0.8, "Corroborated"],
  ["unverified", "var(--triage)", 0.55, "Unverified"], ["disputed", "var(--triage)", 0.3, "Disputed"],
  ["contradicted", "var(--critical)", 0.8, "Contradicted"],
];

async function loadHistory(force = false) {
  if (api.MOCK && hist.data) return;
  if (!force && Date.now() - hist.at < 10000) return;      // at most every 10 s while the board is busy
  hist.at = Date.now();
  try { hist.data = await api.getHistory(incidentId); } catch { hist.data = null; }
  renderHistory();
}

function dur(s) {
  if (s == null) return "–";
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 && m < 10 ? `${m} min ${s % 60} s` : `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

function historyChart(points) {
  const W = 600, H = 120;
  const pts = points.length === 1 ? [points[0], points[0]] : points;
  const val = (p, k) => Number(p[k]) || 0;
  const max = Math.max(1, ...pts.map((p) => LAYERS.reduce((sum, [k]) => sum + val(p, k), 0)));
  const x = (i) => (i / (pts.length - 1)) * W;
  const y = (v) => H - (v / max) * (H - 4);
  const base = pts.map(() => 0);
  const areas = LAYERS.map(([k, color, opacity]) => {
    const top = pts.map((p, i) => base[i] + val(p, k));
    const d = `M${pts.map((_, i) => `${x(i).toFixed(1)},${y(top[i]).toFixed(1)}`).join("L")}`
      + `L${pts.map((_, i) => `${x(pts.length - 1 - i).toFixed(1)},${y(base[pts.length - 1 - i]).toFixed(1)}`).join("L")}Z`;
    top.forEach((v, i) => { base[i] = v; });
    return `<path d="${d}" fill="${color}" fill-opacity="${opacity}"></path>`;
  }).join("");
  const last = points[points.length - 1];
  const label = LAYERS.map(([k, , , name]) => `${val(last, k)} ${name.toLowerCase()}`).join(", ");
  return `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="w-full h-[120px] block" role="img" aria-label="Claims by status over time; now ${esc(label)}">
      <line x1="0" y1="${H - 0.5}" x2="${W}" y2="${H - 0.5}" stroke="var(--line)"></line>${areas}
    </svg>
    <div class="mt-1 flex justify-between mono text-[10.5px] text-faint"><span>${clock(points[0].t)}</span><span>max ${max} claims</span><span>${clock(last.t)}</span></div>
    <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
      ${LAYERS.map(([k, color, opacity, name]) => `<span class="flex items-center gap-1"><span class="inline-block w-2.5 h-2.5 rounded-sm" style="background:${color};opacity:${opacity}"></span>${name} <span class="tnum">${val(last, k)}</span></span>`).join("")}
    </div>`;
}

function historyRows(rows, verb) {
  if (!rows.length) return "";
  return `<ul class="mt-1 divide-y divide-line">${rows.slice(0, 6).map((r) => `
    <li class="py-1.5 flex items-baseline gap-2 text-sm">
      <a href="#${esc(r.item_id)}" class="flex-1 min-w-0 hover:underline">${esc(r.title)}</a>
      <span class="mono text-[11px] text-muted whitespace-nowrap">${verb} after ${dur(r.after_s)}</span>
    </li>`).join("")}</ul>`;
}

function renderHistory() {
  const el = $("history");
  const d = hist.data;
  if (!d || !d.available || isWarning(state.incident) || !d.timeline?.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const s = d.stats || {};
  const tile = (label, value, sub) => `
    <div class="rounded border border-line bg-canvas/40 px-3 py-2">
      <div class="label text-faint">${label}</div>
      <div class="mt-0.5 text-lg font-bold tnum">${value}</div>
      <div class="text-[11px] text-muted">${sub}</div>
    </div>`;
  const quick = s.debunked ? `${s.debunked} debunked · median ${dur(s.median_debunk_s)}`
    : s.updates ? `update → board ${dur(Math.round((s.p50_update_ms || 0) / 1000))}` : "";
  el.innerHTML = `
  <details class="card" ${hist.open ? "open" : ""}>
    <summary class="px-4 py-3 flex items-center justify-between gap-2">
      <span class="label flex items-center gap-2">${icon("monitoring", "text-authority text-[17px]")}Over time <span class="chip chip-tag">Tiger Data</span></span>
      <span class="flex items-center gap-2 min-w-0"><span class="label text-faint truncate">${esc(quick)}</span>${icon("expand_more", "chev text-muted")}</span>
    </summary>
    <div class="border-t border-line px-4 py-3">
      <div class="grid grid-cols-1 min-[480px]:grid-cols-3 gap-2">
        ${tile("Rumour → debunked", dur(s.median_debunk_s), `median of ${s.debunked || 0}`)}
        ${tile("Report → confirmed", dur(s.median_confirm_s), `median of ${s.confirmed || 0}`)}
        ${tile("Page change → board", s.p50_update_ms != null ? dur(Math.round(s.p50_update_ms / 1000)) : "–", `median of ${s.updates || 0} updates`)}
      </div>
      <h3 class="label mt-4 mb-1">Claims by status · per ${esc(d.bucket || "minute")}</h3>
      ${historyChart(d.timeline)}
      ${d.debunked?.length ? `<h3 class="label mt-4">Debunked by officials</h3>${historyRows(d.debunked, "debunked")}` : ""}
      ${d.confirmed?.length ? `<h3 class="label mt-4">Reported first, confirmed later</h3>${historyRows(d.confirmed, "confirmed")}` : ""}
      <p class="mt-3 text-[11px] text-faint">Times run from the first report on the board. Every change is kept as a time series in Tiger Data (Postgres + TimescaleDB).</p>
    </div>
  </details>`;
  el.querySelector("details").addEventListener("toggle", (e) => { hist.open = e.target.open; });
}

// ---------------------------------------------------------------- official warning view

function renderWarning() {
  const inc = state.incident;
  const it = state.items[0];
  const text = it?.sources?.[0]?.evidence_quote || it?.summary || inc.summary;
  const src = it?.sources?.[0] || inc.sources?.[0];
  const el = $("warning-view");
  el.classList.remove("hidden");
  el.innerHTML = `
  <article class="card accent-sev${esc(inc.severity)} p-4 sm:p-5">
    <div class="flex items-center justify-between gap-2 flex-wrap">
      <div class="flex items-center gap-2 flex-wrap">${sevChip(inc.severity, inc.severity_label)}<span class="chip chip-corroborated">${icon("account_balance", "text-[13px]")}Official warning</span>
        ${isEnded(inc) ? `<span class="chip chip-tag">ended</span>` : ""}</div>
      <span class="label text-faint">issued ${ago(inc.created_at)}</span>
    </div>
    <h1 class="mt-3 text-xl sm:text-2xl font-extrabold tracking-tight leading-snug">${esc(inc.name)}</h1>
    <dl class="mt-4 grid gap-3 sm:grid-cols-2">
      <div class="rounded border border-line bg-canvas/40 p-3">
        <dt class="label flex items-center gap-1">${icon("schedule", "text-[15px]")}Valid</dt>
        <dd class="mt-1 mono text-sm">${esc(rangeText(inc.valid))}</dd>
        <dd class="mt-0.5 mono text-[11px] ${isEnded(inc) ? "text-critical" : "text-faint"}">${esc(isEnded(inc) ? "this warning has ended" : validText(inc.valid))}</dd>
      </div>
      <div class="rounded border border-line bg-canvas/40 p-3">
        <dt class="label flex items-center gap-1">${icon("location_on", "text-[15px]")}Area</dt>
        <dd class="mt-1 text-sm">${esc(inc.area?.label || "")}</dd>
      </div>
    </dl>
    <div class="mt-4">
      <div class="label mb-1.5">Official text</div>
      <blockquote class="rounded border-l-[3px] border-authority bg-authority/5 px-3 py-2.5 text-base leading-relaxed">${esc(text)}</blockquote>
    </div>
    <div class="mt-4 pt-3 border-t border-line flex flex-wrap items-center justify-between gap-2">
      <span class="text-sm text-muted flex items-center gap-1.5">${icon("account_balance", "text-muted")}${esc(inc.source_label || src?.name || "Official source")}</span>
      ${src?.url ? `<a href="${esc(src.url)}" target="_blank" rel="noopener" class="btn">${icon("open_in_new", "text-[16px]")}Open official source</a>` : ""}
      ${capturedText(src) ? `<span class="basis-full mono text-[10.5px] text-faint" title="${esc(src.captured_from)}">${esc(capturedText(src))}</span>` : ""}
    </div>
  </article>`;
}

// ---------------------------------------------------------------- mode + filters

const keyCount = () => state.items.filter((i) => i.importance === "key").length;
const hasImportance = () => state.items.some((i) => i.importance);   // v0.1 backends have no importance field

function setMode(mode, render = true) {
  view.mode = mode;
  $("mode-key").className = `px-3 min-h-[36px] text-sm font-semibold ${mode === "key" ? "bg-elevated text-ink" : "text-muted"}`;
  $("mode-all").className = `px-3 min-h-[36px] text-sm font-semibold border-l border-line ${mode === "all" ? "bg-elevated text-ink" : "text-muted"}`;
  $("mode-key").setAttribute("aria-pressed", String(mode === "key"));
  $("mode-all").setAttribute("aria-pressed", String(mode === "all"));
  if (render) renderTimeline();
}
$("mode-key").onclick = () => setMode("key");
$("mode-all").onclick = () => setMode("all");
$("filter-btn").onclick = () => {
  view.filtersOpen = !view.filtersOpen;
  $("filter-panel").classList.toggle("hidden", !view.filtersOpen);
  $("filter-btn").setAttribute("aria-expanded", String(view.filtersOpen));
};

function allTags() {
  const status = new Set(state.tags.status || []);
  const category = new Set(state.tags.category || []);
  const place = new Set(state.tags.place || []);
  for (const it of state.items) { status.add(it.status); if (it.category) category.add(it.category); }
  return { status: STATUS_ORDER.filter((s) => status.has(s)), held: state.items.some((i) => i.held), category: [...category], place: [...place] };
}

function chipButton(group, value, label, cls, count) {
  const on = filter[group].has(value);
  return `<button type="button" class="fchip chip ${cls}" aria-pressed="${on}" data-group="${group}" data-value="${esc(value)}">${label}${count != null ? ` <span class="tnum opacity-80">${count}</span>` : ""}</button>`;
}

function clearFilters(render = true) {
  filter.status.clear(); filter.category.clear(); filter.place.clear();
  if (render) { renderFilters(); renderTimeline(); }
}

function renderFilters() {
  const t = allTags();
  const count = (pred) => state.items.filter(pred).length;
  const active = filter.status.size + filter.category.size + filter.place.size;
  const parts = [
    `<button type="button" class="fchip chip chip-tag" aria-pressed="${!active}" data-all="1">Clear</button>`,
    ...t.status.map((s) => chipButton("status", s, `<span class="dot"></span>${esc(STATUS[s]?.label || s)}`, `chip-${s}`, count((i) => i.status === s))),
  ];
  if (t.held) parts.push(chipButton("status", "held", "Held", "chip-held", count((i) => i.held)));
  if (t.category.length) parts.push(`<span class="basis-full h-0"></span>`);
  parts.push(...t.category.map((c) => chipButton("category", c, esc(CATEGORY_LABEL(c)), "chip-tag", count((i) => i.category === c))));
  if (t.place.length) parts.push(`<span class="basis-full h-0"></span>`);
  parts.push(...t.place.map((p) => chipButton("place", p, `${icon("location_on", "text-[13px]")}${esc(p)}`, "chip-tag", count((i) => i.place === p))));
  const el = $("filters");
  el.innerHTML = parts.join("");
  el.querySelectorAll("[data-group]").forEach((b) => {
    b.onclick = () => {
      const set = filter[b.dataset.group];
      set.has(b.dataset.value) ? set.delete(b.dataset.value) : set.add(b.dataset.value);
      renderFilters(); renderTimeline();
    };
  });
  el.querySelector("[data-all]").onclick = () => clearFilters();
  $("filter-n").textContent = active ? String(active) : "";
  $("filter-n").classList.toggle("hidden", !active);
  $("n-key").textContent = hasImportance() ? keyCount() : state.items.length;
  $("mode-key").disabled = hasImportance() && keyCount() === 0;
  $("n-all").textContent = state.items.length;
}

// OR within a tag group, AND across groups.
function passes(it) {
  if (filter.status.size && !(filter.status.has(it.status) || (filter.status.has("held") && it.held))) return false;
  if (filter.category.size && !filter.category.has(it.category)) return false;
  if (filter.place.size && !filter.place.has(it.place)) return false;
  return true;
}
const inView = (it) => (view.mode === "all" || !hasImportance() || it.importance === "key") && passes(it);

// ---------------------------------------------------------------- timeline items

const LOG_ICON = { seen: "visibility", status: "swap_vert", held: "pan_tool", removed: "remove_circle", source_added: "add_circle" };

function sourceRow(s) {
  const social = s.kind === "social";
  const who = social && s.author ? `<span class="text-ink">@${esc(s.author)}</span>` : "";
  const att = s.attributed_to ? `<span class="text-faint">citing ${esc(s.attributed_to)}</span>` : "";
  return `
  <li class="text-xs">
    <details>
      <summary class="flex items-start gap-2 py-1">
        ${icon(KIND_ICON[s.kind] || "public", "text-muted text-[16px] mt-0.5")}
        <span class="min-w-0 flex-1">
          <span class="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
            <span class="font-semibold text-ink">${esc(s.name)}</span>
            <span class="chip chip-tag !text-[10px] !py-0">${esc(s.kind)}</span>
            <span class="${s.stance === "denies" ? "text-critical" : s.stance === "asserts" ? "text-muted" : "text-triage"}">${esc(STANCE[s.stance] || s.stance)}</span>
            ${who} ${att}
          </span>
          ${social && s.author_signals ? `<span class="block mono text-[10.5px] text-faint mt-0.5">${esc(s.author_signals)}</span>` : ""}
        </span>
        <span class="material-symbols-outlined chev text-faint text-[16px]">expand_more</span>
      </summary>
      <div class="ml-6 mb-1.5 pl-2 border-l-2 border-line">
        <q class="block italic text-muted">${esc(s.evidence_quote)}</q>
        <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 mono text-[10.5px] text-faint">
          <span>seen ${clock(s.seen_at)}</span>
          <a href="${esc(s.url)}" target="_blank" rel="noopener" class="text-authority hover:underline flex items-center gap-0.5">open source ${icon("open_in_new", "text-[12px]")}</a>
          ${capturedText(s) ? `<span title="${esc(s.captured_from)}">${esc(capturedText(s))}</span>` : ""}
        </div>
      </div>
    </details>
  </li>`;
}

function itemBody(it) {
  const voted = lsGet(VOTES_KEY, {})[it.id];
  const c = it.community || { still_true: 0, not_true: 0, reports: 0 };
  const log = (it.log || []).map((e) => `
    <li class="flex gap-2 py-1">
      <span class="mono text-[11px] text-faint shrink-0 w-[60px]">${clock(e.at)}</span>
      ${icon(LOG_ICON[e.kind] || "fiber_manual_record", "text-[15px] text-muted shrink-0")}
      <span class="text-xs text-muted">${esc(e.text)}</span>
    </li>`).join("");
  const tags = (it.tags || []).map((t) => `<span class="chip chip-tag">${esc(CATEGORY_LABEL(t))}</span>`).join(" ");
  return `
  <div class="px-3.5 pb-3.5 sm:px-4 sm:pb-4 border-t border-line">
    <div class="pt-3 flex items-start justify-between gap-2">
      <p class="text-sm text-muted">${esc(it.summary)}</p>
      <div class="relative shrink-0 -mt-1">
        <button type="button" class="btn !min-h-[32px] !px-1.5 !bg-transparent !border-transparent hover:!border-line" aria-haspopup="menu" aria-expanded="false" aria-label="More actions" data-menu="${esc(it.id)}">${icon("more_vert")}</button>
        <div class="menu hidden" role="menu" data-menu-for="${esc(it.id)}">
          <button type="button" role="menuitem" class="w-full text-left px-3 py-2 text-sm flex items-center gap-2 hover:bg-card" data-report="${esc(it.id)}">${icon("flag", "text-critical")}Report</button>
        </div>
      </div>
    </div>
    ${it.status === "contradicted" ? `<div class="mt-1 label text-critical flex items-center gap-1">${icon("block", "text-[14px]")}Not true according to an official source</div>` : ""}
    ${it.held ? `<div class="mt-2 rounded border border-triage/40 bg-triage/10 px-2.5 py-2 text-sm text-triage flex items-start gap-2">${icon("pan_tool", "text-[16px] mt-0.5")}<span><b>Held back: unconfirmed reassurance.</b> Wait for an official source before acting on this.</span></div>` : ""}

    <details class="mt-3 rounded border border-line bg-canvas/40">
      <summary class="px-2.5 py-2 flex items-center justify-between">
        <span class="label flex items-center gap-1.5">${icon("fact_check", "text-[15px]")}Verification log · ${(it.log || []).length}</span>
        <span class="material-symbols-outlined chev text-faint">expand_more</span>
      </summary>
      <ol class="px-2.5 pb-2 border-t border-line">${log}</ol>
    </details>

    <div class="mt-3 flex flex-wrap items-center justify-between gap-2">
      <span class="text-sm text-muted flex items-center gap-1.5">${icon("where_to_vote", "text-authority")}Is this still true?</span>
      <div class="flex items-center gap-2" data-vote-row="${esc(it.id)}">
        <button type="button" class="btn ${voted === true ? "btn-on" : ""}" data-vote="yes" ${voted !== undefined ? "disabled" : ""} aria-label="Yes, still true">${icon("thumb_up", "text-[16px]")}Yes <span class="tnum text-muted">${esc(c.still_true)}</span></button>
        <button type="button" class="btn ${voted === false ? "btn-on" : ""}" data-vote="no" ${voted !== undefined ? "disabled" : ""} aria-label="No, not true">${icon("thumb_down", "text-[16px]")}No <span class="tnum text-muted">${esc(c.not_true)}</span></button>
      </div>
    </div>
    <div class="mt-1 mono text-[10.5px] text-faint">community signals only; they never change the status${c.reports ? ` · ${esc(c.reports)} report${c.reports > 1 ? "s" : ""}` : ""}</div>

    <footer class="mt-3 pt-3 border-t border-line">
      <div class="label mb-1">Sources · ${(it.sources || []).length}${it.origins ? ` <span class="text-faint normal-case tracking-normal">(${esc(it.origins.asserting)} asserting origin${it.origins.asserting === 1 ? "" : "s"}, ${esc(it.origins.denying)} denying)</span>` : ""}</div>
      <ul class="flex flex-col">${(it.sources || []).map(sourceRow).join("")}</ul>
      ${tags ? `<div class="mt-2 flex flex-wrap gap-1.5">${tags}</div>` : ""}
    </footer>
  </div>`;
}

function itemCard(it) {
  const open = view.expanded.has(it.id);
  const contradicted = it.status === "contradicted";
  const age = changeAge(it.id);
  const ch = changes.get(it.id);
  const glow = age < 3200 ? `glow ${GLOW_CLASS[ch.kind] || ""}` : age < 60000 ? `glow-static ${GLOW_CLASS[ch.kind] || ""}` : "";
  const ribbon = age < 60000 ? `<span class="ribbon ${RIBBON_CLASS[ch.kind] || ""}" style="${animDelay(age)}">${esc(ribbonText(ch))}</span>` : "";
  return `
  <li id="${esc(it.id)}" class="relative pl-10 scroll-mt-32" data-item="${esc(it.id)}">
    <span class="tl-node ${esc(it.status)}" style="top:10px">${icon(STATUS[it.status]?.icon || "help", "text-[15px]")}</span>
    <article class="card accent-${esc(it.status)} ${glow}" style="${age < 3200 ? animDelay(age) : ""}">
      <button type="button" class="w-full text-left px-3.5 py-3 sm:px-4 flex items-start gap-2" data-toggle="${esc(it.id)}" aria-expanded="${open}">
        <span class="min-w-0 flex-1">
          <span class="flex flex-wrap items-center gap-1.5">
            ${ribbon}${statusChip(it.status)} ${it.held ? heldChip() : ""}${it.superseded ? `<span class="chip chip-tag" title="A newer official warning replaced this one">superseded</span>` : ""}
            <span class="mono text-[11px] text-faint">${ago(it.updated_at)} · ${clock(it.updated_at)}</span>
          </span>
          <span class="mt-1.5 block font-semibold leading-snug ${contradicted ? "struck" : ""}">${contradicted ? `<span class="sr-only">False: </span>` : ""}${esc(it.title)}</span>
          ${open ? "" : `<span class="mt-0.5 block text-sm text-muted truncate">${esc(it.summary)}</span>`}
        </span>
        <span class="material-symbols-outlined text-faint shrink-0 mt-0.5 transition-transform ${open ? "rotate-180" : ""}">expand_more</span>
      </button>
      ${open ? itemBody(it) : ""}
    </article>
  </li>`;
}

function sortItems() {
  state.items.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
}

function renderTimeline() {
  sortItems();
  const shown = state.items.filter(inView);
  const el = $("timeline");
  el.innerHTML = shown.length ? shown.map(itemCard).join("")
    : `<li class="card p-4 text-sm text-muted">No items in this view.${view.mode === "key" ? " Try “All”." : ""}</li>`;
  $("tl-count").textContent = `${shown.length} of ${state.items.length} items`;
  wireItems(el);
}

function wireItems(root) {
  root.querySelectorAll("[data-toggle]").forEach((b) => {
    b.onclick = () => {
      const id = b.dataset.toggle;
      view.expanded.has(id) ? view.expanded.delete(id) : view.expanded.add(id);
      renderTimeline();
    };
  });
  root.querySelectorAll("[data-menu]").forEach((b) => {
    b.onclick = (ev) => {
      ev.stopPropagation();
      const menu = root.querySelector(`[data-menu-for="${CSS.escape(b.dataset.menu)}"]`);
      const open = menu.classList.contains("hidden");
      closeMenus();
      if (open) { menu.classList.remove("hidden"); b.setAttribute("aria-expanded", "true"); }
    };
  });
  root.querySelectorAll("[data-report]").forEach((b) => {
    b.onclick = () => { closeMenus(); openReport(b.dataset.report); };
  });
  root.querySelectorAll("[data-vote-row]").forEach((row) => {
    row.querySelectorAll("[data-vote]").forEach((b) => {
      b.onclick = () => castVote(row.dataset.voteRow, b.dataset.vote === "yes");
    });
  });
}

function closeMenus() {
  document.querySelectorAll(".menu").forEach((m) => m.classList.add("hidden"));
  document.querySelectorAll("[data-menu]").forEach((b) => b.setAttribute("aria-expanded", "false"));
}
document.addEventListener("click", closeMenus);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeMenus(); closeReport(); } });

function replaceItem(updated, flash = false) {
  const i = state.items.findIndex((x) => x.id === updated.id);
  if (i >= 0) state.items[i] = updated; else state.items.push(updated);
  renderFilters();
  keepPosition(renderTimeline);
  if (flash) document.getElementById(updated.id)?.querySelector("article")?.classList.add("flash");
}

// If the reader is down in the timeline, re-render without moving what they are looking at.
function keepPosition(fn) {
  const top = 120;                                         // sticky header + controls
  const lis = [...document.querySelectorAll("#timeline > li[data-item]")];
  const anchor = window.scrollY > 0 ? lis.find((li) => li.getBoundingClientRect().bottom > top) : null;
  const before = anchor?.getBoundingClientRect().top;
  fn();
  if (anchor) {
    const el = document.getElementById(anchor.dataset.item);
    if (el) window.scrollBy(0, el.getBoundingClientRect().top - before);
  }
}

// Mock mode only: approximate the backend's key_facts rules for a changed item.
function mockKeyFacts(it) {
  const kf = state.keyFacts || (state.keyFacts = {});
  for (const k of ["confirmed", "not_true", "developing", "dont_act_yet"]) kf[k] = (kf[k] || []).filter((f) => f.item_id !== it.id);
  if (it.status === "verified" && it.category !== "instruction") kf.confirmed.unshift({ item_id: it.id, title: it.title, category: it.category, severity: 4 });
  if (it.status === "contradicted") kf.not_true.unshift({ item_id: it.id, title: it.title, debunked_by: "Halvik Rescue Services" });
}

// "↑ 3 new updates" pill when updates land above the reader.
let pillCount = 0;
function timelineAbove() { return $("timeline-wrap").getBoundingClientRect().top < 56; }
function notePill(n) {
  if (!timelineAbove()) return;
  pillCount += n;
  let pill = $("new-pill");
  if (!pill) {
    pill = document.createElement("button");
    pill.id = "new-pill"; pill.type = "button"; pill.className = "new-pill";
    pill.onclick = () => {
      hidePill();
      if (view.mode === "key" && [...changes.keys()].some((id) => state.items.find((x) => x.id === id)?.importance !== "key")) setMode("all");
      window.scrollTo({ top: $("timeline-wrap").getBoundingClientRect().top + window.scrollY - 120, behavior: "smooth" });
    };
    document.body.appendChild(pill);
  }
  pill.innerHTML = `${icon("arrow_upward", "text-[16px]")}${pillCount} new update${pillCount === 1 ? "" : "s"}`;
}
function hidePill() { pillCount = 0; $("new-pill")?.remove(); }
window.addEventListener("scroll", () => { if (pillCount && !timelineAbove()) hidePill(); }, { passive: true });

// ---------------------------------------------------------------- votes + reports

async function castVote(itemId, stillTrue) {
  const it = state.items.find((x) => x.id === itemId);
  if (!it) return;
  try {
    const res = await api.vote(it, stillTrue);
    const votes = lsGet(VOTES_KEY, {});
    votes[itemId] = stillTrue;
    lsSet(VOTES_KEY, votes);
    replaceItem({ ...it, community: res.community });
    toast("Thanks, your answer was recorded");
  } catch (err) {
    toast("Could not send vote: " + err.message);
  }
}

let reportingId = null;
function openReport(itemId) {
  const it = state.items.find((x) => x.id === itemId);
  if (!it) return;
  reportingId = itemId;
  $("report-title").textContent = it.title;
  $("report-modal").classList.remove("hidden");
  document.querySelector('#report-form input[name="reason"]').focus();
}
function closeReport() {
  $("report-modal").classList.add("hidden");
  $("report-form").reset();
  reportingId = null;
}
$("report-cancel").onclick = closeReport;
$("report-modal").addEventListener("click", (e) => { if (e.target.id === "report-modal") closeReport(); });
$("report-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const it = state.items.find((x) => x.id === reportingId);
  if (!it) return closeReport();
  const fd = new FormData(e.target);
  try {
    const res = await api.report(it, fd.get("reason"), fd.get("note"));
    replaceItem({ ...it, community: res.community });
    toast("Report sent. Thank you.");
  } catch (err) {
    toast("Could not send report: " + err.message);
  }
  closeReport();
});

// ---------------------------------------------------------------- load + live

function renderAll() {
  renderHeader();
  if (isWarning(state.incident)) {
    for (const id of ["key-facts", "ctx-checks", "history", "controls", "timeline-wrap"]) $(id).classList.add("hidden");
    renderWarning();
    return;
  }
  $("warning-view").classList.add("hidden");
  $("controls").classList.remove("hidden");
  $("timeline-wrap").classList.remove("hidden");
  renderKeyFacts();
  renderContextChecks();
  renderHistory();
  renderFilters();
  renderTimeline();
}

async function fetchDetail() {
  const d = await api.getIncident(incidentId);
  state.incident = d.incident;
  state.items = d.items || [];
  state.tags = d.tags || state.tags;
  state.keyFacts = d.key_facts || null;
  state.contextChecks = d.context_checks || [];
  renderPhotos(d);                                         // photos from the sources (photos.js)
}

async function load() {
  if (!incidentId) {
    $("inc-head").innerHTML = `<div class="card p-4 text-critical text-sm">No incident id in the URL.</div>`;
    return;
  }
  try {
    await fetchDetail();
  } catch (err) {
    $("inc-head").innerHTML = `<div class="card p-4 text-critical text-sm">Could not load incident: ${esc(err.message)}</div>`;
    return;
  }
  liveFx.seed(state.items, incidentId);
  api.getHealth().then((h) => setPausedChip(!!h.paused, h.paused_since)).catch(() => {});
  setMode(hasImportance() && keyCount() > 0 ? "key" : "all", false);
  renderAll();
  loadBulletin();
  loadHistory(true);
  if (location.hash) goTo(decodeURIComponent(location.hash.slice(1)));
}

// key_facts only come with the detail endpoint, so refresh it (debounced) when the stream says something changed.
let refreshTimer = null;
function scheduleRefresh() {
  if (api.MOCK) return;
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    try {
      await fetchDetail(); renderHeader();
      if (!isWarning(state.incident)) { renderKeyFacts(); renderFilters(); keepPosition(renderTimeline); }
      loadBulletin(); loadHistory();
    }
    catch (err) { console.warn("refresh failed", err); }
  }, 1200);
}

function live() {
  api.subscribe({
    incident(inc) {
      if (inc.id !== incidentId) return;
      state.incident = { ...state.incident, ...inc };
      if (isWarning(state.incident)) renderWarning(); else renderHeader();
      scheduleRefresh();
    },
    item(it) {
      const ch = liveFx.onItem(it);                          // classify + feed the toast (any incident)
      if (it.incident_id !== incidentId) return;
      if (ch) {
        changes.set(it.id, ch);
        setTimeout(() => keepPosition(renderTimeline), 61000);   // let the ribbon go
        notePill(1);
      }
      replaceItem(it, false);
      if (ch && api.MOCK) mockKeyFacts(it);                // the real backend recomputes key_facts (picked up by the refresh)
      if (ch) renderKeyFacts();
      scheduleRefresh();
    },
    pipeline: (ev) => liveFx.onPipeline(ev),
    control: (c) => setPausedChip(!!c.paused, c.since),
    context_check(ev) {
      liveFx.onContextCheck(ev);
      if (ev.incident_id !== incidentId) return;
      state.contextChecks = [ev, ...(state.contextChecks || [])].slice(0, 10);
      renderContextChecks();
    },
  }, setLive);
}

window.addEventListener("hashchange", () => { if (location.hash) goTo(decodeURIComponent(location.hash.slice(1))); });
initTheme();
startAgoTicker();
load().finally(live);
onResume(async () => {
  await fetchDetail();
  renderHeader();
  if (isWarning(state.incident)) renderWarning();
  else { renderKeyFacts(); renderContextChecks(); renderFilters(); keepPosition(renderTimeline); }
  loadBulletin(); loadHistory(true);
});
