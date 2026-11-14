// FloodGate API client: fetch + Server-Sent Events, with a ?mock=1 fixture mode for frontend development.
// Contract: docs/API.md

const params = new URLSearchParams(location.search);
export const MOCK = params.get("mock") === "1";
// ?mock=1&simdelay=0: the mock live update plays out instantly (for headless screenshots).
export const SIM_FAST = MOCK && params.get("simdelay") === "0";

// Keep ?mock=1 on internal links while in mock mode.
export function link(path) {
  if (!MOCK) return path;
  return path + (path.includes("?") ? "&" : "?") + "mock=1";
}

function mockPath(path) {
  const p = path.split("?")[0].replace(/^\/api\//, "");
  const m = p.match(/^incidents\/([^/]+)$/);
  if (m) return `mock/incident-${encodeURIComponent(m[1])}.json`;
  return `mock/${p}.json`;
}

// Fixture timestamps are rebased so the newest one is ~1 minute ago, keeping "x min ago" realistic.
const MOCK_EPOCH = Date.parse("2026-11-14T10:13:00Z");
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;
function rebase(v, shift) {
  if (typeof v === "string") return ISO_RE.test(v) ? new Date(Date.parse(v) + shift).toISOString().replace(/\.\d+Z$/, "Z") : v;
  if (Array.isArray(v)) return v.map((x) => rebase(x, shift));
  // real FMI validity windows stay as published
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, k === "valid" ? x : rebase(x, shift)]));
  return v;
}

export async function get(path) {
  const url = MOCK ? mockPath(path) : path;
  // no-store: pages restored from the back/forward cache must never show stale data
  const r = await fetch(url, { headers: { Accept: "application/json" }, cache: MOCK ? "default" : "no-store" });
  if (!r.ok) throw new Error(`GET ${url} → HTTP ${r.status}`);
  const data = await r.json();
  return MOCK ? rebase(data, Date.now() - MOCK_EPOCH) : data;
}

// ---- admin token (v0.9): every POST /api/admin/... carries X-Admin-Token ------------
const TOKEN_KEY = "fg-admin-token";
// Ask the server whether a token is right, without saving it. true / false; throws on network errors.
export async function checkToken(token) {
  if (MOCK) return token !== "bad";
  const r = await fetch("/api/admin/auth", { method: "POST", headers: { "X-Admin-Token": token, Accept: "application/json" } });
  if (r.status === 401) return false;
  if (r.status === 429) throw new Error("Too many wrong tokens. Try again in a few minutes.");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return true;
}
export function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } }
export function setToken(t) { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } }
// Dev aid for screenshots: ?mock=1&token=… pre-sets a token (mock mode only, never for the real server).
if (MOCK && params.get("token")) setToken(params.get("token"));

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

// Community: in mock mode the server is simulated from the item's current counts.
export async function vote(item, stillTrue) {
  if (MOCK) {
    const c = { ...item.community };
    stillTrue ? c.still_true++ : c.not_true++;
    return { community: c };
  }
  return post(`/api/items/${encodeURIComponent(item.id)}/vote`, { still_true: stillTrue });
}

export async function report(item, reason, note) {
  if (MOCK) return { community: { ...item.community, reports: item.community.reports + 1 } };
  return post(`/api/items/${encodeURIComponent(item.id)}/report`, { reason, note: note || null });
}

// ---- spoken bulletin (ElevenLabs) + history (Tiger Data), v0.10 ----------------
const incPath = (id, rest) => `/api/incidents/${encodeURIComponent(id)}/${rest}`;

export async function getBulletin(id, lang = "en") {
  if (MOCK) return mockBulletin(lang);
  return get(incPath(id, `bulletin?lang=${encodeURIComponent(lang)}`));
}

// Voices the current script (cached server-side per script). Errors carry the server's reason (budget, paused…).
export async function voiceBulletin(id, lang = "en") {
  if (MOCK) { await new Promise((r) => setTimeout(r, 900)); return { audio: null, cached: false, mock: true }; }
  const r = await fetch(incPath(id, `bulletin/audio?lang=${encodeURIComponent(lang)}`), { method: "POST", headers: { Accept: "application/json" } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

export async function getHistory(id) {
  if (MOCK) return mockHistory();
  return get(incPath(id, "history"));
}

export async function getUsage(hours = 24) {
  if (MOCK) {
    const now = Date.now(), rows = [];
    for (let h = 11; h >= 0; h--) for (const [service, calls, credits, lat] of [["context.dev", 14, 30, 6200], ["gemini", 22, 0, 3100], ["elevenlabs", 2, 0, 2400], ["pipeline", 9, 0, 11800]])
      rows.push({ bucket: new Date(now - h * 3600e3).toISOString(), service, calls: Math.max(0, calls - (h % 4) * 3), credits: credits ? credits - (h % 3) * 5 : 0, avg_latency_ms: lat, failures: h === 5 && service === "gemini" ? 1 : 0 });
    return { available: true, hours, rows };
  }
  return get(`/api/history/usage?hours=${hours}`);
}

function mockBulletin(lang) {
  const local = lang !== "en";
  const lines = local
    ? [["intro", "Fiktive Übung. Dies ist kein echter Notfall."], ["intro", "FloodGate-Lagemeldung für Halvik."], ["heading", "Von den Behörden bestätigt:"], ["fact", "Die Harbour Bridge ist gesperrt."], ["outro", "Die Quellen zu jeder Zeile stehen im FloodGate-Lagebild."]]
    : [["intro", "Fictional exercise. This is not a real emergency."], ["intro", "FloodGate situation update for Halvik."], ["heading", "Confirmed by official sources:"], ["fact", "Harbour Bridge is closed to all traffic."], ["heading", "Not true, according to officials:"], ["fact", "Reports that Harbour Bridge has collapsed are false."], ["outro", "Sources for every line are on the FloodGate board."]];
  return { lang, id: `mock-${lang}`, lines: lines.map(([kind, text]) => ({ kind, text })), text: lines.map((l) => l[1]).join("\n"),
           chars: 300, verbatim: lang === "de", languages: ["en", "de"], audio: null, can_voice: true };
}

function mockHistory() {
  const t0 = Date.now() - 20 * 60000;
  const timeline = Array.from({ length: 21 }, (_, i) => ({
    t: new Date(t0 + i * 60000).toISOString(), verified: Math.min(8, Math.floor(i / 2)), corroborated: i > 6 ? 2 : 0,
    unverified: Math.max(2, 9 - Math.floor(i / 3)), disputed: i > 10 && i < 15 ? 1 : 0, contradicted: i > 12 ? 3 : 0, held: i > 12 ? 1 : 0, severity: i > 8 ? 4 : 3,
  }));
  return {
    available: true, bucket: "1 minute", timeline,
    confirmed: [{ item_id: "x1", title: "Saltmarsh south of Ferry Street is being evacuated.", first_kind: "social", after_s: 312, status: "verified" }],
    debunked: [{ item_id: "x2", title: "Harbour Bridge has collapsed.", first_kind: "social", after_s: 178, status: "contradicted" },
               { item_id: "x3", title: "All of Halvik will be evacuated tonight.", first_kind: "social", after_s: 641, status: "contradicted" }],
    stats: { items: 31, confirmed: 1, median_confirm_s: 312, debunked: 2, median_debunk_s: 641, updates: 14, p50_update_ms: 11800, p90_update_ms: 19400 },
  };
}

// ---- scenario admin (v0.3) ---------------------------------------------------
export const getScenario = () => get("/api/admin/scenario");
export async function scenarioStep(body) { return MOCK ? mockScenario.step(body) : post("/api/admin/scenario/step", body); }
export async function scenarioReset() { return MOCK ? mockScenario.reset() : post("/api/admin/scenario/reset", {}); }
export async function scenarioAutoplay(body) { return MOCK ? mockScenario.autoplay(body) : post("/api/admin/scenario/autoplay", body); }

// ---- pause processing (v0.8) -------------------------------------------------------
// Mock: ?paused=1 simulates the paused state.
// Mock: ?paused=1 paused · ?auth=1 token required (token "bad" is rejected) · ?showcase=1 showcase running · ?budget=1 budget reached
const mockControl = {
  paused: params.get("paused") === "1" || params.get("budget") === "1",
  since: params.get("paused") === "1" || params.get("budget") === "1" ? new Date(Date.now() - 6 * 60000).toISOString().replace(/\.\d+Z$/, "Z") : null,
  reason: params.get("budget") === "1" ? "daily credit budget of 2000 reached" : null,
  showcase: { running: params.get("showcase") === "1", max_concurrent: 3, interval_s: 75, hold_s: 180 },
  auth_required: params.get("auth") === "1",
  credit_budget_daily: 2000,
  credits_today: params.get("budget") === "1" ? 2004 : 312,
};
function mockAuth() {
  if (!mockControl.auth_required) return;
  if (!getToken() || getToken() === "bad") throw authError("/api/admin/(mock)");
}
export async function getHealth() {
  if (MOCK) return { ok: true, mode: "mock", grouping_model: "claude-haiku-5-5", paused: mockControl.paused, paused_since: mockControl.since };
  return get("/api/health");
}
export async function getControl() { return MOCK ? { ...mockControl } : get("/api/admin/control"); }
export async function setControl(paused) {
  if (MOCK) {
    mockAuth();
    Object.assign(mockControl, { paused, reason: null, since: new Date().toISOString().replace(/\.\d+Z$/, "Z") });
    mockScenarios.emit("control", { ...mockControl });
    return { ...mockControl };
  }
  return post("/api/admin/control", { paused });
}
export async function setShowcase(body) {
  if (MOCK) {
    mockAuth();
    mockControl.showcase = { ...mockControl.showcase, ...body };
    mockScenarios.emit("control", structuredClone(mockControl));
    return structuredClone(mockControl);
  }
  return post("/api/admin/showcase", body);
}

// ---- scenario control centre (v0.6): many scenarios ------------------------------
export const getScenarios = () => get("/api/admin/scenarios");
const scPath = (id, what) => `/api/admin/scenarios/${encodeURIComponent(id)}/${what}`;
export async function stepScenario(id, body) { return MOCK ? mockScenarios.step(id, body) : post(scPath(id, "step"), body); }
export async function resetScenario(id) { return MOCK ? mockScenarios.reset(id) : post(scPath(id, "reset"), {}); }
export async function autoplayScenario(id, body) { return MOCK ? mockScenarios.autoplay(id, body) : post(scPath(id, "autoplay"), body); }
export async function bulkScenarios(body) { return MOCK ? mockScenarios.bulk(body) : post("/api/admin/scenarios/bulk", body); }

export async function checkMonitor(id) {
  if (MOCK) { mockAuth(); return { ok: true }; }
  return post(`/api/admin/check/${encodeURIComponent(id)}`, {});
}

// ---- live stream -----------------------------------------------------------

const EVENT_TYPES = ["incident", "item", "alert", "activity", "monitor", "scenario", "pipeline", "context_check", "control"];

/**
 * subscribe({incident, item, alert, activity, monitor}, onStatus)
 * onStatus(state) with state in "live" | "offline" | "mock".
 */
export function subscribe(handlers, onStatus = () => {}) {
  if (MOCK) {
    onStatus("mock");
    mockScenario.handlers = handlers;
    mockScenarios.handlers = handlers;
    if (location.pathname.endsWith("admin.html")) mockScenarios.boot();
    else simulate(handlers);
    return () => {};
  }
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
const HIDDEN_CLOSE_MS = 60000;

// Mock mode: exercise the live paths with one alert, one activity row and one item update.
// Scripted update for home/incident mock mode: two Halvik sources run through the pipeline over ~8 s, then a burst
// of item events (one unverified → FALSE, one new verified fact, two new reports) and an incident event.
// ?simdelay=<ms> = when it starts (default 3000); ?simdelay=0 plays everything instantly (headless screenshots).
async function simulate(handlers) {
  const start = Number(params.get("simdelay") ?? 3000);
  const scale = SIM_FAST ? 0 : 1;
  const iso = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const INC = "halvik-storm-surge";
  let d;
  try { d = await get(`/api/incidents/${INC}`); } catch (err) { console.warn("mock simulation failed", err); return; }
  const events = [];                                   // [t_ms, type, payloadFn]
  const pipe = (t, sid, name, stage, t0, detail) =>
    events.push([t, "pipeline", () => ({ source_id: sid, source_name: name, incident_id: INC, stage, at: iso(), elapsed_ms: t - t0, detail: detail || null })]);
  for (const [sid, name, t0, extra] of [["halvik-rescue", "Halvik Rescue Services", 0, 0], ["halvik-herald", "The Halvik Herald", 900, 300]]) {
    pipe(t0, sid, name, "detected", t0, "text hash changed");
    pipe(t0 + 300, sid, name, "extracting", t0, "Context.dev scrape + JSON extraction");
    pipe(t0 + 5200 + extra, sid, name, "extracted", t0, sid.endsWith("rescue") ? "27 claims, 4 new, 1 withdrawn" : "21 claims, 2 new");
    pipe(t0 + 5400 + extra, sid, name, "grouping", t0, "claude-haiku-5-5");
    pipe(t0 + 7300 + extra, sid, name, "grouped", t0, null);
  }
  const base = d.items.find((i) => /all of halvik/i.test(i.title)) || d.items.find((i) => i.status === "unverified" && !i.held);
  const rescueSrc = { source_id: "halvik-rescue", name: "Halvik Rescue Services", kind: "official", url: "/sites/rescue/", captured_from: null,
    stance: "asserts", attributed_to: null, author: null, author_signals: null };
  const boardSrc = (author, signals, quote) => ({ source_id: "halvik-board", name: "Halvik Board", kind: "social", url: "/sites/board/", captured_from: null,
    stance: "asserts", attributed_to: null, author, author_signals: signals, evidence_quote: quote });
  const mk = (id, title, status, category, condition, src, extra = {}) => {
    const at = iso();
    return { id, incident_id: INC, title, summary: extra.summary || "", status, held: false, importance: extra.importance || "normal",
      safety_direction: "protective", category, condition, hazard: "storm_surge", place: extra.place || null, tags: [category, "storm_surge"],
      origins: { asserting: 1, denying: 0 }, sources: [{ ...src, seen_at: at }],
      log: [{ at, kind: "seen", text: `First seen: ${src.name} states it` }], community: { still_true: 0, not_true: 0, reports: 0 },
      first_seen: at, updated_at: at };
  };
  events.push([7650, "item", () => {
    const at = iso();
    return { ...base, status: "contradicted", importance: "key", updated_at: at,
      summary: base.summary + " Halvik Rescue Services says it is false.",
      origins: { ...base.origins, denying: 1 },
      sources: [...base.sources, { ...rescueSrc, stance: "denies", evidence_quote: "Reports that all of Halvik will be evacuated tonight are false.", seen_at: at }],
      log: [...base.log, { at, kind: "seen", text: "Halvik Rescue Services says it is false" },
            { at, kind: "status", text: "unverified → contradicted: Halvik Rescue Services says it is false" }] };
  }]);
  events.push([7720, "item", () => mk("it_sim_v1", "Evacuation extended to Old Harbour below Harbour Street", "verified", "evacuation", "evacuation_ordered",
    { ...rescueSrc, evidence_quote: "Residents of Old Harbour living below Harbour Street must also leave by 17:00." },
    { importance: "key", place: "Old Harbour", summary: "Halvik Rescue Services states it." })]);
  events.push([7790, "item", () => mk("it_sim_r1", "Water is over the quay at Old Harbour", "unverified", "observation", "flooded",
    boardSrc("harbour_hanna", "Joined May 2019 · 210 followers · 4 reposts", "Water is over the quay at Old Harbour now"),
    { place: "Old Harbour", summary: "Asserted by 1 social post." })]);
  events.push([7860, "item", () => mk("it_sim_r2", "Traffic queues on Northgate Road as people leave", "unverified", "observation", "restricted",
    boardSrc("northgate_niko", "Joined March 2021 · 96 followers · 1 repost", "Northgate Road is jammed, everyone heading inland"),
    { place: "Northgate Road", summary: "Asserted by 1 social post." })]);
  events.push([7950, "incident", () => {
    const c = { ...d.incident.counts };
    c.contradicted += 1; c.unverified += 1; c.verified += 1; c.total += 3;
    return { ...d.incident, counts: c, updated_at: iso() };
  }]);
  events.push([8000, "activity", () => ({ at: iso(), service: "context.dev", action: "scrape+extract", target: "/sites/rescue/",
    latency_ms: 5200, credits: 5, cache: "miss", ok: true, detail: "27 claims" })]);
  pipe(8100, "halvik-rescue", "Halvik Rescue Services", "done", 0, "1 status change, 2 alerts");
  pipe(8800, "halvik-herald", "The Halvik Herald", "done", 900, "no status changes");   // after its grouped (8500)
  events.push([12000, "context_check", () => ({ source_id: "halvik-rescue", source_name: "Halvik Rescue Services", incident_id: INC,
    status: "changed", title: "Evacuation order extended to Old Harbour", summary: "Update no. 5 extends the evacuation to Old Harbour below Harbour Street and adds a 'Not true' entry.",
    importance: "high", confidence: 0.97, latency_ms: 11200, elapsed_ms: 12000, credits: 1, at: iso(), pipeline_new_claims: 4, disagreement: false })]);
  events.sort((a, b) => a[0] - b[0]);                   // equal delays fire in registration order
  for (const [t, type, payload] of events) {
    setTimeout(() => { try { handlers[type]?.(payload()); } catch (err) { console.error(err); } }, start + t * scale);
  }
}

// ---- mock scenario simulator (admin.html?mock=1) --------------------------------
// Applying a step "publishes" the changed pages and plays the pipeline stages for each with realistic timings.
const STAGE_PLAN = [
  ["detected", 0, "text hash changed"],
  ["extracting", 400, "Context.dev scrape + JSON extraction"],
  ["extracted", 15800, null],
  ["grouping", 16100, "claude-haiku-5-5"],
  ["grouped", 23400, null],
  ["done", 23900, null],
];
const PAGE_SOURCE = { rescue: "halvik-rescue", herald: "halvik-herald", board: "halvik-board" };

const mockScenario = {
  handlers: {},
  state: null,
  timers: [],
  emit(type, data) { try { this.handlers[type]?.(data); } catch (e) { console.error(e); } },
  async load() {
    if (!this.state) this.state = await get("/api/admin/scenario");
    return this.state;
  },
  playPipeline(pages, startOffsetMs = 0) {
    const now = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");
    pages.forEach((page, k) => {
      const source_id = PAGE_SOURCE[page] || page;
      const stagger = k * 2600;                                          // sources finish one after another
      for (const [stage, t, detail] of STAGE_PLAN) {
        const due = stagger + t * (1 + k * 0.15) - startOffsetMs;
        const d = detail ?? (stage === "extracted" ? `${18 + k * 6} claims, ${3 + k} new, ${k} withdrawn`
          : stage === "grouped" ? `${3 + k} claims grouped, ${k} new groups` : `${k + 1} status changes, ${k ? 0 : 1} alerts`);
        const fire = () => this.emit("pipeline", { source_id, stage, at: now(), elapsed_ms: Math.round(stagger + t * (1 + k * 0.15)) - stagger, detail: d });
        if (due <= 0) fire(); else this.timers.push(setTimeout(fire, due));
      }
    });
  },
  async step(body) {
    const st = await this.load();
    const to = body.next ? Math.min(st.current_step + 1, st.steps.length - 1) : body.to;
    st.current_step = to;
    this.emit("scenario", structuredClone(st));
    this.emit("activity", { at: new Date().toISOString().replace(/\.\d+Z$/, "Z"), service: "monitor", action: "scenario step applied",
      target: `step ${to}`, latency_ms: null, credits: null, cache: null, ok: true, detail: st.steps[to].title });
    this.playPipeline(st.steps[to].changes || []);
    return structuredClone(st);
  },
  async reset() {
    const st = await this.load();
    this.timers.forEach(clearTimeout); this.timers = [];
    st.current_step = 0; st.autoplay.running = false;
    this.emit("scenario", structuredClone(st));
    return structuredClone(st);
  },
  async autoplay(body) {
    const st = await this.load();
    st.autoplay = { running: !!body.running, interval_s: body.interval_s ?? st.autoplay.interval_s };
    this.emit("scenario", structuredClone(st));
    return structuredClone(st);
  },
  // On page load: show a run already in progress (board done, herald grouping, rescue extracting).
  async boot() {
    const st = await this.load();
    const d = Number(params.get("simdelay") ?? 2500);
    const pages = st.steps[st.current_step]?.changes || ["rescue", "herald", "board"];
    // pretend the current step was applied ~19 s ago, so the stopwatches are mid-run
    this.playPipeline(pages.slice().reverse(), 19000);
    if (d > 0) this.timers.push(setTimeout(() => this.emit("alert", {
      id: "al_mock_admin", level: "potential", title: "Halvik Rescue Services updated its situation page",
      body: "Change detected. Verifying through Context.dev…", incident_id: "halvik-storm-surge", item_id: null,
      created_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    }), d));
  },
};


// ---- mock control centre (admin.html?mock=1, v0.6) ---------------------------------
// Steps/resets/autoplay/bulk change the fixture state locally and replay pipeline stages (+ a Context.dev verdict).
const mockScenarios = {
  handlers: {},
  list: null,
  emit(type, data) { try { this.handlers[type]?.(data); } catch (e) { console.error(e); } },
  async load() {
    if (!this.list) this.list = (await get("/api/admin/scenarios")).scenarios;
    return this.list;
  },
  find(id) { return this.list.find((s) => s.id === id); },
  iso: () => new Date().toISOString().replace(/\.\d+Z$/, "Z"),
  // Replay the pipeline for the pages a step changes; startOffsetMs > 0 = pretend it started that long ago.
  play(sc, pageIds, startOffsetMs = 0, delayMs = 0) {
    const pages = sc.pages.filter((p) => pageIds.includes(p.id));
    pages.forEach((page, k) => {
      const stagger = k * 2600;
      for (const [stage, t, detail] of STAGE_PLAN) {
        const tt = stagger + t * (1 + k * 0.15);
        const due = delayMs + tt - startOffsetMs;
        const d = detail ?? (stage === "extracted" ? `${14 + k * 5} claims, ${3 + k} new` : stage === "grouped" ? `${3 + k} claims grouped` : `${k + 1} status changes`);
        const ev = () => ({ source_id: page.source_id, source_name: page.name, incident_id: sc.incident_id, stage, at: this.iso(),
          elapsed_ms: Math.round(tt - stagger), detail: d });
        const fire = () => { const e = ev(); sc.pipeline[page.source_id] = e; this.emit("pipeline", e); };
        if (due <= 0) fire(); else setTimeout(fire, SIM_FAST ? 0 : due);
      }
      if (page.kind === "official") {
        const due = delayMs + stagger + 19000 - startOffsetMs;
        const fire = () => this.emit("context_check", { source_id: page.source_id, source_name: page.name, incident_id: sc.incident_id,
          status: "changed", title: sc.steps[Math.max(0, sc.current_step)]?.title || "Page updated", summary: "Semantic monitor run after the change.",
          importance: "high", confidence: 0.95, latency_ms: 18200, elapsed_ms: 19000, credits: 1, at: this.iso(), pipeline_new_claims: 5, disagreement: false });
        if (due <= 0) fire(); else setTimeout(fire, SIM_FAST ? 0 : due);
      }
    });
  },
  apply(sc, to, delayMs = 0) {
    sc.current_step = to;
    this.emit("scenario", structuredClone(sc));
    this.emit("activity", { at: this.iso(), service: "scenario", action: "step", target: sc.id, latency_ms: null, credits: null, cache: null,
      ok: true, detail: `step ${to}: ${sc.steps[to].title}` });
    this.play(sc, sc.steps[to].changes || [], 0, delayMs);
  },
  async step(id, body) {
    mockAuth();
    await this.load();
    const sc = this.find(id);
    const to = body.next ? Math.min(sc.current_step + 1, sc.steps.length - 1) : body.to;
    this.apply(sc, to);
    return structuredClone(sc);
  },
  async reset(id) {
    mockAuth();
    await this.load();
    const sc = this.find(id);
    Object.assign(sc, { current_step: -1, autoplay: { ...sc.autoplay, running: false }, pipeline: {} });
    this.emit("scenario", structuredClone(sc));
    return structuredClone(sc);
  },
  async autoplay(id, body) {
    mockAuth();
    await this.load();
    const sc = this.find(id);
    sc.autoplay = { running: !!body.running, interval_s: body.interval_s ?? sc.autoplay.interval_s };
    this.emit("scenario", structuredClone(sc));
    return structuredClone(sc);
  },
  async bulk({ action, ids = [], interval_s = 60, stagger_s = 5 }) {
    mockAuth();
    await this.load();
    ids.forEach((id, i) => {
      const sc = this.find(id);
      if (!sc) return;
      const delay = i * stagger_s * 1000;
      if (action === "start" && sc.current_step < 0) this.apply(sc, 0, delay);
      else if (action === "next" && sc.current_step < sc.steps.length - 1) this.apply(sc, sc.current_step + 1, delay);
      else if (action === "reset") Object.assign(sc, { current_step: -1, autoplay: { ...sc.autoplay, running: false }, pipeline: {} });
      else if (action === "autoplay_on") sc.autoplay = { running: true, interval_s };
      else if (action === "autoplay_off") sc.autoplay = { ...sc.autoplay, running: false };
      if (action !== "start" && action !== "next") this.emit("scenario", structuredClone(sc));
    });
    return { scenarios: structuredClone(this.list) };
  },
  // On load: Halvik's current step is mid-run (started ~15 s ago) and a Context.dev verdict lands a moment later.
  async boot() {
    await this.load();
    const h = this.find("halvik");
    if (h && h.current_step >= 0) this.play(h, h.steps[h.current_step].changes || [], 15000);
  },
};
