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
  return `<span class="${cls}">updated ${ago(u)}${showChecked ? ` · checked ${ago(c)}` : ""}</span>`;
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

export const statusChip = (status, n = null) =>
  `<span class="chip chip-${esc(status)}"><span class="dot"></span>${esc(STATUS[status]?.label || status)}${n === null ? "" : ` <span class="tnum">${n}</span>`}</span>`;

export const heldChip = () => `<span class="chip chip-held" title="Held back: unconfirmed reassurance">${icon("pan_tool", "text-[13px]")}Held</span>`;

export const SEVERITY = {
  4: { label: "Critical", color: "#EF4444" },
  3: { label: "Severe", color: "#F97316" },
  2: { label: "Warning", color: "#F59E0B" },
  1: { label: "Advisory", color: "#38BDF8" },
};
export const sevColor = (s) => (SEVERITY[s] || SEVERITY[1]).color;
export const sevChip = (s, label) =>
  `<span class="chip chip-sev${esc(s)}">L${esc(s)} · ${esc(label || SEVERITY[s]?.label || "")}</span>`;

export const KIND_ICON = { official: "account_balance", news: "newspaper", social: "forum", other: "public" };
export const STANCE = { asserts: "states", reports_unverified: "relays, unverified", denies: "says false" };

export const CATEGORY_LABEL = (c) => String(c || "").replace(/_/g, " ");

export function countChips(counts = {}) {
  const parts = STATUS_ORDER.filter((s) => counts[s]).map((s) => statusChip(s, counts[s]));
  if (counts.held) parts.push(`<span class="chip chip-held">Held <span class="tnum">${counts.held}</span></span>`);
  return parts.join(" ");
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
  txt.textContent = state === "live" ? "LIVE" : state === "mock" ? "MOCK DATA" : "RECONNECTING";
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
export function warningColor(inc) {
  const m = String(inc?.name || "").match(/\b(yellow|orange|red)\b/i);
  return m ? WARNING_COLORS[m[1].toLowerCase()] : sevColor(inc?.severity);
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
    <div class="rounded border border-line bg-canvas/40 px-2 py-1.5 mono text-[11px] leading-snug">
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
