// Scenario control centre (admin / demo tool, v0.6): many fictional scenarios across Europe.
// Bulk + per-scenario actions, filters, a map, and for the selected scenario: steps, live pipeline, page previews,
// alerts and Context.dev verdicts. Live via SSE (scenario, pipeline, activity, alert, context_check).
import * as api from "./api.js";
import { createLive } from "./live.js";
import { basemap, initTheme, esc, ago, clock, icon, lsGet, lsSet, setLive, startAgoTicker, toast, activityRows, onResume } from "./ui.js";

const $ = (id) => document.getElementById(id);
const SEL_KEY = "fg-admin-selected";
const state = {
  scenarios: [], credits: 0, selectedId: null,
  pipeline: {},                 // source_id → {stage, elapsed_ms, receivedAt, detail, times}
  alerts: [], checks: [], activity: [],
  previewVersion: {},           // scenario id → cache buster for its page iframes
};
const filters = { scale: new Set(), status: new Set(), hazard: "", language: "", q: "" };

const SCALES = ["catastrophic", "major", "moderate", "minor"];
const SCALE_COLOR = { catastrophic: "#EF4444", major: "#F97316", moderate: "#F59E0B", minor: "#38BDF8" };
const HAZARD_ICON = {
  flood: "flood", storm: "storm", storm_surge: "tsunami", fire: "local_fire_department", earthquake: "earthquake",
  landslide: "landslide", hazmat: "science", power_outage: "power_off", heatwave: "thermostat", avalanche: "ac_unit",
  tsunami: "tsunami", dam_failure: "water_damage", other: "warning",
};
const LANG_FLAG = { en: "🇬🇧", de: "🇩🇪", fr: "🇫🇷", it: "🇮🇹", es: "🇪🇸", nl: "🇳🇱", pl: "🇵🇱", sv: "🇸🇪", pt: "🇵🇹", cs: "🇨🇿", el: "🇬🇷", fi: "🇫🇮" };
const STAGES = ["detected", "extracting", "extracted", "grouping", "grouped", "done"];
const TERMINAL = new Set(["done", "failed", "skipped"]);
const STAGE_LABEL = { detected: "change detected", extracting: "Context.dev extracting", extracted: "claims extracted",
  grouping: "grouping claims", grouped: "grouped", done: "on the board", failed: "failed", skipped: "skipped" };

$("board-link").href = api.link("index.html");
$("sites-link").href = api.MOCK ? "../fake_site/" : "/sites/";

// Bell only on admin (no pop-ups): finished updates and Context.dev verdicts of every scenario.
const liveFx = createLive({
  toasts: false,
  getIncident: (id) => { const sc = state.scenarios.find((s) => s.incident_id === id); return sc ? { name: sc.name } : null; },
});

// ---------------------------------------------------------------- pause processing (v0.8)
const EMBED = new URLSearchParams(location.search).get("embed") === "1";
const control = { paused: false, since: null, reason: null, showcase: null, auth_required: false, credit_budget_daily: null, credits_today: null, viewers: null };
const readOnly = () => EMBED || (control.auth_required && !api.getToken());

function applyAuth(msg = "") {
  document.body.classList.toggle("read-only", readOnly());
  $("ro-banner").classList.toggle("hidden", !readOnly() || EMBED);
  $("ro-msg").textContent = msg;
  $("ss-auth").innerHTML = readOnly()
    ? `${icon("visibility", "text-[14px]")}read-only`
    : control.auth_required ? `${icon("key", "text-[14px]")}admin (token)` : `${icon("lock_open", "text-[14px]")}local admin, no token needed`;
}
$("token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = $("token-input").value.trim();
  if (!t) return;
  let ok;
  try { ok = await api.checkToken(t); }              // the server decides; nothing unlocks on a guess
  catch (err) { applyAuth(err.message); return; }
  $("token-input").value = "";
  if (!ok) { applyAuth("Token rejected."); return; }
  api.setToken(t);
  applyAuth();
  renderAll();
  toast("Admin unlocked.");
});
// A token saved earlier may be outdated (e.g. changed on the server): check it once on load.
async function recheckSavedToken() {
  const t = api.getToken();
  if (!t || EMBED || !control.auth_required) return;
  try {
    if (!(await api.checkToken(t))) { api.setToken(""); applyAuth("Your saved token is no longer valid."); renderAll(); }
  } catch { /* offline: leave as is; actions will tell */ }
}
window.addEventListener("fg-auth-rejected", () => { applyAuth("Token rejected."); renderAll(); });

function applyControl(c) {
  Object.assign(control, { paused: !!c.paused, since: c.since || null, reason: c.reason || null,
    showcase: c.showcase || null, auth_required: !!c.auth_required, viewers: c.viewers ?? null,
    credit_budget_daily: c.credit_budget_daily ?? null, credits_today: c.credits_today ?? null });
  const t = control.since ? new Date(control.since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
  $("paused-banner").classList.toggle("hidden", !control.paused);
  $("paused-text").innerHTML = `<b>Paused${t ? ` since ${t}` : ""}${control.reason ? ` (${esc(control.reason)})` : ""}</b>: no Context.dev or LLM calls; page monitors stopped. Edits made now are picked up on resume.`;
  renderStatus();
  applyAuth($("ro-msg").textContent);
  if (EMBED) renderEmbed();
  const btn = $("pause-btn");
  btn.setAttribute("aria-pressed", String(control.paused));
  btn.classList.toggle("btn-on", control.paused);
  btn.innerHTML = control.paused
    ? `${icon("play_circle", "text-[17px]")}<span class="hidden sm:inline">Resume</span>`
    : `${icon("pause_circle", "text-[17px]")}<span class="hidden sm:inline">Pause processing</span>`;
  btn.title = control.paused ? "Resume processing" : "Pause processing: stop spending Context.dev and LLM credits";
  document.querySelectorAll(".paused-hint").forEach((el) => el.classList.toggle("hidden", !control.paused));
}
function showcaseText() {
  const sc = control.showcase;
  if (!sc) return "";
  if (!sc.running) return "Showcase off";
  const idle = sc.only_when_watched !== false && control.viewers === 0;
  return `Showcase ${idle ? "waiting for viewers" : "running"} · ${sc.max_concurrent} at a time · step every ${sc.interval_s} s`
    + (control.viewers != null ? ` · ${control.viewers} watching` : "");
}
function budgetText() {
  if (control.credits_today == null) return "";
  return control.credit_budget_daily ? `credits today ${Math.round(control.credits_today)} / ${control.credit_budget_daily}` : `credits today ${Math.round(control.credits_today)}`;
}
function renderStatus() {
  const sc = control.showcase;
  $("ss-showcase").innerHTML = sc ? `${sc.running ? `<span class="live-dot on"></span>` : icon("slideshow", "text-[14px]")}<span class="${sc.running ? "text-ink" : ""}">${esc(showcaseText())}</span>` : "";
  const ratio = control.credit_budget_daily ? control.credits_today / control.credit_budget_daily : 0;
  $("ss-budget").innerHTML = budgetText() ? `${icon("toll", "text-[14px]")}<span class="${ratio >= 1 ? "text-critical" : ratio >= 0.9 ? "text-triage" : ""}">${esc(budgetText())}</span>` : "";
  const btn = $("showcase-btn");
  btn.classList.toggle("btn-on", !!sc?.running);
  btn.setAttribute("aria-pressed", String(!!sc?.running));
  if (sc) {
    if (document.activeElement?.closest("#showcase-pop") == null) {
      $("sc-max").value = sc.max_concurrent; $("sc-interval").value = sc.interval_s; $("sc-hold").value = sc.hold_s;
      $("sc-watched").checked = sc.only_when_watched !== false;
    }
    $("sc-toggle").textContent = sc.running ? "Stop showcase" : "Start showcase";
  }
}
const showcaseSettings = () => ({
  max_concurrent: Math.min(8, Math.max(1, Number($("sc-max").value) || 3)),
  interval_s: Math.max(30, Number($("sc-interval").value) || 45),
  hold_s: Math.max(30, Number($("sc-hold").value) || 120),
  only_when_watched: $("sc-watched").checked,
});
$("showcase-btn").onclick = (e) => {
  e.stopPropagation();
  const pop = $("showcase-pop");
  pop.classList.toggle("hidden");
  $("showcase-btn").setAttribute("aria-expanded", String(!pop.classList.contains("hidden")));
};
$("showcase-pop").onclick = (e) => e.stopPropagation();
document.addEventListener("click", () => { $("showcase-pop").classList.add("hidden"); $("showcase-btn").setAttribute("aria-expanded", "false"); });
async function sendShowcase(body, msg) {
  try { applyControl(await api.setShowcase(body)); toast(msg); }
  catch (err) { if (err.status !== 401) toast("Failed: " + err.message); }
}
$("sc-save").onclick = () => sendShowcase(showcaseSettings(), "Showcase settings saved");
$("sc-toggle").onclick = () => {
  const running = !control.showcase?.running;
  if (running && !confirm(`Start the showcase?\nThe server will run ${showcaseSettings().max_concurrent} scenarios at a time by itself, spending Context.dev credits until paused or the daily budget is reached${$("sc-watched").checked ? " (only while someone has a page open)" : ""}.`)) return;
  sendShowcase({ running, ...showcaseSettings() }, running ? "Showcase started" : "Showcase stopped");
};

async function togglePause(paused) {
  try { applyControl(await api.setControl(paused)); toast(paused ? "Processing paused" : "Processing resumed"); }
  catch (err) { if (err.status !== 401) toast("Failed: " + err.message); }
}
$("pause-btn").onclick = () => togglePause(!control.paused);
$("resume-btn").onclick = () => togglePause(false);
const pageUrl = (u) => (api.MOCK ? u.replace(/^\/sites\//, "../fake_site/") : u);   // mock dev server has no /sites/

// ---------------------------------------------------------------- scenario helpers

const lastStep = (sc) => sc.steps.length - 1;
const statusOf = (sc) => (sc.current_step < 0 ? "not_started" : sc.current_step >= lastStep(sc) ? "finished" : "running");
const busy = (sc) => sc.pages.some((p) => { const r = state.pipeline[p.source_id]; return r && r.stage && !TERMINAL.has(r.stage); });
const selected = () => state.scenarios.find((s) => s.id === state.selectedId);

function statusChip(sc) {
  const st = statusOf(sc);
  if (st === "not_started") return `<span class="chip chip-tag">not started</span>`;
  if (st === "finished") return `<span class="chip chip-verified">${icon("check", "text-[12px]")}finished</span>`;
  return `<span class="chip chip-corroborated">${busy(sc) ? `<span class="dot pulse"></span>` : ""}step ${sc.current_step}/${lastStep(sc)}</span>`;
}
const scaleChip = (sc) => `<span class="chip" style="color:${SCALE_COLOR[sc.scale]};border-color:${SCALE_COLOR[sc.scale]}66">${esc(sc.scale)}</span>`;
const langTag = (sc) => `<span title="language">${LANG_FLAG[sc.language] || ""} ${esc(String(sc.language || "").toUpperCase())}</span>`;
const autoChip = (sc) => (sc.autoplay?.running ? `<span class="chip chip-unverified">${icon("play_circle", "text-[12px]")}auto ${esc(sc.autoplay.interval_s)}s</span>` : "");

function visibleScenarios() {
  const q = filters.q.trim().toLowerCase();
  return state.scenarios.filter((sc) =>
    (!filters.scale.size || filters.scale.has(sc.scale)) &&
    (!filters.status.size || filters.status.has(statusOf(sc))) &&
    (!filters.hazard || sc.hazard === filters.hazard) &&
    (!filters.language || sc.language === filters.language) &&
    (!q || `${sc.name} ${sc.region} ${sc.summary} ${sc.id}`.toLowerCase().includes(q)));
}

// ---------------------------------------------------------------- actions

async function run(fn, msg) {
  try {
    if (msg) toast(msg);
    const res = await fn();
    if (res?.scenarios) res.scenarios.forEach(upsertScenario);
    else if (res?.id) upsertScenario(res);
    renderAll();
  } catch (err) {
    if (err.status !== 401) toast("Failed: " + err.message);   // 401: fg-auth-rejected switches to read-only
  }
}

const START_CREDITS = 15;
function perScenario(action, sc, extra = {}) {
  if (action === "start") return run(() => api.stepScenario(sc.id, { to: 0 }), `Starting ${sc.name}…`);
  if (action === "next") return run(() => api.stepScenario(sc.id, { next: true }), `${sc.name}: next step…`);
  if (action === "jump") return run(() => api.stepScenario(sc.id, { to: extra.to }), `${sc.name}: step ${extra.to}…`);
  if (action === "reset") {
    if (!confirm(`Reset "${sc.name}"?\nThis deletes its incident (items, alerts), pages and snapshots and stops its monitors.`)) return;
    return run(() => api.resetScenario(sc.id), `Resetting ${sc.name}…`);
  }
  if (action === "autoplay") {
    const interval_s = Math.max(10, Number(extra.interval) || sc.autoplay?.interval_s || 60);
    const running = !sc.autoplay?.running;
    return run(() => api.autoplayScenario(sc.id, { running, interval_s }), running ? `${sc.name}: autoplay every ${interval_s}s` : `${sc.name}: autoplay off`);
  }
}

document.querySelectorAll("[data-bulk]").forEach((b) => {
  b.onclick = () => {
    const action = b.dataset.bulk;
    const list = visibleScenarios();
    if (!list.length) return toast("No scenarios match the filters");
    const ids = list.map((s) => s.id);
    const interval_s = Math.max(10, Number($("bulk-interval").value) || 60);
    const stagger_s = Math.max(0, Number($("bulk-stagger").value) || 0);
    if (action === "reset" && !confirm(`Reset ${ids.length} scenario(s)?\nThis deletes their incidents, pages and snapshots and stops their monitors.`)) return;
    if (action === "start") {
      const n = list.filter((s) => s.current_step < 0).length;
      if (!n) return toast("All filtered scenarios are already started");
      if (n > 4 && !confirm(`Start ${n} scenarios?\nEach start costs roughly ${START_CREDITS} Context.dev credits (≈${n * START_CREDITS} in total), spaced ${stagger_s}s apart.`)) return;
    }
    run(() => api.bulkScenarios({ action, ids, interval_s, stagger_s }), `${action.replace("_", " ")} → ${ids.length} scenario(s)`);
  };
});

// ---------------------------------------------------------------- filters

function renderFilters() {
  const count = (pred) => state.scenarios.filter(pred).length;
  $("f-scale").innerHTML = SCALES.map((s) => `
    <button type="button" class="fchip chip" aria-pressed="${filters.scale.has(s)}" data-scale="${s}"
      style="color:${SCALE_COLOR[s]};border-color:${SCALE_COLOR[s]}66;${filters.scale.has(s) ? `background:${SCALE_COLOR[s]}26` : ""}">
      ${esc(s)} <span class="tnum opacity-80">${count((x) => x.scale === s)}</span></button>`).join("");
  const ST = [["not_started", "not started"], ["running", "running"], ["finished", "finished"]];
  $("f-status").innerHTML = ST.map(([k, label]) => `
    <button type="button" class="fchip chip ${filters.status.has(k) ? "chip-corroborated" : "chip-tag"}" aria-pressed="${filters.status.has(k)}" data-status="${k}">
      ${esc(label)} <span class="tnum opacity-80">${count((x) => statusOf(x) === k)}</span></button>`).join("");
  const hazards = [...new Set(state.scenarios.map((s) => s.hazard))].sort();
  const langs = [...new Set(state.scenarios.map((s) => s.language))].sort();
  $("f-hazard").innerHTML = `<option value="">All hazards</option>` + hazards.map((h) => `<option value="${esc(h)}" ${filters.hazard === h ? "selected" : ""}>${esc(h.replace(/_/g, " "))}</option>`).join("");
  $("f-language").innerHTML = `<option value="">All languages</option>` + langs.map((l) => `<option value="${esc(l)}" ${filters.language === l ? "selected" : ""}>${LANG_FLAG[l] || ""} ${esc(l.toUpperCase())}</option>`).join("");
  document.querySelectorAll("[data-scale]").forEach((b) => { b.onclick = () => { toggle(filters.scale, b.dataset.scale); renderAll(); }; });
  document.querySelectorAll("[data-status]").forEach((b) => { b.onclick = () => { toggle(filters.status, b.dataset.status); renderAll(); }; });
}
const toggle = (set, v) => (set.has(v) ? set.delete(v) : set.add(v));
$("f-hazard").onchange = (e) => { filters.hazard = e.target.value; renderAll(); };
$("f-language").onchange = (e) => { filters.language = e.target.value; renderAll(); };
$("f-q").oninput = (e) => { filters.q = e.target.value; renderList(); renderMap(false); $("bulk-n").textContent = visibleScenarios().length; };
$("f-clear").onclick = () => {
  filters.scale.clear(); filters.status.clear(); filters.hazard = ""; filters.language = ""; filters.q = ""; $("f-q").value = "";
  renderAll();
};

// ---------------------------------------------------------------- list

function actionButtons(sc, compact = true) {
  const st = statusOf(sc);
  const cls = `${compact ? "btn !min-h-[30px] !px-2 !text-xs" : "btn"} admin-action`;
  return `
    ${st === "not_started" ? `<button type="button" class="${cls}" data-act="start" data-id="${esc(sc.id)}">${icon("play_arrow", "text-[16px]")}Start</button>`
      : `<button type="button" class="${cls}" data-act="next" data-id="${esc(sc.id)}" ${st === "finished" ? "disabled" : ""}>${icon("skip_next", "text-[16px]")}Next</button>`}
    <button type="button" class="${cls} ${sc.autoplay?.running ? "btn-on" : ""}" data-act="autoplay" data-id="${esc(sc.id)}" ${st === "finished" ? "disabled" : ""}>
      ${icon(sc.autoplay?.running ? "pause_circle" : "play_circle", "text-[16px]")}${sc.autoplay?.running ? "Auto on" : "Auto"}</button>
    <button type="button" class="${cls}" data-act="reset" data-id="${esc(sc.id)}" ${st === "not_started" ? "disabled" : ""}>${icon("restart_alt", "text-[16px]")}Reset</button>`;
}

function scenarioCard(sc) {
  const sel = sc.id === state.selectedId;
  return `
  <article class="card card-hover p-3 cursor-pointer ${sel ? "ring-1 ring-authority bg-authority/5" : ""}" style="border-left:3px solid ${SCALE_COLOR[sc.scale]}" data-select="${esc(sc.id)}" tabindex="0" aria-current="${sel}">
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0 flex items-start gap-2">
        ${icon(HAZARD_ICON[sc.hazard] || "warning", "text-[20px] mt-0.5")}
        <div class="min-w-0">
          <div class="font-semibold leading-snug">${esc(sc.name)}</div>
          <div class="mono text-[11px] text-faint truncate">${esc(sc.region)} · ${langTag(sc)}</div>
        </div>
      </div>
      <div class="flex flex-col items-end gap-1 shrink-0">${statusChip(sc)}${autoChip(sc)}</div>
    </div>
    <p class="mt-1.5 text-sm text-muted clamp-2">${esc(sc.summary)}</p>
    <div class="mt-2 flex flex-wrap gap-1.5">${actionButtons(sc)}</div>
  </article>`;
}

function renderList() {
  const vis = visibleScenarios();
  $("bulk-n").textContent = vis.length;
  $("sc-list").innerHTML = SCALES.map((scale) => {
    const group = vis.filter((s) => s.scale === scale);
    if (!group.length) return "";
    return `
    <div>
      <h3 class="label mb-2 flex items-center gap-2"><span class="w-2.5 h-2.5 rounded-full" style="background:${SCALE_COLOR[scale]}"></span>${esc(scale)} <span class="text-faint">${group.length}</span></h3>
      <div class="flex flex-col gap-2">${group.map(scenarioCard).join("")}</div>
    </div>`;
  }).join("") || `<div class="card p-4 text-sm text-muted">No scenarios match the filters.</div>`;
  wireActions($("sc-list"));
  $("sc-list").querySelectorAll("[data-select]").forEach((el) => {
    el.onclick = (e) => { if (!e.target.closest("button")) select(el.dataset.select, true); };
    el.onkeydown = (e) => { if (e.key === "Enter" && e.target === el) select(el.dataset.select, true); };
  });
}

function wireActions(root) {
  root.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      const sc = state.scenarios.find((s) => s.id === b.dataset.id);
      if (sc) perScenario(b.dataset.act, sc, { to: Number(b.dataset.to), interval: $("sel-interval")?.value });
    };
  });
}

function select(id, scroll = false) {
  state.selectedId = id;
  lsSet(SEL_KEY, id);
  history.replaceState(null, "", `${location.pathname}${location.search}#${encodeURIComponent(id)}`);
  renderList(); renderMap(false); renderSelected();
  loadSelectedDetail();
  if (scroll && window.innerWidth < 1024) $("sel").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- map

let map, layer;
function initMap() {
  if (typeof L === "undefined") return;
  map = L.map("sc-map", { zoomControl: true, attributionControl: true }).setView([50, 10], 4);
  basemap(map, { labels: false });
  layer = L.featureGroup().addTo(map);
}

function renderMap(fit) {
  if (!map) return;
  layer.clearLayers();
  const vis = new Set(visibleScenarios().map((s) => s.id));
  for (const sc of state.scenarios) {
    if (!sc.area || sc.area.lat == null) continue;
    const color = SCALE_COLOR[sc.scale] || "#94A3B8";
    const running = statusOf(sc) === "running";
    const sel = sc.id === state.selectedId;
    const m = L.circleMarker([sc.area.lat, sc.area.lon], {
      radius: sel ? 11 : 7, color: running ? "#F8FAFC" : color, weight: running ? 2.5 : 1, fillColor: color,
      fillOpacity: vis.has(sc.id) ? 0.9 : 0.2, opacity: vis.has(sc.id) ? 1 : 0.3, className: running && busy(sc) ? "fg-area-pulse" : "",
    });
    m.bindTooltip(`${esc(sc.name)}<br><span style="opacity:.7">${esc(sc.scale)} · ${esc(statusOf(sc).replace("_", " "))}</span>`, { className: "fg-tip", direction: "top" });
    m.on("click", () => select(sc.id, true));
    layer.addLayer(m);
  }
  if (fit && layer.getLayers().length) map.fitBounds(layer.getBounds().pad(0.15), { maxZoom: 6 });
}

// ---------------------------------------------------------------- selected scenario

function stepper(row) {
  const idx = row.stage === "failed" || row.stage === "skipped" ? STAGES.length : STAGES.indexOf(row.stage);
  const done = Object.keys(row.times || {}).filter((s) => STAGES.includes(s)).map((s) => STAGES.indexOf(s));
  const failedAt = row.stage === "failed" ? Math.max(0, ...done) : -1;
  return `
  <ol class="mt-2 grid grid-cols-6 gap-1" aria-label="Pipeline stages">
    ${STAGES.map((s, i) => {
      let cls = "bg-elevated border-line";
      if (row.stage === "skipped") cls = "bg-elevated border-line opacity-50";
      else if (failedAt === i) cls = "bg-critical/20 border-critical";
      else if (row.stage === "done" || i < idx || failedAt > i) cls = "bg-ok/15 border-ok/60";
      else if (i === idx) cls = "bg-authority/15 border-authority pulse";
      const t = row.times?.[s];
      return `<li class="min-w-0" title="${esc(STAGE_LABEL[s])}${t != null ? ` · ${fmtSecs(t)}` : ""}">
        <div class="h-1.5 rounded-full border ${cls}"></div>
        <div class="mt-1 mono text-[9.5px] leading-tight truncate hidden sm:block ${i === idx ? "text-ink" : "text-faint"}">${esc(s)}</div></li>`;
    }).join("")}
  </ol>`;
}
const fmtSecs = (ms) => `${(ms / 1000).toFixed(1)} s`;
const currentMs = (row) => (TERMINAL.has(row.stage) || !row.stage ? row.elapsed_ms || 0 : (row.elapsed_ms || 0) + (performance.now() - row.receivedAt));

function pipelineRows(sc) {
  return sc.pages.map((p) => {
    const row = state.pipeline[p.source_id] || {};
    const running = row.stage && !TERMINAL.has(row.stage);
    const chip = !row.stage ? `<span class="chip chip-tag">idle</span>`
      : row.stage === "done" ? `<span class="chip chip-verified">${icon("check", "text-[12px]")}done</span>`
      : row.stage === "failed" ? `<span class="chip chip-contradicted">failed</span>`
      : row.stage === "skipped" ? `<span class="chip chip-tag">skipped</span>`
      : `<span class="chip chip-corroborated"><span class="dot pulse"></span>${esc(STAGE_LABEL[row.stage] || row.stage)}</span>`;
    return `
    <div class="rounded-lg border ${running ? "border-authority/60" : "border-line"} bg-canvas/40 p-3">
      <div class="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
        <div class="min-w-0 flex-1 basis-[11rem]">
          <div class="font-semibold truncate">${esc(p.name)} <span class="chip chip-tag !text-[10px] !py-0 align-middle">${esc(p.kind)}</span></div>
          <div class="mono text-[10.5px] text-faint truncate">${esc(p.source_id)}</div>
        </div>
        <div class="flex items-center gap-2 shrink-0">${chip}
          <span class="mono tnum text-lg font-bold ${running ? "text-authority" : row.stage === "done" ? "text-ok" : "text-muted"}" data-sw="${esc(p.source_id)}">${row.stage ? fmtSecs(currentMs(row)) : "–"}</span></div>
      </div>
      ${stepper(row)}
      <div class="mt-1.5 mono text-[11px] ${row.stage === "failed" ? "text-critical" : "text-muted"} min-h-[1.2em]">${row.stage ? esc(row.detail || STAGE_LABEL[row.stage]) : "waiting for a change…"}</div>
    </div>`;
  }).join("");
}

function renderSelected() {
  const sc = selected();
  const el = $("sel-top");
  if (!sc) {
    el.innerHTML = `<div class="card p-4 text-sm text-muted">Select a scenario on the map or in the list.</div>`;
    $("sel-previews").innerHTML = ""; $("sel-feeds").innerHTML = ""; previewKey = "";
    return;
  }
  const cur = sc.current_step;
  const started = cur >= 0;
  const alerts = state.alerts.filter((a) => a.incident_id === sc.incident_id).slice(0, 5);
  const checks = state.checks.filter((c) => c.incident_id === sc.incident_id).slice(0, 6);
  el.innerHTML = `
  <div class="card p-4" style="border-left:3px solid ${SCALE_COLOR[sc.scale]}">
    <div class="flex flex-wrap items-start justify-between gap-2">
      <div class="min-w-0 flex items-start gap-2.5">
        ${icon(HAZARD_ICON[sc.hazard] || "warning", "text-[26px] mt-0.5")}
        <div class="min-w-0">
          <h1 class="text-xl font-extrabold tracking-tight leading-tight">${esc(sc.name)}</h1>
          <div class="mono text-[11px] text-faint">${esc(sc.region)} · ${langTag(sc)} · ${esc(sc.hazard.replace(/_/g, " "))} · ${esc(sc.area?.label || "")}</div>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-1.5">${scaleChip(sc)}${statusChip(sc)}${autoChip(sc)}</div>
    </div>
    <p class="mt-2 text-sm text-muted">${esc(sc.summary)}</p>
    <div class="mt-3 flex flex-wrap items-center gap-2">
      ${actionButtons(sc, false)}
      <label class="flex items-center gap-1 text-xs text-muted admin-action">every
        <input id="sel-interval" type="number" min="10" max="900" step="5" value="${esc(sc.autoplay?.interval_s || 60)}" class="w-16 rounded border border-line bg-canvas px-2 py-1.5 text-sm text-ink mono focus:outline-none focus:border-authority" aria-label="Autoplay interval in seconds">s</label>
      <span class="paused-hint admin-action ${control.paused ? "" : "hidden"} text-xs text-triage flex items-center gap-1">${icon("schedule", "text-[14px]")}paused: will be processed on resume</span>
      <span class="flex-1"></span>
      ${started ? `<a href="${api.link(`incident.html?id=${encodeURIComponent(sc.incident_id)}`)}" target="_blank" rel="noopener" class="btn">${icon("open_in_new", "text-[16px]")}Open on board</a>` : ""}
    </div>
  </div>

  <div class="card">
    <div class="px-4 py-3 border-b border-line flex items-center justify-between gap-2">
      <h2 class="font-bold flex items-center gap-2">${icon("route", "text-authority")}Live pipeline</h2>
      <span class="label text-faint hidden sm:inline">change → Context.dev → claims → grouping → board</span>
    </div>
    <div class="p-3 flex flex-col gap-2.5" id="sel-pipeline">${pipelineRows(sc)}</div>
  </div>

  <div class="card">
    <div class="px-4 py-3 border-b border-line flex items-center justify-between gap-2">
      <h2 class="font-bold flex items-center gap-2">${icon("format_list_numbered", "text-authority")}Steps</h2>
      <span class="label text-muted">${started ? `step ${cur} of ${lastStep(sc)}` : "not started"}</span>
    </div>
    <ol class="p-2 flex flex-col gap-1.5">
      ${sc.steps.map((st) => {
        const isCur = st.n === cur, done = st.n < cur;
        return `
        <li class="rounded-lg border ${isCur ? "border-authority bg-authority/10" : "border-line"} ${done ? "opacity-70" : ""} p-3">
          <div class="flex items-start gap-3">
            <span class="w-7 h-7 shrink-0 rounded-full grid place-items-center mono text-xs font-bold ${isCur ? "bg-authority text-canvas" : done ? "bg-ok/20 text-ok" : "bg-elevated text-muted"}">${done ? icon("check", "text-[16px]") : esc(st.n)}</span>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span class="mono text-xs text-faint">${esc(st.clock)}</span>
                <span class="font-semibold leading-snug">${esc(st.title)}</span>
                ${isCur ? `<span class="chip chip-corroborated">current</span>` : ""}
              </div>
              <p class="mt-0.5 text-sm text-muted">${esc(st.description)}</p>
              ${st.expect ? `<p class="mt-1 text-xs text-faint italic">${icon("lightbulb", "text-[13px]")} Expect: ${esc(st.expect)}</p>` : ""}
              <div class="mt-1.5 flex flex-wrap gap-1">${(st.changes || []).map((c) => `<span class="chip chip-tag">${icon("edit_document", "text-[12px]")}${esc(sc.pages.find((p) => p.id === c)?.name || c)}</span>`).join("")}</div>
            </div>
            ${isCur ? "" : `<button type="button" class="btn !min-h-[30px] !px-2 !text-xs shrink-0 admin-action" data-act="jump" data-to="${esc(st.n)}" data-id="${esc(sc.id)}">Jump to</button>`}
          </div>
        </li>`;
      }).join("")}
    </ol>
  </div>

`;
  wireActions(el);
  // previews live in their own stable container so their iframes don't reload on every re-render
  renderPreviews(sc);
  $("sel-feeds").innerHTML = `
  <div class="grid gap-4 xl:grid-cols-2">
    <div class="card">
      <div class="px-4 py-3 border-b border-line"><h2 class="label flex items-center gap-2">${icon("notifications_active", "text-critical text-[18px]")}Newest alerts</h2></div>
      <div class="p-3 flex flex-col gap-2">${alerts.map(alertRow).join("") || `<div class="text-sm text-muted">No alerts for this scenario yet.</div>`}</div>
    </div>
    <div class="card">
      <div class="px-4 py-3 border-b border-line"><h2 class="label flex items-center gap-2">${icon("travel_explore", "text-authority text-[18px]")}Context.dev verdicts</h2></div>
      <div class="p-3 flex flex-col gap-2">${checks.map(checkRow).join("") || `<div class="text-sm text-muted">No Context.dev monitor runs yet.</div>`}</div>
    </div>
  </div>`;
}

let previewKey = "";
function renderPreviews(sc) {
  const host = $("sel-previews");
  if (!host) return;
  const cur = sc.current_step;
  const started = cur >= 0;
  const v = state.previewVersion[sc.id] || 0;
  const key = `${sc.id}|${cur}|${v}`;
  if (key === previewKey) return;                                    // keep iframes alive across re-renders
  previewKey = key;
  host.dataset.key = key;
  host.innerHTML = `
  <div class="card">
    <div class="px-4 py-3 border-b border-line flex items-center justify-between gap-2">
      <h2 class="font-bold flex items-center gap-2">${icon("preview", "text-authority")}Fictional source pages</h2>
      <span class="label text-faint hidden sm:inline">refresh when a step is applied</span>
    </div>
    <div class="p-3 grid gap-3 sm:grid-cols-3">
      ${started ? sc.pages.map((p) => {
        const url = pageUrl(p.url);
        return `
        <figure class="rounded-lg border border-line overflow-hidden bg-canvas">
          <figcaption class="px-2.5 py-1.5 flex items-center justify-between gap-2 border-b border-line">
            <span class="text-xs font-semibold truncate">${esc(p.name)}</span>
            <a href="${esc(url)}" target="_blank" rel="noopener" class="label text-authority flex items-center gap-0.5 shrink-0">Open ${icon("open_in_new", "text-[13px]")}</a>
          </figcaption>
          <div class="relative h-48 overflow-hidden bg-white">
            <iframe src="${esc(url + (url.includes("?") ? "&" : "?") + "v=" + v)}" title="Preview: ${esc(p.name)}" loading="lazy" sandbox="allow-same-origin" tabindex="-1"
              style="width:300%;height:300%;transform:scale(.3333);transform-origin:0 0;border:0;pointer-events:none"></iframe>
            <a href="${esc(url)}" target="_blank" rel="noopener" class="absolute inset-0" aria-label="Open ${esc(p.name)} in a new tab"></a>
          </div>
        </figure>`;
      }).join("") : `<div class="sm:col-span-3 text-sm text-muted">Pages are published when the scenario starts.</div>`}
    </div>
  </div>

`;
}

const ALERT_CHIP = { potential: "chip-unverified", critical: "chip-contradicted", debunk: "chip-contradicted", info: "chip-corroborated" };
function alertRow(a) {
  const href = a.incident_id ? api.link(`incident.html?id=${encodeURIComponent(a.incident_id)}`) + (a.item_id ? `#${encodeURIComponent(a.item_id)}` : "") : null;
  return `
  <${href ? `a href="${href}" target="_blank" rel="noopener"` : "div"} class="block rounded border border-line bg-canvas/40 px-2.5 py-2 hover:border-line2">
    <div class="flex items-center justify-between gap-2"><span class="chip ${ALERT_CHIP[a.level] || "chip-tag"}">${esc(a.level)}</span>${ago(a.created_at, "mono text-[11px] text-faint")}</div>
    <div class="mt-1 text-sm font-semibold leading-snug">${esc(a.title)}</div>
    ${a.body ? `<div class="text-xs text-muted">${esc(a.body)}</div>` : ""}
  </${href ? "a" : "div"}>`;
}
const CTX_CHIP = { changed: "chip-corroborated", no_change: "chip-tag", baseline: "chip-tag", failed: "chip-contradicted", skipped: "chip-tag" };
function checkRow(c) {
  return `
  <div class="rounded border ${c.disagreement ? "border-triage/50" : "border-line"} bg-canvas/40 px-2.5 py-2">
    <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
      ${c.disagreement ? `<span class="chip chip-unverified">review</span>` : `<span class="chip ${CTX_CHIP[c.status] || "chip-tag"}">${esc(String(c.status).replace("_", " "))}</span>`}
      <span class="text-xs text-muted truncate">${esc(c.source_name || c.source_id)}</span>
      <span class="ml-auto mono text-[10.5px] text-faint">${clock(c.at)}</span>
    </div>
    ${c.title ? `<div class="mt-1 text-sm font-semibold leading-snug">${esc(c.title)}</div>` : ""}
    <div class="mono text-[10.5px] text-faint">${c.importance ? `${esc(c.importance)} importance · ` : ""}${c.confidence != null ? `${Math.round(c.confidence * 100)}% conf · ` : ""}${c.latency_ms != null ? fmtSecs(c.latency_ms) : ""}</div>
    ${c.disagreement ? `<div class="mt-0.5 text-xs text-triage">Context.dev saw no meaningful change, but ${esc(c.pipeline_new_claims ?? "some")} new claims were extracted. Review.</div>` : ""}
  </div>`;
}

// Context.dev verdicts for the selected scenario come with its incident detail (if it has started).
async function loadSelectedDetail() {
  const sc = selected();
  if (!sc || sc.current_step < 0 || api.MOCK) return;
  try {
    const d = await api.getIncident(sc.incident_id);
    for (const c of d.context_checks || []) if (!state.checks.some((x) => x.at === c.at && x.source_id === c.source_id)) state.checks.push(c);
    state.checks.sort((a, b) => (b.at || "").localeCompare(a.at || ""));
    renderSelected();
  } catch { /* incident may not exist yet */ }
}

// ---------------------------------------------------------------- state updates

function seedPipeline(sc) {
  for (const [sid, ev] of Object.entries(sc.pipeline || {})) {
    if (!ev || state.pipeline[sid]?.receivedAt) continue;          // live events win over the snapshot
    state.pipeline[sid] = { stage: ev.stage, elapsed_ms: ev.elapsed_ms || 0, receivedAt: performance.now(), detail: ev.detail || "", times: { [ev.stage]: ev.elapsed_ms || 0 } };
  }
}

function upsertScenario(sc) {
  const i = state.scenarios.findIndex((s) => s.id === sc.id);
  const prev = i >= 0 ? state.scenarios[i] : null;
  if (i >= 0) state.scenarios[i] = sc; else state.scenarios.push(sc);
  if (!prev || prev.current_step !== sc.current_step) state.previewVersion[sc.id] = Date.now();
  if (sc.current_step < 0) for (const p of sc.pages) delete state.pipeline[p.source_id];   // reset
  seedPipeline(sc);
}

function onPipeline(ev) {
  const row = state.pipeline[ev.source_id] || (state.pipeline[ev.source_id] = { times: {} });
  if (ev.stage === "detected") row.times = {};
  Object.assign(row, { stage: ev.stage, elapsed_ms: ev.elapsed_ms || 0, receivedAt: performance.now(), detail: ev.detail || "" });
  row.times[ev.stage] = ev.elapsed_ms || 0;
  const sc = selected();
  if (sc && sc.pages.some((p) => p.source_id === ev.source_id)) $("sel-pipeline") && ($("sel-pipeline").innerHTML = pipelineRows(sc));
  if (ev.stage === "detected" || TERMINAL.has(ev.stage)) { renderList(); renderMap(false); }   // busy markers
}

setInterval(() => {
  for (const [sid, row] of Object.entries(state.pipeline)) {
    if (!row.stage || TERMINAL.has(row.stage)) continue;
    const el = document.querySelector(`[data-sw="${CSS.escape(sid)}"]`);
    if (el) el.textContent = fmtSecs(currentMs(row));
  }
}, 100);

function renderCredits() {
  $("credits-top").textContent = state.credits;
  $("credits").textContent = `${state.credits} Context.dev credits`;
}

function renderAll() {
  if (EMBED) { renderEmbed(); renderCredits(); return; }
  renderFilters();
  renderList();
  renderMap(false);
  renderSelected();
  renderCredits();
}

// ---------------------------------------------------------------- embed=1: compact "behind the scenes" (demo page)
let embedQueued = false;
function renderEmbed() {
  if (!EMBED || embedQueued) return;
  embedQueued = true;
  requestAnimationFrame(() => {
    embedQueued = false;
    const running = state.scenarios.filter((sc) => statusOf(sc) === "running" || busy(sc));
    const checks = state.checks.slice(0, 5);
    $("embed").innerHTML = `
    <div class="p-3 flex flex-col gap-3">
      <div class="card p-3">
        <div class="flex items-center justify-between gap-2">
          <h2 class="font-bold flex items-center gap-2">${icon("route", "text-authority")}Behind the scenes</h2>
          <span class="mono text-[11px] text-authority">${state.credits} credits</span>
        </div>
        <div class="mt-1.5 flex flex-col gap-0.5 mono text-[11.5px] text-muted">
          ${control.showcase ? `<span class="flex items-center gap-1.5">${control.showcase.running ? `<span class="live-dot on"></span>` : icon("slideshow", "text-[14px]")}${esc(showcaseText())}</span>` : ""}
          ${control.paused ? `<span class="text-triage flex items-center gap-1.5">${icon("pause_circle", "text-[14px]")}Paused${control.reason ? `: ${esc(control.reason)}` : ""}</span>` : ""}
          ${budgetText() ? `<span>${esc(budgetText())}</span>` : ""}
        </div>
      </div>
      <div class="card">
        <div class="px-3 py-2 border-b border-line label">Running now · ${running.length}</div>
        <div class="p-2 flex flex-col gap-3">
          ${running.map((sc) => `
          <div>
            <div class="flex items-center justify-between gap-2 px-1 mb-1.5">
              <span class="text-sm font-semibold truncate">${icon(HAZARD_ICON[sc.hazard] || "warning", "text-[16px]")} ${esc(sc.name)}</span>${statusChip(sc)}
            </div>
            ${sc.pages.some((pg) => state.pipeline[pg.source_id]?.stage)
              ? `<div class="flex flex-col gap-1.5">${pipelineRows(sc)}</div>`
              : `<div class="px-1 mono text-[11px] text-faint">waiting for the next step · ${esc(sc.steps[sc.current_step + 1]?.title || "")}</div>`}
          </div>`).join("") || `<div class="p-2 text-sm text-muted">Nothing running at the moment.</div>`}
        </div>
      </div>
      <div class="card">
        <div class="px-3 py-2 border-b border-line label flex items-center gap-1.5">${icon("travel_explore", "text-authority text-[15px]")}Context.dev verdicts</div>
        <div class="p-2 flex flex-col gap-1.5">${checks.map(checkRow).join("") || `<div class="p-1 text-sm text-muted">No Context.dev monitor runs yet.</div>`}</div>
      </div>
      <div class="card">
        <div class="px-3 py-2 border-b border-line label">API activity</div>
        <div class="p-2 flex flex-col gap-1 max-h-[22rem] overflow-y-auto">${activityRows(state.activity, 25)}</div>
      </div>
    </div>`;
  });
}

// ---------------------------------------------------------------- usage per hour (v0.10, Tiger Data continuous aggregate)

async function loadUsage() {
  let d;
  try { d = await api.getUsage(24); } catch { d = null; }
  const card = $("usage-card");
  if (!d || !d.available || !d.rows?.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const hours = [...new Set(d.rows.map((r) => r.bucket))].sort();
  const services = {};
  for (const r of d.rows) {
    if (!USAGE_SERVICES.has(r.service)) continue;            // scenario / control / monitor rows are not API calls
    const s = services[r.service] ||= { calls: 0, credits: 0, failures: 0, latSum: 0, perHour: {} };
    s.calls += r.calls; s.credits += r.credits || 0; s.failures += r.failures; s.latSum += (r.avg_latency_ms || 0) * r.calls;
    s.perHour[r.bucket] = r.calls;
  }
  const order = Object.entries(services).sort((a, b) => b[1].calls - a[1].calls);
  const bars = (s) => { const max = Math.max(1, ...Object.values(s.perHour)); return `<span class="inline-flex items-end gap-px h-5" aria-hidden="true">${hours.map((h) =>
    `<span class="w-1.5 rounded-sm bg-authority" style="height:${Math.max(1, Math.round(((s.perHour[h] || 0) / max) * 20))}px;opacity:${s.perHour[h] ? 0.85 : 0.15}"></span>`).join("")}</span>`; };
  $("usage").innerHTML = `
    <table class="w-full text-xs">
      <thead><tr class="label text-faint text-left"><th class="font-normal pb-1">Service</th><th class="font-normal pb-1 hidden sm:table-cell">Calls per hour</th>
        <th class="font-normal pb-1 text-right">Calls</th><th class="font-normal pb-1 text-right">Credits</th><th class="font-normal pb-1 text-right">Avg</th></tr></thead>
      <tbody>${order.map(([name, s]) => `
        <tr class="border-t border-line">
          <td class="py-1.5"><span class="chip ${SERVICE_CHIP_ADMIN[name] || "chip-tag"}">${esc(name)}</span>${s.failures ? ` <span class="text-critical tnum">${s.failures} failed</span>` : ""}</td>
          <td class="py-1.5 hidden sm:table-cell">${bars(s)}</td>
          <td class="py-1.5 text-right tnum">${s.calls}</td>
          <td class="py-1.5 text-right tnum">${s.credits ? Math.round(s.credits) : "–"}</td>
          <td class="py-1.5 text-right tnum text-muted">${s.calls ? (s.latSum / s.calls / 1000).toFixed(1) + " s" : "–"}</td>
        </tr>`).join("")}</tbody>
    </table>
    <p class="mt-2 text-[11px] text-faint">Every API call is stored in Tiger Data; this table reads an hourly continuous aggregate.</p>`;
}
const USAGE_SERVICES = new Set(["context.dev", "gemini", "anthropic", "elevenlabs", "pipeline"]);
const SERVICE_CHIP_ADMIN = { "context.dev": "chip-corroborated", gemini: "chip-verified", anthropic: "chip-verified" };

// ---------------------------------------------------------------- load + live

async function load() {
  const [sc, act, al] = await Promise.allSettled([api.getScenarios(), api.getActivity(60), api.getAlerts()]);
  api.getControl().then((c) => { applyControl(c); recheckSavedToken(); }).catch(() => {});   // older backends: no pause endpoint
  if (sc.status === "fulfilled") {
    sc.value.scenarios.forEach(upsertScenario);
    state.credits = sc.value.credits_spent ?? 0;
  } else {
    $("sc-list").innerHTML = `<div class="card p-4 text-sm text-critical">Scenario routes are not available (${esc(sc.reason?.message)}). Is the backend running the v0.6 build?</div>`;
  }
  if (act.status === "fulfilled") { state.activity = act.value.activity || []; if (sc.status !== "fulfilled") state.credits = act.value.credits_spent || 0; }
  if (al.status === "fulfilled") state.alerts = al.value.alerts || [];
  const fromHash = decodeURIComponent(location.hash.slice(1));
  const want = [fromHash, lsGet(SEL_KEY, null), "halvik"].find((id) => id && state.scenarios.some((s) => s.id === id));
  state.selectedId = want || state.scenarios[0]?.id || null;
  $("activity").innerHTML = activityRows(state.activity, 40);
  renderAll();
  renderMap(true);
  loadSelectedDetail();
  if (!EMBED) loadUsage();
}
if (!EMBED) setInterval(loadUsage, 60000);

function live() {
  api.subscribe({
    scenario(sc) { upsertScenario(sc); renderAll(); },
    pipeline(ev) { onPipeline(ev); liveFx.onPipeline(ev); if (EMBED && (ev.stage === "detected" || TERMINAL.has(ev.stage) || ev.stage === "extracted")) renderEmbed(); },
    item: (it) => liveFx.onItem(it),
    control: applyControl,
    activity(row) {
      state.activity.unshift(row);
      state.activity = state.activity.slice(0, 200);
      if (row.credits) { state.credits += row.credits; renderCredits(); }
      $("activity").innerHTML = activityRows(state.activity, 40);
      renderEmbed();
    },
    alert(a) {
      const i = state.alerts.findIndex((x) => x.id === a.id);
      if (i >= 0) state.alerts[i] = a; else state.alerts.unshift(a);
      if (a.incident_id === selected()?.incident_id) renderSelected();
    },
    context_check(c) {
      liveFx.onContextCheck(c);
      state.checks.unshift(c);
      state.checks = state.checks.slice(0, 100);
      if (c.incident_id === selected()?.incident_id) renderSelected();
      renderEmbed();
    },
  }, setLive);
}

if (EMBED) document.body.classList.add("embed");
else initMap();
initTheme();
applyAuth();
startAgoTicker();
load().finally(live);
onResume(() => load());
