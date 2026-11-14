// Live update feedback, shared by the home, incident and admin pages:
//  - ONE toast slot: "Update from <source> · checking with Context.dev…" with a running stopwatch, turning into a result
//    ("Checked in 9.4 s · 2 newly verified · 1 FALSE · 3 new reports"). A newer update replaces the toast (cross-fade).
//    Toasts only for incidents in the reader's focus (opts.inFocus); everything else goes silently to the bell.
//  - the bell: every finished update and Context.dev verdict, newest first (sessionStorage), unread badge, quiet mode;
//  - classification of item events against what the page already knew (newly verified / FALSE / held / ...);
//  - "(n) " tab-title counter while the tab is hidden.
import * as api from "./api.js";
import { esc, icon, statusChip, ago, lsGet, lsSet } from "./ui.js";

const TERMINAL = new Set(["done", "failed", "skipped"]);
const RECENT_MS = 120000;                         // home page: an unseen item counts as new if first seen < 2 min ago
const STAGE_TEXT = {
  detected: "change detected", extracting: "checking with Context.dev…", extracted: "claims extracted",
  grouping: "grouping claims…", grouped: "updating statuses…", done: "done", failed: "failed", skipped: "skipped",
};
const PRIORITY = { false: 0, verified: 1, held: 2, changed: 3, new: 4, withdrew: 5 };
const KIND_TEXT = {
  verified: ["newly verified", "newly verified"], false: ["FALSE", "FALSE"], held: ["held back", "held back"],
  changed: ["status change", "status changes"], new: ["new report", "new reports"], withdrew: ["source withdrew", "sources withdrew"],
};
const STATUS_WORD = { verified: "Verified", corroborated: "Corroborated", unverified: "Unverified", disputed: "Disputed", contradicted: "FALSE" };
const ACCENT_COLOR = { critical: "#EF4444", ok: "#10B981", authority: "#38BDF8", triage: "#F59E0B", severe: "#F97316", line: "#64748B" };

export const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// Ribbon text for an in-place highlight: "NEW", "Unverified → FALSE", ...
export function ribbonText(ch) {
  if (!ch) return "";
  if (ch.isNew) return ch.kind === "false" ? "NEW · FALSE" : ch.kind === "verified" ? "NEW · VERIFIED" : "NEW";
  if (ch.kind === "held") return "HELD BACK";
  if (ch.kind === "withdrew") return "SOURCE WITHDREW";
  return `${STATUS_WORD[ch.from] || ch.from || "?"} → ${STATUS_WORD[ch.to] || ch.to}`;
}

// CSS animation-delay that continues an animation across re-renders (negative = already running for that long).
export const animDelay = (sinceMs) => `animation-delay:-${Math.max(0, sinceMs) / 1000}s`;

const fmtSecs = (ms) => `${(ms / 1000).toFixed(1)} s`;
const isoNow = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

// =====================================================================================================
// Bell: one per page (singleton), mounted next to the header's live indicator.
// =====================================================================================================
const BELL_KEY = "fg-bell";
const BELL_READ_KEY = "fg-bell-read";
const QUIET_KEY = "fg-quiet";
const ss = {
  get(k, d) { try { const v = sessionStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
};

const bell = {
  entries: ss.get(BELL_KEY, []),
  readAt: ss.get(BELL_READ_KEY, ""),
  quiet: lsGet(QUIET_KEY, false) === true,
  mounted: false,
  open: false,

  unread() { return this.entries.filter((e) => e.at > this.readAt).length; },
  add(entry) {
    this.entries = [{ id: `${entry.incident_id || "x"}:${Date.now()}:${Math.random().toString(36).slice(2, 6)}`, at: isoNow(), ...entry }, ...this.entries].slice(0, 50);
    ss.set(BELL_KEY, this.entries);
    this.render();
  },
  markRead() {
    this.readAt = isoNow();
    ss.set(BELL_READ_KEY, this.readAt);
    this.render();
  },
  setQuiet(q) {
    this.quiet = q;
    lsSet(QUIET_KEY, q);
    if (q) document.getElementById("fg-toasts")?.replaceChildren();
    this.render();
  },

  mount() {
    if (this.mounted) return;
    const dot = document.getElementById("live-dot");
    if (!dot) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "fg-bell";
    btn.className = "bell-btn";
    btn.setAttribute("aria-haspopup", "dialog");
    btn.setAttribute("aria-expanded", "false");
    btn.onclick = (e) => { e.stopPropagation(); this.toggle(); };
    dot.parentElement.insertBefore(btn, dot);
    const panel = document.createElement("div");
    panel.id = "fg-bell-panel";
    panel.className = "bell-panel hidden";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Updates");
    panel.onclick = (e) => e.stopPropagation();
    document.body.appendChild(panel);
    document.addEventListener("click", () => this.close());
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") this.close(); });
    this.mounted = true;
    if (api.MOCK && !this.entries.length) this.seedMock();
    this.render();
    if (new URLSearchParams(location.search).get("bell") === "1") this.toggle();   // dev/screenshot aid
  },
  toggle() { this.open ? this.close() : this.show(); },
  show() {
    this.open = true;
    this.render();
    this.markRead();                                          // opening the panel clears the badge
  },
  close() {
    if (!this.open) return;
    this.open = false;
    this.render();
  },

  render() {
    if (!this.mounted) return;
    const btn = document.getElementById("fg-bell");
    const n = this.unread();
    btn.setAttribute("aria-expanded", String(this.open));
    btn.setAttribute("aria-label", `Updates${n ? `, ${n} unread` : ""}${this.quiet ? ", quiet mode" : ""}`);
    btn.innerHTML = `${icon(this.quiet ? "notifications_off" : n ? "notifications_active" : "notifications", "text-[20px]")}${n ? `<span class="bell-badge tnum">${n > 99 ? "99+" : n}</span>` : ""}`;
    const panel = document.getElementById("fg-bell-panel");
    panel.classList.toggle("hidden", !this.open);
    if (!this.open) return;
    panel.innerHTML = `
      <div class="flex items-center justify-between gap-2 px-3 py-2.5 border-b border-line">
        <span class="font-bold flex items-center gap-2">${icon("notifications", "text-authority")}Updates</span>
        <button type="button" class="btn !min-h-[28px] !px-2 !text-xs" data-markread>${icon("done_all", "text-[15px]")}Mark all read</button>
      </div>
      <label class="flex items-center justify-between gap-3 px-3 py-2 border-b border-line cursor-pointer">
        <span class="text-sm"><b>Quiet mode</b><span class="block text-xs text-muted">No pop-ups; updates only collect here.</span></span>
        <input type="checkbox" class="bell-switch" data-quiet ${this.quiet ? "checked" : ""} aria-label="Quiet mode">
      </label>
      <ol class="bell-list">
        ${this.entries.map((e) => `
        <li><a href="${esc(e.href || "#")}" class="flex items-start gap-2.5 px-3 py-2.5 hover:bg-card ${e.at > this.readAt ? "bg-authority/5" : ""}">
          <span class="mt-1.5 w-2 h-2 rounded-full shrink-0" style="background:${ACCENT_COLOR[e.accent] || ACCENT_COLOR.line}"></span>
          <span class="min-w-0 flex-1">
            <span class="flex items-center justify-between gap-2"><span class="label truncate">${esc(e.name || "")}</span>${ago(e.at, "mono text-[10.5px] text-faint shrink-0")}</span>
            <span class="block text-sm leading-snug">${esc(e.text)}</span>
            ${e.sub ? `<span class="block mono text-[10.5px] text-faint truncate">${esc(e.sub)}</span>` : ""}
          </span>
        </a></li>`).join("") || `<li class="px-3 py-6 text-sm text-muted text-center">No updates yet. Finished checks and Context.dev verdicts appear here.</li>`}
      </ol>`;
    panel.querySelector("[data-markread]").onclick = () => this.markRead();
    panel.querySelector("[data-quiet]").onchange = (e) => this.setQuiet(e.target.checked);
  },

  seedMock() {
    const t = (s) => new Date(Date.now() - s * 1000).toISOString().replace(/\.\d+Z$/, "Z");
    const link = (id) => api.link(`incident.html?id=${encodeURIComponent(id)}`);
    this.entries = [
      { id: "m1", incident_id: "halvik-storm-surge", name: "Halvik storm surge", text: "Checked in 9.4 s · 1 newly verified · 1 FALSE", sub: "Halvik Rescue Services, The Halvik Herald", accent: "critical", at: t(95), href: link("halvik-storm-surge") },
      { id: "m2", incident_id: "halvik-storm-surge", name: "Halvik storm surge", text: "Context.dev monitor: Saltmarsh evacuation ordered · high importance (24.9 s)", sub: "Halvik Rescue Services", accent: "authority", at: t(80), href: link("halvik-storm-surge") },
      { id: "m3", incident_id: "varnes-river-flood", name: "Varnes river flooding", text: "Checked in 7.1 s · no change to verified facts", sub: "Varnes Board", accent: "line", at: t(400), href: link("varnes-river-flood") },
    ];
    this.readAt = t(200);                                    // two unread
  },
};

// =====================================================================================================
export function createLive(opts = {}) {
  const o = {
    getIncident: () => null,                      // id → incident summary (for name/kind), if the page knows it
    inFocus: () => true,                          // id → may this incident show a toast? (else: bell only)
    toasts: false,                                // pop-up cards (off: the board shows one brief "latest" line instead)
    bell: false,                                  // the bell with an update history (off: missed updates just go)
    onUpdate: null,                               // ({name, headline, more, accent, href, at}) for each meaningful update
    graceMs: api.SIM_FAST ? 0 : 2000,             // keep collecting items this long after the last source is done
    autoDismissMs: 8000,
    ...opts,
  };
  const known = new Map();                        // item_id → {status, held, logLen}
  const seededIncidents = new Set();              // incidents whose full item list the page loaded
  const seenIncidents = new Set();
  const runs = new Map();                         // incident_id → active run
  let hiddenCount = 0;
  let baseTitle = document.title;
  if (o.bell) bell.mount();

  // ------------------------------------------------------------ seeding + classification
  function seed(items, incidentId) {
    for (const it of items || []) known.set(it.id, { status: it.status, held: !!it.held, logLen: (it.log || []).length });
    if (incidentId) seededIncidents.add(incidentId);
  }
  function seedIncidents(list) { for (const inc of list || []) seenIncidents.add(inc.id); }

  const recent = (iso) => iso && Date.now() - Date.parse(iso) < RECENT_MS;
  const statusKind = (status) => (status === "verified" ? "verified" : status === "contradicted" ? "false" : "changed");
  const newKind = (it) => (it.status === "verified" ? "verified" : it.status === "contradicted" ? "false" : it.held ? "held" : "new");

  function classify(it) {
    const prev = known.get(it.id);
    const log = it.log || [];
    let ch = null;
    if (prev) {
      if (prev.status !== it.status) ch = { kind: statusKind(it.status), from: prev.status };
      else if (!prev.held && it.held) ch = { kind: "held" };
      else if (log.length > prev.logLen && log.slice(prev.logLen).some((e) => e.kind === "removed")) ch = { kind: "withdrew" };
    } else if (seededIncidents.has(it.incident_id) || recent(it.first_seen)) {
      ch = { kind: newKind(it), isNew: true };
    } else {
      // No baseline (home page): read what changed from recent log entries.
      const st = [...log].reverse().find((e) => e.kind === "status" && recent(e.at));
      if (st) {
        const m = String(st.text || "").match(/(\w+)\s*→\s*(\w+)/);
        ch = { kind: statusKind(it.status), from: m ? m[1] : null };
      } else if (log.some((e) => e.kind === "removed" && recent(e.at))) ch = { kind: "withdrew" };
      else if (it.held && log.some((e) => e.kind === "held" && recent(e.at))) ch = { kind: "held" };
    }
    known.set(it.id, { status: it.status, held: !!it.held, logLen: log.length });
    if (!ch) return null;
    return { ...ch, to: it.status, item_id: it.id, incident_id: it.incident_id, title: it.title, status: it.status, at: Date.now() };
  }

  // ------------------------------------------------------------ runs
  const isWarningId = (id) => o.getIncident(id)?.kind === "official_warning" || /^fmi-/.test(id || "");
  const incidentName = (id) => o.getIncident(id)?.name || String(id || "").replace(/-/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  const incidentHref = (id, itemId) => api.link(`incident.html?id=${encodeURIComponent(id)}`) + (itemId ? `#${encodeURIComponent(itemId)}` : "");
  const canToast = (id) => o.toasts && !bell.quiet && !!o.inFocus(id);
  const note = (entry) => { if (o.bell) bell.add(entry); };     // the bell, when a page has one
  // One brief line per update: the most important change, in plain words. "No change" and failures are not news.
  const HEADLINE = { false: "Shown false", verified: "Confirmed", held: "Held back, don't act yet", changed: "Updated",
                     new: "New report", withdrew: "Withdrawn by its source" };
  function announce(run, r) {
    if (!o.onUpdate || r.failed || !r.top.length) return;
    const top = r.top[0];
    o.onUpdate({ incident_id: run.id, name: incidentName(run.id), headline: `${HEADLINE[top.kind] || "Updated"}: ${top.title}`,
                 more: run.changes.size - 1, accent: r.accent, href: incidentHref(run.id, top.item_id), at: isoNow() });
  }

  function getRun(id, create = true) {
    let run = runs.get(id);
    if ((!run || run.finished) && create) {
      run = { id, sources: new Map(), changes: new Map(), finished: false, toast: run?.toast || null, timer: null, ctx: null };
      runs.set(id, run);
    }
    return run;
  }

  function onPipeline(ev) {
    const id = ev.incident_id;
    if (!id || isWarningId(id)) return;            // FMI churn never produces "checking" toasts
    const run = getRun(id);
    const src = run.sources.get(ev.source_id) || { name: ev.source_name || ev.source_id };
    Object.assign(src, { name: ev.source_name || src.name, stage: ev.stage, elapsed_ms: ev.elapsed_ms || 0, receivedAt: performance.now(), detail: ev.detail });
    run.sources.set(ev.source_id, src);
    render(run);
    scheduleFinish(run);
  }

  function onItem(it) {
    const ch = classify(it);
    if (ch && !isWarningId(it.incident_id)) {
      const run = getRun(it.incident_id);
      const prev = run.changes.get(ch.item_id);
      if (!prev || PRIORITY[ch.kind] < PRIORITY[prev.kind]) run.changes.set(ch.item_id, ch);
      if (!run.sources.size) render(run);           // items without a pipeline run (e.g. another trigger)
      scheduleFinish(run);
    }
    return ch;
  }

  // New official warnings: only orange/red (severity ≥ 3) are worth a note.
  function onIncident(inc) {
    const isNew = !seenIncidents.has(inc.id);
    seenIncidents.add(inc.id);
    if (isNew && inc.kind === "official_warning" && inc.severity >= 3 && inc.status !== "ended") {
      note({ incident_id: inc.id, name: `New official warning · L${inc.severity}`, text: inc.name, sub: inc.area?.label || "",
        accent: inc.severity >= 4 ? "critical" : "severe", href: incidentHref(inc.id) });
      if (canToast(inc.id)) warningToast(inc);
      o.onUpdate?.({ incident_id: inc.id, name: inc.name, headline: "New official warning", more: 0,
                     accent: inc.severity >= 4 ? "critical" : "severe", href: incidentHref(inc.id), at: isoNow() });
      if (o.bell) bumpTitle(1);
    }
    return isNew;
  }

  // v0.5: Context.dev semantic-monitor verdict (arrives ~7–25 s after a change to an official page).
  function ctxLine(ev) {
    const t = ev.elapsed_ms ?? ev.latency_ms;
    const secs = t != null ? ` (${fmtSecs(t)})` : "";
    if (ev.disagreement) return { amber: true, text: `Context.dev saw no meaningful change, but ${ev.pipeline_new_claims ?? "some"} new claims were extracted · review` };
    if (ev.status === "changed") return { text: `Context.dev monitor: ${ev.title || "meaningful change"} · ${ev.importance || "unknown"} importance${secs}` };
    if (ev.status === "no_change") return { text: `Context.dev monitor: no meaningful change${secs}` };
    if (ev.status === "baseline") return { text: `Context.dev monitor created (baseline)${secs}` };
    if (ev.status === "failed") return { amber: true, text: `Context.dev monitor check failed${secs}` };
    return null;
  }

  function onContextCheck(ev) {
    if (!ev.incident_id || isWarningId(ev.incident_id)) return;
    const line = ctxLine(ev);
    if (!line) return;
    note({ incident_id: ev.incident_id, name: incidentName(ev.incident_id), text: line.text, sub: ev.source_name || ev.source_id,
      accent: line.amber ? "triage" : "authority", href: incidentHref(ev.incident_id) });
    const run = runs.get(ev.incident_id);
    if (run?.toast?.isConnected) {                   // add the second opinion to the toast that is still on screen
      run.ctx = line;
      render(run);
      if (run.finished) armDismiss(run.toast);
      return;
    }
    if (!canToast(ev.incident_id) || slotBusy()) return;
    const el = card(line.amber ? "triage" : "authority", "result", `
        ${icon("travel_explore", "toast-icon text-[20px]")}
        <a class="min-w-0 flex-1 block" href="${incidentHref(ev.incident_id)}">
          <div class="label toast-label truncate">${esc(incidentName(ev.incident_id))} · ${esc(ev.source_name || ev.source_id)}</div>
          <div class="text-sm leading-snug">${esc(line.text)}</div>
        </a>`);
    show(el);
    armDismiss(el);
  }

  function allTerminal(run) {
    return [...run.sources.values()].every((s) => TERMINAL.has(s.stage));
  }

  function scheduleFinish(run) {
    clearTimeout(run.timer);
    if (run.sources.size && !allTerminal(run)) return;
    run.timer = setTimeout(() => finish(run), o.graceMs);
  }

  function finish(run) {
    run.finished = true;
    const r = resultOf(run);
    note({ incident_id: run.id, name: incidentName(run.id), text: r.plain, sub: r.names, accent: r.accent,
      href: incidentHref(run.id, r.topItem) });
    announce(run, r);
    render(run);
    const n = run.changes.size;
    if (n && o.bell) bumpTitle(n);
    if (run.toast?.isConnected) armDismiss(run.toast);
  }

  // ------------------------------------------------------------ toast slot
  function container() {
    let c = document.getElementById("fg-toasts");
    if (!c) {
      c = document.createElement("div");
      c.id = "fg-toasts";
      c.className = "fg-toasts";
      c.setAttribute("aria-live", "polite");
      document.body.appendChild(c);
    }
    return c;
  }
  const current = () => [...container().children].find((el) => !el.classList.contains("toast-out"));
  // A running "checking" toast is not interrupted by a late result or verdict of another incident.
  const slotBusy = () => current()?.dataset.state === "checking";

  // Put a toast in the single slot, cross-fading out whatever was there.
  function show(el) {
    for (const old of [...container().children]) if (old !== el) dismiss(old);
    if (!el.isConnected) container().appendChild(el);
  }

  function card(accent, state, inner) {
    const el = document.createElement("div");
    el.className = "toast-card";
    el.setAttribute("role", "status");
    el.dataset.accent = accent;
    el.dataset.state = state;
    el.innerHTML = `<div class="flex items-start gap-3 p-3 pl-3.5">${inner}
      <button type="button" class="toast-close" aria-label="Dismiss">${icon("close", "text-[18px]")}</button></div>`;
    el.querySelector(".toast-close").onclick = () => dismiss(el);
    return el;
  }

  function elapsed(run) {
    let ms = 0;
    for (const s of run.sources.values()) {
      const v = TERMINAL.has(s.stage) ? s.elapsed_ms : s.elapsed_ms + (performance.now() - s.receivedAt);
      ms = Math.max(ms, v || 0);
    }
    return ms;
  }

  function resultOf(run) {
    const sources = [...run.sources.values()];
    const names = sources.map((s) => s.name).join(", ");
    const failed = sources.some((s) => s.stage === "failed") && !sources.some((s) => s.stage === "done");
    const counts = {};
    for (const ch of run.changes.values()) counts[ch.kind] = (counts[ch.kind] || 0) + 1;
    const parts = Object.keys(PRIORITY).filter((k) => counts[k]).map((k) => `${counts[k]} ${KIND_TEXT[k][counts[k] === 1 ? 0 : 1]}`);
    const top = [...run.changes.values()].sort((a, b) => PRIORITY[a.kind] - PRIORITY[b.kind]);
    if (failed) return { failed, names, accent: "triage", plain: `Couldn't verify the update from ${names}, retrying`, parts, top, topItem: null };
    const took = sources.length ? `Checked in ${fmtSecs(elapsed(run))}` : "Updated";
    const accent = counts.false ? "critical" : counts.verified ? "ok" : parts.length ? "authority" : "line";
    return { failed, names, accent, plain: `${took} · ${parts.length ? parts.join(" · ") : "no change to verified facts"}`, parts, top, topItem: top[0]?.item_id || null };
  }

  function render(run) {
    // Out of focus or quiet: no toast at all (the bell gets the result). A toast already on screen keeps updating.
    if (!run.toast?.isConnected) {
      if (!canToast(run.id)) return;
      if (run.finished && slotBusy()) return;          // don't replace someone else's live "checking" toast with a late result
    }
    const sources = [...run.sources.values()];
    const names = sources.map((s) => s.name).join(", ");
    let accent, iconName, title, sub, lines = "";
    if (!run.finished) {
      const stage = sources.filter((s) => !TERMINAL.has(s.stage)).map((s) => STAGE_TEXT[s.stage] || s.stage)[0] || "finishing…";
      accent = "authority"; iconName = "radar";
      title = sources.length ? `Update from ${esc(names)}` : "New information";
      sub = `<span class="tnum" data-tsw="${esc(run.id)}">${fmtSecs(elapsed(run))}</span><span class="pulse">· ${esc(stage)}</span>`;
    } else {
      const r = resultOf(run);
      accent = r.accent;
      iconName = r.failed ? "sync_problem" : r.parts.length ? "fact_check" : "check_circle";
      title = esc(r.plain);
      sub = r.failed ? `<span>${esc(sources.find((s) => s.stage === "failed")?.detail || "Context.dev call failed")}</span>` : names ? `<span>${esc(names)}</span>` : "";
      lines = r.failed ? "" : r.top.slice(0, 3).map((ch) => `
        <a class="toast-line" href="${incidentHref(ch.incident_id, ch.item_id)}">
          ${ch.kind === "held" ? `<span class="chip chip-held">Held</span>` : statusChip(ch.status)}
          <span class="min-w-0 flex-1 truncate ${ch.status === "contradicted" ? "struck" : ""}">${esc(ch.title)}</span>
        </a>`).join("");
    }
    const inner = `
        ${icon(iconName, `toast-icon text-[22px] ${run.finished ? "" : "pulse"}`)}
        <div class="min-w-0 flex-1">
          <div class="label truncate toast-label">${esc(incidentName(run.id))}</div>
          <div class="toast-title">${title}</div>
          ${sub ? `<div class="mt-0.5 mono text-[11px] text-muted flex flex-wrap gap-x-1.5">${sub}</div>` : ""}
          ${lines ? `<div class="mt-2 flex flex-col gap-1">${lines}</div>` : ""}
          ${run.ctx ? `<div class="mt-2 pt-2 border-t border-line flex items-start gap-1.5 text-[12.5px] leading-snug ${run.ctx.amber ? "text-triage" : "text-authority"}">${icon("travel_explore", "text-[16px] mt-px")}<span>${esc(run.ctx.text)}</span></div>` : ""}
        </div>`;
    let el = run.toast;
    if (!el?.isConnected) {
      el = card(accent, run.finished ? "result" : "checking", inner);
      run.toast = el;
      show(el);
      return;
    }
    el.dataset.accent = accent;
    el.dataset.state = run.finished ? "result" : "checking";
    el.firstElementChild.innerHTML = `${inner}<button type="button" class="toast-close" aria-label="Dismiss">${icon("close", "text-[18px]")}</button>`;
    el.querySelector(".toast-close").onclick = () => dismiss(el);
  }

  function warningToast(inc) {
    if (slotBusy()) return;
    const el = card(inc.severity >= 4 ? "critical" : "severe", "result", `
        ${icon("thunderstorm", "toast-icon text-[22px]")}
        <a class="min-w-0 flex-1 block" href="${incidentHref(inc.id)}">
          <div class="label toast-label">New official warning · L${esc(inc.severity)}</div>
          <div class="toast-title">${esc(inc.name)}</div>
          <div class="mono text-[11px] text-muted truncate">${esc(inc.area?.label || "")}</div>
        </a>`);
    show(el);
    armDismiss(el);
  }

  function dismiss(el) {
    if (!el || el.classList.contains("toast-out")) return;
    clearTimeout(el._fgTimer);
    el.classList.add("toast-out");
    setTimeout(() => el.remove(), reducedMotion() ? 0 : 200);
  }

  function armDismiss(el) {
    if (!el) return;
    clearTimeout(el._fgTimer);
    el._fgTimer = setTimeout(() => dismiss(el), o.autoDismissMs);
    el.onmouseenter = () => clearTimeout(el._fgTimer);
    el.onmouseleave = () => { clearTimeout(el._fgTimer); el._fgTimer = setTimeout(() => dismiss(el), o.autoDismissMs); };
  }

  // ------------------------------------------------------------ tab title counter
  function bumpTitle(n) {
    if (!document.hidden) return;
    if (!hiddenCount) baseTitle = document.title.replace(/^\(\d+\)\s*/, "");
    hiddenCount += n;
    document.title = `(${hiddenCount}) ${baseTitle}`;
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && hiddenCount) { hiddenCount = 0; document.title = baseTitle; }
  });

  // stopwatch in the "checking" toast
  setInterval(() => {
    for (const run of runs.values()) {
      if (run.finished || !run.toast?.isConnected) continue;
      const el = run.toast.querySelector(`[data-tsw]`);
      if (el) el.textContent = fmtSecs(elapsed(run));
    }
  }, 100);

  return { seed, seedIncidents, onPipeline, onItem, onIncident, onContextCheck, classify };
}

// =====================================================================================================
// "Paused" chip next to the live indicator (home + incident; admin has its own banner).
// =====================================================================================================
export function setPausedChip(paused, since) {
  const dot = document.getElementById("live-dot");
  if (!dot) return;
  let chip = document.getElementById("fg-paused");
  if (!chip) {
    chip = document.createElement("span");
    chip.id = "fg-paused";
    chip.className = "chip chip-unverified hidden";
    chip.innerHTML = `${icon("pause_circle", "text-[13px]")}Paused`;
    const txt = document.getElementById("live-text");
    (txt || dot).insertAdjacentElement("afterend", chip);
  }
  chip.classList.toggle("hidden", !paused);
  chip.title = paused ? `Processing paused${since ? ` since ${new Date(since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}: no new checks until it resumes` : "";
}
