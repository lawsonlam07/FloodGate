"""Finnish Meteorological Institute warnings from its official CAP (Common Alerting Protocol) Atom feed.

This is an official, structured, machine-readable feed (like a sensor), so it is read directly and spends no
Context.dev credits. Web pages (official sites, news, social posts) still go through Context.dev.

Feed: https://alerts.fmi.fi/cap/feed/atom_en-GB.xml — each <entry> embeds a CAP 1.2 <alert> with fi/sv/en <info>
blocks, polygons for every warning area, and Alert / Update / Cancel message types.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ATOM = "{http://www.w3.org/2005/Atom}"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"

COLOUR_SEVERITY = [("red", 4), ("orange", 3), ("yellow", 1)]   # checked in this order


def _utc(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def warning_key(identifier: str) -> str:
    """FMI identifiers look like urn:oid:2.49.0.1.246.0.0.2026.<message seq>.<warning id>; the last part stays the
    same across Alert -> Update -> Cancel of one warning, so it is the stable key."""
    return identifier.rsplit(".", 1)[-1]


def incident_id(key: str) -> str:
    return "fmi-" + hashlib.sha1(key.encode()).hexdigest()[:10]


def colour_of(text: str) -> str | None:
    for colour, _ in COLOUR_SEVERITY:
        if re.search(rf"\b{colour}\b", text or "", re.I):
            return colour
    return None


def hazard_for(event: str) -> str:
    e = (event or "").lower()
    if "sea level" in e or "high water" in e:
        return "storm_surge"
    if "flood" in e or "rain" in e:
        return "flood"
    if "fire" in e:
        return "fire"
    if "wind" in e or "storm" in e or "thunder" in e:
        return "storm"
    return "other"


def _polygon(text: str, max_points: int = 30) -> list[list[float]]:
    pts = []
    for pair in (text or "").split():
        try:
            lat, lon = pair.split(",")
            pts.append([round(float(lat), 3), round(float(lon), 3)])
        except ValueError:
            continue
    if len(pts) > max_points:                       # keep payloads small: simple decimation is fine for display
        step = math.ceil(len(pts) / max_points)
        pts = pts[::step] + [pts[-1]]
    return pts


def area_geometry(polygons: list[list[list[float]]]) -> dict:
    pts = [p for poly in polygons for p in poly]
    if not pts:
        return {"lat": None, "lon": None, "radius_km": None}
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    lat, lon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    dy = (max(lats) - min(lats)) * 111.0
    dx = (max(lons) - min(lons)) * 111.0 * math.cos(math.radians(lat))
    return {"lat": round(lat, 4), "lon": round(lon, 4), "radius_km": round(math.hypot(dx, dy) / 2, 1)}


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """One dict per Atom entry (all message types)."""
    out = []
    root = ET.fromstring(xml_bytes)
    for entry in root.iter(f"{ATOM}entry"):
        alert = entry.find(f".//{CAP}alert")
        if alert is None:
            continue
        infos = alert.findall(f"{CAP}info")
        info = next((i for i in infos if (i.findtext(f"{CAP}language") or "").startswith("en")), infos[0] if infos else None)
        if info is None:
            continue
        identifier = alert.findtext(f"{CAP}identifier") or ""
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        msg_type = alert.findtext(f"{CAP}msgType") or "Alert"
        areas, polygons = [], []
        for area in info.findall(f"{CAP}area"):
            areas.append(area.findtext(f"{CAP}areaDesc") or "")
            polygons += [p for p in (_polygon(x.text) for x in area.findall(f"{CAP}polygon")) if len(p) >= 3]
        headline = (info.findtext(f"{CAP}headline") or title).strip()
        out.append({
            "key": warning_key(identifier),
            "identifier": identifier,
            "msg_type": msg_type,
            "removed": msg_type == "Cancel" or title.upper().startswith("REMOVED"),
            "sent": _utc(alert.findtext(f"{CAP}sent")),
            "title": title,
            "event": info.findtext(f"{CAP}event") or "",
            "headline": headline,
            "description": (info.findtext(f"{CAP}description") or "").strip(),
            "instruction": (info.findtext(f"{CAP}instruction") or "").strip(),
            "onset": _utc(info.findtext(f"{CAP}onset")),
            "expires": _utc(info.findtext(f"{CAP}expires")),
            "web": info.findtext(f"{CAP}web") or "https://en.ilmatieteenlaitos.fi/warnings",
            "areas": [a for a in areas if a],
            "polygons": polygons,
            "colour": colour_of(headline) or colour_of(title),
        })
    return out


def active_warnings(entries: list[dict], now_utc: str) -> list[dict]:
    """Latest message per warning; keep it if it is not cancelled and not expired."""
    latest: dict[str, dict] = {}
    for e in entries:
        cur = latest.get(e["key"])
        if cur is None or (e["sent"] or "") > (cur["sent"] or ""):
            latest[e["key"]] = e
    return [e for e in latest.values()
            if not e["removed"] and (e["expires"] is None or e["expires"] > now_utc)]
