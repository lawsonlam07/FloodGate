// Shared rendering helpers.

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function timeAgo(iso) {
  if (!iso) return "–";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (Number.isNaN(s)) return "–";
  if (s < 10) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function clock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

// <time> element that the ticker keeps fresh.
export const ago = (iso, cls = "") =>
  `<time class="${cls}" datetime="${esc(iso)}" data-ago="${esc(iso)}" title="${esc(iso ? new Date(iso).toLocaleString() : "")}">${timeAgo(iso)}</time>`;

export function startAgoTicker() {
  setInterval(() => {
    document.querySelectorAll("[data-ago]").forEach((el) => { el.textContent = timeAgo(el.dataset.ago); });
  }, 1000);                                             // second-level labels must not freeze and jump
}

// "updated 3m ago · checked 5s ago" (checked only when it differs meaningfully from updated).
export function updatedChecked(inc, cls = "") {
  const u = inc?.updated_at, c = inc?.checked_at;
  const showChecked = c && (!u || Date.parse(c) - Date.parse(u) > 30000);
  return `<span class="${cls}">Updated ${ago(u)}${showChecked ? `, checked ${ago(c)}` : ""}</span>`;
}

export const icon = (name, cls = "") => `<span class="material-symbols-outlined ${cls}" aria-hidden="true">${esc(name)}</span>`;

export const STATUS = {
  verified:     { label: "Verified",     icon: "verified",      order: 0 },
  corroborated: { label: "Corroborated", icon: "group",         order: 1 },
  unverified:   { label: "Unverified",   icon: "help",          order: 2 },
  disputed:     { label: "Disputed",     icon: "compare_arrows", order: 3 },
  contradicted: { label: "Contradicted", icon: "block",         order: 4 },
};
export const STATUS_ORDER = ["verified", "corroborated", "unverified", "disputed", "contradicted"];

// For the public the rules' five statuses fold into three words; the detail stays in the hover text.
export const SIMPLE = {
  confirmed:   { label: "Confirmed",   cls: "chip-verified" },
  unconfirmed: { label: "Unconfirmed", cls: "chip-unverified" },
  false:       { label: "False",       cls: "chip-contradicted" },
};
export const simpleOf = (status) => (status === "verified" ? "confirmed" : status === "contradicted" ? "false" : "unconfirmed");
const STATUS_DETAIL = {
  verified: "An official source states it", corroborated: "Several independent sources agree; no official confirmation yet",
  unverified: "Only one source so far", disputed: "Sources disagree", contradicted: "An official source says it is false",
};
export const simpleCounts = (c = {}) => ({
  confirmed: c.verified || 0, unconfirmed: (c.corroborated || 0) + (c.unverified || 0) + (c.disputed || 0), false: c.contradicted || 0,
});
export const simpleChip = (key, n = null, title = "") =>
  `<span class="chip ${SIMPLE[key].cls}"${title ? ` title="${esc(title)}"` : ""}><span class="dot"></span>${SIMPLE[key].label}${n === null ? "" : ` <span class="tnum">${n}</span>`}</span>`;

export const statusChip = (status, n = null) => simpleChip(simpleOf(status), n, STATUS_DETAIL[status] || "");

export const heldChip = () => `<span class="chip chip-held" title="An unconfirmed reassurance: wait for an official source before acting on it">Don't act yet</span>`;

export const SEVERITY = {
  4: { label: "Critical", color: "#EF4444" },
  3: { label: "Severe", color: "#F97316" },
  2: { label: "Warning", color: "#F59E0B" },
  1: { label: "Advisory", color: "#38BDF8" },
};
export const sevColor = (s) => (SEVERITY[s] || SEVERITY[1]).color;
export const sevChip = (s, label) =>
  `<span class="chip chip-sev${esc(s)}">L${esc(s)} ${esc(SEVERITY[s]?.label || label || "")}</span>`;

export const KIND_ICON = { official: "account_balance", news: "newspaper", social: "forum", other: "public" };
export const STANCE = { asserts: "states", reports_unverified: "relays, unverified", denies: "says false" };

export const CATEGORY_LABEL = (c) => String(c || "").replace(/_/g, " ");

export function countChips(counts = {}) {
  const c = simpleCounts(counts);
  return Object.keys(SIMPLE).filter((k) => c[k]).map((k) => simpleChip(k, c[k])).join(" ");
}

// localStorage that never throws (private mode, blocked storage, previews).
export function lsGet(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : JSON.parse(v);
  } catch { return fallback; }
}
export function lsSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
}

export function setLive(state) {
  const dot = document.getElementById("live-dot");
  const txt = document.getElementById("live-text");
  if (!dot || !txt) return;
  dot.className = "live-dot" + (state === "live" ? " on" : state === "mock" ? " mock" : "");
  txt.textContent = state === "live" ? "Live" : state === "mock" ? "Mock data" : "Reconnecting";
}

export function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "status");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// ---- v0.2 helpers -------------------------------------------------------------

export const isWarning = (inc) => inc?.kind === "official_warning";

// "until Sun 00:00" (or "expired"); full date in the title attribute.
export function untilText(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  if (d.getTime() < Date.now()) return "expired";
  return "until " + d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function rangeText(valid) {
  if (!valid) return "";
  const f = (iso) => iso ? new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "?";
  if (!valid.to) return valid.from ? `Since ${f(valid.from)}, no end time given` : "No end time given";
  return `${f(valid.from)} → ${f(valid.to)}`;
}

// "from Sun 03:00" when a warning has not started yet, "until 18:00" while active, "ended" after.
export function validText(valid) {
  if (!valid) return "";
  const now = Date.now();
  const from = valid.from ? Date.parse(valid.from) : NaN;
  const to = valid.to ? Date.parse(valid.to) : NaN;
  const fmt = (t) => new Date(t).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  if (!Number.isNaN(from) && from > now) return `from ${fmt(from)}`;
  if (!Number.isNaN(to)) return to < now ? "ended" : `until ${fmt(to)}`;
  return "";
}
export const isEnded = (inc) => inc?.status === "ended";

// FMI's public warning colours, taken from the warning's own colour word; falls back to the severity colour.
export const WARNING_COLORS = { yellow: "#FACC15", orange: "#F97316", red: "#EF4444" };
// Official warnings are drawn on our one L1–L4 scale; their level already comes from the official colour
// (red = L4, orange = L3, yellow = L1), so the map needs a single key.
export function warningColor(inc) {
  return sevColor(inc?.severity);
}

// ---- v0.3 shared components ----------------------------------------------------

export const hostOf = (url) => { try { return new URL(url, location.href).host; } catch { return url || ""; } };

// "captured by Context.dev from <host>", only when it differs from the viewer link.
export function capturedText(src) {
  const cf = src?.captured_from;
  if (!cf || cf === src.url) return "";
  return `captured by Context.dev from ${hostOf(cf)}`;
}

const SERVICE_CHIP = { "context.dev": "chip-corroborated", anthropic: "chip-verified", gemini: "chip-verified", elevenlabs: "chip-tag", monitor: "chip-tag" };

// API activity feed rows (home System panel + admin page).
export function activityRows(list, limit = 60) {
  return list.slice(0, limit).map((a) => `
    <div class="rounded border border-line bg-canvas/40 px-2 py-1.5 mono text-xs leading-snug">
      <div class="flex items-center justify-between gap-2">
        <span class="flex items-center gap-1.5 min-w-0"><span class="chip ${SERVICE_CHIP[a.service] || "chip-tag"}">${esc(a.service)}</span>
          <span class="text-ink truncate">${esc(a.action)}</span></span>
        <span class="text-faint shrink-0">${ago(a.at)}</span>
      </div>
      <div class="text-muted truncate" title="${esc(a.target)}">${esc(a.target)}</div>
      <div class="text-faint flex flex-wrap gap-x-3">
        ${a.latency_ms != null ? `<span>${esc(a.latency_ms)} ms</span>` : ""}
        ${a.credits != null ? `<span class="text-authority">${esc(a.credits)} credits</span>` : ""}
        ${a.cache ? `<span>cache ${esc(a.cache)}</span>` : ""}
        ${a.ok === false ? `<span class="text-critical">failed</span>` : ""}
        ${a.detail ? `<span>${esc(a.detail)}</span>` : ""}
      </div>
    </div>`).join("") || `<div class="text-sm text-muted">No activity yet.</div>`;
}

// ---- v0.7: fresh data after back/forward navigation and long hidden periods ----------
// A page restored from the back/forward cache has stale data and a closed EventSource: reload it cleanly.
// After the tab was hidden for more than a minute, re-fetch.
export function onResume(refetch, staleMs = 60000) {
  window.addEventListener("pageshow", (e) => { if (e.persisted) location.reload(); });
  let hiddenAt = document.hidden ? Date.now() : 0;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { hiddenAt = Date.now(); return; }
    if (hiddenAt && Date.now() - hiddenAt > staleMs) { try { refetch(); } catch (err) { console.warn("refetch failed", err); } }
    hiddenAt = 0;
  });
}

// ---- light / dark theme ---------------------------------------------------------------------------------------
// The page's <head> applies the saved theme before anything is drawn (no flash); this adds the toggle button next to
// the live indicator and keeps other open pages (e.g. the demo's frames) in step through the storage event.
const THEME_KEY = "fg-theme";
export const currentTheme = () => (document.documentElement.dataset.theme === "light" ? "light" : "dark");

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  window.dispatchEvent(new CustomEvent("fg-theme", { detail: t }));
}

export function setTheme(t) {
  try { localStorage.setItem(THEME_KEY, t); } catch { /* private mode: this page only */ }
  applyTheme(t);
}

export function initTheme() {
  window.addEventListener("storage", (e) => { if (e.key === THEME_KEY) applyTheme(e.newValue === "light" ? "light" : "dark"); });
  const dot = document.getElementById("live-dot");
  if (!dot || document.getElementById("theme-btn")) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "theme-btn";
  btn.className = "theme-btn";
  const paint = () => {
    const light = currentTheme() === "light";
    btn.innerHTML = icon(light ? "dark_mode" : "light_mode", "text-[20px]");
    btn.title = light ? "Switch to dark mode" : "Switch to light mode";
    btn.setAttribute("aria-label", btn.title);
  };
  btn.onclick = () => setTheme(currentTheme() === "light" ? "dark" : "light");
  dot.parentElement.insertBefore(btn, dot);
  paint();
  window.addEventListener("fg-theme", paint);
}

// Keyless Esri basemaps in the page's theme; `basemap(map)` adds them and swaps them when the theme changes.
export function basemap(map, { labels = true } = {}) {
  const esri = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas";
  const url = (part) => `${esri}/World_${currentTheme() === "light" ? "Light" : "Dark"}_Gray_${part}/MapServer/tile/{z}/{y}/{x}`;
  const base = L.tileLayer(url("Base"), {
    maxZoom: 16,
    attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Esri, HERE, Garmin, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  let ref = null;
  if (labels) {
    map.createPane("labels");                               // place names above the areas, never catching clicks
    map.getPane("labels").style.zIndex = 450;
    map.getPane("labels").style.pointerEvents = "none";
    ref = L.tileLayer(url("Reference"), { maxZoom: 16, pane: "labels", opacity: 0.85 }).addTo(map);
  }
  window.addEventListener("fg-theme", () => { base.setUrl(url("Base")); ref?.setUrl(url("Reference")); });
}
