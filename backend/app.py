"""FloodGate API server: REST + Server-Sent Events, our monitors, and the static frontend.

Run:  uv run uvicorn backend.app:app --port 8000      (or ./run.sh)
"""

from __future__ import annotations

import asyncio
import hmac
import time
from collections import defaultdict, deque
import json
import logging
import mimetypes
import os
import re
from html import escape as _h
from urllib.parse import quote
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import URL
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from .config import (ADMIN_TOKEN, CONTEXT_REPLAY_DIR, CREDIT_BUDGET, FEED_MONITORS, PUBLIC_BASE_URL, ROOT, SCENARIO_ERRORS,
                     SITE_LOCAL_URL, SOURCES, site_public_url)
from .context_client import ContextClient, ReplayContextClient
from .context_monitors import ContextMonitors
from .monitors import CapFeedMonitor, Scheduler
from . import bulletin
from . import history as tiger
from . import llm as llms
from .pipeline import Pipeline
from .scenario import SITE_DIR, ScenarioEngine
from .store import Store

class _QuietShutdown(logging.Filter):
    """On Ctrl-C uvicorn cancels open live-update streams and logs a traceback for each: expected, not an error."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, asyncio.CancelledError) and "graceful shutdown" in str(exc))


logging.getLogger("uvicorn.error").addFilter(_QuietShutdown())

store = Store()
state: dict = {}


def make_context_client():
    if CONTEXT_REPLAY_DIR:
        return ReplayContextClient(ROOT / CONTEXT_REPLAY_DIR, {s.path: s.replay_name for s in SOURCES})
    key = os.environ.get("CONTEXT_DEV_API_KEY")
    return ContextClient(key) if key else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.load()
    store.history = tiger.make()
    if store.history:                              # starting point for the timelines: the state as of this start
        for inc in store.incidents.values():
            if inc.is_live():
                store.history.record("incident", store.incident_view(inc), store)
                for it in store.visible_items(inc.id):
                    store.history.record("item", it.to_api(), store)
    ctx = make_context_client()
    llm = llms.make()
    key = os.environ.get("CONTEXT_DEV_API_KEY")
    monitors_api = ContextMonitors(key, store) if key and not CONTEXT_REPLAY_DIR else None
    pipeline = Pipeline(store, ctx, llm, monitors_api)
    # Page monitors exist only for started scenarios (the engine adds/removes them); feed monitors always run.
    monitors = [CapFeedMonitor(f.id, f.name, f.kind, f.url, f.interval_s, f.interval_s) for f in FEED_MONITORS]
    scheduler = Scheduler(store, monitors, pipeline.on_change, pipeline.in_incident)
    scenario = ScenarioEngine(store, scheduler, pipeline)
    scenario.restore()
    state.update(pipeline=pipeline, scheduler=scheduler, scenario=scenario, context_ready=ctx is not None)
    scheduler.start()
    yield
    await scenario.stop_all()
    await scheduler.stop()
    store.save()
    if store.history:
        store.history.flush(3)
    # Blocking Context.dev / LLM calls run in worker threads, and Python would wait for them (up to ~90 s) before
    # exiting. The state is saved, so exit now instead of making Ctrl-C hang.
    print("FloodGate: state saved, exiting.", flush=True)
    os._exit(0)


app = FastAPI(title="FloodGate", lifespan=lifespan)


class AdminGuard:
    """With ADMIN_TOKEN set (hosted demo), every admin action needs the token; GETs stay public (read-only view).

    Plain ASGI middleware on purpose: it must not buffer or wrap the long-lived /api/stream responses.
    Guessing is throttled: after MAX_FAILURES wrong tokens from one address within WINDOW_S, it gets 429 for a while
    (the client address comes from the proxy's X-Forwarded-For; uvicorn runs with --proxy-headers)."""

    MAX_FAILURES, WINDOW_S = 10, 600

    def __init__(self, app):
        self.app = app
        self.failures: dict[str, deque] = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        if (scope["type"] == "http" and ADMIN_TOKEN and scope["method"] == "POST"
                and scope["path"].startswith("/api/admin/")):
            ip = (scope.get("client") or ("?", 0))[0]
            recent = self.failures[ip]
            while recent and time.monotonic() - recent[0] > self.WINDOW_S:
                recent.popleft()
            if len(recent) >= self.MAX_FAILURES:
                await JSONResponse({"detail": "too many wrong tokens; try again in a few minutes"},
                                   status_code=429)(scope, receive, send)
                return
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            given = headers.get("x-admin-token") or headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given.encode(), ADMIN_TOKEN.encode()):
                recent.append(time.monotonic())
                await JSONResponse({"detail": "admin token required"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(AdminGuard)


class FreshPages:
    """Pages, scripts and styles change with every deploy: tell browsers to check for a newer copy each time (a cheap
    304 when unchanged), so nobody ends up with old and new script files mixed. Fonts and images keep normal caching."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path in ("/", "/demo") or path.endswith((".html", ".js", ".css"))) or path.startswith(("/api/", "/sites/")):
            return await self.app(scope, receive, send)

        async def send_fresh(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": headers + [(b"cache-control", b"no-cache")]}
            await send(message)
        await self.app(scope, receive, send_fresh)


app.add_middleware(FreshPages)


# ---- read API ------------------------------------------------------------------------------------------------
@app.get("/api/incidents")
def incidents():
    return {"incidents": store.incident_list()}


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: str):
    detail = store.incident_detail(incident_id)
    if detail is None:
        raise HTTPException(404, "unknown incident")
    return detail


@app.get("/api/incidents/{incident_id}/history")
async def incident_history(incident_id: str):
    """Over-time view from Tiger Data: status counts per minute, time to confirm / debunk, update latency."""
    inc = store.incidents.get(incident_id)
    if inc is None or store.hidden(incident_id):
        raise HTTPException(404, "unknown incident")
    if store.history is None:
        return {"available": False, "reason": "TIGER_DATABASE_URL not set"}
    try:
        return await asyncio.to_thread(store.history.incident_history, incident_id, inc.created_at)
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}


# ---- spoken bulletins (ElevenLabs) ---------------------------------------------------------------------------
_voicing: dict[str, asyncio.Lock] = {}


def _bulletin(incident_id: str, lang: str) -> tuple[dict, dict]:
    detail = store.incident_detail(incident_id)
    if detail is None:
        raise HTTPException(404, "unknown incident")
    langs = bulletin.languages(detail["incident"])
    if lang not in langs:
        raise HTTPException(400, f"bulletin languages for this incident: {langs}")
    return detail, bulletin.script(detail, lang)


@app.get("/api/incidents/{incident_id}/bulletin")
def get_bulletin(incident_id: str, lang: str = "en"):
    """The bulletin script (rule-built, no LLM) and its audio URL if it has been voiced already."""
    detail, b = _bulletin(incident_id, lang)
    cached = bulletin.audio_path(b["id"]).exists()
    return {**b, "languages": bulletin.languages(detail["incident"]),
            "audio": f"/api/audio/{b['id']}.mp3" if cached else None,
            "can_voice": bool(os.environ.get("ELEVENLABS_API_KEY"))}


@app.post("/api/incidents/{incident_id}/bulletin/audio")
async def voice_bulletin(incident_id: str, lang: str = "en"):
    """Voice the current bulletin with ElevenLabs (cached per script, so each board state is voiced once)."""
    _, b = _bulletin(incident_id, lang)
    path = bulletin.audio_path(b["id"])
    if not path.exists() and state.get("scenario") and state["scenario"].control["paused"]:
        raise HTTPException(409, "processing is paused: no new audio")
    lock = _voicing.setdefault(b["id"], asyncio.Lock())
    async with lock:                                   # two listeners pressing play = one ElevenLabs call
        cached = path.exists()
        t0 = asyncio.get_running_loop().time()
        try:
            _, cost = await asyncio.to_thread(bulletin.synthesize, b)
        except bulletin.TTSError as exc:
            store.add_activity({"service": "elevenlabs", "action": "voice bulletin", "target": incident_id,
                                "latency_ms": None, "credits": None, "cache": None, "ok": False, "detail": str(exc)})
            raise HTTPException(503, str(exc))
        if not cached:
            store.add_activity({"service": "elevenlabs", "action": "voice bulletin", "target": incident_id,
                                "latency_ms": round((asyncio.get_running_loop().time() - t0) * 1000), "credits": None,
                                "cache": None, "ok": True, "detail": f"{lang} · {cost} characters · {bulletin.MODEL}"})
    _voicing.pop(b["id"], None)
    return {"audio": f"/api/audio/{b['id']}.mp3", "cached": cached, "id": b["id"],
            "chars_today": bulletin.chars_used_today(), "char_budget_daily": bulletin.CHAR_BUDGET_DAILY}


@app.get("/api/audio/{digest}.mp3")
def audio(digest: str):
    if not (len(digest) == 20 and all(c in "0123456789abcdef" for c in digest)):
        raise HTTPException(404, "no such audio")
    path = bulletin.audio_path(digest)
    if not path.exists():
        raise HTTPException(404, "no such audio")
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/history/usage")
async def history_usage(hours: int = 24):
    """Calls, credits, latency and failures per service per hour (TimescaleDB continuous aggregate)."""
    if store.history is None:
        return {"available": False, "reason": "TIGER_DATABASE_URL not set"}
    try:
        return await asyncio.to_thread(store.history.usage, max(1, min(hours, 24 * 30)))
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}


@app.get("/api/alerts")
def alerts():
    return {"alerts": [a for a in store.alerts if not store.hidden_alert(a)]}


@app.get("/api/monitors")
def monitors():
    return {"monitors": list(store.monitors.values())}


@app.get("/api/activity")
def activity(limit: int = 50):
    return {"activity": list(store.activity)[: max(1, min(limit, 300))], "credits_spent": store.credits_spent}


@app.get("/api/health")
def health():
    return {"ok": True, "mode": "replay" if CONTEXT_REPLAY_DIR else "live",
            "context_dev": state.get("context_ready", False), "grouping_model": state["pipeline"].llm_label if state.get("pipeline") else None,
            "llm_provider": state["pipeline"].llm.provider if state.get("pipeline") and state["pipeline"].llm else None,
            "site_public_url": site_public_url() or None, "site_local_url": SITE_LOCAL_URL,
            "paused": bool(state["scenario"].control["paused"]) if state.get("scenario") else False,
            "credit_budget": CREDIT_BUDGET,
            "history": store.history.status() if store.history else {"enabled": False}}


# ---- community signals (shown, never used for verdicts) ------------------------------------------------------
class Vote(BaseModel):
    still_true: bool


class Report(BaseModel):
    reason: Literal["misleading", "wrong_location", "outdated", "other"]
    note: str = Field(default="", max_length=500)


def _item(item_id: str):
    it = store.items.get(item_id)
    if it is None:
        raise HTTPException(404, "unknown item")
    return it


@app.post("/api/items/{item_id}/vote")
def vote(item_id: str, body: Vote):
    it = _item(item_id)
    it.community["still_true" if body.still_true else "not_true"] += 1
    store.publish("item", it.to_api())
    store.save()
    return {"community": it.community}


@app.post("/api/items/{item_id}/report")
def report(item_id: str, body: Report):
    it = _item(item_id)
    it.community["reports"] += 1
    store.add_activity({"service": "community", "action": "report", "target": item_id, "latency_ms": None,
                        "credits": None, "cache": None, "ok": True, "detail": body.reason})
    store.publish("item", it.to_api())
    store.save()
    return {"community": it.community}


# ---- admin -----------------------------------------------------------------------------------------------------
@app.post("/api/admin/check/{monitor_id}")
def check_now(monitor_id: str):
    if not state["scheduler"].check_now(monitor_id):
        raise HTTPException(404, "unknown monitor")
    return {"ok": True}


class StepBody(BaseModel):
    to: int | None = None
    next: bool = False


class AutoplayBody(BaseModel):
    running: bool
    interval_s: int | None = Field(default=None, ge=10, le=3600)


def _engine() -> ScenarioEngine:
    return state["scenario"]


def _known(sid: str) -> str:
    if sid not in _engine().defs:
        raise HTTPException(404, f"unknown scenario '{sid}'")
    return sid


async def _step(sid: str, body: StepBody) -> dict:
    eng = _engine()
    try:
        if body.next:
            return await eng.next(sid)
        if body.to is not None:
            return await eng.go_to(sid, body.to)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    raise HTTPException(422, 'give {"to": n} or {"next": true}')


class BulkBody(BaseModel):
    action: Literal["start", "next", "reset", "autoplay_on", "autoplay_off"]
    ids: list[str] = Field(default_factory=list, max_length=200)
    interval_s: int | None = Field(default=None, ge=10, le=3600)
    stagger_s: float = Field(default=5, ge=0, le=120)


class ControlBody(BaseModel):
    paused: bool


class FictionalBody(BaseModel):
    enabled: bool


@app.post("/api/admin/auth")
def admin_auth():
    """Token check for the admin page's unlock form (the guard above rejects a wrong token before this runs)."""
    return {"ok": True, "auth_required": bool(ADMIN_TOKEN)}


@app.get("/api/admin/control")
def control():
    return _engine().control_view()


@app.post("/api/admin/control")
async def set_control(body: ControlBody):
    return await _engine().set_paused(body.paused)


@app.post("/api/admin/fictional")
async def set_fictional(body: FictionalBody):
    """Show or hide every fictional scenario on the board. Hiding freezes them as they are (nothing resets), so
    switching back on carries on where they were."""
    return await _engine().set_fictional(body.enabled)


class ShowcaseBody(BaseModel):
    running: bool
    max_concurrent: int | None = Field(default=None, ge=1, le=8)
    interval_s: int | None = Field(default=None, ge=30, le=600)
    hold_s: int | None = Field(default=None, ge=30, le=3600)
    only_when_watched: bool | None = None


@app.post("/api/admin/showcase")
async def showcase(body: ShowcaseBody):
    return await _engine().set_showcase(body.running, body.max_concurrent, body.interval_s, body.hold_s,
                                        body.only_when_watched)


@app.get("/api/admin/scenarios")
def scenarios():
    return {"scenarios": _engine().views(), "credits_spent": store.credits_spent, "rejected": SCENARIO_ERRORS}


@app.post("/api/admin/scenarios/bulk")
async def scenarios_bulk(body: BulkBody):
    try:
        return {"scenarios": await _engine().bulk(body.action, body.ids, body.interval_s, body.stagger_s)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/scenarios/{sid}/step")
async def scenario_step_any(sid: str, body: StepBody):
    return await _step(_known(sid), body)


@app.post("/api/admin/scenarios/{sid}/reset")
async def scenario_reset_any(sid: str):
    return await _engine().reset(_known(sid))


@app.post("/api/admin/scenarios/{sid}/autoplay")
async def scenario_autoplay_any(sid: str, body: AutoplayBody):
    return await _engine().set_autoplay(_known(sid), body.running, body.interval_s)


# Aliases for the original single-scenario admin page (Halvik). Its "reset" meant "replay from the start".
@app.get("/api/admin/scenario")
def scenario_state():
    return _engine().view("halvik")


@app.post("/api/admin/scenario/step")
async def scenario_step(body: StepBody):
    return await _step("halvik", body)


@app.post("/api/admin/scenario/reset")
async def scenario_reset():
    await _engine().reset("halvik")
    return await _engine().go_to("halvik", 0)


@app.post("/api/admin/scenario/autoplay")
async def scenario_autoplay(body: AutoplayBody):
    return await _engine().set_autoplay("halvik", body.running, body.interval_s)


# ---- live stream -------------------------------------------------------------------------------------------------
@app.get("/api/stream")
async def stream(request: Request):
    async def events():
        q = store.subscribe()
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            return          # server shutting down: uvicorn cancels open streams after the graceful timeout
        finally:
            store.unsubscribe(q)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- the fictional sites, served for humans at /sites (links never depend on the tunnel) ------------------------------
SITE_DIR.mkdir(parents=True, exist_ok=True)          # generated at runtime by the scenario engine
# Slim containers have no /etc/mime.types: make sure self-hosted fonts get their proper type.
mimetypes.add_type("font/woff2", ".woff2")


class SiteFiles(StaticFiles):
    """data/sites/ as published by the scenario engine, plus every site of a scenario that isn't running (its whole
    storyline, see ScenarioEngine.preview_page), so any fictional site can be visited at any time."""

    async def get_response(self, path: str, scope) -> Response:
        engine = state.get("scenario")
        if engine is not None and scope["method"] in ("GET", "HEAD"):
            for target in (path, f"{path}/index.html"):
                page = engine.preview_page(target)
                if page is None:
                    continue
                if target != path and not scope["path"].endswith("/"):      # relative links need the trailing slash
                    url = URL(scope=scope)
                    return RedirectResponse(url.replace(path=url.path + "/"))
                return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            photo = engine.photo(path) if engine is not None and exc.status_code == 404 else None
            if photo is None:
                raise
            return FileResponse(photo)


app.mount("/sites", SiteFiles(directory=SITE_DIR, html=True), name="sites")

# ---- frontend (mounted last so /api wins) --------------------------------------------------------------------------
FRONTEND = ROOT / "frontend"
FRONTEND.mkdir(exist_ok=True)

# Search engines and link previews (WhatsApp, Slack, X) don't run JavaScript, so the board and each incident page get
# their title, description, preview tags and schema.org data from the server: that is how people find an incident
# and see, before clicking, which official source it rests on.
SITE_TITLE = "FloodGate"
SITE_TAGLINE = "FloodGate: holding back the flood of information in an emergency"
SITE_DESCRIPTION = ("In an emergency, information floods in: posts, rumours, news and official updates. FloodGate holds "
                    "back the flood and lets through what official sources confirm, with a source on every line.")


def _base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def _page_with_meta(page: str, title: str, description: str, url: str, jsonld: dict | None = None,
                    share_title: str | None = None) -> HTMLResponse:
    html = (FRONTEND / page).read_text()
    html = re.sub(r"<title>.*?</title>\s*", "", html, count=1, flags=re.S)
    html = re.sub(r'<meta name="description"[^>]*>\s*', "", html, count=1)
    tags = (f"<title>{_h(title)}</title>\n"
            f'<meta name="description" content="{_h(description)}">\n'
            f'<link rel="canonical" href="{_h(url)}">\n'
            '<meta property="og:type" content="website">\n<meta property="og:site_name" content="FloodGate">\n'
            f'<meta property="og:title" content="{_h(share_title or title)}">\n<meta property="og:description" content="{_h(description)}">\n'
            f'<meta property="og:url" content="{_h(url)}">\n<meta name="twitter:card" content="summary">\n')
    if jsonld:
        tags += f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False).replace("</", "<\\/")}</script>\n'
    return HTMLResponse(html.replace("</head>", tags + "</head>", 1))


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def board_page(request: Request):
    base = _base_url(request)
    return _page_with_meta("index.html", SITE_TITLE, SITE_DESCRIPTION, f"{base}/", share_title=SITE_TAGLINE, jsonld=
                           {"@context": "https://schema.org", "@type": "WebSite", "name": "FloodGate", "url": f"{base}/",
                            "description": SITE_DESCRIPTION})


# The demo landing page lives at /demo (the link in the submission); the old /demo.html address still leads there.
@app.get("/demo", include_in_schema=False)
def demo_page(request: Request):
    base = _base_url(request)
    return _page_with_meta("demo.html", "FloodGate demo", "In an emergency, information floods in. FloodGate lets through "
                           "only what official sources confirm, live on real official warnings and on 39 demo sites "
                           "from 13 fictional emergencies.", f"{base}/demo", share_title=SITE_TAGLINE)


@app.get("/demo/", include_in_schema=False)
@app.get("/demo.html", include_in_schema=False)
def demo_redirect():
    return RedirectResponse("/demo", status_code=308)


@app.get("/incident.html", include_in_schema=False)
def incident_page(request: Request, id: str = ""):
    base = _base_url(request)
    inc = store.incidents.get(id)
    if inc is None or store.hidden(id):
        return _page_with_meta("incident.html", "Incident · FloodGate", SITE_DESCRIPTION, f"{base}/incident.html")
    v = store.incident_view(inc)
    absolute = lambda u: f"{base}{u}" if (u or "").startswith("/") else (u or "")
    sources = sorted(v.get("sources") or [], key=lambda s: s.get("kind") != "official")
    officials = [s["name"] for s in sources if s.get("kind") == "official"]
    title = f"{v['name']} · FloodGate"                                   # the tab: short
    share_title = f"{v['name']}: verified updates" + (f" from {officials[0]}" if officials else "")   # previews / search
    summary = v.get("summary") or ""
    description = (summary if len(summary) <= 200 else summary[:197].rsplit(" ", 1)[0] + "…")
    if sources:
        description += " Sources: " + ", ".join(s["name"] for s in sources) + "."
    url = f"{base}/incident.html?id={quote(inc.id)}"
    area = v.get("area") or {}
    jsonld = {
        "@context": "https://schema.org", "@type": "SpecialAnnouncement", "name": v["name"], "text": summary,
        "datePosted": v.get("created_at"), "dateModified": v.get("updated_at"), "url": url,
        "spatialCoverage": {"@type": "Place", "name": area.get("label") or v["name"],
                            **({"geo": {"@type": "GeoCoordinates", "latitude": area["lat"], "longitude": area["lon"]}}
                               if area.get("lat") is not None and area.get("lon") is not None else {})},
        "isBasedOn": [{"@type": "WebPage", "name": s["name"], "url": absolute(s.get("url"))} for s in sources],
        "publisher": {"@type": "Organization", "name": "FloodGate", "url": f"{base}/"},
    }
    return _page_with_meta("incident.html", title, description, url, jsonld, share_title=share_title)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
