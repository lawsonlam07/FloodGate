# FloodGate API contract (mock-up v0)

Backend: FastAPI on `http://127.0.0.1:8000`. The frontend is served by the same server (`/` → `frontend/index.html`,
`/incident.html?id=<incident_id>`), so all calls are same-origin, relative paths (`/api/...`).

All timestamps are ISO-8601 UTC strings (`"2026-11-14T10:41:07Z"`). All ids are opaque strings.

## Concepts

- **Incident**: one disaster (e.g. "Halvik storm surge"). Has an area on the map and a severity (1–4).
- **Feed item**: one *claim group*, i.e. the same factual claim seen across one or more sources
  ("Harbour Bridge has collapsed"). It carries a **status** decided by transparent rules, never by the LLM:

  | status         | meaning                                                                 | colour (DESIGN.md)        |
  |----------------|-------------------------------------------------------------------------|---------------------------|
  | `verified`     | an official source states it                                            | verified green `#10B981`  |
  | `corroborated` | ≥ 2 independent non-official origins assert it, nobody denies it        | authority blue `#38BDF8`  |
  | `unverified`   | a single origin (rumour)                                                | amber `#F59E0B`           |
  | `disputed`     | non-official sources both assert and deny it                            | amber `#F59E0B`           |
  | `contradicted` | an official source (directly or as quoted by news) says it is false     | debunked red `#991B1B` / text `#EF4444`, strike-through title |

  `held: true` = the claim would make people take risks (e.g. "water is going down, safe to return") and is not
  confirmed by an official source, so it is shown with a warning ("Held back: unconfirmed reassurance").
- **Origin**: independent sources. Near-identical social posts collapse into one origin; a news article quoting
  an official counts as that official's origin.

## Endpoints

### `GET /api/incidents`
Active incidents, **sorted by severity desc, then updated_at desc**.

```json
{
  "incidents": [
    {
      "id": "halvik-storm-surge",
      "name": "Halvik storm surge",
      "summary": "Red storm surge warning. Saltmarsh residents south of Ferry Street must leave by 16:00. Harbour Bridge closed. 2 rumours contradicted.",
      "severity": 4,
      "severity_label": "critical",            // 4 critical | 3 severe | 2 warning | 1 advisory
      "status": "active",                       // active | monitoring
      "hazards": ["storm_surge"],
      "area": {"lat": 63.2, "lon": 21.3, "radius_km": 6, "label": "Halvik (fictional)"},
      "counts": {"verified": 9, "corroborated": 2, "unverified": 14, "disputed": 0, "contradicted": 3, "held": 2, "total": 28},
      "highlights": [                           // up to 3 short lines for the card: newest verified + newest contradicted
        {"item_id": "it_9f2c", "status": "contradicted", "title": "Harbour Bridge has collapsed"},
        {"item_id": "it_41aa", "status": "verified", "title": "Saltmarsh residents south of Ferry Street must leave by 16:00"}
      ],
      "sources": [{"id": "halvik-rescue", "name": "Halvik Rescue Services", "kind": "official", "url": "https://…/rescue/"}],
      "created_at": "2026-11-14T10:02:11Z",
      "updated_at": "2026-11-14T10:41:07Z"
    }
  ]
}
```

### `GET /api/incidents/{id}`
The incident (same shape as above) plus its feed, **items sorted by updated_at desc (newest first)**, plus the tags
present (for the filter bar).

```json
{
  "incident": { "...": "same shape as in /api/incidents" },
  "tags": {
    "status": ["verified", "corroborated", "unverified", "contradicted"],
    "category": ["warning", "evacuation", "closure", "damage", "utilities", "observation", "instruction", "all_clear"],
    "place": ["Harbour Bridge", "Saltmarsh", "Lindholm Island"]
  },
  "items": [
    {
      "id": "it_9f2c",
      "incident_id": "halvik-storm-surge",
      "title": "Harbour Bridge has collapsed",
      "summary": "Asserted by 3 social posts (near-identical, counted as 1 origin). Halvik Rescue Services says it is false.",
      "status": "contradicted",
      "held": false,
      "safety_direction": "protective",         // protective | risk_taking | neutral
      "category": "damage",
      "condition": "collapsed",
      "hazard": "storm_surge",
      "place": "Harbour Bridge",
      "tags": ["damage", "storm_surge", "Harbour Bridge"],
      "origins": {"asserting": 1, "denying": 1},
      "sources": [
        {
          "source_id": "halvik-board",
          "name": "Halvik Board",
          "kind": "social",                     // official | news | social | other
          "url": "https://…/board/",
          "stance": "asserts",                  // asserts | reports_unverified | denies
          "attributed_to": null,
          "author": "halvik_alerts_now",        // social posts only, else null
          "author_signals": "Joined 12 November 2026 · 31 followers · 212 reposts",   // observable signals, never labels
          "evidence_quote": "Harbour Bridge has COLLAPSED!!",
          "seen_at": "2026-11-14T10:02:40Z"
        }
      ],
      "log": [                                  // verification log, oldest first
        {"at": "2026-11-14T10:02:40Z", "kind": "seen", "text": "First seen on Halvik Board (@halvik_alerts_now)"},
        {"at": "2026-11-14T10:03:05Z", "kind": "seen", "text": "The Halvik Herald relays it as unverified (posts circulating on social media)"},
        {"at": "2026-11-14T10:03:31Z", "kind": "status", "text": "unverified → contradicted: Halvik Rescue Services says it is false"}
      ],
      "community": {"still_true": 2, "not_true": 11, "reports": 1},
      "first_seen": "2026-11-14T10:02:40Z",
      "updated_at": "2026-11-14T10:03:31Z"
    }
  ]
}
```

### `GET /api/alerts`
Banner alerts, newest first. The frontend shows the newest non-dismissed one as a big banner at the top of the home
page (dismiss is client-side, keyed by alert id).

```json
{
  "alerts": [
    {
      "id": "al_77",
      "level": "potential",          // potential (change detected, verifying) | critical (verified danger) | debunk (official says a rumour is false) | info
      "title": "Halvik Rescue Services updated its situation page",
      "body": "Change detected 2 s ago. Verifying through Context.dev…",
      "incident_id": "halvik-storm-surge",   // may be null
      "item_id": null,                        // may be set for critical/debunk
      "created_at": "2026-11-14T10:41:07Z"
    }
  ]
}
```

### `POST /api/items/{item_id}/vote`  — "Is this still true?" (Waze-style)
Body `{"still_true": true}` → `{"community": {"still_true": 3, "not_true": 11, "reports": 1}}`.
Community votes are shown as signals only; they never change the status.

### `POST /api/items/{item_id}/report`
Body `{"reason": "misleading" | "wrong_location" | "outdated" | "other", "note": "optional text"}` →
`{"community": {...}}`.

### `GET /api/monitors`
Our own monitors (cheap change detection; Context.dev is only called when something changes).

```json
{
  "monitors": [
    {
      "id": "halvik-rescue", "name": "Halvik Rescue Services", "type": "page", "kind": "official",
      "url": "https://…/rescue/", "mode": "incident", "interval_s": 10,
      "checks": 120, "changes": 3, "last_check": "…", "last_change": "…", "last_error": null,
      "note": null
    },
    {
      "id": "fmi-warnings", "name": "FMI weather warnings (CAP feed)", "type": "feed", "kind": "official",
      "url": "https://alerts.fmi.fi/cap/feed/rss_en-GB.rss", "mode": "quiet", "interval_s": 300,
      "checks": 4, "changes": 1, "last_check": "…", "last_change": "…", "last_error": null,
      "note": "16 active warnings (0 orange/red)"
    }
  ]
}
```

### `GET /api/activity?limit=50`
Live log of external calls, newest first (the "API activity" panel).

```json
{
  "activity": [
    {"at": "…", "service": "context.dev", "action": "scrape+extract", "target": "https://…/rescue/",
     "latency_ms": 15703, "credits": 5, "cache": "miss", "ok": true, "detail": "23 claims"},
    {"at": "…", "service": "anthropic", "action": "group claims", "target": "halvik-storm-surge",
     "latency_ms": 7600, "credits": null, "cache": null, "ok": true, "detail": "claude-haiku-5-5 · 23 claims"},
    {"at": "…", "service": "monitor", "action": "change detected", "target": "halvik-rescue",
     "latency_ms": 41, "credits": null, "cache": null, "ok": true, "detail": "text hash changed"}
  ],
  "credits_spent": 30
}
```

### `GET /api/stream` — Server-Sent Events
`EventSource('/api/stream')`. Event types (each `data:` is JSON):

| event      | data                                   | frontend reaction                                  |
|------------|----------------------------------------|----------------------------------------------------|
| `incident` | incident summary (as in /api/incidents) | home: re-render that card + map area; incident page: header |
| `item`     | full feed item                          | incident page: insert/replace item, move to top    |
| `alert`    | alert                                   | home: show banner                                   |
| `activity` | one activity row                        | activity panel: prepend                             |
| `monitor`  | one monitor (as in /api/monitors)       | monitors panel: replace row                         |

A `: ping` comment is sent every 15 s to keep the connection alive.

### `POST /api/admin/check/{monitor_id}`
Force an immediate check of one monitor (demo button). → `{"ok": true}`.

---

## v0.2 changes (map rings, weather warnings, key facts)

### Incident: new/changed fields (in `/api/incidents`, `/api/incidents/{id}` and SSE `incident`)

```json
{
  "kind": "verified_board",          // verified_board = multi-source claims board (Halvik) | official_warning = one official warning (FMI CAP)
  "area": {
    "lat": 61.95, "lon": 21.47, "radius_km": 6, "label": "Halvik (fictional)",
    "rings": [                        // verified_board only, innermost first; draw outermost first.
      {"radius_km": 1.5, "severity": 4},   // colour shift outwards from the epicentre
      {"radius_km": 3.0, "severity": 3},
      {"radius_km": 4.5, "severity": 2},
      {"radius_km": 6.0, "severity": 1}
    ],
    "polygons": null                  // official_warning: [[[lat, lon], ...], ...] (warning areas, may be several); rings null
  },
  "valid": {"from": "2026-11-15T03:00:00Z", "to": "2026-11-15T22:00:00Z"},   // official_warning only, else null
  "source_label": "Finnish Meteorological Institute (CAP feed)"               // official_warning only, else null
}
```

Severity for official warnings comes from the warning colour: yellow → 1 (advisory), orange → 3 (severe), red → 4 (critical).
There can be 10–20 of these at once (real FMI data), mostly yellow.

### Incident detail: `key_facts` (the "core important facts", computed by rules)

```json
"key_facts": {
  "confirmed":    [{"item_id": "…", "title": "Saltmarsh residents south of Ferry Street must leave by 16:00", "category": "evacuation", "severity": 4}],
  "what_to_do":   [{"item_id": "…", "title": "Stay away from the shoreline, piers and Harbour Bridge"}],
  "not_true":     [{"item_id": "…", "title": "Harbour Bridge has collapsed", "debunked_by": "Halvik Rescue Services"}],
  "dont_act_yet": [{"item_id": "…", "title": "Water going down on Pier Street"}],
  "developing":   [{"item_id": "…", "title": "Power out on Lindholm", "status": "corroborated", "origins": 2}]
}
```
- `confirmed`: verified, non-instruction items, highest severity first (max 6).
- `what_to_do`: verified instructions from official sources only (max 5) — never AI advice.
- `not_true`: contradicted items that someone actually asserted or relayed (rumours), newest first.
- `dont_act_yet`: held items (unconfirmed reassurance).
- `developing`: not verified, but ≥ 2 asserting origins or corroborated (max 4).

### Item: new field
`"importance": "key" | "normal"` — key = verified with severity ≥ 3, verified instruction, contradicted rumour, or held.
The feed defaults to showing key items; "All updates" shows everything.

---

## v0.3 changes (severity filter, working source links, scenario admin)

### Source links
- The fictional sites are served by the backend itself at `/sites/…` (`/sites/rescue/`, `/sites/herald/storm-surge-live.html`,
  `/sites/board/`), so links never depend on the tunnel.
- Every `sources[].url` (items and incidents) is a link a viewer can open: `/sites/…` for the fictional sites, the real URL
  for real sources (FMI). New field `sources[].captured_from` = the public URL Context.dev actually scraped (may be stale;
  shown as small print only), `null` for feeds.

### Home page severity filter (frontend only)
Chips L4 · L3 · L2 · L1 with counts; toggle each; default L4–L2 on (L1 hidden); remembered per viewer. Applies to map and list.

### Scenario admin ("timestep" the fictional Halvik storyline)

`GET /api/admin/scenario`
```json
{
  "scenario": "halvik",
  "current_step": 2,                        // -1 = not started
  "steps": [
    {"n": 0, "clock": "10:30", "title": "Before the storm", "description": "Calm. Weekend forecast only.",
     "changes": ["rescue", "herald", "board"]},
    {"n": 1, "clock": "11:10", "title": "Orange storm surge warning", "description": "…", "changes": ["rescue", "herald"]}
  ],
  "pages": [{"id": "rescue", "source_id": "halvik-rescue", "name": "Halvik Rescue Services", "url": "/sites/rescue/"}],
  "pipeline": {"halvik-rescue": {"source_id": "halvik-rescue", "stage": "done", "at": "…", "elapsed_ms": 24100,
               "detail": "2 status changes, 1 alerts"}, "halvik-herald": null, "halvik-board": null},   // latest event per source
  "autoplay": {"running": false, "interval_s": 60}
}
```
`POST /api/admin/scenario/step` body `{"to": 3}` or `{"next": true}` → publishes that step's page versions into `fake_site/`
and nudges the affected monitors to check immediately; returns the scenario state.
`POST /api/admin/scenario/reset` → puts every page back to step 0, deletes the Halvik incident (items, alerts, snapshots) so the
story can be replayed from scratch; returns the scenario state.
`POST /api/admin/scenario/autoplay` body `{"running": true, "interval_s": 60}` → advance one step every interval until the end.

New SSE events:

| event      | data                                                                                           |
|------------|------------------------------------------------------------------------------------------------|
| `scenario` | scenario state (as GET above), on every change                                                 |
| `pipeline` | `{"source_id": "halvik-rescue", "stage": "detected", "at": "…", "elapsed_ms": 0, "detail": "…"}` |

`pipeline.stage` is one of `detected` (monitor saw a change) → `extracting` (Context.dev call started) → `extracted`
(detail: "34 claims, 10 new, 3 withdrawn") → `grouping` → `grouped` → `done` (detail: "2 status changes, 3 alerts"), or
`failed` / `skipped` (detail says why). `elapsed_ms` counts from `detected`, so the admin page can show a live stopwatch per source.

Activity rows with `service: "pipeline"` are logged for the terminal stages (`done`, `failed`, `skipped`): `action` = the stage,
`target` = source_id, `latency_ms` = elapsed since detection, `detail` as in the event. Rows with `service: "scenario"` log
step changes and resets.

### v0.4: pipeline events carry the incident
`pipeline` events (and the scenario state's `pipeline` field) now also include `"source_name"` and `"incident_id"`
(the incident the source feeds, which may not exist yet), so any page can show a live "checking…" toast per incident.
Item `log` entries (`kind`: seen / status / removed, with `at`) tell the frontend what changed in an `item` event.
Incidents also carry `"checked_at"`: the last time any of their sources was checked. `updated_at` now only moves when
something visible changed (new item, status change, source added or withdrawn). Show e.g. "updated 3m ago · checked 5s ago".

### v0.5: Context.dev monitor verdicts (parallel second opinion)
When an official page changes, the backend also "runs now" its Context.dev semantic monitor (in parallel, ~7–25 s).
New SSE event `context_check` and `context_checks` (newest first, max 10) in `GET /api/incidents/{id}`:
```json
{"source_id": "halvik-rescue", "source_name": "Halvik Rescue Services", "incident_id": "halvik-storm-surge",
 "status": "changed",              // changed | no_change | baseline (monitor just created) | failed | skipped
 "title": "Evacuation order extended to Old Harbour", "summary": "…", "importance": "high", "confidence": 0.99,
 "latency_ms": 24100, "elapsed_ms": 24900, "credits": 1, "at": "…",
 "pipeline_new_claims": 3,         // what our own fast pipeline extracted from the same update (null if unknown)
 "disagreement": false}            // true = Context.dev saw no meaningful change but we extracted new claims: review
```

---

## v0.6: many scenarios (admin control centre)

All fictional scenarios (Halvik + `scenario/<id>/scenario.json`, see docs/SCENARIOS.md) are managed by one engine.
A scenario that hasn't started has no pages, no monitors and no incident. Starting it = going to step 0.

`GET /api/admin/scenarios`
```json
{
  "scenarios": [
    {
      "id": "halvik", "name": "Halvik storm surge", "hazard": "storm_surge", "scale": "major", "language": "en",
      "region": "Bothnian Sea coast", "summary": "…", "incident_id": "halvik-storm-surge",
      "area": {"lat": 61.95, "lon": 21.47, "radius_km": 6, "label": "Halvik (fictional)"},
      "current_step": -1,                                  // -1 = not started; len(steps)-1 = finished
      "steps": [{"n": 0, "clock": "10:30", "title": "…", "description": "…", "expect": "…", "changes": ["official", "news"]}],
      "pages": [{"id": "official", "source_id": "halvik-rescue", "name": "Halvik Rescue Services", "kind": "official",
                 "url": "/sites/rescue/"}],
      "autoplay": {"running": false, "interval_s": 60},
      "pipeline": {"halvik-rescue": {"stage": "done", "…": "latest pipeline event or null"}}
    }
  ],
  "credits_spent": 210
}
```
- `POST /api/admin/scenarios/{id}/step` `{"to": 2}` or `{"next": true}` → publishes that step's pages, starts the
  scenario's monitors if needed and nudges them; returns the scenario.
- `POST /api/admin/scenarios/{id}/reset` → deletes the scenario's incident (items, alerts), pages, snapshots and stops its
  monitors (back to "not started"); returns the scenario.
- `POST /api/admin/scenarios/{id}/autoplay` `{"running": true, "interval_s": 45}` → one step per interval until the end.
- `POST /api/admin/scenarios/bulk` `{"action": "start" | "next" | "reset" | "autoplay_on" | "autoplay_off",
  "ids": ["…"], "interval_s": 60, "stagger_s": 5}` → apply to several scenarios (e.g. the filtered list); `stagger_s` spaces
  out starts/steps so they don't all hit Context.dev in the same second. Returns `{"scenarios": [...]}`.
- SSE `scenario` events now carry ONE scenario object (as in the list above) with its `id`.
- The old single-scenario endpoints (`/api/admin/scenario…`) keep working as aliases for `halvik`.

### v0.8: pause processing, sites index
- `GET /api/admin/control` → `{"paused": false, "since": "…"}`; `POST /api/admin/control` `{"paused": true}`.
  Paused: page monitors stop checking (mode `"paused"` in /api/monitors), autoplay and bulk runs stop, no Context.dev or
  LLM calls; FMI feed sync continues (free). Resume re-checks every page against what was last processed, so edits made
  while paused are picked up. Persisted across restarts. SSE `control` event on change; `/api/health` has `"paused"`.
- `/sites/` (and the tunnel root) is now an index of every scenario's fictional pages; links are live for started scenarios.

---

## v0.9: hosted demo (central server, read-only visitors, showcase)

- **Admin token.** When the server has `ADMIN_TOKEN` set, every `POST /api/admin/...` needs header `X-Admin-Token: <token>`
  (or `Authorization: Bearer <token>`), else `401 {"detail": "admin token required"}`. All GETs stay public, so anyone can
  watch the admin page read-only. Public POSTs (vote, report) need no token.
- **Control state** (`GET /api/admin/control`, SSE `control`) now:
```json
{"paused": false, "since": "…", "reason": null,          // reason e.g. "daily credit budget of 2000 reached"
 "showcase": {"running": true, "max_concurrent": 3, "interval_s": 75, "hold_s": 180},
 "auth_required": true, "credit_budget_daily": 2000, "credits_today": 312.0}
```
- `POST /api/admin/showcase` `{"running": true, "max_concurrent": 3, "interval_s": 75, "hold_s": 180}` (all but running
  optional). Showcase = the server runs scenarios by itself for every viewer: keeps `max_concurrent` running (alternating
  sizes), a step every `interval_s`, holds finished ones `hold_s` then resets them and starts the next. Pausing stops it
  from advancing; the daily budget auto-pauses with a `reason`.
- Hosted mode: fictional sites are served by the backend at `/sites/…` (no tunnel); `/api/health.site_public_url` shows it.

## v0.10: Gemini, spoken bulletins (ElevenLabs), history (Tiger Data)

- **LLM provider.** Grouping and fast-path extraction run on Google Gemini (`gemini-3.5-flash-lite` by default) with
  Claude as an automatic fallback when a call fails (`backend/llm.py`). Activity rows now use `"service": "gemini"` (or
  `"anthropic"` after a fallback; the `detail` then says so). `GET /api/health` adds `"llm_provider"`, and
  `"grouping_model"` is the configured model.
- **Spoken bulletin.** `GET /api/incidents/{id}/bulletin?lang=en` returns the script, built by rules from the key facts
  (no LLM): in the pages' own language it reads the officials' sentences word for word (`"verbatim": true`); in English it
  reads the board's lines. Scenario bulletins always start with "Fictional exercise".
```json
{"lang": "pl", "id": "e26b5d72ce8b10eb397f", "chars": 518, "verbatim": true, "languages": ["en", "pl"],
 "lines": [{"kind": "intro|heading|fact|outro", "text": "…"}], "text": "…",
 "audio": "/api/audio/e26b5d72ce8b10eb397f.mp3",        // null until someone has pressed Listen for this script
 "can_voice": true}
```
  `POST /api/incidents/{id}/bulletin/audio?lang=pl` voices it with ElevenLabs (about 1-4 s) or returns the cached file:
  `{"audio": "/api/audio/<id>.mp3", "cached": false, "chars_today": 114, "char_budget_daily": 20000}`. Errors: `409` while
  processing is paused (cached audio still plays), `503` with `detail` when the daily character budget is reached or
  ElevenLabs fails. Audio is cached per script, so each state of the board is voiced at most once per language.
  `GET /api/audio/{id}.mp3` serves it. Activity rows: `"service": "elevenlabs", "action": "voice bulletin"`.
- **History (Tiger Data).** Every SSE event is also written to Postgres + TimescaleDB hypertables when
  `TIGER_DATABASE_URL` is set. `GET /api/incidents/{id}/history`:
```json
{"available": true, "bucket": "1 minute",
 "timeline": [{"t": "…", "verified": 6, "corroborated": 0, "unverified": 11, "disputed": 2, "contradicted": 1, "held": 4, "severity": 3}],
 "debunked":  [{"item_id": "…", "title": "…", "first_kind": "social", "after_s": 178, "status": "contradicted"}],
 "confirmed": [{"item_id": "…", "title": "…", "first_kind": "social", "after_s": 312, "status": "verified"}],
 "stats": {"items": 32, "confirmed": 1, "median_confirm_s": 312, "debunked": 2, "median_debunk_s": 641,
           "updates": 7, "p50_update_ms": 8901, "p90_update_ms": 15111}}
```
  Times count from the item's first report on the board; "debunked"/"confirmed" only include items that stood unconfirmed
  first. The timeline is gap-filled per bucket (15 s for runs under 15 min, then 1 min, then 5 min). Without the database:
  `{"available": false, "reason": "…"}`. `GET /api/history/usage?hours=24` returns calls, credits, average latency and
  failures per service per hour (a TimescaleDB continuous aggregate). `GET /api/health` adds
  `"history": {"enabled", "ok", "written", "dropped", "queued", "error"}`.
- **Context.dev monitor slots.** Plans allow a fixed number of monitors (`CONTEXT_MONITOR_LIMIT`, default 10), fewer than
  the official pages. When a new one is needed and the plan is full, the least recently used FloodGate monitor is deleted
  first. If no slot can be freed, the `context_check` has `"status": "skipped"` (not `failed`) with the reason in
  `summary`, and that source rests for 10 minutes.
- **Superseded warnings.** Official warnings replace each other (orange → red → lowered) but each stays `verified`. Per
  hazard, only the newest warning-level item counts for the incident's `severity`, its summary and `key_facts.confirmed`;
  the others carry `"superseded": true` in `GET /api/incidents/{id}` items (SSE `item` events omit the field; the next
  detail fetch has it). Newest = first seen latest, then the later clock time stated on the official page.
- **Showcase pacing (v0.10).** Defaults: 3 at a time, a step every 45 s, finished scenarios held 120 s. Steps of all
  showcase scenarios take turns, evenly spaced (`interval_s / max_concurrent` apart: a change every ~15 s). Finished
  scenarios no longer hold a slot. `only_when_watched` (default true; `POST /api/admin/showcase` accepts it): while no
  page has the live stream open, showcase waits and spends nothing, keeps finished results on the board, and resumes
  within seconds of a visitor arriving. `GET /api/admin/control` adds `"viewers"` (open live-update connections).
- **Admin token check.** `POST /api/admin/auth` (with `X-Admin-Token`) returns `{"ok": true}` for the right token and
  `401` otherwise; the admin page unlocks only after it, and re-checks a saved token on load. After 10 wrong tokens from
  one address within 10 minutes, every admin POST from that address gets `429` until the window passes.
