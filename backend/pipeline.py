"""change detected -> Context.dev evidence -> claims diff -> LLM grouping -> rule-based verdicts -> feed + alerts.

Runs on the asyncio loop; blocking calls (Context.dev, LLM) go to worker threads and their results are
applied back on the loop, so the store is only ever mutated from one thread.
"""

from __future__ import annotations

import asyncio
import difflib
import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import fmi, grouping, rules
from .config import (CONTEXT_MONITOR_KINDS, CONTEXT_MONITORS, CONTEXT_REPLAY_DIR, CREDIT_BUDGET, FAST_UPDATES,
                     SOURCES_BY_ID, Source, site_public_url)
from . import images
from .extraction import extract_source, extract_update, ground, normalize
from .monitors import Monitor
from .grouping import Group
from .store import Incident, Item, Store, now, source_meta

BATCH_MAX_WAIT_S = 4.0      # when several pages of one incident change together, group their claims in one LLM call


def claim_key(c: dict) -> str:
    """Stable identity of a claim across re-extractions of the same page (quotes are verbatim, wording is not)."""
    author = (c.get("_post") or {}).get("author_handle", "")
    return f"{normalize(c.get('evidence_quote', ''))}|{c['condition']}|{c['stance']}|{author}"


def same_claim(new: dict, old: dict) -> bool:
    """Is a freshly extracted claim one we already hold for this source? (re-extractions word claims differently)"""
    if new["stance"] != old["stance"]:
        return False
    if (new.get("_post") or {}).get("author_handle") != (old.get("_post") or {}).get("author_handle"):
        return False
    a, b = normalize(new.get("evidence_quote", "")), normalize(old.get("evidence_quote", ""))
    if not a or not b:
        return False
    if not (a == b or a in b or b in a or difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.8):
        return False
    # One sentence can carry several claims ("stay away from the shore, the piers and the bridge"): the subject
    # must match too. The condition is ignored on purpose: re-extractions sometimes classify the same fact differently.
    x, y = rules.subject_key(new.get("subject", "")), rules.subject_key(old.get("subject", ""))
    return bool(x and y) and (x == y or x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= 0.7)


@dataclass
class Capture:
    """One source's fresh evidence, waiting to be merged into its incident."""
    src: Source
    content_hash: str
    t0: float
    generation: int
    claims: list = field(default_factory=list)
    markdown: str = ""
    mode: str = "full"          # full = Context.dev JSON extraction; fast = Context.dev markdown + LLM on the diff
    calm: bool = False


def seen_text(c: dict, first: bool) -> str:
    who = c["source_name"] + (f" (@{c['_post']['author_handle']})" if c.get("_post") else "")
    quoting = f", quoting {c['attributed_to']}" if c.get("attributed_to") else ""
    verb = {"asserts": "states it", "reports_unverified": "relays it as unverified", "denies": "says it is false"}[c["stance"]]
    return f"{'First seen: ' if first else ''}{who} {verb}{quoting}"


class Pipeline:
    def __init__(self, store: Store, context_client, llm, context_monitors=None):
        self.store, self.ctx, self.llm = store, context_client, llm      # llm: backend.llm.make() or None
        self.context_monitors = context_monitors   # parallel Context.dev "run now" checks (see context_monitors.py)
        self.last_update: dict[str, tuple[float, int]] = {}   # source_id -> (detection time, new claims): in memory only
        self.paused = False                                     # admin "pause processing": no Context.dev / LLM calls
        self.source_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.incident_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.pending: dict[str, tuple[str, float]] = {}
        self.credits_this_run = 0.0
        self.retries: dict[str, int] = {}
        self.latest_stage: dict[str, dict] = {}
        self.replay = bool(CONTEXT_REPLAY_DIR)
        # batching: captures queue per incident and are merged together once no capture for it is still running
        self.inflight: dict[str, int] = defaultdict(int)
        self.queue: dict[str, list] = defaultdict(list)
        self.timers: dict[str, asyncio.TimerHandle] = {}
        self.flushing: set[str] = set()
        self.generation: dict[str, int] = defaultdict(int)     # bumped by a scenario reset; stale captures are dropped

    @property
    def llm_label(self) -> str:
        return self.llm.model if self.llm else "no LLM"

    # ---- monitor callbacks ------------------------------------------------------------------------------------
    def in_incident(self, monitor: Monitor) -> bool:
        src = SOURCES_BY_ID.get(monitor.id)
        inc = self.store.incidents.get(src.incident) if src and src.incident else None
        return bool(inc and inc.status == "active")

    async def on_change(self, monitor: Monitor, change: dict) -> None:
        if monitor.type == "feed":
            self._on_feed(monitor, change)
            return
        src = SOURCES_BY_ID[monitor.id]
        t0 = time.monotonic()                   # the per-source stopwatch: detected -> done
        self._stage(src.id, "detected", t0, "first check" if change.get("first_check") else "visible text changed")
        snap = self.store.snapshots.get(src.id)
        if snap and snap.get("hash") == change["hash"]:
            self._stage(src.id, "skipped", t0, "content already processed")
            return
        if snap:                                # a real update to a page we already know
            incident_id = src.incident if src.incident in self.store.incidents else None
            added = change.get("added") or []   # instant, unverified preview straight from our monitor
            preview = f"New on the page: “{added[0]}”" + (f" (+{len(added) - 1} more)" if len(added) > 1 else "") + " " \
                if added else ""
            self.store.add_alert("potential", f"{src.name} updated",
                                 f"{preview}Verifying through Context.dev…", incident_id=incident_id)
        self._second_opinion(src, t0)
        await self.process(src, change["hash"], t0)

    # ---- parallel Context.dev monitor check (never blocks the pipeline) -------------------------------------------
    def _second_opinion(self, src: Source, t0: float) -> None:
        if (CONTEXT_MONITORS and self.context_monitors is not None and not self.replay and not self.paused
                and src.kind in CONTEXT_MONITOR_KINDS and site_public_url()):
            asyncio.ensure_future(self.context_monitors.check(src, t0, self._on_verdict))

    def _on_verdict(self, src: Source, verdict: dict, t0: float) -> None:
        store = self.store
        verdict["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        credits = verdict.get("credits") or 0
        self.credits_this_run += credits
        store.credits_spent += credits
        # Cross-check with our own pipeline: did it extract new claims from the same update?
        detected, new_claims = self.last_update.get(src.id, (0.0, None))
        ours = new_claims if detected >= t0 - 0.001 else None
        verdict["pipeline_new_claims"] = ours
        verdict["disagreement"] = bool(verdict["status"] == "no_change" and ours)
        store.context_checks.insert(0, verdict)
        del store.context_checks[100:]
        text = {"changed": f"change: {verdict.get('title')} ({verdict.get('importance')}, "
                           f"{(verdict.get('confidence') or 0):.2f})",
                "no_change": "no meaningful change",
                "baseline": "monitor created; baseline captured for future checks"}.get(verdict["status"],
                                                                                      verdict.get("summary") or verdict["status"])
        if verdict["disagreement"]:
            text += f" — but our pipeline extracted {ours} new claims: review"
        store.add_activity({"service": "context.dev", "action": "monitor run (semantic)", "target": src.id,
                            "latency_ms": verdict.get("latency_ms"), "credits": credits, "cache": None,
                            "ok": verdict["status"] in ("changed", "no_change", "baseline") and not verdict["disagreement"],
                            "detail": text})
        store.publish("context_check", verdict)
        store.save()

    def _on_feed(self, monitor: Monitor, change: dict) -> None:
        """Sync official CAP warnings into official_warning incidents (one verified item each). No Context.dev."""
        store, seen_at, live = self.store, now(), set()
        for w in change["warnings"]:
            inc_id = fmi.incident_id(w["key"])
            live.add(inc_id)
            label = ", ".join(w["areas"])
            label = label if len(label) <= 80 else label[:77] + "…"
            area = {**fmi.area_geometry(w["polygons"]), "label": label, "polygons": w["polygons"]}
            source = {"id": monitor.id, "name": "Finnish Meteorological Institute", "kind": "official", "url": w["web"]}
            fields = dict(name=w["headline"].split(":")[0].strip() or w["event"], hazards=[fmi.hazard_for(w["event"])],
                          area=area, kind="official_warning", valid={"from": w["onset"], "to": w["expires"]},
                          source_label="Finnish Meteorological Institute (CAP feed)", source=source,
                          summary_text=" ".join(x for x in (w["description"], w["instruction"]) if x))
            inc = store.incidents.get(inc_id)
            created = inc is None
            if created:
                inc = store.incidents[inc_id] = Incident(id=inc_id, **fields)
            else:
                for k, v in fields.items():
                    setattr(inc, k, v)
                inc.status = "active"

            member = {"source_id": monitor.id, "source_name": source["name"], "source_kind": "official",
                      "source_url": w["web"], "stance": "asserts", "attributed_to": None,
                      "evidence_quote": fields["summary_text"] or w["headline"], "proposition": w["headline"],
                      "subject": label, "condition": "warning_issued", "category": "warning",
                      "hazard": fmi.hazard_for(w["event"]), "location": label, "safety_effective": "protective",
                      "seen_at": seen_at, "cap_identifier": w["identifier"]}
            item_id = f"{inc_id}--W1"
            it = store.items.get(item_id)
            changed = created
            if it is None:
                it = store.items[item_id] = Item(id=item_id, incident_id=inc_id, first_seen=seen_at,
                                                 group=Group("W1", "warning_issued", label, w["headline"], [member]))
                it.log.append({"at": seen_at, "kind": "seen", "text": "First seen: Finnish Meteorological Institute issues it"})
                changed = True
            elif it.group.members[0].get("cap_identifier") != w["identifier"]:
                it.group.members, it.group.label, it.group.subject = [member], w["headline"], label
                it.log.append({"at": seen_at, "kind": "seen", "text": "Finnish Meteorological Institute updated the warning"})
                changed = True
            if changed:
                transition = it.reassess()
                if transition:
                    it.log.append({"at": seen_at, "kind": "status", "text": transition})
                it.updated_at = inc.updated_at = seen_at
                store.publish("item", it.to_api())
                store.publish("incident", store.incident_view(inc))
                if created and w["colour"] in ("orange", "red"):
                    store.add_alert("critical" if w["colour"] == "red" else "info",
                                    f"FMI: {w['headline']}", fields["summary_text"], inc_id, item_id)

        for inc in store.incidents.values():           # warnings that left the feed (cancelled / expired) end
            if inc.kind == "official_warning" and inc.status == "active" and inc.id not in live:
                inc.status, inc.updated_at = "ended", seen_at
                store.publish("incident", store.incident_view(inc))
        store.save()

    # ---- processing ---------------------------------------------------------------------------------------
    def _stage(self, source_id: str, stage: str, t0: float, detail: str = "") -> None:
        """SSE `pipeline` event; elapsed_ms counts from detection so the admin page can show a stopwatch."""
        elapsed = round((time.monotonic() - t0) * 1000)
        src = SOURCES_BY_ID.get(source_id)
        event = {"source_id": source_id, "source_name": src.name if src else source_id,
                 "incident_id": src.incident if src else None,
                 "stage": stage, "at": now(), "elapsed_ms": elapsed, "detail": detail}
        self.latest_stage[source_id] = event     # so an admin page opened mid-run can show the current stage
        self.store.publish("pipeline", event)
        if stage in ("failed", "skipped", "done"):
            self.store.add_activity({"service": "pipeline", "action": stage, "target": source_id, "latency_ms": elapsed,
                                     "credits": None, "cache": None, "ok": stage != "failed", "detail": detail})

    async def process(self, src: Source, content_hash: str, t0: float | None = None) -> None:
        t0 = t0 or time.monotonic()
        lock = self.source_locks[src.id]
        if lock.locked():                       # coalesce: re-run once the current pass finishes
            self.pending[src.id] = (content_hash, t0)
            return
        async with lock:
            while content_hash:
                try:
                    await self._run(src, content_hash, t0)
                except Exception as exc:        # never let one bad pass kill the monitor callback
                    self._stage(src.id, "failed", t0, f"{type(exc).__name__}: {exc}"[:200])
                content_hash, t0 = self.pending.pop(src.id, (None, None))

    async def _run(self, src: Source, content_hash: str, t0: float) -> None:
        """Capture (in parallel with other sources), then wait for the incident's batched merge."""
        key = src.incident
        self.inflight[key] += 1
        try:
            cap = await self._capture(src, content_hash, t0)
        finally:
            self.inflight[key] -= 1
        if cap is None:
            self._maybe_flush(key)
            return
        fut = asyncio.get_running_loop().create_future()
        self.queue[key].append((cap, fut))
        self._maybe_flush(key)
        await fut

    # ---- 1. evidence --------------------------------------------------------------------------------------
    def _log_call(self, call, action: str, detail: str) -> None:
        credits = call.credits_consumed or 0
        self.credits_this_run += credits
        self.store.credits_spent += credits
        self.store.add_activity({"service": "context.dev", "action": action, "target": call.target,
                                 "latency_ms": call.latency_ms, "credits": call.credits_consumed,
                                 "cache": call.cache_status, "ok": call.http_status == 200 and not call.error,
                                 "detail": call.error or detail})

    async def _capture(self, src: Source, content_hash: str, t0: float) -> Capture | None:
        store, gen = self.store, self.generation[src.incident]
        if self.paused:                     # e.g. a retry that fires while paused; resume re-checks every page
            self._stage(src.id, "skipped", t0, "processing is paused")
            return None
        if not self.replay:
            if not site_public_url() and not src.path.startswith("http"):
                self._stage(src.id, "skipped", t0,
                            "no public URL (run ./serve.sh or set SITE_PUBLIC_URL): Context.dev cannot reach the page")
                return None
            if self.credits_this_run >= CREDIT_BUDGET:
                self._stage(src.id, "skipped", t0, f"credit budget of {CREDIT_BUDGET:g} reached")
                return None

        snap = store.snapshots.get(src.id) or {}
        if FAST_UPDATES and self.llm is not None and not self.replay and snap.get("markdown"):
            self._stage(src.id, "extracting", t0, f"Context.dev scrape (markdown) + {self.llm.model} on the changed lines")
            upd = await asyncio.to_thread(extract_update, self.ctx, self.llm, src.public_url, src.shape,
                                          snap["markdown"], step=f"update:{src.id}")
            for call in upd.calls:
                self._log_call(call, "scrape (markdown)", f"{len(upd.added_lines)} changed lines")
            if upd.llm_latency_ms:
                store.add_activity({"service": self.llm.provider, "action": "extract update", "target": src.id,
                                    "latency_ms": upd.llm_latency_ms, "credits": None, "cache": None,
                                    "ok": upd.ok, "detail": upd.error or f"{upd.llm_model} · {len(upd.claims)} new claims"})
            if upd.ok:
                self.retries.pop(src.id, None)
                self._stage(src.id, "extracted", t0, f"{len(upd.added_lines)} changed lines → {len(upd.claims)} new claims (fast)")
                return Capture(src, content_hash, t0, gen, upd.claims, upd.markdown, "fast")
            if not upd.needs_full:
                retrying = self._retry_later(src, content_hash)
                self._stage(src.id, "failed", t0, f"{upd.error}; " + ("retrying in 30 s" if retrying else "giving up for now"))
                return None
            # otherwise: fall through to a full Context.dev extraction (big change or LLM trouble)

        self._stage(src.id, "extracting", t0, f"Context.dev scrape + JSON extraction of {src.public_url}")
        ext = await asyncio.to_thread(extract_source, self.ctx, src.public_url, src.shape, step=f"extract:{src.id}")
        for call in ext.calls:
            self._log_call(call, "scrape+extract", f"{len(ext.claims)} claims" if ext.ok else (ext.error or ""))
        if not ext.ok:
            retrying = self._retry_later(src, content_hash)
            self._stage(src.id, "failed", t0, f"{ext.error}; " + ("retrying in 30 s" if retrying else "giving up for now"))
            return None
        self.retries.pop(src.id, None)
        markdown = (((ext.raw or {}).get("markdown") or {}).get("data")) or ""
        self._stage(src.id, "extracted", t0, f"{len(ext.claims)} claims (full)" + (", calm page" if ext.calm else ""))
        return Capture(src, content_hash, t0, gen, ext.claims, markdown, "full", ext.calm)

    # ---- 2. batching ----------------------------------------------------------------------------------------
    def _maybe_flush(self, key: str) -> None:
        if not self.queue.get(key):
            return
        if self.inflight[key] == 0:
            self._start_flush(key)
        elif key not in self.timers:            # don't wait forever on a slow capture
            self.timers[key] = asyncio.get_running_loop().call_later(BATCH_MAX_WAIT_S, self._start_flush, key)

    def _start_flush(self, key: str) -> None:
        if key not in self.flushing:            # a running flush re-checks the queue when it finishes
            self.flushing.add(key)
            asyncio.ensure_future(self._flush(key))

    async def _flush(self, key: str) -> None:
        try:
            async with self.incident_locks[key]:
                timer = self.timers.pop(key, None)
                if timer:
                    timer.cancel()
                batch, self.queue[key] = self.queue[key], []
                latest: dict[str, tuple] = {}
                for cap, fut in batch:          # if a source changed twice, its newest capture wins
                    if cap.src.id in latest:
                        latest[cap.src.id][1].set_result(None)
                        self._stage(cap.src.id, "skipped", latest[cap.src.id][0].t0, "superseded by a newer capture")
                    latest[cap.src.id] = (cap, fut)
                caps = [c for c, _ in latest.values() if c.generation == self.generation[key]]
                for c, _ in latest.values():
                    if c.generation != self.generation[key]:
                        self._stage(c.src.id, "skipped", c.t0, "incident was reset during processing")
                try:
                    if caps:
                        await self._merge(key, caps)
                except Exception as exc:
                    for c in caps:
                        self._stage(c.src.id, "failed", c.t0, f"{type(exc).__name__}: {exc}"[:200])
                finally:
                    for _, fut in latest.values():
                        if not fut.done():
                            fut.set_result(None)
        finally:
            self.flushing.discard(key)
            self._maybe_flush(key)

    # ---- 3. merge: diff, group, verdicts, alerts --------------------------------------------------------------
    async def _merge(self, key: str, caps: list[Capture]) -> None:
        store, seen_at = self.store, now()
        names = ", ".join(c.src.name for c in caps)
        for cap in caps:
            n = store.snapshots.get(cap.src.id, {}).get("n", 0) + 1
            cap.n = n
            for i, c in enumerate(cap.claims, 1):
                c.update(source_meta(cap.src.id))
                c.update(seen_at=seen_at, claim_id=f"{cap.src.id}:{n}:{i}")
                c["key"] = claim_key(c)

        def record_snapshots(new_counts: dict | None = None):
            for cap in caps:
                store.snapshots[cap.src.id] = {"hash": cap.content_hash, "markdown": cap.markdown, "n": cap.n,
                                               "processed_at": seen_at, "url": cap.src.public_url,
                                               "images": images.extract(cap.markdown, cap.src.public_url, site_public_url())}
                self.last_update[cap.src.id] = (cap.t0, (new_counts or {}).get(cap.src.id, len(cap.claims)))

        # Only open an incident once a source asserts or relays something concrete (a calm page, or an official
        # "there are no warnings", must not create one).
        incident = store.incidents.get(key)
        if incident is None:
            if not any(c["stance"] != "denies" and c["condition"] != "other" for cap in caps for c in cap.claims):
                record_snapshots()
                store.save()
                for cap in caps:
                    self._stage(cap.src.id, "done", cap.t0, "no emergency claims: no incident opened")
                return
        incident, created = store.ensure_incident(key)
        alerts: list[tuple[str, str, str, str | None]] = []
        if created:
            alerts.append(("info", f"New incident: {incident.name}", f"First evidence from {names}.", None))

        # diff each source against what we already hold (rules: withdrawn = quote no longer on the page)
        touched: set[str] = set()
        fresh_all, counts = [], {}
        for cap in caps:
            src, page = cap.src, normalize(cap.markdown)
            held = [(it, m) for it in store.incident_items(incident.id) for m in it.group.members if m["source_id"] == src.id]
            gone_ids = set()
            if src.shape != "feed" and page:    # feeds: old posts scrolling off "latest" are not withdrawals
                gone_ids = {id(m) for _, m in held if ground(m.get("evidence_quote", ""), page)[0] == "MISSING"}
            for it in {id(it): it for it, m in held if id(m) in gone_ids}.values():
                it.group.members = [m for m in it.group.members if id(m) not in gone_ids]
                it.log.append({"at": seen_at, "kind": "removed", "text": f"{src.name} no longer states this"})
                touched.add(it.id)
            remaining = [m for _, m in held if id(m) not in gone_ids]
            fresh = []
            for c in cap.claims:
                if not any(same_claim(c, m) for m in remaining + fresh):
                    fresh.append(c)
            fresh_all += fresh
            counts[src.id] = (len(cap.claims), len(fresh), len(gone_ids))

        ok, bad = grouping.split_quarantine(fresh_all)
        store.quarantine = (store.quarantine + bad)[-200:]
        if ok:
            batch_note = f" (one call for {names})" if len(caps) > 1 else ""
            for cap in caps:
                self._stage(cap.src.id, "grouping", cap.t0, f"{len(ok)} new claims → {self.llm_label}{batch_note}")
            groups = {it.group.id: it.group for it in store.incident_items(incident.id)}
            if self.llm is None:
                proposal = grouping.Proposal(None, error="no LLM key set (GEMINI_API_KEY or ANTHROPIC_API_KEY)")
            else:
                proposal = await asyncio.to_thread(grouping.propose, self.llm, groups, ok,
                                                   "+".join(c.src.id for c in caps))
            store.add_activity({"service": self.llm.provider if self.llm else "llm", "action": "group claims", "target": incident.id,
                                "latency_ms": proposal.latency_ms, "credits": None, "cache": None,
                                "ok": proposal.error is None,
                                "detail": proposal.error or f"{proposal.model} · {len(ok)} claims from {len(caps)} source(s)"})
            placements, violations = grouping.apply(groups, ok, proposal.result)
            new_items = 0
            for claim, gid in placements:
                item_id = f"{incident.id}--{gid}"
                it = store.items.get(item_id)
                if it is None:
                    it = store.items[item_id] = Item(id=item_id, incident_id=incident.id, group=groups[gid], first_seen=seen_at)
                    new_items += 1
                it.log.append({"at": seen_at, "kind": "seen", "text": seen_text(claim, first=not it.log)})
                touched.add(item_id)
            detail = (f"{len(ok)} claims → {len({gid for _, gid in placements})} groups, {new_items} new"
                      + (f", {len(violations)} rule overrides" if violations else "")
                      + (f" (LLM unavailable: {proposal.error}; one group per claim)" if proposal.error else ""))
            for cap in caps:
                self._stage(cap.src.id, "grouped", cap.t0, detail)

        # rule-based verdicts
        transitions, verified, debunked = 0, [], []
        for item_id in sorted(touched):
            it = store.items[item_id]
            transition = it.reassess()
            it.updated_at = seen_at
            if transition:
                transitions += 1
                it.log.append({"at": seen_at, "kind": "status", "text": transition})
                if it.status == "verified" and it.severity >= 4:
                    verified.append(it)
                elif it.status == "contradicted" and any(m["stance"] != "denies" for m in it.group.members):
                    debunked.append(it)
            store.publish("item", it.to_api())

        # alerts: one per kind per batch, so a single update doesn't fire a stack of banners
        for level, prefix, items in (("critical", "Verified", verified), ("debunk", "False", debunked)):
            if items:
                first = items[0]
                title = f"{prefix}: {first.title().rstrip('.')}" + (f" (+{len(items) - 1} more)" if len(items) > 1 else "")
                body = first.summary + ("" if len(items) == 1 else
                                        " Also: " + "; ".join(i.title().rstrip(".") for i in items[1:4]) + ".")
                alerts.append((level, title, body, first.id))
        had_snapshot = any(cap.n > 1 for cap in caps)
        if not verified and not debunked and not created and had_snapshot:
            new_n = sum(v[1] for v in counts.values())
            gone_n = sum(v[2] for v in counts.values())
            alerts.append(("info", f"{names}: update checked",
                           f"{new_n} new claims, {gone_n} withdrawn. No change to verified facts.", None))
        for level, title, body, item_id in alerts:
            store.add_alert(level, title, body, incident.id, item_id)

        record_snapshots({sid: v[1] for sid, v in counts.items()})
        incident.checked_at = seen_at
        if touched or created:              # "updated" means something visible changed, not just a re-check
            incident.updated_at = seen_at
        store.publish("incident", store.incident_view(incident))
        store.save()
        for cap in caps:
            total, new, gone = counts[cap.src.id]
            self._stage(cap.src.id, "done", cap.t0,
                        f"{cap.mode}: {new} new / {gone} withdrawn of {total} claims · {transitions} status changes, "
                        f"{len(alerts)} alerts" + (f" · batched with {len(caps) - 1} other source(s)" if len(caps) > 1 else ""))

    def _retry_later(self, src: Source, content_hash: str, delay_s: float = 30, max_tries: int = 3) -> bool:
        """A failed extraction is retried a few times; the last good snapshot stays in place meanwhile."""
        n = self.retries.get(src.id, 0) + 1
        if n > max_tries:
            self.retries.pop(src.id, None)
            return False
        self.retries[src.id] = n
        loop = asyncio.get_running_loop()
        loop.call_later(delay_s, lambda: asyncio.ensure_future(self.process(src, content_hash)))
        return True

    def forget_source(self, source_id: str) -> None:
        """Drop queued work and retries for a source (scenario reset); in-flight captures become stale."""
        self.pending.pop(source_id, None)
        self.retries.pop(source_id, None)
        src = SOURCES_BY_ID.get(source_id)
        if src and src.incident:
            self.generation[src.incident] += 1
