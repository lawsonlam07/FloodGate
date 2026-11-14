"""Context.dev Monitors, run in parallel with our fast pipeline: an independent, AI-judged second check of each update.

For each official page we keep one long-lived *semantic* page monitor on Context.dev (daily schedule as a safety net).
When our local monitor sees the page change, we call "run now" (POST /v1/monitors/{id}/run), wait for the run, and
attach Context.dev's verdict (title, summary, importance, confidence) to the update. Measured (poc/monitor_experiment.py):
~7 s for a run without a change, ~25 s with one, 1 credit per run; the AI judge ignores timestamp-only edits.

This never blocks the fast path. Runs of one monitor never overlap (Context.dev skips extra calls as "superseded"),
so calls are serialised per source and coalesced.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Callable

import requests

from .config import Source
from .store import Store, now

API = "https://api.context.dev/v1"
INSTRUCTIONS = ("Report changes to emergency information: warnings, evacuation orders, closures, shelters, damage, "
                "casualties, and corrections of false reports. Ignore changes to timestamps, update numbers, layout "
                "and unrelated stories.")


class ContextMonitors:
    def __init__(self, api_key: str, store: Store):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        self.store = store
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.pending: dict[str, float] = {}
        self.disabled: dict[str, str] = {}          # source_id -> reason (e.g. monitor limit reached)
        self.limit_hit = False                      # plan's monitor allowance reached: create no new monitors

    # ---- HTTP -----------------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        for _ in range(4):
            r = self.session.request(method, API + path, json=body, timeout=60)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "1")))
                continue
            break
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {data.get('error_code') or ''} {data.get('message') or ''}".strip())
        return data

    def _wait_run(self, monitor_id: str, run_id: str, timeout: float = 120) -> dict:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            run = self._call("GET", f"/monitors/{monitor_id}/runs/{run_id}")
            if run["status"] in ("completed", "failed", "skipped"):
                return run
            time.sleep(0.7)
        raise TimeoutError(f"monitor run {run_id} not finished after {timeout:.0f}s")

    # ---- monitor lifecycle -----------------------------------------------------------------------------------
    def _ensure_monitor(self, src: Source) -> tuple[str, bool, int]:
        """Return (monitor_id, created_now, credits). Recreates the monitor if the page URL changed (tunnel rotation)."""
        url = src.public_url
        known = self.store.context_monitors.get(src.id)
        if known and known.get("url") == url:
            return known["id"], False, 0
        if known:                                   # URL changed: the old monitor watches a dead address
            try:
                self._call("DELETE", f"/monitors/{known['id']}")
            except Exception:
                pass
        mon = self._call("POST", "/monitors", {
            "name": f"FloodGate · {src.name}"[:200],
            "target": {"type": "page", "url": url, "instructions": INSTRUCTIONS},
            "change_detection": {"type": "semantic"},
            "schedule": {"type": "interval", "frequency": 1, "unit": "days"},
        })
        credits = 0
        if mon.get("initial_run_id"):
            credits = self._wait_run(mon["id"], mon["initial_run_id"]).get("credits_charged") or 0
        self.store.context_monitors[src.id] = {"id": mon["id"], "url": url, "created_at": now()}
        return mon["id"], True, credits

    def _check_blocking(self, src: Source) -> dict:
        start = time.monotonic()
        verdict = {"source_id": src.id, "source_name": src.name, "incident_id": src.incident, "status": "failed",
                   "title": None, "summary": None, "importance": None, "confidence": None, "credits": 0}
        try:
            monitor_id, created, credits = self._ensure_monitor(src)
            verdict.update(monitor_id=monitor_id, credits=credits)
            if created:                             # the baseline is the current page: nothing to compare yet
                verdict["status"] = "baseline"
                return verdict
            run = None
            for attempt in range(3):
                started = self._call("POST", f"/monitors/{monitor_id}/run")
                run = self._wait_run(monitor_id, started.get("run_id") or started.get("id"))
                if run["status"] != "skipped":
                    break
                time.sleep(2 + attempt * 2)         # superseded by a run already in flight: try again shortly
            verdict["credits"] += run.get("credits_charged") or 0
            if run["status"] != "completed":
                verdict["status"] = run["status"]
                verdict["summary"] = (run.get("error") or {}).get("message") or run.get("skip_reason")
                return verdict
            verdict["status"] = "changed" if run.get("change_detected") else "no_change"
            if run.get("change_id"):
                changes = self._call("GET", f"/monitors/{monitor_id}/changes").get("data") or []
                chg = next((c for c in changes if c.get("id") == run["change_id"]), None) or (changes[0] if changes else {})
                verdict.update({k: chg.get(k) for k in ("title", "summary", "importance", "confidence")})
        except Exception as exc:
            verdict["summary"] = f"{type(exc).__name__}: {exc}"[:200]
            if "MONITOR_LIMIT_EXCEEDED" in str(exc):
                self.disabled[src.id] = "Context.dev monitor limit reached"
                self.limit_hit = True               # reported once (this verdict); other new sources are skipped quietly
        finally:
            verdict["latency_ms"] = round((time.monotonic() - start) * 1000)
            verdict["at"] = now()
        return verdict

    def delete_monitors(self, monitor_ids: list[str]) -> None:
        """Blocking. Remove monitors we no longer need (scenario reset); frees plan slots."""
        for mid in monitor_ids:
            try:
                self._call("DELETE", f"/monitors/{mid}")
            except Exception:
                pass
        self.limit_hit = False
        self.disabled = {k: v for k, v in self.disabled.items() if "limit" not in v}

    # ---- async entry point -----------------------------------------------------------------------------------
    async def check(self, src: Source, t0: float, on_verdict: Callable[[Source, dict, float], None]) -> None:
        """Run-now on the source's monitor; serialised per source, extra requests coalesced into one follow-up run."""
        if src.id in self.disabled:
            return
        if self.limit_hit and src.id not in self.store.context_monitors:
            self.disabled[src.id] = "Context.dev monitor limit reached"
            return
        lock = self.locks[src.id]
        if lock.locked():
            self.pending[src.id] = t0
            return
        async with lock:
            while t0 is not None:
                verdict = await asyncio.to_thread(self._check_blocking, src)
                on_verdict(src, verdict, t0)
                t0 = self.pending.pop(src.id, None)
