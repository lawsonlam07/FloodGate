"""Minimal Context.dev client: retries on 429/5xx, timing, credit budget and a per-call log."""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

BASE_URL = "https://api.context.dev/v1"
# Context.dev fetches our pages through a public tunnel (or our server); too many at once and they time out.
_SLOTS = threading.BoundedSemaphore(int(os.environ.get("CONTEXT_MAX_CONCURRENT", "4")))
RETRYABLE = {429, 502, 503, 504}


def load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE lines) so we don't need python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CallLog:
    step: str
    endpoint: str
    target: str
    http_status: int | None = None
    outputs: str = ""               # e.g. "markdown:ok json:ok screenshot:FAIL"
    latency_ms: int = 0
    retries: int = 0
    credits_consumed: float | None = None
    credits_remaining: float | None = None
    cache_status: str | None = None
    cache_age_ms: int | None = None
    request_id: str | None = None
    error: str | None = None


class ContextClient:
    def __init__(self, api_key: str, *, timeout_s: float = 90, max_retries: int = 3, budget: float | None = None):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.budget = budget
        self.credits_spent = 0.0
        self.log: list[CallLog] = []

    # ---- low level -------------------------------------------------------

    def post(self, step: str, endpoint: str, body: dict, target: str) -> tuple[dict | None, CallLog]:
        if self.budget is not None and self.credits_spent >= self.budget:
            raise BudgetExceeded(f"spent {self.credits_spent:g} credits, budget is {self.budget:g}")
        with _SLOTS:                     # a burst (many scenarios stepping at once) queues instead of swamping the site
            return self._post(step, endpoint, body, target)

    def _post(self, step: str, endpoint: str, body: dict, target: str) -> tuple[dict | None, CallLog]:
        entry = CallLog(step=step, endpoint=endpoint, target=target)
        start = time.monotonic()
        resp = None
        while True:
            try:
                resp = self.session.post(BASE_URL + endpoint, json=body, timeout=self.timeout_s)
            except requests.RequestException as exc:
                entry.error = f"{type(exc).__name__}: {exc}"
                break
            if resp.status_code in RETRYABLE and entry.retries < self.max_retries:
                entry.retries += 1
                time.sleep(_retry_after(resp) + random.uniform(0.1, 0.6))
                continue
            break
        entry.latency_ms = round((time.monotonic() - start) * 1000)

        data = None
        if resp is not None:
            entry.http_status = resp.status_code
            try:
                data = resp.json()
            except ValueError:
                entry.error = entry.error or f"non-JSON response: {resp.text[:200]!r}"

        if isinstance(data, dict):
            km = data.get("key_metadata") or {}
            cm = data.get("cache_metadata") or {}
            entry.credits_consumed = km.get("credits_consumed")
            entry.credits_remaining = km.get("credits_remaining")
            entry.cache_status = cm.get("status")
            entry.cache_age_ms = cm.get("age_ms")
            entry.request_id = data.get("request_id")
            entry.outputs = " ".join(
                f"{name}:{'ok' if out.get('success') else 'FAIL'}"
                for name, out in data.items()
                if isinstance(out, dict) and out.get("requested")
            )
            if resp is not None and resp.status_code >= 400:
                entry.error = f"{data.get('error_code')}: {data.get('message')}"
        if entry.credits_consumed:
            self.credits_spent += entry.credits_consumed

        self.log.append(entry)
        return data, entry

    # ---- endpoints -------------------------------------------------------

    def scrape(
        self,
        step: str,
        url: str,
        *,
        markdown: bool = True,
        json_schema: dict | None = None,
        instructions: str | None = None,
        screenshot: bool = False,
        max_age_ms: int | None = 0,
    ) -> tuple[dict | None, CallLog]:
        body: dict = {
            "url": url,
            "formats": {"markdown": markdown, "json": json_schema is not None, "screenshot": screenshot},
            "timeoutOpts": {"milliseconds": 60_000, "behavior": "return-partial"},
        }
        if max_age_ms is not None:      # None = use Context.dev's default cache (3 days)
            body["maxAgeMs"] = max_age_ms
        if json_schema is not None:
            body["jsonParams"] = {"schema": json_schema}
            if instructions:
                body["jsonParams"]["instructions"] = instructions
        if screenshot:
            body["screenshotParams"] = {"area": "fullPage", "format": "png"}
        return self.post(step, "/web/scrape", body, url)


def _retry_after(resp: requests.Response) -> float:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def print_log(log: list[CallLog]) -> None:
    headers = ["step", "endpoint", "HTTP", "outputs", "latency", "retries", "credits", "remaining", "cache", "age_ms"]
    rows = [
        [
            e.step, e.endpoint, str(e.http_status or "-"), e.outputs or (e.error or "-")[:40],
            f"{e.latency_ms} ms", str(e.retries), _fmt(e.credits_consumed), _fmt(e.credits_remaining),
            e.cache_status or "-", _fmt(e.cache_age_ms),
        ]
        for e in log
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))
    errors = [e for e in log if e.error]
    for e in errors:
        print(f"  ! {e.step}: {e.error}")


def _fmt(v) -> str:
    if v is None:
        return "-"
    return f"{v:g}" if isinstance(v, float) else str(v)


class ReplayContextClient:
    """Drop-in for ContextClient.scrape that serves saved responses (out/<run>/<name>.raw.json).

    For development and tests: no network, no credits. `url_map` maps a URL fragment to a saved file name,
    e.g. {"/rescue/": "rescue"}.
    """

    def __init__(self, replay_dir: Path, url_map: dict[str, str]):
        self.replay_dir = Path(replay_dir)
        self.url_map = url_map
        self.credits_spent = 0.0
        self.log: list[CallLog] = []

    def scrape(self, step: str, url: str, **_kwargs) -> tuple[dict | None, CallLog]:
        import json
        start = time.monotonic()
        entry = CallLog(step=step, endpoint="/web/scrape (replay)", target=url, credits_consumed=0, cache_status="replay")
        name = next((n for frag, n in self.url_map.items() if frag in url), None)
        path = self.replay_dir / f"{name}.raw.json" if name else None
        if path is None or not path.exists():
            entry.http_status, entry.error = 404, f"no saved response for {url} in {self.replay_dir}"
            self.log.append(entry)
            return None, entry
        data = json.loads(path.read_text())
        entry.http_status = 200
        entry.outputs = " ".join(f"{k}:{'ok' if v.get('success') else 'FAIL'}" for k, v in data.items()
                                 if isinstance(v, dict) and v.get("requested"))
        entry.latency_ms = round((time.monotonic() - start) * 1000)
        self.log.append(entry)
        return data, entry
