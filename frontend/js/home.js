// Home page: alert banner, map of affected areas, disasters ranked by severity, system panel.
if (window.self !== window.top) document.body.classList.add("framed");   // shown inside demo.html
import * as api from "./api.js";
import { createLive, animDelay, setPausedChip } from "./live.js";
import { basemap, initTheme, SIMPLE, simpleChip, simpleCounts, esc, ago, icon, sevChip, sevColor, countChips, statusChip, lsGet, lsSet, setLive, startAgoTicker, KIND_ICON, toast, isWarning, untilText, validText, isEnded, warningColor, activityRows, updatedChecked, onResume } from "./ui.js";

const state = { incidents: [], monitors: [], activity: [], creditsSpent: 0 };

// ---------------------------------------------------------------- live update feedback
// Toasts only for incidents in focus: passing the severity filter and (with "Follow map" on) inside the map view.
const liveFx = createLive({
  getIncident: (id) => state.incidents.find((i) => i.id === id),
  inFocus: (id) => {
    const inc = state.incidents.find((i) => i.id === id);
    if (!inc) return !focus.follow;                      // unknown yet: in focus only when showing everything
    if (!sevOn.has(Number(inc.severity))) return false;
    return !focus.follow || inMapView(inc);
  },
  onUpdate: showLatest,                                  // one brief line at the top instead of pop-ups
});
const fx = new Map();                     // incident_id → {at, plus: {status: +n}} for card pulse / "+1" / hot rings
const fxAge = (id) => (fx.has(id) ? Date.now() - fx.get(id).at : Infinity);
const lastItemChange = new Map();         // incident_id → when an item of it last changed status / appeared

// Three words: confirmed / unconfirmed / false (the rules' five statuses folded), with "+n" right after a change.
function countChipsFx(inc) {
  const age = fxAge(inc.id);
  const c = simpleCounts(inc.counts || {});
  const plus = simpleCounts(age < 4000 ? fx.get(inc.id).plus : {});
  const badge = (k) => (plus[k] > 0 ? `<span class="plus-badge" style="${animDelay(age)}">+${plus[k]}</span>` : "");
  return Object.keys(SIMPLE).filter((k) => c[k]).map((k) => `<span class="inline-flex items-center">${simpleChip(k, c[k])}${badge(k)}</span>`).join(" ");
}

// ---------------------------------------------------------------- severity filter (map + list)
const SEV_KEY = "fg-sev-filter";
const sevOn = new Set(lsGet(SEV_KEY, [4, 3, 2]).map(Number));         // default: L1 (yellow warnings) hidden
const visible = () => state.incidents.filter((i) => sevOn.has(Number(i.severity)));

function renderSevFilter() {
  const el = document.getElementById("sev-filter");
  if (!el) return;
  const count = (lvl) => state.incidents.filter((i) => Number(i.severity) === lvl).length;
  const hidden = state.incidents.length - visible().length;
  el.innerHTML = `
    <span class="label mr-1">Severity</span>
    ${[4, 3, 2, 1].map((lvl) => `
      <button type="button" class="fchip chip ${sevOn.has(lvl) ? `chip-sev${lvl}` : "chip-tag"}" aria-pressed="${sevOn.has(lvl)}" data-sev="${lvl}">
        L${lvl} <span class="tnum opacity-80">${count(lvl)}</span></button>`).join("")}
    ${hidden ? `<span class="label text-faint ml-1">${hidden} hidden by filter</span>` : ""}`;
  el.querySelectorAll("[data-sev]").forEach((b) => {
    b.onclick = () => {
      const lvl = Number(b.dataset.sev);
      sevOn.has(lvl) ? sevOn.delete(lvl) : sevOn.add(lvl);
      lsSet(SEV_KEY, [...sevOn]);
      renderIncidents();
      renderMap(false);
    };
  });
}

// ---------------------------------------------------------------- incidents

function sortIncidents() {
  state.incidents.sort((a, b) => (b.severity - a.severity) || (b.updated_at || "").localeCompare(a.updated_at || ""));
}

const cap = (t) => (t ? t[0].toUpperCase() + t.slice(1) : t);
const SHORT = { "Finnish Meteorological Institute": "FMI", "MeteoAlarm (Europe's national weather services)": "MeteoAlarm", "US Geological Survey": "USGS", "UK Environment Agency": "Environment Agency" };
const officialShort = (inc) => { const n = officialOf(inc); return SHORT[n] || n.replace(/ \(.*\)$/, ""); };
const officialOf = (inc) => (inc.sources || []).filter((x) => x.kind === "official").map((x) => x.name).join(", ") || inc.source_label || "";

function incidentCard(inc) {
  const href = api.link(`incident.html?id=${encodeURIComponent(inc.id)}`);
  const highlights = (inc.highlights || []).slice(0, 3).map((h) => `
    <li class="flex items-start gap-2 text-[15px]">
      ${h.status === "contradicted" ? '<span class="mt-[9px] w-1.5 h-1.5 rounded-full shrink-0 bg-critical"></span>' : h.status === "verified" ? '<span class="mt-[9px] w-1.5 h-1.5 rounded-full shrink-0 bg-ok"></span>' : '<span class="mt-[9px] w-1.5 h-1.5 rounded-full shrink-0 bg-triage"></span>'}
      <span class="${h.status === "contradicted" ? "struck" : ""}">${esc(h.title)}</span>
    </li>`).join("");
  const age = fxAge(inc.id);
  const glow = age < 3200 ? `glow ${inc.counts?.contradicted ? "glow-false" : "glow-ok"}` : "";
  return `
  <a href="${href}" class="card card-hover accent-sev${esc(inc.severity)} block p-4 ${glow}" style="${glow ? animDelay(age) : ""}" data-incident="${esc(inc.id)}">
    <div class="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
      <div class="flex items-center gap-2 min-w-0">${sevChip(inc.severity, inc.severity_label)}
        <span class="label text-faint truncate">${esc(cap((inc.hazards || []).join(", ").replace(/_/g, " ")))}</span></div>
      <span class="label text-faint shrink-0 text-right">${age < 60000 ? `<span class="updated-chip">Updated just now</span>` : `Updated ${ago(inc.updated_at)}`}</span>
    </div>
  </a>`;
}

// Full card: anything severe, and every verified board. Compact row: lower-severity official warnings.
const isFullCard = (inc) => !isWarning(inc) || inc.severity >= 3;
const WARN_OPEN_KEY = "fg-warnings-open";

function warningRow(inc) {
  const href = api.link(`incident.html?id=${encodeURIComponent(inc.id)}`);
  return `
  <a href="${href}" class="flex items-center gap-2.5 px-3 py-2.5 border-t border-line first:border-t-0 hover:bg-elevated min-h-[44px]" data-incident="${esc(inc.id)}">
    <span class="w-2.5 h-2.5 rounded-full shrink-0" style="background:${warningColor(inc)}"></span>
    <span class="min-w-0 flex-1">
      <span class="block text-sm font-medium truncate">${esc(inc.name)}</span>
      <span class="block mono text-xs text-faint truncate">${esc(inc.area?.label || "")}</span>
    </span>
    <span class="mono text-xs text-muted shrink-0 text-right" title="${esc(inc.valid ? `${inc.valid.from} → ${inc.valid.to}` : "")}">${esc(validText(inc.valid))}</span>
  </a>`;
}

function renderIncidents() {
  sortIncidents();
  renderSevFilter();
  const all = visible();
  const shown = shownList();
  const elsewhere = all.length - shown.length;
  renderListNote(shown.length, elsewhere, all.length, [...shown].sort((a, b) => b.severity - a.severity)[0]);
  const full = shown.filter(isFullCard);
  const compact = shown.filter((i) => !isFullCard(i))
    .sort((a, b) => (b.severity - a.severity) || (a.valid?.to || "").localeCompare(b.valid?.to || ""));
  const open = lsGet(WARN_OPEN_KEY, false);
  const el = document.getElementById("incidents");
  const hiddenN = state.incidents.length - all.length;
  const emptyMsg = focus.follow && elsewhere && !shown.length
    ? `No incidents here. <button type="button" class="text-authority underline" data-see-all>See all</button>`
    : hiddenN ? `No incidents at the selected severity levels (${hiddenN} hidden by filter).` : "No active disasters. Monitors are watching.";
  el.innerHTML = (full.length ? full.map(incidentCard).join("")
      : (compact.length ? "" : `<div class="card p-4 text-muted text-sm">${emptyMsg}</div>`))
    + (compact.length ? `
    <details id="warn-group" class="card" ${open ? "open" : ""}>
      <summary class="px-3 py-3 flex items-center justify-between gap-2 min-h-[44px]">
        <span class="label flex items-center gap-2">Lower-level official warnings
          <span class="chip chip-tag tnum">${compact.length}</span></span>
        <span class="flex items-center gap-2 min-w-0"><span class="label text-faint hidden sm:inline truncate">${esc([...new Set(compact.map(officialShort))].join(", "))}</span>${icon("expand_more", "chev text-muted")}</span>
      </summary>
      <div class="border-t border-line">${compact.map(warningRow).join("")}</div>
    </details>` : "");
  document.getElementById("warn-group")?.addEventListener("toggle", (e) => lsSet(WARN_OPEN_KEY, e.target.open));
  el.querySelector("[data-see-all]")?.addEventListener("click", () => renderMap(true));

}

function renderListNote(n, elsewhere, total, top) {
  const el = document.getElementById("list-note");
  if (!el) return;
  const highest = top ? ` · highest ${sevChip(top.severity, top.severity_label)}` : "";
  el.innerHTML = focus.follow
    ? `<span>Showing <b class="text-ink tnum">${n}</b> in this area${highest}${elsewhere ? ` · <span class="tnum">${elsewhere}</span> more elsewhere · <button type="button" class="text-authority underline" data-follow="off">show all</button>` : ""}</span>
       <button type="button" class="fchip chip chip-corroborated" aria-pressed="true" data-follow="off">Follow map</button>`
    : `<span>Showing all <b class="text-ink tnum">${total}</b>${highest}</span>
       <button type="button" class="fchip chip chip-tag" aria-pressed="false" data-follow="on">Follow map</button>`;
  el.querySelectorAll("[data-follow]").forEach((b) => {
    b.onclick = () => { focus.follow = b.dataset.follow === "on"; lsSet(FOLLOW_KEY, focus.follow); renderIncidents(); };
  });
}

// ---------------------------------------------------------------- map

let map, warnLayer, ringLayer;
let programmaticMove = false;

function initMap() {
  if (typeof L === "undefined") {
    document.getElementById("map").innerHTML = `<div class="p-4 text-sm text-muted">Map library failed to load.</div>`;
    return;
  }
  // The map must never trap the page: the mouse wheel scrolls the page (zoom with + / − or a double click), and on
  // touch screens one finger scrolls the page while two fingers pinch-zoom the map. Tapping an area opens it.
  const touch = L.Browser.mobile || matchMedia("(pointer: coarse)").matches;
  map = L.map("map", { zoomControl: true, attributionControl: true, worldCopyJump: true, scrollWheelZoom: false, dragging: !touch });
  document.getElementById("map-note").textContent = touch ? "pinch to zoom · tap an area to open it" : "drag to move · tap an area to open it";
  basemap(map);                                           // Esri gray canvas in the page's theme (dark or light)
  map.createPane("warnings");                             // official warning polygons sit under the rings
  map.getPane("warnings").style.zIndex = 390;
  warnLayer = L.featureGroup().addTo(map);
  ringLayer = L.featureGroup().addTo(map);
  restoreView();
  // A pan or zoom by the user turns the chosen preset into a custom view.
  const markCustom = () => { if (!programmaticMove && focus.area !== "custom") { focus.area = "custom"; renderAreaPicker(); } };
  map.on("dragstart", markCustom);
  map.on("zoomstart", markCustom);
  map.on("zoomend", drawAllRings);                        // rings ↔ dot depending on on-screen size
  map.on("moveend", onMapMoved);                          // the list follows the map
  renderAreaPicker();                                     // restoreView may have moved the map before these handlers existed
}

const openIncident = (inc) => () => { location.href = api.link(`incident.html?id=${encodeURIComponent(inc.id)}`); };

// ---- verified boards: epicentre rings (geographic), or a small dot when they'd be tiny ----------
// Rings are real-size L.circle layers, so they shrink when you zoom out. Once the outer ring would be smaller
// than MIN_RING_PX on screen, the stack is replaced by a 5 px dot (L4 gets a faint pulsing halo).
const MIN_RING_PX = 12;
const LABEL_DIRS = [["top", [0, -6]], ["bottom", [0, 6]], ["right", [8, 0]], ["left", [-8, 0]]];

const ringsOf = (inc) => {
  const a = inc.area;
  return (a.rings && a.rings.length ? a.rings : [{ radius_km: a.radius_km || 2, severity: inc.severity }])
    .slice().sort((x, y) => x.radius_km - y.radius_km);              // innermost first
};

function metersPerPixel(lat) {
  return (40075016.686 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, map.getZoom() + 8);
}

function drawRings(inc, labelDir) {
  const a = inc.area;
  const ll = [a.lat, a.lon];
  const rings = ringsOf(inc);
  const n = rings.length;
  const outerPx = (rings[n - 1].radius_km * 1000) / metersPerPixel(a.lat);
  const hot = fxAge(inc.id) < 6000;
  const label = `L${inc.severity} · ${esc(inc.name)}`;
  let anchor;
  if (outerPx < MIN_RING_PX) {
    const color = sevColor(inc.severity);
    if (inc.severity >= 4) {
      const halo = L.circleMarker(ll, { radius: 9, stroke: false, fillColor: color, fillOpacity: 0.28, className: hot ? "fg-ring-hot" : "fg-dot-halo" });
      halo.on("click", openIncident(inc));
      ringLayer.addLayer(halo);
    }
    anchor = L.circleMarker(ll, { radius: 5, color: "#0B111A", weight: 1, fillColor: color, fillOpacity: 1, className: hot ? "fg-ring-hot" : "" });
  } else {
    [...rings].reverse().forEach((r, k) => {                          // outermost first
      const i = n - 1 - k;                                            // 0 = innermost
      const color = sevColor(r.severity);
      const t = i / Math.max(1, n - 1);
      const c = L.circle(ll, {
        radius: r.radius_km * 1000, color, weight: 1, opacity: 0.6 - 0.3 * t, fillColor: color, fillOpacity: 0.45 - 0.3 * t,
        className: hot ? "fg-ring-hot" : i === 0 && r.severity >= 4 ? "fg-area-pulse" : "",
      });
      c.on("click", openIncident(inc));
      c.bindTooltip(label, { sticky: true, className: "fg-tip" });
      ringLayer.addLayer(c);
    });
    anchor = L.circleMarker(ll, { radius: 3.5, color: "#F8FAFC", weight: 1.5, fillColor: sevColor(rings[0].severity), fillOpacity: 1 });
  }
  // Calmer map: permanent text labels only for L3+ that don't collide (see planLabels); the rest on hover/tap.
  const off = Object.fromEntries(LABEL_DIRS)[labelDir];
  anchor.bindTooltip(label, labelDir
    ? { permanent: true, interactive: true, direction: labelDir, className: "fg-tip", offset: off }
    : { direction: "top", className: "fg-tip", offset: [0, -6] });
  anchor.on("click", openIncident(inc));
  anchor.on("tooltipopen", (e) => { const el = e.tooltip.getElement(); if (el) el.onclick = openIncident(inc); });   // the name label opens it too
  ringLayer.addLayer(anchor);
}

// Greedy label placement: most severe first; try top/bottom/right/left; skip a label that would overlap one already placed.
function planLabels(list) {
  const size = map.getSize();
  const placed = [];
  const plan = new Map();
  for (const inc of [...list].sort((a, b) => b.severity - a.severity)) {
    const pt = map.latLngToContainerPoint([inc.area.lat, inc.area.lon]);
    const w = 14 + 7 * `L${inc.severity} · ${inc.name}`.length, h = 22;
    const tries = { top: [pt.x - w / 2, pt.y - 10 - h], bottom: [pt.x - w / 2, pt.y + 10], right: [pt.x + 12, pt.y - h / 2], left: [pt.x - 12 - w, pt.y - h / 2] };
    for (const [dir, [x, y]] of Object.entries(tries)) {
      const box = [x, y, x + w, y + h];
      if (box[0] < 2 || box[1] < 2 || box[2] > size.x - 2 || box[3] > size.y - 34) continue;  // inside the map, clear of the credits line (2 lines on phones)
      if (!placed.some((b) => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]))) {
        placed.push(box); plan.set(inc.id, dir); break;
      }
    }
  }
  return plan;
}

function drawAllRings() {
  if (!map) return;
  ringLayer.clearLayers();
  const list = visible().filter((inc) => !isWarning(inc) && inc.area && inc.area.lat != null && inc.area.lon != null);
  const labels = planLabels(list);
  list.sort((x, y) => x.severity - y.severity)                        // most severe drawn on top
    .forEach((inc) => drawRings(inc, labels.get(inc.id) || null));
}

// ---- official warnings: polygons in FMI's public colours ------------------------------
function drawPolygons(inc) {
  const color = warningColor(inc);
  for (const poly of inc.area.polygons || []) {
    const p = L.polygon(poly, { pane: "warnings", color, weight: 1, opacity: 0.55, fillColor: color, fillOpacity: 0.12 });
    p.bindTooltip(`${esc(inc.name)}<br><span style="opacity:.75">${esc(inc.area?.label || "")} · ${esc(validText(inc.valid))}</span>`, { sticky: true, className: "fg-tip" });
    p.on("click", openIncident(inc));
    warnLayer.addLayer(p);
  }
}

// Geographic extent of an incident: ring extent for boards, polygon bbox for warnings (cached per object).
const extentCache = new WeakMap();
function extentOf(inc) {
  if (extentCache.has(inc)) return extentCache.get(inc);
  const a = inc.area;
  let b = null;
  if (a && isWarning(inc)) {
    for (const poly of a.polygons || []) for (const pt of poly) b = b ? b.extend(L.latLng(pt[0], pt[1])) : L.latLngBounds([pt, pt]);
  } else if (a && a.lat != null && a.lon != null) {
    b = L.latLng(a.lat, a.lon).toBounds(ringsOf(inc).at(-1).radius_km * 2000);
  }
  extentCache.set(inc, b);
  return b;
}

function dataBounds() {
  let b = null;
  for (const inc of visible()) {
    const e = extentOf(inc);
    if (e) b = b ? b.extend(e) : L.latLngBounds(e.getSouthWest(), e.getNorthEast());
  }
  return b;
}

function renderMap(fit = false) {
  if (!map) return;
  warnLayer.clearLayers();
  const warnings = visible().filter((inc) => isWarning(inc) && inc.area?.polygons?.length)
    .sort((x, y) => x.severity - y.severity);
  for (const inc of warnings) drawPolygons(inc);   // warning radius_km is hundreds of km: polygons only
  const b = fit ? dataBounds() : null;
  if (b) fitTo(b.pad(0.08), "custom", 10);
  drawAllRings();
}

// ---------------------------------------------------------------- area focus (v0.7)
const AREAS = {
  europe:   { label: "All Europe",      b: [[34, -25], [71.5, 45]] },
  nordics:  { label: "Nordics",         b: [[54.5, 4], [71.2, 32]] },
  british:  { label: "British Isles",   b: [[49.6, -11], [61, 2.2]] },
  western:  { label: "Western Europe",  b: [[42.2, -5.2], [54.2, 9.8]] },
  central:  { label: "Central Europe",  b: [[45.6, 5.8], [55.1, 24.2]] },
  southern: { label: "Southern Europe", b: [[35, -9.8], [46.6, 28.5]] },
  eastern:  { label: "Eastern Europe",  b: [[44, 21], [60, 41]] },
};
const VIEW_KEY = "fg-map-view";
const FOLLOW_KEY = "fg-follow-map";
const focus = { area: "europe", follow: lsGet(FOLLOW_KEY, true) !== false };

function fitTo(bounds, area, maxZoom = 12) {
  focus.area = area;
  programmaticMove = true;
  map.fitBounds(bounds, { maxZoom, animate: false });
  programmaticMove = false;
  renderAreaPicker();
  onMapMoved();
}

function restoreView() {
  const q = new URLSearchParams(location.search);
  const v = (q.get("view") || "").split(",").map(Number);              // ?view=lat,lon,zoom (shareable)
  if (q.get("area") && AREAS[q.get("area")]) return fitTo(AREAS[q.get("area")].b, q.get("area"));
  if (v.length === 3 && v.every((x) => !Number.isNaN(x))) { programmaticMove = true; map.setView([v[0], v[1]], v[2]); programmaticMove = false; focus.area = "custom"; return; }
  const saved = lsGet(VIEW_KEY, null);
  if (saved && Array.isArray(saved.center) && saved.zoom != null) {
    programmaticMove = true; map.setView(saved.center, saved.zoom); programmaticMove = false;
    focus.area = saved.area || "custom";
    return;
  }
  fitTo(AREAS.europe.b, "europe");
}

let moveTimer = null;
function onMapMoved() {
  if (!map) return;
  const c = map.getCenter();
  lsSet(VIEW_KEY, { center: [Number(c.lat.toFixed(4)), Number(c.lng.toFixed(4))], zoom: map.getZoom(), area: focus.area });
  clearTimeout(moveTimer);
  moveTimer = setTimeout(() => { renderIncidents(); renderAreaPicker(); }, 120);
}

const inMapView = (inc) => {
  if (!map) return true;
  const e = extentOf(inc);
  return !e || map.getBounds().intersects(e);
};
const shownList = () => (focus.follow && map ? visible().filter(inMapView) : visible());

function renderAreaPicker() {
  const el = document.getElementById("area-picker");
  if (!el) return;
  const opts = Object.entries(AREAS).map(([k, a]) => [k, a.label]).concat([["near", "Near me"]]);
  if (focus.area === "custom") opts.unshift(["custom", "This view"]);
  el.innerHTML = opts.map(([k, label]) => `<option value="${k}" ${focus.area === k ? "selected" : ""}>${esc(label)}</option>`).join("");
  el.onchange = () => {
    const k = el.value;
    if (k === "custom") return;
    if (k !== "near") return fitTo(AREAS[k].b, k);
    if (!navigator.geolocation) return toast("Location is not available in this browser");
    toast("Finding your location…");
    navigator.geolocation.getCurrentPosition(
      (pos) => fitTo(L.latLng(pos.coords.latitude, pos.coords.longitude).toBounds(300000), "near", 10),   // ~150 km around you
      (err) => { toast(err.code === 1 ? "Location permission denied: pick an area instead" : "Couldn't get your location"); renderAreaPicker(); },
      { timeout: 10000, maximumAge: 600000 });
  };
}

// ---------------------------------------------------------------- latest update (one line at the top)
// Replaces the pop-ups: the newest meaningful update, replaced by the next one. Missed ones simply go.

const ACCENT = { critical: "#EF4444", ok: "#10B981", authority: "#38BDF8", triage: "#F59E0B", severe: "#F97316", line: "#64748B" };

function showLatest(u) {
  document.getElementById("latest").innerHTML = `
    <a href="${esc(u.href)}" class="card flex items-center gap-2.5 px-3 py-2 hover:border-line2">
      <span class="w-2 h-2 rounded-full shrink-0" style="background:${ACCENT[u.accent] || ACCENT.line}"></span>
      <span class="label shrink-0">Latest</span>
      <span class="min-w-0 text-sm leading-snug line-clamp-2 sm:line-clamp-1"><b>${esc(u.name)}</b>
        <span class="text-muted">· ${esc(u.headline)}${u.more > 0 ? ` (+${u.more} more)` : ""}</span></span>
      ${ago(u.at, "label text-faint shrink-0 ml-auto hidden sm:inline")}
    </a>`;
}

// On arrival, the newest alert stands in until the first live update.
const ALERT_KIND = { critical: ["Confirmed", "critical"], debunk: ["Shown false", "critical"], potential: ["Being checked", "triage"], info: ["Update", "authority"] };

function showNewestAlert(alerts) {
  const a = [...alerts].sort((x, y) => (y.created_at || "").localeCompare(x.created_at || ""))[0];
  if (!a || document.getElementById("latest").innerHTML.trim()) return;
  const [word, accent] = ALERT_KIND[a.level] || ALERT_KIND.info;
  const inc = state.incidents.find((i) => i.id === a.incident_id);
  const title = String(a.title || "").replace(/^(verified|verifying…?|false|info|not true):\s*/i, "");
  showLatest({ name: inc?.name || "Update", headline: `${word}: ${title}`, more: 0, accent, at: a.created_at,
               href: a.incident_id ? api.link(`incident.html?id=${encodeURIComponent(a.incident_id)}`) : "#" });
}

// ---------------------------------------------------------------- system panel

function renderMonitors() {
  const el = document.getElementById("monitors");
  el.innerHTML = state.monitors.map((m) => `
    <div class="rounded border border-line bg-canvas/40 p-2.5">
      <div class="flex items-center justify-between gap-2">
        <div class="flex items-center gap-2 min-w-0">
          ${icon(KIND_ICON[m.kind] || "public", "text-muted")}
          <span class="text-sm font-semibold truncate">${esc(m.name)}</span>
        </div>
        <span class="chip ${m.mode === "incident" ? "chip-contradicted" : "chip-tag"}">${esc(m.mode)} · ${esc(m.interval_s)}s</span>
      </div>
      <div class="mt-1 mono text-xs text-muted flex flex-wrap gap-x-3 gap-y-0.5">
        <span>${esc(m.type)}</span><span>checks ${esc(m.checks)}</span><span>changes ${esc(m.changes)}</span>
        <span>last check ${ago(m.last_check)}</span>${m.last_change ? `<span>last change ${ago(m.last_change)}</span>` : ""}
      </div>
      ${m.note ? `<div class="mt-1 text-xs text-muted">${esc(m.note)}</div>` : ""}
      ${m.last_error ? `<div class="mt-1 text-xs text-critical">${esc(m.last_error)}</div>` : ""}
      <div class="mt-1.5 flex items-center justify-between gap-2">
        <a class="text-xs mono text-faint truncate hover:text-authority" href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.url)}</a>
        <button type="button" class="btn !min-h-[28px] !text-xs shrink-0 admin-action" data-check="${esc(m.id)}">${icon("refresh", "text-[14px]")}Check now</button>
      </div>
    </div>`).join("") || `<div class="text-sm text-muted">No monitors.</div>`;
  el.querySelectorAll("[data-check]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try { await api.checkMonitor(b.dataset.check); toast("Check requested"); }
      catch (err) { toast("Check failed: " + err.message); }
      finally { setTimeout(() => { b.disabled = false; }, 1500); }
    };
  });
}

function renderActivity() {
  const el = document.getElementById("activity");
  el.innerHTML = activityRows(state.activity);
  document.getElementById("credits").textContent = `${state.creditsSpent} Context.dev credits`;
  const nCtx = state.activity.filter((a) => a.service === "context.dev").length;
  document.getElementById("sys-summary").textContent = `${state.monitors.length} monitors · ${nCtx} Context.dev calls · ${state.creditsSpent} credits`;
}

// v0.9: on a hosted server only the admin (with a token) may trigger checks; everyone else watches.
function applyAuthControl(c) {
  document.body.classList.toggle("read-only", !!c?.auth_required && !api.getToken());
}

// ---------------------------------------------------------------- load + live

async function load() {
  const results = await Promise.allSettled([api.getIncidents(), api.getAlerts(), api.getMonitors(), api.getActivity(60)]);
  const [inc, al, mon, act] = results;
  api.getHealth().then((h) => setPausedChip(!!h.paused, h.paused_since)).catch(() => {});
  api.getControl().then(applyAuthControl).catch(() => {});   // read-only visitors: no "Check now"
  if (inc.status === "fulfilled") state.incidents = (inc.value.incidents || []).filter((i) => !isEnded(i));
  else document.getElementById("incidents").innerHTML = `<div class="card p-4 text-critical text-sm">Could not load incidents: ${esc(inc.reason?.message)}</div>`;
  if (al.status === "fulfilled") setTimeout(() => showNewestAlert(al.value.alerts || []), 0);
  if (mon.status === "fulfilled") state.monitors = mon.value.monitors || [];
  if (act.status === "fulfilled") { state.activity = act.value.activity || []; state.creditsSpent = act.value.credits_spent || 0; }
  if (inc.status === "fulfilled") { liveFx.seedIncidents(state.incidents); renderIncidents(); }
  renderMap(false);                                       // keep the user's area; "see all" fits to the data
  renderMonitors();
  renderActivity();
}

function upsert(list, obj, key = "id") {
  const i = list.findIndex((x) => x[key] === obj[key]);
  if (i >= 0) list[i] = obj; else list.unshift(obj);
}

function live() {
  api.subscribe({
    incident(inc) {
      if (isEnded(inc)) {                                   // expired / cancelled warning: remove from map + list
        state.incidents = state.incidents.filter((i) => i.id !== inc.id);
        const latest = document.getElementById("latest");
        if (latest.querySelector(`a[href*="id=${encodeURIComponent(inc.id)}"]`)) latest.innerHTML = "";
        renderIncidents(); renderMap(false);
        return;
      }
      const prev = state.incidents.find((i) => i.id === inc.id);
      const isNew = !prev;
      liveFx.onIncident(inc);                                 // quiet toast for a new orange/red official warning
      const realChange = isNew || prev.updated_at !== inc.updated_at || Date.now() - (lastItemChange.get(inc.id) || 0) < 15000;
      if (realChange && (!isWarning(inc) || (isNew && inc.severity >= 3))) {
        const plus = {};
        for (const k of ["verified", "corroborated", "unverified", "disputed", "contradicted", "held"]) {
          const d = (inc.counts?.[k] || 0) - (prev?.counts?.[k] || 0);
          if (prev && d > 0) plus[k] = d;
        }
        fx.set(inc.id, { at: Date.now(), plus });
        setTimeout(drawAllRings, 6100);                     // stop the hot-ring pulse
        setTimeout(renderIncidents, 61000);                 // drop the "Updated just now" chip
      }
      upsert(state.incidents, inc);
      renderIncidents();
      renderMap(false);                                     // never move the map under the reader
    },
    pipeline: (ev) => liveFx.onPipeline(ev),
    context_check: (ev) => liveFx.onContextCheck(ev),
    control: (c) => { setPausedChip(!!c.paused, c.since); applyAuthControl(c); },
    item: (it) => { if (liveFx.onItem(it)) lastItemChange.set(it.incident_id, Date.now()); },
    activity(row) {
      state.activity.unshift(row);
      state.activity = state.activity.slice(0, 200);
      if (row.credits) state.creditsSpent += row.credits;
      renderActivity();
    },
    monitor(m) { upsert(state.monitors, m); renderMonitors(); },
  }, setLive);
}

document.querySelectorAll('a[href="admin.html"]').forEach((a) => { a.href = api.link("admin.html"); });
document.getElementById("demo-link")?.setAttribute("href", api.link("demo.html"));
if (location.hash === "#system" || new URLSearchParams(location.search).get("system") === "1") document.getElementById("system").open = true;
initTheme();
initMap();
startAgoTicker();
load().finally(live);
onResume(() => load());
