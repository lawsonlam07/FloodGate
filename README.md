# FloodGate

Live, verified situation board for emergencies (Junction 2026, Context.dev challenge). Social media is fast and wrong;
officials are right and slow. FloodGate watches both and shows what is actually confirmed, with a source on every line.

# Hosting

## Local

```bash
uv sync                         # creates .venv from pyproject.toml + uv.lock
cp .env.example .env            # add all the API Keys needed.

./serve.sh                      # terminal 1: fictional sites + public tunnel (URL saved to data/site_public_url)
./run.sh                        # terminal 2: backend + frontend on http://127.0.0.1:8000
```

- **Board:** http://127.0.0.1:8000 · **Demo view** (board + behind the scenes): http://127.0.0.1:8000/demo.html
- **Scenario control centre:** http://127.0.0.1:8000/admin.html: start, step, autoplay or reset the fictional
  disasters (13 across Europe, minor to catastrophic), switch on showcase mode, or pause all processing.
- **Fictional sites:** http://127.0.0.1:8000/sites/ (pages exist while their scenario is started).
- Starting a scenario costs roughly 15 Context.dev credits, a full storyline about 40. Real FMI weather warnings are free.
- To start from scratch, stop the server and delete `data/`.

## Server

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

Put a TLS-terminating proxy (e.g. Caddy) in front for HTTPS.

# How it Works

```
our monitors (cheap, every 5–60 s)          backend/monitors.py   page hash / official feeds; triggers only
   │ change detected                           (+ in parallel: Context.dev semantic monitor "run now", second opinion)
   ▼
Context.dev scrape + JSON extraction         backend/extraction.py evidence: claims with verbatim quotes
   │ (small updates: Context.dev markdown + LLM on the changed lines only)
   ▼
LLM grouping + rule guardrails               backend/grouping.py   "same claim?" across sources and languages
   │ claim groups (one call per update, however many pages changed)
   ▼
status ladder (plain rules, no LLM)          backend/rules.py      verified / corroborated / unverified /
   │                                                                disputed / contradicted, held-back reassurances
   ▼
store + alerts + Server-Sent Events          backend/store.py, backend/app.py → frontend/
```

- API contract: `docs/API.md`. Scenario format: `docs/SCENARIOS.md`.
- Which sources are official is configured by us (`backend/config.py`, scenario files), never decided by the LLM.
- `poc/` holds the proof-of-concept scripts (`uv run poc/extract.py`, `uv run poc/grouping.py`,
  `uv run poc/monitor_experiment.py`).
- Without uv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (exported from uv.lock).
- Everything in `scenario/` is fictional: invented towns, organisations, newspapers and accounts.

## Sources
To gather information, we use context.dev to scrape information from sites such as government, news and social media.

<img width="1490" height="849" alt="image" src="https://github.com/user-attachments/assets/ff2c7c05-a86c-461f-9457-277f55a49c14" />
<img width="1698" height="867" alt="image" src="https://github.com/user-attachments/assets/395c2470-7245-44c9-b46c-36b2cbaf6831" />
<img width="1186" height="716" alt="image" src="https://github.com/user-attachments/assets/3b2044eb-e03b-4651-987a-83a069f9a559" />

## Features
<img width="1640" height="876" alt="image" src="https://github.com/user-attachments/assets/98207436-b68d-414b-919b-2fe1f19c2f71" />
<img width="1532" height="894" alt="image" src="https://github.com/user-attachments/assets/76849179-1180-4a33-b9c3-2ec60269a669" />



