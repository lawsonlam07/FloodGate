// FloodGate API client: fetch + Server-Sent Events.
// Contract: docs/API.md

export async function get(path) {
  // no-store: pages restored from the back/forward cache must never show stale data
  const r = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!r.ok) throw new Error(`GET ${path} → HTTP ${r.status}`);
  return r.json();
}

// ---- admin token (v0.9): every POST /api/admin/... carries X-Admin-Token ------------
const TOKEN_KEY = "fg-admin-token";
// Ask the server whether a token is right, without saving it. true / false; throws on network errors.
export async function checkToken(token) {
  const r = await fetch("/api/admin/auth", { method: "POST", headers: { "X-Admin-Token": token, Accept: "application/json" } });
  if (r.status === 401) return false;
  if (r.status === 429) throw new Error("Too many wrong tokens. Try again in a few minutes.");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return true;
}
export function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } }
export function setToken(t) { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } }

function authError(path) {
  setToken("");                                         // rejected or missing: back to read-only
  const err = new Error("Token rejected");
  err.status = 401;
  window.dispatchEvent(new CustomEvent("fg-auth-rejected", { detail: { path } }));
  return err;
}

async function post(path, body) {
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  if (path.startsWith("/api/admin/") && getToken()) headers["X-Admin-Token"] = getToken();
  const r = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (r.status === 401 && path.startsWith("/api/admin/")) throw authError(path);
  if (!r.ok) throw new Error(`POST ${path} → HTTP ${r.status}`);
  return r.json();
}

// ---- endpoints -------------------------------------------------------------

export const getIncidents = () => get("/api/incidents");
export const getIncident = (id) => get(`/api/incidents/${encodeURIComponent(id)}`);
export const getAlerts = () => get("/api/alerts");
export const getMonitors = () => get("/api/monitors");
export const getActivity = (limit = 50) => get(`/api/activity?limit=${limit}`);

export const vote = (item, stillTrue) => post(`/api/items/${encodeURIComponent(item.id)}/vote`, { still_true: stillTrue });
export const report = (item, reason, note) => post(`/api/items/${encodeURIComponent(item.id)}/report`, { reason, note: note || null });

// ---- spoken bulletin (ElevenLabs) + history (Tiger Data), v0.10 ----------------
const incPath = (id, rest) => `/api/incidents/${encodeURIComponent(id)}/${rest}`;

export const getBulletin = (id, lang = "en") => get(incPath(id, `bulletin?lang=${encodeURIComponent(lang)}`));

// Voices the current script (cached server-side per script). Errors carry the server's reason (budget, paused…).
export async function voiceBulletin(id, lang = "en") {
  const r = await fetch(incPath(id, `bulletin/audio?lang=${encodeURIComponent(lang)}`), { method: "POST", headers: { Accept: "application/json" } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

export const getHistory = (id) => get(incPath(id, "history"));
export const getUsage = (hours = 24) => get(`/api/history/usage?hours=${hours}`);

// ---- pause processing (v0.8), fictional on/off, showcase ----------------------------
export const getHealth = () => get("/api/health");
export const getControl = () => get("/api/admin/control");
export const setControl = (paused) => post("/api/admin/control", { paused });
export const setFictional = (enabled) => post("/api/admin/fictional", { enabled });
export const setShowcase = (body) => post("/api/admin/showcase", body);

// ---- scenario control centre (v0.6) --------------------------------------------------
export const getScenarios = () => get("/api/admin/scenarios");
const scPath = (id, what) => `/api/admin/scenarios/${encodeURIComponent(id)}/${what}`;
export const stepScenario = (id, body) => post(scPath(id, "step"), body);
export const resetScenario = (id) => post(scPath(id, "reset"), {});
export const autoplayScenario = (id, body) => post(scPath(id, "autoplay"), body);
export const bulkScenarios = (body) => post("/api/admin/scenarios/bulk", body);
export const checkMonitor = (id) => post(`/api/admin/check/${encodeURIComponent(id)}`, {});

// ---- live stream -----------------------------------------------------------

const EVENT_TYPES = ["incident", "item", "alert", "activity", "monitor", "scenario", "pipeline", "context_check", "control"];
const HIDDEN_CLOSE_MS = 60000;

/**
 * subscribe({incident, item, alert, activity, monitor, ...}, onStatus)
 * onStatus(state) with state in "live" | "offline".
 */
export function subscribe(handlers, onStatus = () => {}) {
  // A tab hidden for over a minute closes its stream: it stops counting as a viewer (the server's showcase only runs
  // while someone watches) and phones save battery. Back in view it reconnects; pages refetch their data on resume.
  let es = null, hideTimer = null, stopped = false;
  const open = () => {
    try {
      es = new EventSource("/api/stream");
    } catch {
      onStatus("offline");
      return;
    }
    es.onopen = () => onStatus("live");
    es.onerror = () => onStatus("offline"); // EventSource reconnects by itself
    for (const type of EVENT_TYPES) {
      es.addEventListener(type, (ev) => {
        let data;
        try { data = JSON.parse(ev.data); } catch { return; }
        try { handlers[type]?.(data); } catch (err) { console.error(`handler for ${type} failed`, err); }
      });
    }
  };
  const close = () => { es?.close(); es = null; };
  const onVisibility = () => {
    clearTimeout(hideTimer);
    if (document.hidden) hideTimer = setTimeout(close, HIDDEN_CLOSE_MS);
    else if (!es && !stopped) open();
  };
  document.addEventListener("visibilitychange", onVisibility);
  open();
  onVisibility();
  return () => { stopped = true; clearTimeout(hideTimer); document.removeEventListener("visibilitychange", onVisibility); close(); };
}
