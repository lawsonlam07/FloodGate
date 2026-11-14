"""Our own monitors: cheap, frequent change detection. They are TRIGGERS only, never evidence.

  PageMonitor  fetches a page directly (conditional GET), hashes its visible text, reports when it changes.
               Fast and free, so it can run every few seconds while an incident is active ("incident mode")
               and every minute otherwise ("quiet mode").
  CapFeedMonitor  polls an official CAP feed (FMI warnings) and reports the active warnings when it changes.

When a monitor reports a change, the pipeline captures the evidence through Context.dev (scrape + extraction).
That keeps Context.dev as the backbone while spending credits only when something actually changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
import time
from typing import Awaitable, Callable

import requests

from . import fmi
from .store import Store, now

USER_AGENT = "floodgate-monitor/0.1 (hackathon prototype)"


def visible_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def block_text(page: str) -> str:
    """Visible text with one line per block element (paragraph, list item, heading...)."""
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|li|h[1-6]|div|article|section|blockquote|td|tr)>", "\n", page)
    page = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in page.splitlines() if line.strip())


def added_sentences(old: str, new: str, limit: int = 3) -> list[str]:
    """Sentences on the new page that were not on the old one: an instant, unverified preview of the change.

    Lines where only numbers changed ("update no. 4" -> "no. 5") are skipped as noise."""
    def split(t):
        return [x.strip() for line in t.splitlines() for x in re.split(r"(?<=[.!?])\s+", line) if len(x.strip()) > 20]
    no_digits = lambda x: re.sub(r"\d+", "#", x)
    metadata = re.compile(r"^@|followers|reposts|^Joined ", re.I)     # social-feed chrome, not content
    before = {no_digits(x) for x in split(old)}
    return [x[:240] for x in split(new) if no_digits(x) not in before and not metadata.search(x)][:limit]


class Monitor:
    type = "base"

    def __init__(self, id: str, name: str, kind: str, url: str, quiet_s: int, incident_s: int):
        self.id, self.name, self.kind, self.url = id, name, kind, url
        self.quiet_s, self.incident_s = quiet_s, incident_s
        self.mode = "quiet"
        self.checks = self.changes = 0
        self.last_check = self.last_change = self.last_error = self.note = None
        self.wake = asyncio.Event()
        self.forced = False

    @property
    def interval_s(self) -> int:
        return self.incident_s if self.mode == "incident" else self.quiet_s

    def status(self) -> dict:
        return {"id": self.id, "name": self.name, "type": self.type, "kind": self.kind, "url": self.url,
                "mode": self.mode, "interval_s": self.interval_s, "checks": self.checks, "changes": self.changes,
                "last_check": self.last_check, "last_change": self.last_change, "last_error": self.last_error,
                "note": self.note}

    def check(self) -> dict | None:       # blocking; run in a thread
        raise NotImplementedError


class PageMonitor(Monitor):
    type = "page"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.etag = self.last_modified = None
        self.last_hash: str | None = None
        self.last_text = ""

    def check(self) -> dict | None:
        headers = {"User-Agent": USER_AGENT}
        forced, self.forced = self.forced, False        # a nudge re-reads the page fully (mtime has 1 s resolution)
        if self.etag and not forced:
            headers["If-None-Match"] = self.etag
        if self.last_modified and not forced:
            headers["If-Modified-Since"] = self.last_modified
        r = requests.get(self.url, headers=headers, timeout=15)
        if r.status_code == 304:
            return None
        r.raise_for_status()
        r.encoding = r.encoding if "charset" in r.headers.get("content-type", "") else "utf-8"
        self.etag, self.last_modified = r.headers.get("ETag"), r.headers.get("Last-Modified")
        digest = hashlib.sha256(visible_text(r.text).encode()).hexdigest()[:16]
        if digest == self.last_hash:
            return None
        first = self.last_hash is None
        text = block_text(r.text)
        added = [] if first else added_sentences(self.last_text, text)
        self.last_hash, self.last_text = digest, text
        return {"hash": digest, "first_check": first, "added": added}


class CapFeedMonitor(Monitor):
    """Polls an official CAP Atom feed (FMI warnings). Reports the full set of active warnings whenever the feed
    changes; the pipeline turns them into official_warning incidents (and ends the ones that disappeared)."""
    type = "feed"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_hash: str | None = None

    def check(self) -> dict | None:
        r = requests.get(self.url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        digest = hashlib.sha256(r.content).hexdigest()[:16]
        if digest == self.last_hash:
            return None
        warnings = fmi.active_warnings(fmi.parse_feed(r.content), now())
        severe = sum(w["colour"] in ("orange", "red") for w in warnings)
        self.note = f"{len(warnings)} active warnings ({severe} orange/red)"
        first = self.last_hash is None
        self.last_hash = digest
        return {"warnings": warnings, "first_check": first}


ChangeHandler = Callable[[Monitor, dict], Awaitable[None]]


class Scheduler:
    """Runs every monitor on its own loop; interval depends on whether its incident is active."""

    def __init__(self, store: Store, monitors: list[Monitor], on_change: ChangeHandler,
                 in_incident: Callable[[Monitor], bool]):
        self.store, self.monitors, self.on_change, self.in_incident = store, {m.id: m for m in monitors}, on_change, in_incident
        self.tasks: dict[str, asyncio.Task] = {}
        self.started = False
        self.paused = False                 # admin "pause processing": page monitors stop checking (feeds are free)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        for m in self.monitors.values():
            if not paused and m.type == "page":
                # Re-check every page against what the pipeline last processed, so edits made while paused are
                # picked up (the pipeline skips pages whose content it already processed).
                m.last_hash, m.forced = None, True
            m.wake.set()

    def start(self) -> None:
        self.started = True
        for m in self.monitors.values():
            self._launch(m)

    def _launch(self, m: Monitor) -> None:
        self.store.monitors[m.id] = m.status()
        self.tasks[m.id] = asyncio.create_task(self._run(m), name=f"monitor:{m.id}")

    def add_monitor(self, m: Monitor) -> bool:
        """Start watching a source (e.g. a scenario was started). No-op if it is already watched."""
        if m.id in self.monitors:
            return False
        self.monitors[m.id] = m
        if self.started:
            self._launch(m)
        return True

    def remove_monitor(self, monitor_id: str) -> None:
        """Stop watching a source (e.g. a scenario was reset)."""
        self.monitors.pop(monitor_id, None)
        task = self.tasks.pop(monitor_id, None)
        if task:
            task.cancel()
        self.store.monitors.pop(monitor_id, None)

    async def stop(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def check_now(self, monitor_id: str) -> bool:
        m = self.monitors.get(monitor_id)
        if m:
            m.forced = True
            m.wake.set()
        return m is not None

    async def _run(self, m: Monitor) -> None:
        while True:
            m.wake.clear()                  # clear BEFORE checking, so a nudge arriving mid-check is not lost
            if self.paused and m.type == "page":
                m.mode = "paused"
                self.store.monitors[m.id] = m.status()
                self.store.publish("monitor", m.status())
                await m.wake.wait()         # woken by resume (or a nudge, which re-enters this branch while paused)
                continue
            m.mode = "incident" if self.in_incident(m) else "quiet"
            start = time.monotonic()
            change = None
            try:
                change = await asyncio.to_thread(m.check)
                m.last_error = None
            except Exception as exc:    # a monitor must never die; record and keep polling
                m.last_error = f"{type(exc).__name__}: {exc}"[:200]
            m.checks += 1
            m.last_check = now()
            if change:
                m.changes += 1
                m.last_change = m.last_check
                self.store.add_activity({"service": "monitor", "action": "change detected", "target": m.id,
                                         "latency_ms": round((time.monotonic() - start) * 1000), "credits": None,
                                         "cache": None, "ok": True,
                                         "detail": "first check" if change.get("first_check") else
                                         ("new feed entries" if m.type == "feed" else "visible text changed")})
                asyncio.create_task(self.on_change(m, change), name=f"change:{m.id}")
            self.store.monitors[m.id] = m.status()
            self.store.publish("monitor", m.status())
            try:
                await asyncio.wait_for(m.wake.wait(), timeout=m.interval_s)
            except asyncio.TimeoutError:
                pass
