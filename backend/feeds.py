"""Real, official sources beyond FMI: public structured feeds, read directly (no Context.dev credits), like FMI's CAP.

  gdacs         GDACS, the UN / European Commission Global Disaster Alert and Coordination System: floods, cyclones,
                volcanoes, droughts and (orange/red only) wildfires worldwide. Green / Orange / Red -> L1 / L3 / L4.
  usgs-quakes   US Geological Survey: earthquakes of magnitude 4.5+ in the past day. Its PAGER impact alert where
                given (green / yellow / orange / red), otherwise our estimate from the magnitude.
  uk-ea-floods  UK Environment Agency: flood warnings in force in England. Severe flood warning / flood warning /
                flood alert -> L4 / L3 / L2 (the official three levels).
  meteoalarm    MeteoAlarm (EUMETNET): orange and red weather warnings from Europe's national weather services
                (Finland comes from FMI directly). Orange / red -> L3 / L4. Region shapes from
                backend/meteoalarm_regions.json (deploy/build_meteoalarm_regions.py), else the warning's CAP polygons.

Each parser returns active warnings in the same shape as `fmi.parse_feed` (key, headline, description, onset,
expires, web, areas, polygons, ...) plus `level` (1-4, the official level on our scale) and `hazard`, so the pipeline
turns them into official_warning incidents exactly like FMI's. Points become a circle so the map can shade them.

Note: these services' firewalls reject custom User-Agent strings that carry a URL, so we use the library default.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from . import fmi


@dataclass(frozen=True)
class Feed:
    id: str
    publisher: str                 # who issues it (shown as the official source; also spoken in bulletins)
    short: str                     # prefix for alerts ("GDACS: ...")
    label: str                     # short credit line on warning pages
    url: str
    prefix: str                    # incident id prefix
    interval_s: int = 300
    user_agent: str | None = None  # None = requests' default


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def circle(lat: float, lon: float, radius_km: float, n: int = 28) -> list[list[float]]:
    """A polygon approximating a circle around a point (for feeds that give a point, not an area)."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.15, math.cos(math.radians(lat))))
    return [[round(lat + dlat * math.sin(2 * math.pi * i / n), 4), round(lon + dlon * math.cos(2 * math.pi * i / n), 4)]
            for i in range(n)]


def incident_id(feed: Feed, key: str) -> str:
    return f"{feed.prefix}-" + hashlib.sha1(key.encode()).hexdigest()[:10]


# ---- GDACS ------------------------------------------------------------------------------------------------------
GDACS_TYPES = {"FL": ("flood", "Flood"), "TC": ("storm", "Tropical cyclone"), "VO": ("other", "Volcano"),
               "DR": ("heatwave", "Drought"), "WF": ("fire", "Wildfire"), "EQ": ("earthquake", "Earthquake"),
               "TS": ("tsunami", "Tsunami")}
GDACS_LEVEL = {"Red": 4, "Orange": 3, "Green": 1}
G = "{http://www.gdacs.org}"
GEO = "{http://www.w3.org/2003/01/geo/wgs84_pos#}"


def parse_gdacs(content: bytes, now_utc: str) -> list[dict]:
    out = []
    for it in ET.fromstring(content).iter("item"):
        etype, level = it.findtext(f"{G}eventtype") or "", it.findtext(f"{G}alertlevel") or ""
        if it.findtext(f"{G}iscurrent") != "true" or etype not in GDACS_TYPES or etype == "EQ":   # quakes: USGS
            continue
        if etype == "WF" and level not in ("Orange", "Red"):     # hundreds of small green fire notifications
            continue
        lat, lon = it.findtext(f".//{GEO}lat"), it.findtext(f".//{GEO}long")
        if lat is None or lon is None:
            continue
        hazard, kind = GDACS_TYPES[etype]
        lvl = GDACS_LEVEL.get(level, 1)
        country = ", ".join(c.strip() for c in (it.findtext(f"{G}country") or "").split(",") if c.strip())
        name = (it.findtext(f"{G}eventname") or "").strip()
        severity_text = (it.findtext(f"{G}severity") or "").strip()
        title = re.sub(r"(,\s*)+,", ",", (it.findtext("title") or "").strip())
        try:
            onset = _iso(parsedate_to_datetime(it.findtext(f"{G}fromdate")))
            todate = _iso(parsedate_to_datetime(it.findtext(f"{G}todate")))
        except (TypeError, ValueError):
            onset = todate = None
        key = f"{etype}{it.findtext(f'{G}eventid')}"
        countries = [c.strip() for c in country.split(",") if c.strip()]
        where = countries[0] + (f" and {len(countries) - 1} more countries" if len(countries) > 2
                                else f" and {countries[1]}" if len(countries) == 2 else "") if countries else name
        label = (f"Tropical cyclone {name.title()}" if etype == "TC" and name
                 else f"Drought in {re.sub(r'-\d{4}$', '', name)}" if etype == "DR" and name else f"{kind} in {where}")
        radius = {4: 220, 3: 160, 1: 90}[lvl]
        out.append({
            "key": key, "identifier": f"{key}-{it.findtext(f'{G}episodeid')}", "msg_type": "Alert", "removed": False,
            "sent": _iso(datetime.now(timezone.utc)), "title": title, "event": kind,
            "name": label, "headline": f"{level} alert: {label}",
            "description": " ".join(x for x in (title + ".", severity_text + "." if severity_text else "") if x),
            "instruction": "", "onset": onset,
            "expires": todate if todate and todate > now_utc else None,      # ongoing events have past "todate"s
            "web": (it.findtext("link") or "https://www.gdacs.org").replace("http://", "https://"),
            "areas": [country] if country else [], "polygons": [circle(float(lat), float(lon), radius)],
            "colour": level.lower() if level in ("Red", "Orange") else None, "level": lvl, "hazard": hazard,
        })
    return out


# ---- USGS -------------------------------------------------------------------------------------------------------
USGS_ALERT = {"red": 4, "orange": 3, "yellow": 2, "green": 1}


def parse_usgs(content: bytes, now_utc: str) -> list[dict]:
    out = []
    for f in json.loads(content).get("features", []):
        p, (lon, lat, depth) = f["properties"], f["geometry"]["coordinates"][:3]
        mag = p.get("mag") or 0
        level = USGS_ALERT.get(p.get("alert") or "") or (3 if mag >= 6.5 else 2 if mag >= 5.5 else 1)
        when = datetime.fromtimestamp((p.get("time") or 0) / 1000, tz=timezone.utc)
        place = p.get("place") or "unknown location"
        near = re.sub(r"^\d+ km [NSEW]{1,3} of ", "near ", place)
        tsunami = " A tsunami message was issued by the relevant warning centre." if p.get("tsunami") else ""
        alert = f" USGS PAGER impact alert: {p['alert']}." if p.get("alert") else ""
        out.append({
            "key": f["id"], "identifier": f"{f['id']}-{p.get('updated')}", "msg_type": "Alert", "removed": False,
            "sent": _iso(when), "title": p.get("title") or "", "event": "Earthquake",
            "name": f"M {mag:.1f} earthquake {near}", "headline": f"M {mag:.1f} earthquake {place}",
            "description": f"Magnitude {mag:.1f} earthquake {place}, at a depth of {depth:.0f} km, "
                           f"{when:%d %B %Y %H:%M} UTC.{alert}{tsunami}",
            "instruction": "", "onset": _iso(when), "expires": None,
            "web": p.get("url") or "https://earthquake.usgs.gov/earthquakes/map/",
            "areas": [place], "polygons": [circle(lat, lon, max(25.0, 10 ** (0.5 * mag - 1.6)))],
            "colour": None, "level": level, "hazard": "tsunami" if p.get("tsunami") else "earthquake",
        })
    return out


# ---- UK Environment Agency -----------------------------------------------------------------------------------------
EA_LEVEL = {1: 4, 2: 3, 3: 2}            # severe flood warning, flood warning, flood alert (4 = no longer in force)
_ea_area_cache: dict[str, tuple[float, float]] = {}


def _ea_area_point(url: str, session: requests.Session) -> tuple[float, float] | None:
    if url not in _ea_area_cache:
        try:
            item = session.get(url, timeout=20).json().get("items") or {}
            item = item[0] if isinstance(item, list) else item
            _ea_area_cache[url] = (float(item["lat"]), float(item["long"]))
        except Exception:
            return None
    return _ea_area_cache[url]


def parse_uk_ea(content: bytes, now_utc: str, session: requests.Session | None = None) -> list[dict]:
    session = session or requests.Session()
    out = []
    for it in json.loads(content).get("items", [])[:80]:
        sev = int(it.get("severityLevel") or 4)
        if sev not in EA_LEVEL:
            continue
        area = it.get("floodArea") or {}
        pt = _ea_area_point((area.get("@id") or "").replace("http://", "https://"), session)
        if pt is None:
            continue
        name = it.get("description") or it.get("eaAreaName") or "England"
        key = it.get("floodAreaID") or area.get("notation") or name
        raised = it.get("timeRaised")
        severity = it.get("severity") or "Flood warning"
        label = f"{severity} for {area['riverOrSea']}" if area.get("riverOrSea") else f"{severity}: {name}"
        out.append({
            "key": key, "identifier": f"{key}-{it.get('timeMessageChanged') or raised}", "msg_type": "Alert",
            "removed": False, "sent": raised and raised + "Z", "title": it.get("severity") or "", "event": "Flood",
            "name": label, "headline": f"{severity}: {name}",
            "description": (it.get("message") or "").strip() or f"{it.get('severity')} for {name}.",
            "instruction": "", "onset": raised and raised + "Z", "expires": None,
            "web": f"https://check-for-flooding.service.gov.uk/target-area/{key}",
            "areas": [x for x in (name, area.get("county")) if x], "polygons": [circle(pt[0], pt[1], 4)],
            "colour": "red" if sev == 1 else None, "level": EA_LEVEL[sev], "hazard": "flood",
        })
    return out


# ---- MeteoAlarm ---------------------------------------------------------------------------------------------------
MA_BASE = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-"
MA_COUNTRIES = {   # feed slug -> (name, ISO code); Finland is read from FMI directly
    "austria": ("Austria", "AT"), "belgium": ("Belgium", "BE"), "bosnia-herzegovina": ("Bosnia and Herzegovina", "BA"),
    "bulgaria": ("Bulgaria", "BG"), "croatia": ("Croatia", "HR"), "cyprus": ("Cyprus", "CY"), "czechia": ("Czechia", "CZ"),
    "denmark": ("Denmark", "DK"), "estonia": ("Estonia", "EE"), "france": ("France", "FR"), "germany": ("Germany", "DE"),
    "greece": ("Greece", "GR"), "hungary": ("Hungary", "HU"), "iceland": ("Iceland", "IS"), "ireland": ("Ireland", "IE"),
    "israel": ("Israel", "IL"), "italy": ("Italy", "IT"), "latvia": ("Latvia", "LV"), "lithuania": ("Lithuania", "LT"),
    "luxembourg": ("Luxembourg", "LU"), "malta": ("Malta", "MT"), "moldova": ("Moldova", "MD"),
    "montenegro": ("Montenegro", "ME"), "netherlands": ("Netherlands", "NL"),
    "republic-of-north-macedonia": ("North Macedonia", "MK"), "norway": ("Norway", "NO"), "poland": ("Poland", "PL"),
    "portugal": ("Portugal", "PT"), "romania": ("Romania", "RO"), "serbia": ("Serbia", "RS"), "slovakia": ("Slovakia", "SK"),
    "slovenia": ("Slovenia", "SI"), "spain": ("Spain", "ES"), "sweden": ("Sweden", "SE"), "switzerland": ("Switzerland", "CH"),
    "ukraine": ("Ukraine", "UA"), "united-kingdom": ("United Kingdom", "GB"),
}
MA_CENTRE = {"GB": (54.0, -2.5), "CH": (46.8, 8.2), "UA": (49.0, 31.5)}     # countries missing from the region file
MA_HAZARD = [("coastal", "storm_surge"), ("flood", "flood"), ("rain", "flood"), ("fire", "fire"),
             ("high-temp", "heatwave"), ("avalanche", "landslide"), ("wind", "storm"), ("thunder", "storm")]
_MA_TITLE = re.compile(r"^(Yellow|Orange|Red) (.+?) Warning issued for (.+?) - (.+)$", re.I)
A = "{http://www.w3.org/2005/Atom}"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
_regions: dict | None = None
_ma_country_entries: dict[str, list[dict]] = {}     # last good parse per country (a failed fetch keeps it)
_ma_cap: dict[str, dict] = {}                       # identifier -> text, instructions, polygons, sender


def ma_regions() -> dict:
    global _regions
    if _regions is None:
        path = Path(__file__).with_name("meteoalarm_regions.json")
        _regions = json.loads(path.read_text()) if path.exists() else {}
    return _regions


def _norm(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


_by_name: dict[tuple[str, str], dict] | None = None


def ma_region(code: str | None, iso: str, area: str) -> dict | None:
    """A region by its EMMA_ID code, else by name within the country (some countries renumbered their regions,
    e.g. France: Gard is FR812 in the feed and FR022 in the region file)."""
    global _by_name
    hit = ma_regions().get(code or "")
    if hit and hit["country"] == iso:
        return hit
    if _by_name is None:
        _by_name = {(r["country"], _norm(r["name"])): r for r in ma_regions().values()}
    return _by_name.get((iso, _norm(area))) or _by_name.get((iso, _norm(re.sub(r"\s*\(.*?\)", "", area or ""))))


def _geocode(entry) -> str | None:
    """The EMMA_ID code: <cap:geocode> holds <valueName>/<value> in the Atom (default) namespace, not cap:."""
    g = entry.find(f"{CAP}geocode")
    return next(((c.text or "").strip() for c in g if c.tag.rsplit("}", 1)[-1] == "value"), None) if g is not None else None


def _ma_entries(content: bytes, now_utc: str) -> list[dict]:
    """Orange and red entries of one country's Atom feed (one entry per warning per region)."""
    out = []
    for e in ET.fromstring(content).iter(f"{A}entry"):
        m = _MA_TITLE.match((e.findtext(f"{A}title") or "").strip())
        if not m or m[1].lower() not in ("orange", "red"):
            continue
        expires = _iso(datetime.fromisoformat(e.findtext(f"{CAP}expires"))) if e.findtext(f"{CAP}expires") else None
        if (e.findtext(f"{CAP}message_type") or "").lower() == "cancel" or (expires and expires <= now_utc):
            continue
        cap_link = next((l.get("href") for l in e.findall(f"{A}link") if l.get("type") == "application/cap+xml"), None)
        out.append({"identifier": e.findtext(f"{CAP}identifier") or cap_link, "colour": m[1].lower(),
                    "type": m[2], "country": m[3], "area": (e.findtext(f"{CAP}areaDesc") or m[4]).strip(),
                    "emma": _geocode(e), "event": e.findtext(f"{CAP}event") or m[2],
                    "sent": e.findtext(f"{CAP}sent"), "onset": e.findtext(f"{CAP}onset"), "expires": expires,
                    "cap": cap_link})
    return out


def _ma_cap_details(url: str, session: requests.Session) -> dict:
    """English description / instruction, sender and any polygons from the warning's CAP message."""
    try:
        root = ET.fromstring(session.get(url, timeout=20).content)
    except Exception:
        return {}
    infos = root.findall(f"{CAP}info")
    info = next((i for i in infos if (i.findtext(f"{CAP}language") or "").startswith("en")), infos[0] if infos else None)
    if info is None:
        return {}
    polys = [p for p in (fmi._polygon(x.text) for x in info.iter(f"{CAP}polygon")) if len(p) >= 3]
    return {"description": (info.findtext(f"{CAP}description") or "").strip(),
            "instruction": (info.findtext(f"{CAP}instruction") or "").strip(),
            "sender": (info.findtext(f"{CAP}senderName") or "").strip(), "web": info.findtext(f"{CAP}web"),
            "polygons": polys[:12]}


def fetch_meteoalarm(session: requests.Session, now_utc: str) -> list[dict]:
    for slug in MA_COUNTRIES:
        try:
            r = session.get(MA_BASE + slug, timeout=30)
            r.raise_for_status()
            _ma_country_entries[slug] = _ma_entries(r.content, now_utc)
        except Exception:
            pass                                   # keep that country's last good list
    by_id: dict[str, list[dict]] = {}
    for slug, entries in _ma_country_entries.items():
        for e in entries:
            if not e["expires"] or e["expires"] > now_utc:
                by_id.setdefault(e["identifier"], []).append({**e, "slug": slug})
    # One service often issues back-to-back messages for the same areas and hazard: show them as one warning.
    groups: dict[str, list[dict]] = {}
    same: dict[tuple, str] = {}
    for ident, es in sorted(by_id.items(), key=lambda kv: min(e["onset"] or "" for e in kv[1])):
        sig = (es[0]["slug"], es[0]["type"].lower(), frozenset(e["area"] for e in es))
        groups.setdefault(same.setdefault(sig, ident), []).extend(es)
    regions, out = ma_regions(), []
    for ident, es in groups.items():
        first = es[0]
        name_c, iso = MA_COUNTRIES[first["slug"]]
        if ident not in _ma_cap and first["cap"] and len(_ma_cap) < 2000:
            _ma_cap[ident] = _ma_cap_details(first["cap"], session)
        cap = _ma_cap.get(ident) or {}
        areas = sorted({e["area"] for e in es})
        polys = [p for e in es for p in ((ma_region(e["emma"], iso, e["area"]) or {}).get("polygons") or [])]
        if not polys:
            polys = cap.get("polygons") or []
        if not polys:                              # last resort: a country-level marker
            pts = [(r["lat"], r["lon"]) for r in regions.values() if r["country"] == iso]
            lat, lon = MA_CENTRE.get(iso) or ((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                                              if pts else (50.0, 10.0))
            polys = [circle(lat, lon, 60)]
        kind = first["type"].replace("-", " ").lower()
        colour = "red" if any(e["colour"] == "red" for e in es) else "orange"
        where = areas[0] + (f" and {len(areas) - 1} more" if len(areas) > 1 else "")
        sender = cap.get("sender")
        out.append({
            "key": ident, "identifier": f"{ident}-{max(e['sent'] or '' for e in es)}", "msg_type": "Alert",
            "removed": False, "sent": first["sent"], "title": first["type"], "event": first["event"],
            "name": f"{colour.title()} {kind} warning: {where} ({name_c})", "headline": f"{colour.title()} {kind} warning: {where} ({name_c})",
            "description": cap.get("description") or f"{colour.title()} {kind} warning for {', '.join(areas[:8])}"
                           f"{f' and {len(areas) - 8} more areas' if len(areas) > 8 else ''} ({name_c}).",
            "instruction": cap.get("instruction") or "",
            "onset": _iso(datetime.fromisoformat(first["onset"])) if first["onset"] else None,
            "expires": max((e["expires"] for e in es if e["expires"]), default=None),
            "web": cap.get("web") or f"https://meteoalarm.org?region={iso}",
            "areas": areas, "polygons": polys[:60], "colour": colour, "level": 4 if colour == "red" else 3,
            "hazard": next((h for k, h in MA_HAZARD if k in first["type"].lower()), "other"),
            "publisher": f"{sender} (via MeteoAlarm)" if sender else None,
        })
    return out


# ---- registry -----------------------------------------------------------------------------------------------------
FMI = Feed("fmi-warnings", "Finnish Meteorological Institute", "FMI", "Finnish Meteorological Institute (CAP feed)",
           "https://alerts.fmi.fi/cap/feed/atom_en-GB.xml", "fmi", 300, "floodgate-monitor/0.1 (hackathon prototype)")
FEEDS = {f.id: f for f in (
    FMI,
    Feed("gdacs", "GDACS (UN and European Commission)", "GDACS", "GDACS, Global Disaster Alert and Coordination System (EC-JRC, CC BY 4.0)",
         "https://www.gdacs.org/xml/rss.xml", "gdacs", 600),
    Feed("usgs-quakes", "US Geological Survey", "USGS", "USGS earthquake feed (M4.5+, past day)",
         "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson", "usgs", 300),
    Feed("uk-ea-floods", "UK Environment Agency", "Environment Agency", "Environment Agency flood warnings (England)",
         "https://environment.data.gov.uk/flood-monitoring/id/floods", "ukea", 300),
    Feed("meteoalarm", "MeteoAlarm (Europe's national weather services)", "MeteoAlarm",
         "MeteoAlarm, EUMETNET (CC BY 4.0)", "https://feeds.meteoalarm.org", "meteo", 600),
)}


def level_of(w: dict) -> int:
    """Our 1-4 scale for any feed's warning (FMI gives colours: red 4, orange 3, yellow 1)."""
    return w.get("level") or {"red": 4, "orange": 3}.get(w.get("colour") or "", 1)


def fetch(feed: Feed, session: requests.Session, now_utc: str) -> tuple[str, list[dict]]:
    """(fingerprint, active warnings) for one feed. The fingerprint changes when the feed's content does."""
    if feed.id == "meteoalarm":
        warnings = fetch_meteoalarm(session, now_utc)
        return hashlib.sha256(json.dumps(sorted(w["identifier"] for w in warnings)).encode()).hexdigest()[:16], warnings
    r = session.get(feed.url, headers={"User-Agent": feed.user_agent} if feed.user_agent else {}, timeout=30)
    r.raise_for_status()
    return hashlib.sha256(r.content).hexdigest()[:16], active(feed, r.content, now_utc, session)


def active(feed: Feed, content: bytes, now_utc: str, session: requests.Session | None = None) -> list[dict]:
    if feed.id == FMI.id:
        return fmi.active_warnings(fmi.parse_feed(content), now_utc)
    if feed.id == "gdacs":
        return parse_gdacs(content, now_utc)
    if feed.id == "usgs-quakes":
        return parse_usgs(content, now_utc)
    if feed.id == "uk-ea-floods":
        return parse_uk_ea(content, now_utc, session)
    return []
