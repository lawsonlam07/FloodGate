"""Experiment: can Context.dev Monitors + "run now" give us fast, AI-judged change detection?

Creates a throwaway fictional test page (fake_site/_monitor_test/, served through ./serve.sh's tunnel), two monitors
on it (exact and semantic) on a once-a-day schedule, then for each scenario edits the page, calls
POST /v1/monitors/{id}/run and polls the run (no webhook needed) to measure:
  - wall time from "run now" to a completed run
  - whether a change was reported, and what the change record contains (diff, summary, importance, confidence)
Scenarios: meaningful change, cosmetic change (timestamp only), no change, two "run now" calls back to back.
Deletes the monitors and the test page at the end.

Run: uv run poc/monitor_experiment.py      (needs ./serve.sh running and CONTEXT_DEV_API_KEY in .env)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.context_client import load_dotenv  # noqa: E402

API = "https://api.context.dev/v1"
PAGE_DIR = ROOT / "fake_site" / "_monitor_test"

TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex, nofollow">
<title>Monitor test – Halvik Rescue Services</title></head><body>
<p><strong>FICTIONAL EXERCISE:</strong> Halvik is not a real place. Test page for a hackathon demo.</p>
<main><h1>Storm surge in Halvik</h1>
<p>Situation update · Published 14 November 2026 at {clock}</p>
<h2>Warnings</h2><p>Red warning: storm surge. The sea level at Old Harbour is expected to peak at 2.1 metres at around 18:00.</p>
<h2>Evacuation</h2><p>Residents of Saltmarsh living south of Ferry Street must leave their homes by 16:00.</p>{extra}
<h2>Closures</h2><ul><li>Harbour Bridge is closed to all traffic as a precaution.</li></ul>
</main></body></html>"""

MEANINGFUL = ("<p>The evacuation order is extended to Old Harbour. Residents of Old Harbour living below Harbour Street "
              "must leave their homes by 17:00.</p>")
INSTRUCTIONS = ("Report changes to emergency information: warnings, evacuation orders, closures, shelters, and "
                "corrections of false reports. Ignore changes to timestamps, update numbers and layout.")


def write_page(clock: str, extra: str = "") -> None:
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PAGE_DIR / "index.html.tmp"
    tmp.write_text(TEMPLATE.format(clock=clock, extra=extra))
    tmp.replace(PAGE_DIR / "index.html")


class Api:
    def __init__(self, key: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        self.credits = 0

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        for _ in range(4):
            r = self.s.request(method, API + path, json=body, timeout=60)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "1")))
                continue
            break
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {json.dumps(data)[:400]}")
        self.credits += (data.get("key_metadata") or {}).get("credits_consumed") or 0
        return data

    def wait_run(self, mon: str, run: str, timeout: float = 180) -> tuple[dict, float]:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            r = self.call("GET", f"/monitors/{mon}/runs/{run}")
            if r["status"] in ("completed", "failed", "skipped"):
                return r, time.monotonic() - start
            time.sleep(0.5)
        raise TimeoutError(f"run {run} still not finished after {timeout}s")


def main() -> int:
    load_dotenv(ROOT / ".env")
    url_file = ROOT / "data" / "site_public_url"
    if not url_file.exists():
        print("No tunnel URL: start ./serve.sh first.")
        return 2
    page_url = url_file.read_text().strip().rstrip("/") + "/_monitor_test/"
    api = Api(os.environ["CONTEXT_DEV_API_KEY"])

    write_page("12:45")
    if requests.get(page_url, timeout=15).status_code != 200:
        print(f"{page_url} is not reachable through the tunnel")
        return 2
    print(f"test page: {page_url}\n")

    monitors = {}
    try:
        for kind in ("exact", "semantic"):
            body = {"name": f"floodgate experiment ({kind})",
                    "target": {"type": "page", "url": page_url, **({"instructions": INSTRUCTIONS} if kind == "semantic" else {})},
                    "change_detection": {"type": kind},
                    "schedule": {"type": "interval", "frequency": 1, "unit": "days"}}
            t = time.monotonic()
            m = api.call("POST", "/monitors", body)
            monitors[kind] = m["id"]
            run, waited = api.wait_run(m["id"], m["initial_run_id"])
            print(f"[{kind:<8}] created {m['id']}; baseline run {run['status']} after {time.monotonic() - t:.1f}s "
                  f"(credits {run['credits_charged']})")

        def run_now(label: str) -> None:
            for kind, mon in monitors.items():
                t = time.monotonic()
                r = api.call("POST", f"/monitors/{mon}/run")
                run_id = r.get("run_id") or r.get("id")
                run, _ = api.wait_run(mon, run_id)
                wall = time.monotonic() - t
                line = (f"[{kind:<8}] {label:<26} {run['status']:<9} {wall:5.1f}s  change={run['change_detected']}"
                        f"  credits={run['credits_charged']}" + (f"  skip={run['skip_reason']}" if run.get("skip_reason") else ""))
                print(line)
                if run.get("change_id"):
                    changes = api.call("GET", f"/monitors/{mon}/changes").get("data") or []
                    chg = next((c for c in changes if c.get("id") == run["change_id"]), changes[0] if changes else {})
                    for k in ("title", "summary", "importance", "confidence"):
                        if chg.get(k) is not None:
                            print(f"             {k}: {chg[k]}")
                    if chg.get("diff"):
                        print("             diff:\n" + "\n".join("               " + ln[:150] for ln in str(chg["diff"]).splitlines()[:8]))
                    if chg.get("evidence"):
                        print(f"             evidence: {json.dumps(chg['evidence'])[:300]}")

        write_page("15:05", MEANINGFUL)
        run_now("meaningful change")
        write_page("15:20", MEANINGFUL)
        run_now("cosmetic (timestamp only)")
        run_now("no change")

        print("\nback-to-back run-now calls (semantic):")
        mon = monitors["semantic"]
        write_page("15:30")                         # remove the extension again: a real change
        a = api.call("POST", f"/monitors/{mon}/run")
        b = api.call("POST", f"/monitors/{mon}/run")
        for label, r in (("first", a), ("second", b)):
            run, wall = api.wait_run(mon, r.get("run_id") or r.get("id"))
            print(f"  {label}: {run['status']} change={run['change_detected']} skip={run.get('skip_reason')} ({wall:.1f}s)")
    finally:
        for kind, mon in monitors.items():
            try:
                api.call("DELETE", f"/monitors/{mon}")
                print(f"deleted {kind} monitor {mon}")
            except Exception as exc:
                print(f"could not delete {mon}: {exc}")
        shutil.rmtree(PAGE_DIR, ignore_errors=True)
    print(f"\ncredits consumed (reported): {api.credits}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
