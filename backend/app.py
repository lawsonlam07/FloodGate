"""FloodGate API server: REST + Server-Sent Events, our monitors, and the static frontend.

Run:  uv run uvicorn backend.app:app --port 8000      (or ./run.sh)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import (ADMIN_TOKEN, CONTEXT_REPLAY_DIR, CREDIT_BUDGET, FEED_MONITORS, ROOT, SCENARIO_ERRORS, SITE_LOCAL_URL, SOURCES,
                     site_public_url)
from .context_client import ContextClient, ReplayContextClient
from .context_monitors import ContextMonitors
from .monitors import CapFeedMonitor, Scheduler
from .pipeline import ANTHROPIC_MODEL, Pipeline
from .scenario import ScenarioEngine
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
    ctx = make_context_client()
    claude = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
    key = os.environ.get("CONTEXT_DEV_API_KEY")
    monitors_api = ContextMonitors(key, store) if key and not CONTEXT_REPLAY_DIR else None
    pipeline = Pipeline(store, ctx, claude, monitors_api)
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
    # Blocking Context.dev / Claude calls run in worker threads, and Python would wait for them (up to ~90 s) before
    # exiting. The state is saved, so exit now instead of making Ctrl-C hang.
    print("FloodGate: state saved, exiting.", flush=True)
    os._exit(0)


app = FastAPI(title="FloodGate", lifespan=lifespan)


class AdminGuard:
    """With ADMIN_TOKEN set (hosted demo), every admin action needs the token; GETs stay public (read-only view).

    Plain ASGI middleware on purpose: it must not buffer or wrap the long-lived /api/stream responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] == "http" and ADMIN_TOKEN and scope["method"] == "POST"
                and scope["path"].startswith("/api/admin/")):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            given = headers.get("x-admin-token") or headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given.encode(), ADMIN_TOKEN.encode()):
                await JSONResponse({"detail": "admin token required"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(AdminGuard)


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


@app.get("/api/alerts")
def alerts():
    return {"alerts": store.alerts}


@app.get("/api/monitors")
def monitors():
    return {"monitors": list(store.monitors.values())}


@app.get("/api/activity")
def activity(limit: int = 50):
    return {"activity": list(store.activity)[: max(1, min(limit, 300))], "credits_spent": store.credits_spent}


@app.get("/api/health")
def health():
    return {"ok": True, "mode": "replay" if CONTEXT_REPLAY_DIR else "live",
            "context_dev": state.get("context_ready", False), "grouping_model": ANTHROPIC_MODEL,
            "site_public_url": site_public_url() or None, "site_local_url": SITE_LOCAL_URL,
            "paused": bool(state["scenario"].control["paused"]) if state.get("scenario") else False,
            "credit_budget": CREDIT_BUDGET}


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


@app.get("/api/admin/control")
def control():
    return _engine().control_view()


@app.post("/api/admin/control")
async def set_control(body: ControlBody):
    return await _engine().set_paused(body.paused)


class ShowcaseBody(BaseModel):
    running: bool
    max_concurrent: int | None = Field(default=None, ge=1, le=8)
    interval_s: int | None = Field(default=None, ge=30, le=600)
    hold_s: int | None = Field(default=None, ge=30, le=3600)


@app.post("/api/admin/showcase")
async def showcase(body: ShowcaseBody):
    return await _engine().set_showcase(body.running, body.max_concurrent, body.interval_s, body.hold_s)


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
(ROOT / "fake_site").mkdir(exist_ok=True)            # generated at runtime by the scenario engine
app.mount("/sites", StaticFiles(directory=ROOT / "fake_site", html=True), name="sites")

# ---- frontend (mounted last so /api wins) --------------------------------------------------------------------------
FRONTEND = ROOT / "frontend"
FRONTEND.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
