"""In-memory state, persisted to data/state.json, plus a tiny pub/sub bus for Server-Sent Events.

All mutation happens on the asyncio event loop (blocking API calls run in threads and hand results back),
so no locks are needed here. Every published event is also recorded in Tiger Data when it is configured
(backend/history.py), which keeps the full history over time.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import images, rules
from .config import INCIDENT_TEMPLATES, OFFICIAL_NAMES, SOURCES, SOURCES_BY_ID, STATE_FILE
from .grouping import Group


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


STATUS_ORDER = {s: i for i, s in enumerate(rules.STATUSES)}


@dataclass
class Item:
    """A feed item = one claim group plus its rule-based verdict and history."""
    id: str
    incident_id: str
    group: Group
    status: str = "unverified"
    held: bool = False
    origins: dict = field(default_factory=lambda: {"asserting": 0, "denying": 0})
    summary: str = ""
    severity: int = 1
    log: list = field(default_factory=list)
    community: dict = field(default_factory=lambda: {"still_true": 0, "not_true": 0, "reports": 0})
    first_seen: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)

    def title(self) -> str:
        """Display title: the newest statement from the most authoritative source in the group.

        The group label is fixed when the LLM creates the group; facts evolve (1.6 m -> 2.1 m), so show the latest
        official wording when there is one. Members carry positive propositions, so this works for denials too.
        """
        rank = {"official": 3, "news": 2, "social": 1}
        best = max(self.group.members, default=None,
                   key=lambda m: (rank.get(m.get("source_kind"), 0), m.get("seen_at") or ""))
        return (best or {}).get("proposition") or self.group.label

    def _most_common(self, key: str, default: str = "other") -> str:
        values = [m.get(key) for m in self.group.members if m.get(key)]
        return max(set(values), key=values.count) if values else default

    def reassess(self) -> str | None:
        """Recompute the verdict. Returns a log line if the status changed."""
        before = self.status
        verdict = rules.assess(self.group.members, OFFICIAL_NAMES)
        self.status, self.held, self.origins = verdict["status"], verdict["held"], verdict["origins"]
        self.summary = rules.summarize(self.group.members, OFFICIAL_NAMES) or "No source states this any more."
        self.severity = rules.item_severity({"status": self.status, "condition": self.group.condition,
                                             "title": self.title(), "members": self.group.members})
        if self.status != before and self.group.members:
            reason = {
                "verified": "an official source states it",
                "corroborated": "independent sources agree",
                "contradicted": "an official source says it is false",
                "disputed": "sources disagree",
                "unverified": "only one origin",
            }[self.status]
            return f"{before} → {self.status}: {reason}"
        return None

    def is_rumour(self) -> bool:
        """Someone actually asserted or relayed it (not just an official denial of something nobody said)."""
        return any(m["stance"] != "denies" for m in self.group.members)

    def importance(self, category: str) -> str:
        if self.held or (self.status == "contradicted" and self.is_rumour()):
            return "key"
        if self.status == "verified" and (self.severity >= 3 or category == "instruction"):
            return "key"
        return "normal"

    def to_api(self) -> dict:
        members = self.group.members
        safety = "risk_taking" if any(m.get("safety_effective") == "risk_taking" for m in members) else \
            self._most_common("safety_effective", "neutral")
        category, hazard = self._most_common("category"), self._most_common("hazard", "none")
        place = self._most_common("location", "") or None     # a real place named on the page, never the subject
        sources = []
        for m in members:
            post = m.get("_post") or {}
            signals = " · ".join(x for x in (
                post.get("account_joined") and f"Joined {post['account_joined'].removeprefix('Joined ')}",
                post.get("follower_count") is not None and f"{post['follower_count']} followers",
                post.get("repost_count") is not None and f"{post['repost_count']} reposts",
            ) if x) or None
            registered = SOURCES_BY_ID.get(m["source_id"])
            sources.append({
                "source_id": m["source_id"], "name": m.get("source_name"), "kind": m.get("source_kind"),
                # link for humans, computed now from the registry (the scraped tunnel URL goes stale)
                "url": registered.view_url if registered else m.get("source_url"),
                "captured_from": m.get("source_url") if registered else None,
                "stance": m["stance"], "attributed_to": m.get("attributed_to"),
                "author": post.get("author_handle"), "author_signals": signals if post else None,
                "evidence_quote": m.get("evidence_quote"), "time_stated": m.get("time_stated"), "seen_at": m.get("seen_at"),
            })
        return {
            "id": self.id, "incident_id": self.incident_id, "title": self.title(), "summary": self.summary,
            "status": self.status, "held": self.held, "safety_direction": safety, "category": category,
            "condition": self.group.condition, "hazard": hazard, "place": place, "subject": self.group.subject,
            "tags": [t for t in (category, hazard if hazard != "none" else None, place) if t],
            "origins": self.origins, "sources": sources, "log": self.log, "community": self.community,
            "first_seen": self.first_seen, "updated_at": self.updated_at, "severity": self.severity,
            "importance": self.importance(category),
        }


@dataclass
class Incident:
    id: str
    name: str
    hazards: list
    area: dict                          # lat, lon, radius_km, label (+ polygons for official warnings)
    status: str = "active"              # active | ended
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)   # last time something visible changed (new item, status, sources)
    checked_at: str = field(default_factory=now)   # last time a source was checked, even if nothing changed
    kind: str = "verified_board"        # verified_board (multi-source claims) | official_warning (e.g. FMI CAP)
    valid: dict | None = None           # official_warning: {"from", "to"}
    source_label: str | None = None     # official_warning: who issued it
    source: dict | None = None          # official_warning: {"id", "name", "kind", "url"}
    summary_text: str | None = None     # official_warning: the official description
    official_page: dict | None = None   # official_warning: what Context.dev read on the official page (enrich.py)

    def is_live(self) -> bool:
        return self.status == "active" and not (self.valid and self.valid.get("to") and self.valid["to"] <= now())


class Store:
    def __init__(self):
        self.incidents: dict[str, Incident] = {}
        self.items: dict[str, Item] = {}
        self.alerts: list[dict] = []
        self.activity: deque = deque(maxlen=300)
        self.snapshots: dict[str, dict] = {}        # source_id -> {"hash", "keys", "processed_at"}
        self.quarantine: list[dict] = []
        self.context_monitors: dict[str, dict] = {}   # source_id -> Context.dev monitor {id, url, created_at}
        self.context_checks: list[dict] = []          # recent Context.dev monitor verdicts, newest first
        self.monitors: dict[str, dict] = {}         # runtime monitor status (not persisted)
        self.credits_spent = 0.0
        self.seq = 0
        self.history = None                         # backend.history.History when TIGER_DATABASE_URL is set
        self.hide_fictional = False                 # admin switch: the board shows only real official warnings
        self._subscribers: set[asyncio.Queue] = set()

    # ---- pub/sub ----------------------------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def viewers(self) -> int:
        """Open live-update connections (board, incident, demo and admin pages)."""
        return len(self._subscribers)

    def hidden(self, incident_id: str | None) -> bool:
        """True for a fictional scenario incident while the admin has fictional sources switched off."""
        inc = self.incidents.get(incident_id or "")
        return self.hide_fictional and inc is not None and inc.kind != "official_warning"

    def hidden_alert(self, alert: dict) -> bool:
        """Real feed alerts always name their incident; one without (a fictional page change) hides with the rest."""
        return self.hide_fictional and (not alert.get("incident_id") or self.hidden(alert["incident_id"]))

    def publish(self, event: str, data: dict, record: bool = True) -> None:
        if record and self.history is not None:
            self.history.record(event, data, self)
        if self.hide_fictional and (self.hidden_alert(data) if event == "alert" else
                                    event in ("incident", "item", "pipeline", "context_check")
                                    and self.hidden(data.get("id") if event == "incident" else data.get("incident_id"))):
            return                                  # kept and recorded, just not shown while switched off
        for q in list(self._subscribers):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                self._subscribers.discard(q)        # slow client; it will reconnect and refetch

    # ---- helpers ----------------------------------------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}_{self.seq}"

    def ensure_incident(self, template_id: str) -> tuple[Incident, bool]:
        if template_id in self.incidents:
            return self.incidents[template_id], False
        t = INCIDENT_TEMPLATES[template_id]
        inc = Incident(t.id, t.name, list(t.hazards), dict(t.area))
        self.incidents[inc.id] = inc
        return inc, True

    def delete_incident(self, incident_id: str) -> None:
        """Remove an incident with its items and alerts (used by the scenario reset)."""
        inc = self.incidents.get(incident_id)
        if inc is None:
            return
        inc.status = "ended"
        self.publish("incident", self.incident_view(inc))      # clients drop it from map and list
        del self.incidents[incident_id]
        for item_id in [i for i, it in self.items.items() if it.incident_id == incident_id]:
            del self.items[item_id]
        self.alerts = [a for a in self.alerts if a.get("incident_id") != incident_id]

    def incident_items(self, incident_id: str) -> list[Item]:
        return [it for it in self.items.values() if it.incident_id == incident_id]

    def add_activity(self, row: dict) -> None:
        row = {"at": now(), **row, "credits_spent": self.credits_spent}
        self.activity.appendleft(row)
        self.publish("activity", row)

    def add_alert(self, level: str, title: str, body: str, incident_id: str | None = None, item_id: str | None = None) -> dict:
        alert = {"id": self.next_id("al"), "level": level, "title": title, "body": body,
                 "incident_id": incident_id, "item_id": item_id, "created_at": now()}
        self.alerts.insert(0, alert)
        del self.alerts[50:]
        self.publish("alert", alert)
        return alert

    # ---- API views --------------------------------------------------------------------------------------
    @staticmethod
    def superseded(items: list[Item]) -> set[str]:
        """Ids of verified official warnings replaced by a newer one (see rules.superseded_warnings)."""
        return rules.superseded_warnings([(it.id, it._most_common("hazard", "none"), it.first_seen, it.group.members)
                                          for it in items if it.status == "verified"
                                          and it._most_common("category") == "warning"
                                          and rules.is_warning_level(it.group.members)])

    def visible_items(self, incident_id: str) -> list[Item]:
        """Items some source still states. A group whose every statement was withdrawn stays in history only."""
        return [it for it in self.incident_items(incident_id) if it.group.members]

    def incident_view(self, inc: Incident) -> dict:
        items = self.visible_items(inc.id)
        counts = {s: 0 for s in rules.STATUSES} | {"held": 0, "total": len(items)}
        for it in items:
            counts[it.status] += 1
            counts["held"] += it.held
        old = self.superseded(items)                  # a lowered warning must not keep the incident at red
        severity = max((it.severity for it in items if it.id not in old), default=1)
        verified = sorted((it for it in items if it.status == "verified" and it.id not in old),
                          key=lambda it: (-it.severity, it.first_seen))
        contradicted = [it for it in items if it.status == "contradicted"
                        and any(m["stance"] != "denies" for m in it.group.members)]   # rumours, not bare denials
        summary = " ".join(f"{it.title().rstrip('.')}." for it in verified[:3])
        if contradicted:
            summary += f" {len(contradicted)} rumour{'s' if len(contradicted) > 1 else ''} contradicted by officials."
        if not summary:
            summary = f"{len(items)} unconfirmed reports. Waiting for official confirmation."
        newest = lambda its: sorted(its, key=lambda it: it.updated_at, reverse=True)
        highlights = [{"item_id": it.id, "status": it.status, "title": it.title()}
                      for it in (newest(contradicted)[:1] + newest(verified)[:2])]
        if inc.kind == "official_warning":
            area = {**inc.area, "rings": None}
            summary = inc.summary_text or summary
            sources = [{**inc.source, "captured_from": None}] if inc.source else []
        else:
            area = {**inc.area, "rings": rules.rings(severity, inc.area.get("radius_km") or 1), "polygons": None}
            sources = [{"id": s.id, "name": s.name, "kind": s.kind, "url": s.view_url,
                        "captured_from": self.snapshots.get(s.id, {}).get("url")}
                       for s in SOURCES if s.incident == inc.id]
        # the board shows the newest official instructions on each card ("What to do")
        instructions = sorted((it for it in items if it.status == "verified" and it._most_common("category") == "instruction"
                               and any(m.get("source_kind") == "official" and m["stance"] == "asserts" for m in it.group.members)),
                              key=lambda it: it.first_seen, reverse=True)
        what_to_do = [it.title() for it in instructions[:2]] if inc.kind != "official_warning" else \
            [x["text"] for x in ((inc.official_page or {}).get("instructions") or [])[:2]]
        return {
            "id": inc.id, "kind": inc.kind, "name": inc.name, "summary": summary.strip(), "severity": severity,
            "what_to_do": what_to_do, "official_page": inc.official_page,
            "severity_label": rules.SEVERITY_LABELS[severity], "status": "active" if inc.is_live() else "ended",
            "hazards": inc.hazards, "area": area, "valid": inc.valid, "source_label": inc.source_label,
            "counts": counts, "highlights": highlights, "sources": sources,
            "created_at": inc.created_at, "updated_at": inc.updated_at, "checked_at": inc.checked_at,
        }

    def incident_list(self) -> list[dict]:
        views = [self.incident_view(i) for i in self.incidents.values() if i.is_live() and not self.hidden(i.id)]
        views.sort(key=lambda v: v["updated_at"], reverse=True)
        views.sort(key=lambda v: v["severity"], reverse=True)
        return views

    def incident_detail(self, incident_id: str) -> dict | None:
        inc = self.incidents.get(incident_id)
        if inc is None or self.hidden(incident_id):
            return None
        items = sorted(self.visible_items(incident_id), key=lambda it: it.updated_at, reverse=True)
        old = self.superseded(items)
        views = [{**it.to_api(), "superseded": it.id in old} for it in items]
        tags = {
            "status": sorted({v["status"] for v in views}, key=lambda s: STATUS_ORDER[s]),
            "category": sorted({v["category"] for v in views}),
            "place": sorted({v["place"] for v in views if v["place"]}),
        }
        checks = [c for c in self.context_checks if c.get("incident_id") == incident_id][:10]
        return {"incident": self.incident_view(inc), "key_facts": self.key_facts(items), "tags": tags, "items": views,
                "context_checks": checks, "images": self.source_images(inc, items)}

    def source_images(self, inc: Incident, items: list[Item]) -> list[dict]:
        """Photos on the incident's source pages as last captured (backend/images.py): context, never proof."""
        out = []
        for s in SOURCES:
            if s.incident != inc.id:
                continue
            snap = self.snapshots.get(s.id) or {}
            for im in snap.get("images") or []:
                out.append({"url": im.get("local") or im["url"], "alt": im.get("alt") or "", "source_id": s.id,
                            "source_name": s.name, "kind": s.kind, "source_url": s.view_url,
                            "seen_at": snap.get("processed_at"), "label": f"posted by {s.name}, unverified",
                            "item_ids": images.items_for(im, s.id, items) if s.kind == "social" else []})
        return out

    @staticmethod
    def key_facts(items: list[Item]) -> dict:
        """The core facts of an incident, by rule (no LLM). See docs/API.md v0.2."""
        cat = {it.id: it._most_common("category") for it in items}
        official_member = lambda it: any(m.get("source_kind") == "official" and m["stance"] == "asserts"
                                         for m in it.group.members)

        def debunker(it: Item) -> str | None:
            for m in it.group.members:
                if m["stance"] != "denies":
                    continue
                if m.get("source_kind") == "official" and not m.get("attributed_to"):
                    return m.get("source_name")
                quoted = rules.official_by_attribution(m.get("attributed_to"), OFFICIAL_NAMES)
                if quoted:
                    return quoted
            return None

        old = Store.superseded(items)
        verified = [it for it in items if it.status == "verified" and it.id not in old]
        confirmed = sorted((it for it in verified if cat[it.id] != "instruction"),
                           key=lambda it: (-it.severity, it.first_seen))[:6]
        what_to_do = sorted((it for it in verified if cat[it.id] == "instruction" and official_member(it)),
                            key=lambda it: it.first_seen)[:5]
        not_true = sorted((it for it in items if it.status == "contradicted" and it.is_rumour()),
                          key=lambda it: it.updated_at, reverse=True)
        dont_act = sorted((it for it in items if it.held), key=lambda it: it.updated_at, reverse=True)
        developing = sorted((it for it in items if not it.held and it.status in ("corroborated", "disputed", "unverified")
                             and (it.status == "corroborated" or it.origins.get("asserting", 0) >= 2)),
                            key=lambda it: -it.origins.get("asserting", 0))[:4]
        return {
            "confirmed": [{"item_id": it.id, "title": it.title(), "category": cat[it.id], "severity": it.severity}
                          for it in confirmed],
            "what_to_do": [{"item_id": it.id, "title": it.title()} for it in what_to_do],
            "not_true": [{"item_id": it.id, "title": it.title(), "debunked_by": debunker(it)} for it in not_true],
            "dont_act_yet": [{"item_id": it.id, "title": it.title()} for it in dont_act],
            "developing": [{"item_id": it.id, "title": it.title(), "status": it.status,
                            "origins": it.origins.get("asserting", 0)} for it in developing],
        }

    # ---- persistence ------------------------------------------------------------------------------------
    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "seq": self.seq, "credits_spent": self.credits_spent,
            "incidents": [asdict(i) for i in self.incidents.values()],
            "items": [asdict(i) for i in self.items.values()],
            "alerts": self.alerts, "activity": list(self.activity), "snapshots": self.snapshots,
            "quarantine": self.quarantine[-200:],
            "context_monitors": self.context_monitors, "context_checks": self.context_checks[:100],
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(STATE_FILE)

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        data = json.loads(STATE_FILE.read_text())
        self.seq = data.get("seq", 0)
        self.credits_spent = data.get("credits_spent", 0.0)
        self.incidents = {d["id"]: Incident(**d) for d in data.get("incidents", [])}
        for inc in self.incidents.values():          # scenario templates are authoritative (e.g. a moved area)
            t = INCIDENT_TEMPLATES.get(inc.id)
            if t:
                inc.name, inc.hazards, inc.area, inc.kind = t.name, list(t.hazards), dict(t.area), "verified_board"
        for d in data.get("items", []):
            d["group"] = Group(**d["group"])
            self.items[d["id"]] = Item(**d)
        self.alerts = data.get("alerts", [])
        self.activity = deque(data.get("activity", []), maxlen=300)
        self.snapshots = data.get("snapshots", {})
        self.quarantine = data.get("quarantine", [])
        self.context_monitors = data.get("context_monitors", {})
        self.context_checks = data.get("context_checks", [])


def source_meta(source_id: str) -> dict:
    s = SOURCES_BY_ID[source_id]
    return {"source_id": s.id, "source_name": s.name, "source_kind": s.kind, "source_url": s.public_url}
