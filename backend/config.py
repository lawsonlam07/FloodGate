"""Settings (from .env / environment) and the source registry.

Whether a source is official is decided HERE, by us, never by the LLM.

Environment:
  CONTEXT_DEV_API_KEY   Context.dev key (evidence capture)
  ANTHROPIC_API_KEY     Claude key (claim grouping)
  SITE_PUBLIC_URL       public URL of fake_site/ (the tunnel), which Context.dev scrapes; if unset, the URL
                        serve.sh saved in data/site_public_url is used (it follows tunnel rotations)
  SITE_LOCAL_URL        where our own monitors fetch fake_site/ cheaply (default http://127.0.0.1:8765)
  CONTEXT_REPLAY_DIR    if set, serve saved Context.dev responses from this out/<run> dir instead of calling the API
  CREDIT_BUDGET         max Context.dev credits this server process may spend (default 1500)
  FMI_MONITOR           "0" disables the real FMI warnings feed monitor (default on; it costs no credits)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .context_client import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SITE_URL_FILE = ROOT / "data" / "site_public_url"      # written by serve.sh whenever the tunnel URL changes
PORT = int(os.environ.get("PORT", "8000"))
# Hosted mode: the server is reachable at PUBLIC_BASE_URL and serves the fictional sites itself at /sites, so
# Context.dev scrapes them directly (no tunnel, no serve.sh) and our monitors read them from this same process.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
SITE_LOCAL_URL = (os.environ.get("SITE_LOCAL_URL")
                  or (f"http://127.0.0.1:{PORT}/sites" if PUBLIC_BASE_URL else "http://127.0.0.1:8765")).rstrip("/")
# When set, every admin action (POST /api/admin/...) needs this token; everyone else gets a read-only admin view.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# Showcase: the server runs scenarios by itself for every viewer (rotating, autoplaying, resetting). Can also be
# switched on from the admin page. CREDIT_BUDGET_DAILY auto-pauses processing once that many credits were spent today.
SHOWCASE = os.environ.get("SHOWCASE", "0") == "1"
CREDIT_BUDGET_DAILY = float(os.environ.get("CREDIT_BUDGET_DAILY", "0"))   # 0 = no daily cap
CONTEXT_REPLAY_DIR = os.environ.get("CONTEXT_REPLAY_DIR", "")
CREDIT_BUDGET = float(os.environ.get("CREDIT_BUDGET", "1500"))
FMI_MONITOR = os.environ.get("FMI_MONITOR", "1") != "0"
STATE_FILE = ROOT / "data" / "state.json"

# Incremental updates: when a page we already know changes, fetch it from Context.dev as markdown (fast, 1 credit)
# and let Claude Haiku extract claims from only the changed lines, instead of Context.dev's JSON extraction
# re-reading the whole page. First captures and large changes always use Context.dev's JSON extraction.
FAST_UPDATES = True

# Parallel second opinion: on each change to an official page, "run now" its Context.dev semantic monitor and attach
# the AI judge's verdict to the update (never blocks the fast path; ~1 credit per run).
CONTEXT_MONITORS = True
CONTEXT_MONITOR_KINDS = {"official"}
EVIDENCE_DIR = ROOT / "data" / "evidence"


def site_public_url() -> str:
    """SITE_PUBLIC_URL if set; hosted mode: PUBLIC_BASE_URL/sites; else the tunnel URL saved by serve.sh (read fresh)."""
    url = os.environ.get("SITE_PUBLIC_URL", "") or (f"{PUBLIC_BASE_URL}/sites" if PUBLIC_BASE_URL else "")
    if not url and SITE_URL_FILE.exists():
        url = SITE_URL_FILE.read_text().strip()
    return url.rstrip("/")


@dataclass
class Source:
    id: str
    name: str
    kind: str                 # official | news | social | other
    shape: str                # page | feed  (which extraction schema)
    path: str                 # path under the site base URL, or a full URL
    incident: str | None      # incident template this source feeds
    replay_name: str = ""     # saved-response name for replay mode
    quiet_s: int = 60         # our monitor's interval when nothing is happening
    incident_s: int = 5       # ... and while its incident is active

    @property
    def public_url(self) -> str:      # what Context.dev scrapes (evidence)
        return self.path if self.path.startswith("http") else f"{site_public_url()}/{self.path}"

    @property
    def view_url(self) -> str:        # what a human clicks: served by our backend at /sites/, never the rotating tunnel
        return self.path if self.path.startswith("http") else f"/sites/{self.path}"

    @property
    def check_url(self) -> str:       # what our cheap monitor fetches (trigger only, never shown as evidence)
        return self.path if self.path.startswith("http") else f"{SITE_LOCAL_URL}/{self.path}"


@dataclass
class IncidentTemplate:
    id: str
    name: str
    hazards: list[str]
    area: dict                # {"lat", "lon", "radius_km", "label"}
    sources: list[str] = field(default_factory=list)


@dataclass
class FeedMonitorConfig:
    id: str
    name: str
    url: str
    kind: str = "official"
    interval_s: int = 300


# ---- the fictional Halvik scenario (served from fake_site/) -------------------------------------------------
SOURCES = [
    Source("halvik-rescue", "Halvik Rescue Services", "official", "page", "rescue/", "halvik-storm-surge", "rescue"),
    Source("halvik-herald", "The Halvik Herald", "news", "page", "herald/storm-surge-live.html", "halvik-storm-surge", "herald"),
    Source("halvik-board", "Halvik Board", "social", "feed", "board/", "halvik-storm-surge", "board"),
]

INCIDENT_TEMPLATES = {
    "halvik-storm-surge": IncidentTemplate(
        id="halvik-storm-surge",
        name="Halvik storm surge",
        hazards=["storm_surge"],
        # Fictional city on the Bothnian Sea coast (between real towns, labelled fictional everywhere it appears).
        area={"lat": 61.95, "lon": 21.47, "radius_km": 6, "label": "Halvik (fictional)"},
        sources=[s.id for s in SOURCES],
    ),
}

# ---- real-world official feeds --------------------------------------------------------------------------------
# FMI's CAP feed is official structured data (like a sensor): read directly, no Context.dev credits. Each active
# warning becomes an `official_warning` incident with its polygons; see backend/fmi.py.
FEED_MONITORS = [
    FeedMonitorConfig("fmi-warnings", "FMI weather warnings (CAP feed)", "https://alerts.fmi.fi/cap/feed/atom_en-GB.xml"),
] if FMI_MONITOR else []

# ---- data-driven fictional scenarios (scenario/<id>/scenario.json, see docs/SCENARIOS.md) ------------------------
SCENARIO_ROOT = ROOT / "scenario"
SCENARIO_SPECS: dict[str, dict] = {}       # id -> parsed, validated scenario.json
SCENARIO_ERRORS: dict[str, str] = {}       # folder -> why it was rejected
PAGE_KEYS = ("official", "news", "social")


def _validate_scenario(spec: dict, folder: str) -> None:
    for key in ("id", "name", "hazard", "scale", "language", "area", "sources", "steps"):
        if key not in spec:
            raise ValueError(f"missing '{key}'")
    if spec["id"] != folder:
        raise ValueError(f"id '{spec['id']}' does not match folder '{folder}'")
    if spec["id"] == "halvik":
        raise ValueError("'halvik' is reserved for the hand-written storyline")
    for key in ("lat", "lon", "radius_km", "label"):
        if key not in spec["area"]:
            raise ValueError(f"area is missing '{key}'")
    for key in PAGE_KEYS:
        if not (spec["sources"].get(key) or {}).get("name"):
            raise ValueError(f"sources.{key}.name is missing")
    steps = spec["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list")
    for i, step in enumerate(steps):
        if not isinstance(step.get("pages"), dict) or not step.get("title"):
            raise ValueError(f"step {i} needs a title and a pages object")
        unknown = set(step["pages"]) - set(PAGE_KEYS)
        if unknown:
            raise ValueError(f"step {i} has unknown pages {sorted(unknown)}")
    missing = set(PAGE_KEYS) - set(steps[0]["pages"])
    if missing:
        raise ValueError(f"the first step must include all three pages (missing {sorted(missing)})")


def _load_scenarios() -> None:
    import json
    for path in sorted(SCENARIO_ROOT.glob("*/scenario.json")):
        folder = path.parent.name
        try:
            spec = json.loads(path.read_text())
            _validate_scenario(spec, folder)
        except Exception as exc:                # a broken file must never stop the server
            SCENARIO_ERRORS[folder] = f"{type(exc).__name__}: {exc}"
            continue
        SCENARIO_SPECS[spec["id"]] = spec
        ids = []
        for key in PAGE_KEYS:
            meta = spec["sources"][key]
            kind = meta.get("kind") or ("social" if key == "social" else key)
            src = Source(f"{spec['id']}-{key}", meta["name"], kind, "feed" if key == "social" else "page",
                         f"{spec['id']}/{key}/", spec["id"])
            SOURCES.append(src)
            ids.append(src.id)
        INCIDENT_TEMPLATES[spec["id"]] = IncidentTemplate(spec["id"], spec["name"], [spec["hazard"]], dict(spec["area"]), ids)


_load_scenarios()

SOURCES_BY_ID = {s.id: s for s in SOURCES}
OFFICIAL_NAMES = [s.name for s in SOURCES if s.kind == "official"]
