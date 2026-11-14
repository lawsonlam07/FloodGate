"""Transparent, non-LLM rules applied on top of extracted claims.

Right now: the safety-direction gate and the cross-source matching key.
Later: the status ladder (Rumour -> Emerging -> Likely -> Confirmed / Contradicted) lives here too.
"""

from __future__ import annotations

import re
from collections import defaultdict

# Believing these conditions makes people relax.
REASSURING = {"reopened", "water_receding", "all_clear"}
# Believing these conditions makes people more careful, so DENYING one is reassuring.
HAZARD = {
    "closed", "restricted", "damaged", "collapsed", "flooded", "water_rising",
    "warning_issued", "evacuation_ordered", "utility_outage", "casualties",
}


def rule_risk_taking(claim: dict) -> bool:
    """Fixed rule: the source's message would put people at risk if it turned out to be wrong."""
    affirmed = claim["stance"] in ("asserts", "reports_unverified")
    return (affirmed and claim["condition"] in REASSURING) or (claim["stance"] == "denies" and claim["condition"] in HAZARD)


def apply_safety_gate(claim: dict) -> None:
    """Conservative OR: risk_taking if either the LLM or the rule says so. Records disagreements."""
    llm = claim["safety_direction"]
    rule = rule_risk_taking(claim)
    claim["safety_rule"] = "risk_taking" if rule else "-"
    claim["safety_effective"] = "risk_taking" if (rule or llm == "risk_taking") else llm
    claim["safety_disagreement"] = rule != (llm == "risk_taking")


def subject_key(subject: str) -> str:
    s = re.sub(r"[^\w\s-]", "", subject.lower()).strip()
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"\s+", " ", s)


def match_key(claim: dict) -> tuple[str, str]:
    return subject_key(claim.get("subject") or ""), claim["condition"]


def group_claims(claims: list[dict]) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in claims:
        if c["condition"] != "other":
            groups[match_key(c)].append(c)
    return groups


# Includes words that smuggle a denial into a "positive" sentence ("the bridge is undamaged").
_NEGATION = re.compile(r"\b(not|no|never|none|false|untrue|denied|denies|undamaged|unaffected|unharmed|intact)\b|n't\b", re.I)
_ATTRIBUTION = re.compile(r"\b(says?|said|claims?|claimed|reports?|reported|according to)\b", re.I)


def proposition_problems(claim: dict) -> list[str]:
    p = claim.get("proposition", "")
    problems = []
    if _NEGATION.search(p) and claim.get("category") != "instruction":   # "Do not drive..." is fine
        problems.append("negated")
    if _ATTRIBUTION.search(p):
        problems.append("attribution inside proposition")
    return problems


# ================================================================= status ladder (no LLM involved)
#
# A claim group's members are claims from sources. Each member has: source_id, source_kind (official|news|social|other),
# source_name, stance (asserts|reports_unverified|denies), attributed_to, and for social posts _post (author, text, links).

import difflib as _difflib

STATUSES = ["verified", "corroborated", "disputed", "unverified", "contradicted"]
CRITICAL_CONDITIONS = {"evacuation_ordered", "casualties", "collapsed", "warning_issued"}


def _norm_name(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    s = re.sub(r"^\s*the\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def official_by_attribution(attributed_to: str | None, official_names: list[str]) -> str | None:
    """'Rescue Services' quoted by a news site -> 'Halvik Rescue Services' if that is a registered official source."""
    a = _norm_name(attributed_to or "")
    if not a:
        return None
    for name in official_names:
        n = _norm_name(name)
        if a == n or (len(a) > 6 and (a in n or n in a)):
            return name
    return None


def origins(members: list[dict], official_names: list[str]) -> dict[int, str]:
    """Map member index -> origin key. Independent origins are what the ladder counts, not posts.

    - a member quoting someone counts as that someone (a news article quoting an official = the official)
    - near-identical social posts (same text or same link) collapse into one origin
    - otherwise one origin per source (and per author for social feeds)
    """
    keys: dict[int, str] = {}
    for i, m in enumerate(members):
        official = official_by_attribution(m.get("attributed_to"), official_names)
        if official:
            keys[i] = f"official:{_norm_name(official)}"
        elif m.get("attributed_to"):
            keys[i] = f"attr:{_norm_name(m['attributed_to'])}"
        elif m.get("_post"):
            keys[i] = f"post:{m['source_id']}:{m['_post'].get('author_handle')}"
        else:
            keys[i] = f"src:{m['source_id']}"

    # union near-duplicate posts
    posts = [i for i, m in enumerate(members) if m.get("_post") and not m.get("attributed_to")]
    parent = {i: i for i in posts}

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for x, i in enumerate(posts):
        for j in posts[x + 1:]:
            a, b = members[i]["_post"], members[j]["_post"]
            same_link = bool(set(a.get("links") or []) & set(b.get("links") or []))
            similar = _difflib.SequenceMatcher(None, _norm_name(a.get("text", "")), _norm_name(b.get("text", ""))).ratio() >= 0.8
            if same_link or similar:
                parent[find(j)] = find(i)
    for i in posts:
        keys[i] = keys[find(i)]
    return keys


def assess(members: list[dict], official_names: list[str]) -> dict:
    """The transparent verdict for one claim group."""
    keys = origins(members, official_names)
    off_assert = off_deny = relay_assert = relay_deny = False
    assert_origins, deny_origins = set(), set()
    for i, m in enumerate(members):
        direct_official = m.get("source_kind") == "official" and not m.get("attributed_to")
        quoted_official = keys[i].startswith("official:") and not direct_official
        if m["stance"] == "asserts":
            if direct_official:
                off_assert = True
            elif quoted_official:
                relay_assert = True
            else:
                assert_origins.add(keys[i])
        elif m["stance"] == "denies":
            if direct_official:
                off_deny = True
            elif quoted_official:
                relay_deny = True
            else:
                deny_origins.add(keys[i])

    if off_deny or (relay_deny and not off_assert):
        status = "contradicted"
    elif off_assert:
        status = "verified"
    elif relay_assert or (len(assert_origins) >= 2 and not deny_origins):
        status = "corroborated"
    elif assert_origins and deny_origins:
        status = "disputed"
    else:
        status = "unverified"

    risky = any(m.get("safety_effective") == "risk_taking" for m in members)
    held = risky and status not in ("verified", "contradicted")
    return {"status": status, "held": held,
            "origins": {"asserting": len(assert_origins) + int(off_assert or relay_assert),
                        "denying": len(deny_origins) + int(off_deny or relay_deny)}}


def summarize(members: list[dict], official_names: list[str]) -> str:
    """One neutral sentence per stance, built from who said what. No LLM."""
    keys = origins(members, official_names)
    parts = []
    by = {"asserts": {}, "reports_unverified": {}, "denies": {}}
    for i, m in enumerate(members):
        who = m.get("source_name") or m["source_id"]
        by[m["stance"]].setdefault(who, {"n": 0, "origins": set(), "quotes": set(), "kind": m.get("source_kind")})
        e = by[m["stance"]][who]
        e["n"] += 1
        e["origins"].add(keys[i])
        if m.get("attributed_to"):
            e["quotes"].add(m["attributed_to"])

    def label(who, e):
        if e["kind"] == "social" and e["n"] > 1:
            o = len(e["origins"])
            text = f"{e['n']} posts on {who}" + (f" (near-identical, counted as {o} origin{'s' if o > 1 else ''})" if o < e["n"] else "")
        elif e["kind"] == "social":
            text = f"a post on {who}"
        else:
            text = who
        if e["quotes"]:
            text += f" (quoting {', '.join(sorted(e['quotes']))})"
        return text

    if by["asserts"]:
        parts.append("Stated by " + "; ".join(label(w, e) for w, e in by["asserts"].items()) + ".")
    if by["reports_unverified"]:
        parts.append("Relayed as unverified by " + "; ".join(label(w, e) for w, e in by["reports_unverified"].items()) + ".")
    if by["denies"]:
        parts.append("Called false by " + "; ".join(label(w, e) for w, e in by["denies"].items()) + ".")
    return " ".join(parts)


# Europe's common warning colours, in the languages our pages use (extracted propositions are English, but quotes
# keep the page's language, e.g. "Gele waarschuwing", "Orange Warnung", "alerta roja").
_COLOURS = [
    (re.compile(r"\b(red|rot(e[rns]?)?|rouge|ross[oa]|roj[oa]|roo?d[e]?|czerwon\w*|röd[a]?|rött|vermelh\w*|červen\w*|κόκκιν\w*)\b", re.I), 4),
    (re.compile(r"\b(orange|oranje|arancione|naranja|pomarańcz\w*|laranja|oranžov\w*|πορτοκαλ\w*)\b", re.I), 3),
    (re.compile(r"\b(yellow|gelb\w*|jaune|giall[oa]|amarill[oa]|gele?|geel|żółt\w*|gul[at]?|amarel[oa]|žlut\w*|κίτριν\w*)\b", re.I), 1),
]


def warning_colour_severity(*texts: str) -> int | None:
    """Official warning colours: red 4, orange 3, yellow 1. The first text that names a colour wins."""
    for text in texts:
        for rx, sev in _COLOURS:
            if rx.search(text or ""):
                return sev
    return None


def item_severity(item: dict) -> int:
    """1 advisory .. 4 critical, from what is established about one claim group."""
    status, cond = item["status"], item["condition"]
    if status == "verified":
        if cond == "warning_issued":
            quotes = " ".join(m.get("evidence_quote", "") for m in item["members"])
            colour = warning_colour_severity(item["title"], quotes)
            return colour if colour is not None else 3
        if cond in {"evacuation_ordered", "casualties", "collapsed"}:
            return 4
        # Closures, outages, local flooding and damage are disruptive but not life-threatening on their own: warning.
        if cond in {"flooded", "closed", "restricted", "water_rising", "utility_outage", "damaged"}:
            return 2
        return 2
    if status == "corroborated":
        return 3 if cond in CRITICAL_CONDITIONS else 2
    return 1


SEVERITY_LABELS = {4: "critical", 3: "severe", 2: "warning", 1: "advisory"}


_CLOCK = re.compile(r"\b(\d{1,2})[:.h](\d{2})\b")


def stated_minutes(members: list[dict]) -> int:
    """Clock time an official source gives for a statement ("raised to red at 12:45"), in minutes; -1 if none."""
    for m in members:
        if (m.get("source_kind") or m.get("kind")) != "official":
            continue
        hit = _CLOCK.search(m.get("time_stated") or "")
        if hit:
            return int(hit[1]) * 60 + int(hit[2])
    return -1


# A statement about the warning LEVEL (propositions are always English): a colour, or a change of level.
_WARNING_LEVEL = re.compile(r"\b(red|orange|amber|yellow|green)\b|\b(raised|lowered|lifted|upgraded|downgraded|"
                            r"withdrawn|cancell?ed|ended|stood down)\b|\b(warning|alert|threat) level\b", re.I)


def is_warning_level(members: list[dict]) -> bool:
    return any(_WARNING_LEVEL.search(m.get("proposition") or "") for m in members)


def superseded_warnings(warnings: list[tuple[str, str, str, list[dict]]]) -> set[str]:
    """Official warnings replace each other (orange -> red -> lowered -> lifted), but each stays "verified" because
    it was true when issued. Given verified warning items as (id, hazard, first_seen, members), return the ids of all
    but the newest per hazard. Newest = first seen latest; for statements captured together (an official page listing
    earlier updates), the later clock time stated on the official page."""
    by_hazard: dict[str, list] = {}
    for w in warnings:
        by_hazard.setdefault(w[1], []).append(w)
    old = set()
    for group in by_hazard.values():
        if len(group) > 1:
            newest = max(group, key=lambda w: (w[2] or "", stated_minutes(w[3])))
            old |= {w[0] for w in group if w[0] != newest[0]}
    return old


def rings(severity: int, radius_km: float) -> list[dict]:
    """Concentric zones for the map, innermost first: the colour shifts from the incident's severity at the
    epicentre down to advisory at the edge. Ring i (0..n-1) has severity n-i and radius R*(i+1)/n."""
    n = max(1, int(severity))
    return [{"radius_km": round(radius_km * (i + 1) / n, 2), "severity": n - i} for i in range(n)]
