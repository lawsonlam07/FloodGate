# FloodGate

Live, verified situation board for emergencies (Junction 2026, Context.dev challenge). In an emergency, information
floods in: social media is fast and often wrong, officials are right but slow. FloodGate is the gate between the two: it
holds back the flood of rumours and lets through what official sources confirm, with a source on every line.

## Run it locally

```bash
uv sync                         # creates .venv from pyproject.toml + uv.lock
cp .env.example .env            # add CONTEXT_DEV_API_KEY and GEMINI_API_KEY (others optional, see below)

./serve.sh                      # terminal 1: fictional sites + public tunnel (URL saved to data/site_public_url)
                                #   uses cloudflared if installed (sturdier: sudo pacman -S cloudflared), else localhost.run
./run.sh                        # terminal 2: backend + frontend on http://127.0.0.1:8000
```

- **Board:** http://127.0.0.1:8000 · **Demo view** (board + behind the scenes): http://127.0.0.1:8000/demo.html
- **Scenario control centre:** http://127.0.0.1:8000/admin.html: start, step, autoplay or reset the fictional
  disasters (13 across Europe, minor to catastrophic), switch on showcase mode, or pause all processing.
- **Fictional sites:** http://127.0.0.1:8000/sites/ (pages exist while their scenario is started).
- Starting a scenario costs roughly 15 Context.dev credits, a full storyline about 40. Real official warnings are free.
- To start from scratch, stop the server and delete `data/`.

## Real official sources

Alongside the fictional scenarios, the board shows live warnings read directly from official structured feeds (no
Context.dev credits; see `backend/feeds.py`). Each active warning becomes an official warning on the map and list, with
its level mapped onto our L1 to L4 scale, and ends when it leaves the feed.

| Feed | Covers | Official level → ours |
|---|---|---|
| [FMI](https://en.ilmatieteenlaitos.fi/warnings) (CAP) | Weather and sea warnings, Finland | yellow L1, orange L3, red L4 |
| [GDACS](https://www.gdacs.org) (UN and European Commission) | Floods, tropical cyclones, volcanoes, droughts, large wildfires, worldwide | green L1, orange L3, red L4 |
| [USGS](https://earthquake.usgs.gov/earthquakes/feed/) | Earthquakes M4.5+ in the past day, worldwide | PAGER alert if given, else M5.5+ L2, M6.5+ L3 |
| [Environment Agency](https://environment.data.gov.uk/flood-monitoring/doc/reference) | Flood alerts and warnings, England | alert L2, warning L3, severe L4 |
| [MeteoAlarm](https://meteoalarm.org) (EUMETNET) | Orange and red weather warnings from 37 European national weather services (Finland via FMI) | orange L3, red L4 |

`OFFICIAL_FEEDS=fmi-warnings,gdacs` limits which are watched; `FMI_MONITOR=0` turns them all off. GDACS and
MeteoAlarm content is CC BY 4.0 and credited on every warning page. MeteoAlarm names regions by code only; their
outlines come from `backend/meteoalarm_regions.json` (built by `deploy/build_meteoalarm_regions.py`).

**Context.dev reads the official page behind every serious real warning** (L2 and above, `backend/enrich.py`): the feed
is the tripwire, Context.dev captures the human-facing page it links to (a GDACS event report, an Environment Agency
flood page, Météo-France's vigilance page, ...) and extracts key facts and the official instructions, each with a
word-for-word quote checked against the page. They show as "What to do" on the board and the warning page. One
capture per warning (about 5 credits), at most `ENRICH_DAILY` (60) a day, never while paused; `ENRICH_WARNINGS=0`
turns it off.

**Fictional: on / off** (admin page header) hides every fictional scenario from the board, leaving only real
warnings. Hidden scenarios are frozen, not reset: switching back on carries on exactly where they were.

## Partner technology

| | What it does here | Key (in `.env`) |
|---|---|---|
| **Context.dev** | Captures every changed page: scrape + structured claim extraction with verbatim quotes; semantic monitors as a second opinion | `CONTEXT_DEV_API_KEY` |
| **Google Gemini** | Groups claims that say the same thing across sources and languages; extracts claims from the changed lines of a page (`gemini-3.5-flash-lite`: 12/12 grouping checks, 3-5 s per call) | `GEMINI_API_KEY` |
| **ElevenLabs** | Spoken bulletin of each incident's key facts, in English and the pages' language. The script is built by rules (official sentences word for word), never by an LLM | `ELEVENLABS_API_KEY` |
| **Tiger Data** | Postgres + TimescaleDB: every change kept as a time series. Powers "rumour → debunked in 3 min", update latency, and hourly cost per service | `TIGER_DATABASE_URL` |
| Claude (optional) | Automatic fallback if a Gemini call fails | `ANTHROPIC_API_KEY` |

Everything except Context.dev and one LLM key is optional: without a key, that feature is simply off.

## Host it

One central server makes every Context.dev and LLM call; visitors only watch. In hosted mode the server serves the
fictional sites itself, so no tunnel is needed.

```bash
docker build -t floodgate .
docker run -p 8000:8000 -v floodgate-data:/app/data --env-file .env \
  -e PUBLIC_BASE_URL=https://your.domain -e ADMIN_TOKEN=<secret> \
  -e CREDIT_BUDGET_DAILY=3000 floodgate
```

| Variable | Meaning |
|---|---|
| `PUBLIC_BASE_URL` | public address of the server; Context.dev scrapes `PUBLIC_BASE_URL/sites/…` |
| `ADMIN_TOKEN` | required for every admin action; without it the admin page is a read-only view |
| `SHOWCASE=1` | the server runs scenarios by itself for every viewer (also switchable from the admin page) |
| `CREDIT_BUDGET_DAILY` | auto-pause once this many Context.dev credits were spent today |
| `CREDIT_BUDGET` | cap per server run (default 1500) |
| `LLM_PROVIDER` / `GEMINI_MODEL` | `gemini` (default when `GEMINI_API_KEY` is set) or `anthropic`; Gemini model id |
| `CONTEXT_MAX_CONCURRENT` | Context.dev captures in flight at once (default 4); bursts queue instead of timing out |
| `CONTEXT_MONITOR_LIMIT` | monitors your Context.dev plan allows (default 10); the least recently used FloodGate monitor is replaced when full |
| `ELEVENLABS_CHAR_BUDGET_DAILY` | cap on characters voiced per day (default 20000); audio is cached per bulletin |

Put a TLS-terminating proxy (e.g. Caddy) in front for HTTPS.

## How it works

```
our monitors (cheap, every 5–60 s)          backend/monitors.py   page hash / official feeds; triggers only
   │ change detected                           (+ in parallel: Context.dev semantic monitor "run now", second opinion)
   ▼
Context.dev scrape + JSON extraction         backend/extraction.py evidence: claims with verbatim quotes
   │ (small updates: Context.dev markdown + LLM on the changed lines only)
   ▼
LLM grouping + rule guardrails               backend/grouping.py   "same claim?" across sources and languages (Gemini)
   │ claim groups (one call per update, however many pages changed)
   ▼
status ladder (plain rules, no LLM)          backend/rules.py      verified / corroborated / unverified /
   │                                                                disputed / contradicted, held-back reassurances
   ▼
store + alerts + Server-Sent Events          backend/store.py, backend/app.py → frontend/
   │ every event also goes to                  backend/history.py    Tiger Data hypertables (history, analytics)
   ▼ key facts → rule-built script             backend/bulletin.py   ElevenLabs voice, cached per script
```

- Frontend: plain HTML + JS modules, one set of pages for phone and desktop (responsive). Styles, fonts, icons and
  Leaflet are served by us, no CDNs at page load: `uv run frontend/build.py` builds `css/tailwind.css` (standalone
  Tailwind, theme in `frontend/tailwind.config.js`) and `frontend/vendor/` (icons are picked up from the code
  automatically). `run.sh` and `deploy.sh` run it for you.
- API contract: `docs/API.md`. Scenario format: `docs/SCENARIOS.md`.
- Which sources are official is configured by us (`backend/config.py`, scenario files), never decided by the LLM.
- `poc/` holds the proof-of-concept scripts and evals (`uv run poc/extract.py`, `uv run poc/grouping.py out/<run> gemini`,
  `uv run poc/debunk_eval.py`, `uv run poc/monitor_experiment.py`).
- Without uv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (exported from uv.lock).
- Everything in `scenario/` is fictional: invented towns, organisations, newspapers and accounts.

Sources
<img width="1698" height="867" alt="image" src="https://github.com/user-attachments/assets/395c2470-7245-44c9-b46c-36b2cbaf6831" />
<img width="1490" height="849" alt="image" src="https://github.com/user-attachments/assets/ff2c7c05-a86c-461f-9457-277f55a49c14" />
<img width="1186" height="716" alt="image" src="https://github.com/user-attachments/assets/3b2044eb-e03b-4651-987a-83a069f9a559" />


