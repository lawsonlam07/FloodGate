"""Time-series history in Tiger Data (Postgres + TimescaleDB). On when TIGER_DATABASE_URL is set.

The live board runs from the in-memory store (fast; it feeds the Server-Sent Events). Everything the store
publishes is also written here, so we can answer questions over time:
  - how long did a rumour take to be debunked, or a report to be confirmed by an official source?
  - how fast does a changed page reach the board?
  - what did each service (Context.dev, LLM, ...) cost and how fast was it, per hour?

Tables (hypertables, partitioned by time): item_history, incident_history, api_calls, pipeline_stages, events.
`run` = the incident's created_at, so a scenario that is reset and replayed starts a fresh history.

Writes go through a queue to one background thread that inserts in batches. A slow or unreachable database never
blocks or breaks the board; it only loses history (counted in `dropped`).
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_history (
  time timestamptz NOT NULL, incident_id text NOT NULL, run text NOT NULL, item_id text NOT NULL,
  status text, held boolean, severity int, importance text, category text, condition text,
  asserting int, denying int, n_sources int, first_kind text, rumour boolean, first_seen timestamptz, title text);
SELECT create_hypertable('item_history', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS item_history_run ON item_history (incident_id, run, time DESC);

CREATE TABLE IF NOT EXISTS incident_history (
  time timestamptz NOT NULL, incident_id text NOT NULL, run text NOT NULL, kind text, name text, severity int,
  verified int, corroborated int, unverified int, disputed int, contradicted int, held int, total int,
  lat double precision, lon double precision, radius_km double precision);
SELECT create_hypertable('incident_history', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS incident_history_run ON incident_history (incident_id, run, time DESC);

CREATE TABLE IF NOT EXISTS api_calls (
  time timestamptz NOT NULL, service text, action text, target text, latency_ms int, credits double precision,
  ok boolean, detail text);
SELECT create_hypertable('api_calls', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS pipeline_stages (
  time timestamptz NOT NULL, incident_id text, source_id text, stage text, elapsed_ms int, detail text);
SELECT create_hypertable('pipeline_stages', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS events (
  time timestamptz NOT NULL, event text NOT NULL, incident_id text, data jsonb);
SELECT create_hypertable('events', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE);

SELECT add_retention_policy(t, INTERVAL '90 days', if_not_exists => TRUE)
  FROM unnest(ARRAY['item_history', 'incident_history', 'api_calls', 'pipeline_stages', 'events']) AS t;

CREATE MATERIALIZED VIEW IF NOT EXISTS api_usage_hourly
  WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
  SELECT time_bucket('1 hour', time) AS bucket, service, count(*) AS calls,
         sum(coalesce(credits, 0)) AS credits, avg(latency_ms)::int AS avg_latency_ms,
         count(*) FILTER (WHERE NOT ok) AS failures
  FROM api_calls GROUP BY bucket, service WITH NO DATA;
SELECT add_continuous_aggregate_policy('api_usage_hourly', start_offset => INTERVAL '3 days',
  end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);
"""

INSERT = {
    "item_history": "INSERT INTO item_history VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
    "incident_history": "INSERT INTO incident_history VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
    "api_calls": "INSERT INTO api_calls VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
    "pipeline_stages": "INSERT INTO pipeline_stages VALUES (%s,%s,%s,%s,%s,%s)",
    "events": "INSERT INTO events VALUES (%s,%s,%s,%s)",
}


def _ts(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class History:
    def __init__(self, url: str):
        self.url = url
        self.q: queue.Queue = queue.Queue(maxsize=20000)
        self.ok, self.error = False, None
        self.written = self.dropped = 0
        self._read_lock = threading.Lock()
        self._read_conn = None
        threading.Thread(target=self._writer, name="tiger-history", daemon=True).start()

    # ---- recording (called on the event loop; never blocks) ---------------------------------------------------
    def record(self, event: str, data: dict, store) -> None:
        row = self._row(event, data, store)
        if row is None:
            return
        try:
            self.q.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    @staticmethod
    def _run(store, incident_id: str | None) -> str | None:
        inc = store.incidents.get(incident_id) if incident_id else None
        return inc.created_at if inc else None

    def _row(self, event: str, d: dict, store):
        t = datetime.now(timezone.utc)
        if event == "item":
            run = self._run(store, d.get("incident_id"))
            if run is None:
                return None
            sources = d.get("sources") or []
            first = min(sources, key=lambda s: s.get("seen_at") or "~", default={})
            return ("item_history", (t, d["incident_id"], run, d["id"], d.get("status"), d.get("held"),
                                     d.get("severity"), d.get("importance"), d.get("category"), d.get("condition"),
                                     (d.get("origins") or {}).get("asserting"), (d.get("origins") or {}).get("denying"),
                                     len(sources), first.get("kind"), any(s.get("stance") != "denies" for s in sources),
                                     _ts(d.get("first_seen")), d.get("title")))
        if event == "incident":
            run = self._run(store, d.get("id"))
            if run is None:
                return None
            c, a = d.get("counts") or {}, d.get("area") or {}
            return ("incident_history", (t, d["id"], run, d.get("kind"), d.get("name"), d.get("severity"),
                                         c.get("verified"), c.get("corroborated"), c.get("unverified"),
                                         c.get("disputed"), c.get("contradicted"), c.get("held"), c.get("total"),
                                         a.get("lat"), a.get("lon"), a.get("radius_km")))
        if event == "activity":
            return ("api_calls", (t, d.get("service"), d.get("action"), d.get("target"), d.get("latency_ms"),
                                  d.get("credits"), d.get("ok"), (d.get("detail") or "")[:500]))
        if event == "pipeline":
            return ("pipeline_stages", (t, d.get("incident_id"), d.get("source_id"), d.get("stage"),
                                        d.get("elapsed_ms"), (d.get("detail") or "")[:500]))
        if event in ("alert", "context_check", "scenario", "control"):
            return ("events", (t, event, d.get("incident_id") or d.get("id"), json.dumps(d, ensure_ascii=False)))
        return None                                  # "monitor" heartbeats are not worth keeping

    # ---- background writer -------------------------------------------------------------------------------------
    def _writer(self) -> None:
        import psycopg
        backoff, pending = 1, []
        while True:
            try:
                with psycopg.connect(self.url, autocommit=True, connect_timeout=10) as conn:
                    conn.execute(SCHEMA)
                    self.ok, self.error, backoff = True, None, 1
                    while True:
                        pending = pending or self._drain()
                        if pending:
                            self._write(conn, pending)
                            self.written += len(pending)
                            pending = []
            except Exception as exc:  # unreachable / dropped connection: keep the batch, retry with backoff
                self.ok, self.error = False, f"{type(exc).__name__}: {exc}"[:300]
                print(f"FloodGate: Tiger history unavailable ({self.error}); retrying in {backoff} s", flush=True)
                if len(pending) > 5000:
                    self.dropped += len(pending)
                    pending = []
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _drain(self, wait_s: float = 1.0, max_rows: int = 2000) -> list:
        rows = []
        try:
            rows.append(self.q.get(timeout=wait_s))
            while len(rows) < max_rows:
                rows.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return rows

    @staticmethod
    def _write(conn, rows: list) -> None:
        by_table: dict[str, list] = {}
        for table, values in rows:
            by_table.setdefault(table, []).append(values)
        with conn.transaction(), conn.cursor() as cur:
            for table, values in by_table.items():
                cur.executemany(INSERT[table], values)

    # ---- reads (blocking; endpoints call them in a worker thread) --------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        import psycopg
        from psycopg.rows import dict_row
        with self._read_lock:
            for attempt in (1, 2):
                try:
                    if self._read_conn is None or self._read_conn.closed:
                        self._read_conn = psycopg.connect(self.url, autocommit=True, connect_timeout=10,
                                                          row_factory=dict_row)
                    return self._read_conn.execute(sql, params).fetchall()
                except psycopg.OperationalError:
                    self._read_conn = None
                    if attempt == 2:
                        raise
        return []

    def flush(self, timeout_s: float) -> None:
        """Give the writer a moment to empty the queue (used at shutdown)."""
        end = time.monotonic() + timeout_s
        while self.ok and not self.q.empty() and time.monotonic() < end:
            time.sleep(0.1)

    def status(self) -> dict:
        return {"enabled": True, "ok": self.ok, "written": self.written, "dropped": self.dropped,
                "queued": self.q.qsize(), "error": self.error}

    # ---- analytics ---------------------------------------------------------------------------------------------
    def incident_history(self, incident_id: str, run: str) -> dict:
        started = _ts(run) or datetime.now(timezone.utc)
        minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
        bucket = "15 seconds" if minutes < 15 else "1 minute" if minutes < 180 else "5 minutes"
        timeline = self.query("""
            SELECT time_bucket_gapfill(%s::interval, time) AS t,
                   locf(last(verified, time)) AS verified, locf(last(corroborated, time)) AS corroborated,
                   locf(last(unverified, time)) AS unverified, locf(last(disputed, time)) AS disputed,
                   locf(last(contradicted, time)) AS contradicted, locf(last(held, time)) AS held,
                   locf(last(severity, time)) AS severity
            FROM incident_history
            WHERE incident_id = %s AND run = %s AND time >= %s::timestamptz AND time <= now()
            GROUP BY t ORDER BY t""", (bucket, incident_id, run, run))
        verdicts = self.query("""
            SELECT item_id, last(title, time) AS title, last(status, time) AS status, bool_or(rumour) AS rumour,
                   (array_agg(first_kind ORDER BY time))[1] AS first_kind, min(first_seen) AS first_seen,
                   min(time) FILTER (WHERE status = 'verified') AS verified_at,
                   min(time) FILTER (WHERE status = 'contradicted') AS contradicted_at,
                   min(time) FILTER (WHERE status <> 'verified') AS first_open_at,
                   min(time) FILTER (WHERE status <> 'contradicted') AS first_standing_at,
                   max(severity) AS severity
            FROM item_history WHERE incident_id = %s AND run = %s
            GROUP BY item_id""", (incident_id, run))
        latency = self.query("""
            SELECT count(*) AS updates,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms) AS p50_ms,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY elapsed_ms) AS p90_ms
            FROM pipeline_stages WHERE incident_id = %s AND stage = 'done' AND time >= %s::timestamptz""",
                             (incident_id, run))[0]

        def secs(a, b):
            return round((a - b).total_seconds()) if a and b else None

        confirmed, debunked = [], []
        for v in verdicts:
            # only items that were on the board unconfirmed first (a report, later confirmed by an official source)
            if (v["verified_at"] and v["first_kind"] in ("social", "news") and v["first_open_at"]
                    and v["first_open_at"] < v["verified_at"]):
                confirmed.append({"item_id": v["item_id"], "title": v["title"], "first_kind": v["first_kind"],
                                  "after_s": secs(v["verified_at"], v["first_seen"]), "status": v["status"]})
            if v["contradicted_at"] and v["rumour"] and v["first_standing_at"] and v["first_standing_at"] < v["contradicted_at"]:
                debunked.append({"item_id": v["item_id"], "title": v["title"], "first_kind": v["first_kind"],
                                 "after_s": secs(v["contradicted_at"], v["first_seen"]), "status": v["status"]})
        median = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        return {
            "available": True, "run": run, "bucket": bucket,
            "timeline": [{**r, "t": r["t"].strftime("%Y-%m-%dT%H:%M:%SZ")} for r in timeline],
            "confirmed": sorted(confirmed, key=lambda r: r["after_s"] or 0),
            "debunked": sorted(debunked, key=lambda r: r["after_s"] or 0),
            "stats": {
                "items": len(verdicts),
                "confirmed": len(confirmed), "median_confirm_s": median([r["after_s"] for r in confirmed if r["after_s"] is not None]),
                "debunked": len(debunked), "median_debunk_s": median([r["after_s"] for r in debunked if r["after_s"] is not None]),
                "updates": latency["updates"],
                "p50_update_ms": round(latency["p50_ms"]) if latency["p50_ms"] is not None else None,
                "p90_update_ms": round(latency["p90_ms"]) if latency["p90_ms"] is not None else None,
            },
        }

    def usage(self, hours: int = 24) -> dict:
        rows = self.query("""
            SELECT bucket, service, calls, credits, avg_latency_ms, failures FROM api_usage_hourly
            WHERE bucket >= now() - make_interval(hours => %s) ORDER BY bucket, service""", (hours,))
        return {"available": True, "hours": hours,
                "rows": [{**r, "bucket": r["bucket"].strftime("%Y-%m-%dT%H:%M:%SZ"), "credits": float(r["credits"] or 0)}
                         for r in rows]}


def make() -> History | None:
    url = os.environ.get("TIGER_DATABASE_URL", "").strip()
    if not url or os.environ.get("HISTORY", "1") == "0":
        return None
    return History(url)
