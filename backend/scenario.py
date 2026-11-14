"""Admin "timestep" control of every fictional scenario: the hand-written Halvik storyline and all data-driven
scenario/<id>/scenario.json files (docs/SCENARIOS.md).

A scenario that hasn't started has no monitors and no incident (JSON scenarios also have no pages). Starting it means
going to step 0: its pages are published into fake_site/, monitors are added for its sources and nudged, and the normal
pipeline runs (Context.dev -> grouping -> verdicts), exactly as for a real site update. In production a monitor would
find a change on its own within its polling interval; for the demo we nudge it to check immediately.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time

from . import fake_render
from .config import ADMIN_TOKEN, CREDIT_BUDGET_DAILY, INCIDENT_TEMPLATES, ROOT, SCENARIO_SPECS, SHOWCASE, SOURCES_BY_ID
from .monitors import PageMonitor

SITE_DIR = ROOT / "fake_site"
HALVIK_DIR = ROOT / "scenario" / "halvik"
STATE_FILE = ROOT / "data" / "scenario.json"
CONTROL_FILE = ROOT / "data" / "control.json"           # {"paused": bool, "since": ts}: survives restarts


class ScenarioDef:
    """Common shape for both kinds of storyline."""

    def __init__(self, sid: str, meta: dict, incident_id: str, pages: list[dict], steps: list[dict], kind: str):
        self.id, self.meta, self.incident_id, self.pages, self.steps, self.kind = sid, meta, incident_id, pages, steps, kind
        self.page_by_id = {p["id"]: p for p in pages}

    def versions_at(self, n: int) -> dict[str, str]:
        """page id -> marker of the version published at step n (file name for Halvik, 'step<i>' for JSON)."""
        versions: dict[str, str] = {}
        for i, step in enumerate(self.steps[: n + 1]):
            for pid, value in step["pages"].items():
                versions[pid] = value if self.kind == "html" else f"step{i}"
        return versions

    def content(self, page_id: str, marker: str) -> bytes:
        if self.kind == "html":
            return (HALVIK_DIR / marker).read_bytes()
        spec = SCENARIO_SPECS[self.id]
        page = spec["steps"][int(marker.removeprefix("step"))]["pages"][page_id]
        return fake_render.render(spec, page_id, page).encode()


def load_defs() -> dict[str, ScenarioDef]:
    defs: dict[str, ScenarioDef] = {}
    halvik = json.loads((HALVIK_DIR / "steps.json").read_text())
    incident = SOURCES_BY_ID[halvik["pages"][0]["source_id"]].incident
    area = INCIDENT_TEMPLATES[incident].area
    pages = [{**p, "kind": SOURCES_BY_ID[p["source_id"]].kind} for p in halvik["pages"]]
    defs["halvik"] = ScenarioDef("halvik", {
        "name": "Halvik storm surge", "hazard": "storm_surge", "scale": "major", "language": "en",
        "region": "Bothnian Sea coast, Finland",
        "summary": "A storm surge hits a coastal city: bridge-collapse bot swarm, scam evacuation post, official debunks.",
        "area": area}, incident, pages, halvik["steps"], "html")
    for sid, spec in SCENARIO_SPECS.items():
        pages = [{"id": key, "source_id": f"{sid}-{key}", "name": spec["sources"][key]["name"],
                  "kind": SOURCES_BY_ID[f"{sid}-{key}"].kind, "target": f"{sid}/{key}/index.html",
                  "url": f"/sites/{sid}/{key}/"} for key in ("official", "news", "social")]
        steps = [{"n": i, "clock": st.get("clock", ""), "title": st["title"], "description": st.get("description", ""),
                  "expect": st.get("expect", ""), "pages": st["pages"]} for i, st in enumerate(spec["steps"])]
        meta = {k: spec.get(k) for k in ("name", "hazard", "scale", "language", "region", "summary", "area")}
        defs[sid] = ScenarioDef(sid, meta, sid, pages, steps, "json")
    return defs


class ScenarioEngine:
    def __init__(self, store, scheduler, pipeline):
        self.store, self.scheduler, self.pipeline = store, scheduler, pipeline
        self.defs = load_defs()
        self.state: dict[str, dict] = {sid: {"current_step": -1, "applied": {}, "interval_s": 60} for sid in self.defs}
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {sid: asyncio.Lock() for sid in self.defs}
        self.bulk_task: asyncio.Task | None = None
        self.control = {"paused": False, "since": None, "reason": None,
                        "showcase": {"running": SHOWCASE, "max_concurrent": 3, "interval_s": 45, "hold_s": 120,
                                     "only_when_watched": True}}
        if CONTROL_FILE.exists():
            try:
                saved = json.loads(CONTROL_FILE.read_text())
                self.control.update({k: v for k, v in saved.items() if k != "showcase"})
                self.control["showcase"].update(saved.get("showcase") or {})
            except ValueError:
                pass
        self.showcase_task: asyncio.Task | None = None
        self.finished_at: dict[str, float] = {}     # sid -> wall time it reached its last step (showcase retires it)
        self._last_turn = 0.0                       # showcase pacing: when the last showcase step happened (monotonic)
        if STATE_FILE.exists():
            saved = json.loads(STATE_FILE.read_text())
            if "scenarios" not in saved:            # old single-scenario format = Halvik
                saved = {"scenarios": {"halvik": saved}}
            for sid, st in saved["scenarios"].items():
                if sid in self.state:
                    self.state[sid].update({k: st[k] for k in ("current_step", "applied", "interval_s") if k in st})

    # ---- views ----------------------------------------------------------------------------------------------------
    def view(self, sid: str) -> dict:
        d, st = self.defs[sid], self.state[sid]
        task = self.tasks.get(sid)
        out = {
            "id": sid, **d.meta, "incident_id": d.incident_id, "current_step": st["current_step"],
            "steps": [{"n": s["n"], "clock": s.get("clock", ""), "title": s["title"], "description": s.get("description", ""),
                       "expect": s.get("expect", ""), "changes": list(s["pages"])} for s in d.steps],
            "pages": [{"id": p["id"], "source_id": p["source_id"], "name": p["name"], "kind": p["kind"], "url": p["url"]}
                      for p in d.pages],
            "autoplay": {"running": task is not None and not task.done(), "interval_s": st["interval_s"]},
            "pipeline": {p["source_id"]: self.pipeline.latest_stage.get(p["source_id"]) for p in d.pages},
        }
        if sid == "halvik":
            out["scenario"] = "halvik"              # field the original single-scenario admin page used
        return out

    def views(self) -> list[dict]:
        return [self.view(sid) for sid in self.defs]

    def _save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"scenarios": self.state}))
        tmp.replace(STATE_FILE)

    def _publish(self, sid: str) -> None:
        self._save()
        self.store.publish("scenario", self.view(sid))

    def _log(self, sid: str, action: str, detail: str) -> None:
        self.store.add_activity({"service": "scenario", "action": action, "target": sid, "latency_ms": None,
                                 "credits": None, "cache": None, "ok": True, "detail": detail})

    # ---- monitors ---------------------------------------------------------------------------------------------------
    def _add_monitors(self, sid: str) -> None:
        for p in self.defs[sid].pages:
            src = SOURCES_BY_ID[p["source_id"]]
            self.scheduler.add_monitor(PageMonitor(src.id, src.name, src.kind, src.check_url, src.quiet_s, src.incident_s))

    def restore(self) -> None:
        """On server start: watch the sources of every scenario that was started before the restart."""
        self.scheduler.paused = self.pipeline.paused = bool(self.control["paused"])
        for sid, st in self.state.items():
            if st["current_step"] >= 0:
                # Re-render the current pages (a fresh container has none); unchanged text is not re-processed.
                d = self.defs[sid]
                for pid, marker in d.versions_at(st["current_step"]).items():
                    self._write(d, pid, marker)
                self._add_monitors(sid)
        self.write_sites_index()
        if self.control["showcase"]["running"]:
            self.showcase_task = asyncio.ensure_future(self._showcase_loop())

    # ---- pause / resume ("stop burning credits but keep looking around") ---------------------------------------------
    def control_view(self) -> dict:
        view = json.loads(json.dumps(self.control))
        view["showcase"]["running"] = bool(self.showcase_task and not self.showcase_task.done())
        view["auth_required"] = bool(ADMIN_TOKEN)
        view["viewers"] = self.store.viewers()
        view["credit_budget_daily"] = CREDIT_BUDGET_DAILY or None
        view["credits_today"] = round(self.store.credits_spent - self.control.get("budget_start", self.store.credits_spent), 1) \
            if self.control.get("budget_day") == time.strftime("%Y-%m-%d") else 0
        return view

    def _save_control(self) -> None:
        CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONTROL_FILE.write_text(json.dumps(self.control))

    async def set_paused(self, paused: bool, reason: str | None = None) -> dict:
        from .store import now
        if paused == self.control["paused"]:
            return self.control_view()
        if paused:                                  # stop everything that would advance scenarios
            for sid, task in list(self.tasks.items()):
                if not task.done():
                    await self.set_autoplay(sid, False)
            if self.bulk_task and not self.bulk_task.done():
                self.bulk_task.cancel()
        self.control.update(paused=paused, since=now(), reason=reason if paused else None)
        self._save_control()
        self.pipeline.paused = paused
        self.scheduler.set_paused(paused)
        self.store.add_activity({"service": "control", "action": "paused" if paused else "resumed", "target": "all",
                                 "latency_ms": None, "credits": None, "cache": None, "ok": True,
                                 "detail": (f"{reason}: " if reason else "") + "page monitors stopped, no Context.dev / LLM calls" if paused else
                                 "re-checking every page against what was last processed"})
        self.store.publish("control", self.control_view())
        return self.control_view()

    # ---- showcase: scenarios run by themselves for every viewer -------------------------------------------------------
    async def set_showcase(self, running: bool, max_concurrent: int | None = None, interval_s: int | None = None,
                           hold_s: int | None = None, only_when_watched: bool | None = None) -> dict:
        sc = self.control["showcase"]
        if only_when_watched is not None:
            sc["only_when_watched"] = bool(only_when_watched)
        if max_concurrent:
            sc["max_concurrent"] = max(1, min(int(max_concurrent), 8))
        if interval_s:
            sc["interval_s"] = max(30, min(int(interval_s), 600))
        if hold_s:
            sc["hold_s"] = max(30, min(int(hold_s), 3600))
        sc["running"] = running
        self._save_control()
        if self.showcase_task and not self.showcase_task.done():
            self.showcase_task.cancel()
        self.showcase_task = asyncio.ensure_future(self._showcase_loop()) if running else None
        self.store.add_activity({"service": "control", "action": "showcase on" if running else "showcase off",
                                 "target": "all", "latency_ms": None, "credits": None, "cache": None, "ok": True,
                                 "detail": f"{sc['max_concurrent']} at a time, a step every {sc['interval_s']} s, "
                                           f"finished scenarios held {sc['hold_s']} s"
                                           + (", only while someone is watching" if sc.get("only_when_watched", True) else "")
                                           if running else "scenarios left as they are"})
        self.store.publish("control", self.control_view())
        return self.control_view()

    def _rotation(self) -> list[str]:
        """Scenario order that alternates sizes, so viewers always see a mix (big disaster, small flood, ...)."""
        groups: dict[str, list[str]] = {}
        for sid, d in sorted(self.defs.items()):
            groups.setdefault(d.meta.get("scale", "other"), []).append(sid)
        lanes = [groups.get(k, []) for k in ("catastrophic", "minor", "major", "moderate")] + \
                [v for k, v in groups.items() if k not in ("catastrophic", "minor", "major", "moderate")]
        order = []
        for i in range(max((len(lane) for lane in lanes), default=0)):
            order += [lane[i] for lane in lanes if i < len(lane)]
        return order

    def _over_daily_budget(self) -> bool:
        if not CREDIT_BUDGET_DAILY:
            return False
        today = time.strftime("%Y-%m-%d")
        if self.control.get("budget_day") != today:
            self.control["budget_day"], self.control["budget_start"] = today, self.store.credits_spent
            self._save_control()
        return self.store.credits_spent - self.control["budget_start"] >= CREDIT_BUDGET_DAILY

    def _unwatched(self) -> bool:
        sc = self.control["showcase"]
        return sc["running"] and sc.get("only_when_watched", True) and self.store.viewers() == 0

    async def _showcase_turn(self) -> None:
        """Showcase pacing: steps of all running scenarios take turns, evenly spaced (interval / max_concurrent apart,
        e.g. 45 s / 3 = a change every 15 s), and only while someone has a page open. Manual autoplay is not paced."""
        while True:
            sc = self.control["showcase"]
            if not sc["running"]:
                return
            if self._unwatched():
                await asyncio.sleep(2)
                continue
            gap = sc["interval_s"] / max(1, sc["max_concurrent"])
            wait = self._last_turn + gap - time.monotonic()
            if wait <= 0:
                self._last_turn = time.monotonic()
                return
            await asyncio.sleep(min(wait, 2))

    async def _showcase_loop(self) -> None:
        next_i = 0
        sc0, now0 = self.control["showcase"], time.time()
        for sid, st in self.state.items():          # finished before showcase (re)started: their hold is over
            if st["current_step"] >= len(self.defs[sid].steps) - 1:
                self.finished_at.setdefault(sid, now0 - sc0["hold_s"])
        try:
            while True:
                await asyncio.sleep(5)
                if self.control["paused"]:
                    continue
                if self._over_daily_budget():
                    await self.set_paused(True, reason=f"daily credit budget of {CREDIT_BUDGET_DAILY:g} reached")
                    continue
                sc = self.control["showcase"]
                last = {sid: len(d.steps) - 1 for sid, d in self.defs.items()}
                started = [sid for sid, st in self.state.items() if st["current_step"] >= 0]
                finished = [sid for sid in started if self.state[sid]["current_step"] >= last[sid]]
                moving = [sid for sid in started if sid not in finished]
                now_t = time.time()
                for sid in finished:
                    if self._unwatched():           # nobody watching: keep the results up, restart the hold clock
                        self.finished_at[sid] = now_t
                    else:
                        self.finished_at.setdefault(sid, now_t)
                expired = sorted((sid for sid in finished if now_t - self.finished_at[sid] >= sc["hold_s"]),
                                 key=lambda sid: self.finished_at[sid])          # oldest results first
                # Keep the latest few results on the board; retire older ones one at a time (the board never empties).
                if len(finished) > sc["max_concurrent"] and expired and not self._unwatched():
                    await self.reset(expired.pop(0))
                for sid in moving:                  # keep running ones moving (e.g. after a pause stopped autoplay)
                    task = self.tasks.get(sid)
                    if not (task and not task.done()):
                        await self.set_autoplay(sid, True, sc["interval_s"], delay_s=sc["interval_s"])
                if len(moving) < sc["max_concurrent"] and not self._unwatched():   # finished ones don't hold a slot
                    order = self._rotation()        # start at most one scenario per tick, alternating sizes
                    fresh = [order[(next_i + k) % len(order)] for k in range(len(order))]
                    sid = next((x for x in fresh if self.state[x]["current_step"] < 0), None)
                    if sid is None and expired:     # everything has run: replay the oldest finished one
                        sid = expired.pop(0)
                        await self.reset(sid)
                    if sid is not None:
                        next_i = order.index(sid) + 1
                        await self._showcase_turn()
                        await self.go_to(sid, 0)
                        await self.set_autoplay(sid, True, sc["interval_s"], delay_s=sc["interval_s"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:                    # never die silently: log, and the admin can switch it back on
            self.store.add_activity({"service": "control", "action": "showcase stopped", "target": "all",
                                     "latency_ms": None, "credits": None, "cache": None, "ok": False,
                                     "detail": f"{type(exc).__name__}: {exc}"[:200]})
            self.store.publish("control", self.control_view())

    # ---- an index of every scenario's fictional sites (fake_site/index.html, also served at /sites/) --------------------
    def write_sites_index(self) -> None:
        import html as _html
        order = {"catastrophic": 0, "major": 1, "moderate": 2, "minor": 3}
        rows = []
        for sid, d in sorted(self.defs.items(), key=lambda kv: (order.get(kv[1].meta.get("scale"), 9), kv[1].meta.get("name", ""))):
            started = self.state[sid]["current_step"] >= 0
            links = []
            for pg in d.pages:
                target = pg["target"]               # e.g. "<id>/official/index.html" or "herald/storm-surge-live.html"
                href = os.path.dirname(target) + "/" if target.endswith("index.html") else target
                label = f"{_html.escape(pg['name'])} <small>({pg['kind']})</small>"
                links.append(f'<a href="{href}">{label}</a>' if started else f'<span class="off">{label}</span>')
            m = d.meta
            rows.append(f'<tr class="{"on" if started else ""}"><td><b>{_html.escape(m.get("name", sid))}</b><br>'
                        f'<small>{_html.escape(m.get("region", ""))} · {m.get("language", "")} · {m.get("scale", "")}</small></td>'
                        f'<td>{"step " + str(self.state[sid]["current_step"]) if started else "not started"}</td>'
                        f'<td>{"<br>".join(links)}</td></tr>')
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>Fictional exercise sites</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#fff;color:#1b1f24}}.wrap{{max-width:960px;margin:0 auto;padding:24px 16px}}
.note{{background:#ffe14d;padding:12px 16px;border-radius:6px;font-weight:600}}table{{width:100%;border-collapse:collapse;margin-top:16px}}
td{{border-top:1px solid #ddd;padding:10px 8px;vertical-align:top}}tr:not(.on){{color:#888}}.off{{color:#aaa}}a{{color:#0b57d0}}</style></head>
<body><div class="wrap"><h1>Fictional exercise sites</h1>
<p class="note">Everything here is fictional: invented towns, organisations, newspapers and accounts, used as test data for a
hackathon demo. A scenario's pages exist only while it is started (from the FloodGate admin page).</p>
<table>{"".join(rows)}</table></div></body></html>"""
        tmp = SITE_DIR / ".index.html.tmp"
        tmp.write_text(page)
        os.replace(tmp, SITE_DIR / "index.html")

    # ---- stepping ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _sync_images(sid: str) -> None:
        """The scenario's photos (scenario/<id>/images, made by scenario/generate_images.py) next to its pages."""
        src = ROOT / "scenario" / sid / "images"
        if not src.is_dir():
            return
        dst = SITE_DIR / sid / "images"
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.jpg"):
            out = dst / f.name
            if not out.exists() or out.stat().st_size != f.stat().st_size:
                shutil.copyfile(f, out)

    def _write(self, d: ScenarioDef, page_id: str, marker: str) -> None:
        self._sync_images(d.id)
        target = SITE_DIR / d.page_by_id[page_id]["target"]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(d.content(page_id, marker))
        os.replace(tmp, target)                     # atomic: a monitor never reads a half-written page

    async def go_to(self, sid: str, n: int) -> dict:
        d, st = self.defs[sid], self.state[sid]
        if not 0 <= n < len(d.steps):
            raise ValueError(f"step must be between 0 and {len(d.steps) - 1}")
        async with self.locks[sid]:
            versions = d.versions_at(n)
            changed = [pid for pid, marker in versions.items() if st["applied"].get(pid) != marker]
            for pid in changed:
                self._write(d, pid, versions[pid])
            starting = st["current_step"] < 0
            st["current_step"], st["applied"] = n, {**st["applied"], **versions}
            if starting:
                self._add_monitors(sid)             # a fresh monitor checks immediately on its own
            self._publish(sid)
            self.write_sites_index()
            for pid in changed:                     # demo nudge (see module docstring)
                self.scheduler.check_now(d.page_by_id[pid]["source_id"])
            self._log(sid, f"step {n}", f"{d.steps[n]['title']} · pages changed: {', '.join(changed) or 'none'}"
                      + (" · scenario started" if starting else ""))
            return self.view(sid)

    async def next(self, sid: str) -> dict:
        st = self.state[sid]
        if st["current_step"] >= len(self.defs[sid].steps) - 1:
            raise ValueError("already at the last step")
        return await self.go_to(sid, st["current_step"] + 1)

    async def reset(self, sid: str) -> dict:
        """Back to 'not started': incident, items, alerts, snapshots, monitors (ours and Context.dev's) and, for JSON
        scenarios, the pages are removed. Halvik's hand-written pages stay on disk (inert without monitors)."""
        d, st = self.defs[sid], self.state[sid]
        self.finished_at.pop(sid, None)
        await self.set_autoplay(sid, False)
        async with self.locks[sid]:
            source_ids = [p["source_id"] for p in d.pages]
            for src_id in source_ids:
                self.scheduler.remove_monitor(src_id)
            async with self.pipeline.incident_locks[d.incident_id]:
                self.store.delete_incident(d.incident_id)
            ctx_ids = []
            for src_id in source_ids:
                self.store.snapshots.pop(src_id, None)
                self.pipeline.forget_source(src_id)
                known = self.store.context_monitors.pop(src_id, None)
                if known:
                    ctx_ids.append(known["id"])
            if ctx_ids and self.pipeline.context_monitors is not None:
                await asyncio.to_thread(self.pipeline.context_monitors.delete_monitors, ctx_ids)
            if d.kind == "json":
                shutil.rmtree(SITE_DIR / sid, ignore_errors=True)
            st["current_step"], st["applied"] = -1, {}
            self.store.save()
            self._publish(sid)
            self.write_sites_index()
            self._log(sid, "reset", "incident, pages, snapshots and monitors removed")
            return self.view(sid)

    # ---- autoplay ---------------------------------------------------------------------------------------------------
    async def set_autoplay(self, sid: str, running: bool, interval_s: int | None = None, delay_s: float = 0) -> dict:
        st = self.state[sid]
        if interval_s:
            st["interval_s"] = max(10, min(int(interval_s), 3600))
        task = self.tasks.pop(sid, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if running and st["current_step"] < len(self.defs[sid].steps) - 1:
            self.tasks[sid] = asyncio.create_task(self._autoplay(sid, delay_s), name=f"autoplay:{sid}")
        self._publish(sid)
        return self.view(sid)

    async def _autoplay(self, sid: str, delay_s: float) -> None:
        try:
            if delay_s:
                await asyncio.sleep(delay_s)
            last = len(self.defs[sid].steps) - 1
            while self.state[sid]["current_step"] < last:
                await self._showcase_turn()         # showcase: take turns with the other scenarios, only when watched
                await self.next(sid)                # advance right away, then once per interval
                if self.state[sid]["current_step"] >= last:
                    break
                await asyncio.sleep(self.state[sid]["interval_s"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(sid, "autoplay stopped", f"{type(exc).__name__}: {exc}")
        finally:
            if self.tasks.get(sid) is asyncio.current_task():
                self.tasks.pop(sid, None)
            self._publish(sid)

    async def stop_all(self) -> None:
        extra = [t for t in (self.bulk_task, self.showcase_task) if t]
        for task in list(self.tasks.values()) + extra:
            task.cancel()

    # ---- bulk ---------------------------------------------------------------------------------------------------------
    async def bulk(self, action: str, ids: list[str], interval_s: int | None = None, stagger_s: float = 5) -> list[dict]:
        ids = [sid for sid in ids if sid in self.defs]
        stagger_s = max(0.0, min(float(stagger_s), 120.0))
        if action == "reset":
            for sid in ids:
                await self.reset(sid)
        elif action == "autoplay_off":
            for sid in ids:
                await self.set_autoplay(sid, False)
        elif action == "autoplay_on":
            for i, sid in enumerate(ids):           # staggered first steps, then each scenario on its own interval
                await self.set_autoplay(sid, True, interval_s, delay_s=i * stagger_s)
        elif action in ("start", "next"):
            if action == "start":
                ids = [sid for sid in ids if self.state[sid]["current_step"] < 0]
            else:
                ids = [sid for sid in ids if self.state[sid]["current_step"] < len(self.defs[sid].steps) - 1]
            self.bulk_task = asyncio.create_task(self._staggered(ids, stagger_s), name="scenario-bulk")
        else:
            raise ValueError(f"unknown action '{action}'")
        return [self.view(sid) for sid in ids] if ids else []

    async def _staggered(self, ids: list[str], stagger_s: float) -> None:
        """Steps several scenarios one after another, so they don't all hit Context.dev in the same second."""
        for i, sid in enumerate(ids):
            if i:
                await asyncio.sleep(stagger_s)
            try:
                if self.state[sid]["current_step"] < 0:
                    await self.go_to(sid, 0)
                else:
                    await self.next(sid)
            except ValueError:
                pass
