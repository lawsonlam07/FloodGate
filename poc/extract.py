"""PoC: can Context.dev turn our sources into claims in our format?

For each source it does one fresh scrape (markdown + JSON extraction, optionally a screenshot), then checks:
  1. the extraction succeeded and matches our JSON schema exactly
  2. every evidence_quote really appears on the page (grounding check, catches hallucinated quotes)
  3. propositions are bare and positive (no negation, no "X says"), so claims match across sources
  4. for the fictional Halvik pages: spot checks for the tricky cases (rumours, denials, risky claims)
It applies the rule-based safety gate, groups claims across sources by (subject, condition), and prints
an API log (latency, credits, cache status). Raw responses go to out/<run>/.

Usage:
  uv run poc/extract.py --base-url https://<id>.lhr.life            # all three Halvik pages
  uv run poc/extract.py --base-url https://<id>.lhr.life --only rescue --screenshot
  uv run poc/extract.py --url https://yle.fi/uutiset --kind news    # any real page
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import rules
from backend.claims_schema import SCHEMAS
from backend.context_client import BudgetExceeded, ContextClient, load_dotenv, print_log
from backend.extraction import flatten_claims, ground, normalize, save_screenshot, schema_errors
from backend.extraction import silently_empty as _silently_empty

try:
    from jsonschema import Draft202012Validator
except ImportError:  # validation is optional; everything else still works
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Source:
    id: str
    path: str      # path under the base URL (or a full URL for ad-hoc sources)
    kind: str      # official | news | social | other  -- set by US, never by the LLM
    shape: str     # page | feed


HALVIK_SOURCES = [
    Source("rescue", "rescue/", "official", "page"),
    Source("herald", "herald/storm-surge-live.html", "news", "page"),
    Source("board", "board/", "social", "feed"),
]


# ---------------------------------------------------------------- grounding





# ---------------------------------------------------------------- checks

def _txt(c: dict) -> str:
    return f"{c['proposition']} {c['evidence_quote']}".lower()


def _is(c: dict, condition: str, stance: str | None = None) -> bool:
    return c["condition"] == condition and (stance is None or c["stance"] == stance)


def _bridge(c: dict) -> bool:
    return rules.subject_key(c["subject"]) == "harbour bridge"


GENERIC_CHECKS = [
    ("propositions never negated (instructions excepted)",
     lambda cs, d: not any("negated" in rules.proposition_problems(c) for c in cs)),
    ("no 'X says / claims' inside propositions",
     lambda cs, d: not any("attribution inside proposition" in rules.proposition_problems(c) for c in cs)),
]

EXPECTATIONS = {
    "rescue": [
        ("denies the Harbour Bridge collapse", lambda cs, d: any(_bridge(c) and _is(c, "collapsed", "denies") for c in cs)),
        ("denies that an all-clear was given", lambda cs, d: any(_is(c, "all_clear", "denies") for c in cs)),
        ("'no all-clear' is not treated as risk_taking",
         lambda cs, d: any(_is(c, "all_clear", "denies") for c in cs)
         and not any(_is(c, "all_clear", "denies") and c["safety_effective"] == "risk_taking" for c in cs)),
        ("finds the Saltmarsh evacuation order", lambda cs, d: any(_is(c, "evacuation_ordered") and "saltmarsh" in _txt(c) for c in cs)),
        ("finds >= 3 public instructions", lambda cs, d: sum(c["category"] == "instruction" for c in cs) >= 3),
    ],
    "herald": [
        ("never asserts the collapse on its own authority",
         lambda cs, d: not any(_is(c, "collapsed", "asserts") and not c["attributed_to"] for c in cs)),
        ("relays the collapse rumour AND the official denial",
         lambda cs, d: any(_is(c, "collapsed", "reports_unverified") for c in cs) and any(_is(c, "collapsed", "denies") for c in cs)),
        ("'water going down' is risk_taking",
         lambda cs, d: any(_is(c, "water_receding") and c["safety_effective"] == "risk_taking" for c in cs)),
        ("attributes the power cut to Halvik Energy",
         lambda cs, d: any(_is(c, "utility_outage") and "energy" in (c["attributed_to"] or "").lower() for c in cs)),
        ("ignores the 'Most read' sidebar",
         lambda cs, d: not any(w in _txt(c) for c in cs for w in ("halvik fc", "tram depot", "lights festival", "budget"))),
    ],
    "board": [
        ("returns all 9 posts", lambda cs, d: len(d.get("posts", [])) == 9),
        ("collapse asserted by 3 accounts", lambda cs, d: len({c["_post"]["author_handle"] for c in cs if _is(c, "collapsed", "asserts")}) == 3),
        ("captures 'joined 12 November 2026' signal",
         lambda cs, d: any("12 november 2026" in (p.get("account_joined") or "").lower() for p in d.get("posts", []))),
        ("captures repost count 212", lambda cs, d: any(p.get("repost_count") == 212 for p in d.get("posts", []))),
        ("Olli's 'water going down / heading home' is risk_taking",
         lambda cs, d: any("olli" in c["_post"]["author_handle"] and c["safety_effective"] == "risk_taking" for c in cs)),
        ("keeps the lookalike links", lambda cs, d: any("halvik-rescue-alerts" in l for p in d.get("posts", []) for l in p.get("links", []))),
    ],
}


def _sources_for(groups, key) -> set[str]:
    return {c["_source"] for c in groups.get(key, [])}


CROSS_CHECKS = [
    ("Harbour Bridge collapse matched across all 3 sources",
     lambda g: _sources_for(g, ("harbour bridge", "collapsed")) == {"rescue", "herald", "board"}),
    ("Harbour Bridge closure matched across all 3 sources",
     lambda g: _sources_for(g, ("harbour bridge", "closed")) == {"rescue", "herald", "board"}),
    ("collapse group holds the official denial and the social assertions",
     lambda g: any(c["_source"] == "rescue" and c["stance"] == "denies" for c in g.get(("harbour bridge", "collapsed"), []))
     and any(c["_source"] == "board" and c["stance"] == "asserts" for c in g.get(("harbour bridge", "collapsed"), []))),
]


def run_checks(checks, *args) -> tuple[int, int]:
    passed = 0
    for name, check in checks:
        try:
            ok = bool(check(*args))
        except (KeyError, TypeError) as exc:
            ok, name = False, f"{name} (check crashed: {exc})"
        passed += ok
        print(f"   {'PASS' if ok else 'FAIL'}  {name}")
    return passed, len(checks)


# ---------------------------------------------------------------- per-source run





def print_claim(i: int, c: dict) -> None:
    who = f"@{c['_post']['author_handle']}: " if "_post" in c else ""
    attrib = f"  ← {c['attributed_to']}" if c.get("attributed_to") else ""
    when = f"  [{c['time_stated']}]" if c.get("time_stated") else ""
    mark = {"exact": "✓", "near": "≈"}.get(c["grounding"], "✗")
    print(f"   {i:>2}. {mark} {c['stance']:<18} {c['safety_effective']:<11} "
          f"{c['subject']} · {c['condition']}: {who}{c['proposition']}{attrib}{when}")
    notes = []
    if c["grounding"] != "exact":
        notes.append(f"quote {c['grounding']} {c['grounding_score']}: {c['evidence_quote']!r}")
    if c["safety_disagreement"]:
        notes.append(f"safety: LLM said {c['safety_direction']}, rule said {c['safety_rule']}")
    notes += [f"proposition {p}" for p in rules.proposition_problems(c)]
    for n in notes:
        print(f"       ⚠ {n}")


def run_source(client: ContextClient, src: Source, url: str, out_dir: Path, *, screenshot: bool, cached: bool) -> tuple[dict, list[dict]]:
    schema, instructions = SCHEMAS[src.shape]
    summary = {"source": src.id, "kind": src.kind, "url": url, "claims": 0, "schema": "-", "grounded": "-", "checks": "-"}

    print(f"\n━━ {src.id} ({src.kind}) · {url}")
    for attempt in (1, 2):
        print("   scraping (fresh fetch + extraction" + (" + screenshot" if screenshot else "") + ")...", flush=True)
        data, entry = client.scrape(f"extract:{src.id}" + (" (retry)" if attempt > 1 else ""), url, json_schema=schema,
                                    instructions=instructions, screenshot=screenshot, max_age_ms=None if cached else 0)
        print(f"   done in {entry.latency_ms / 1000:.1f} s · HTTP {entry.http_status} · "
              f"{entry.credits_consumed if entry.credits_consumed is not None else '?'} credits · cache {entry.cache_status}")
        # Seen in testing: extraction "succeeds" with an empty list on a page full of text. Treat that as a failure.
        if attempt == 1 and _silently_empty(data, src.shape):
            print("   ⚠ extraction returned no items although the page has text; retrying once")
            continue
        break
    summary.update(latency_ms=entry.latency_ms, credits=entry.credits_consumed, cache=entry.cache_status)

    if not isinstance(data, dict):
        print(f"   request failed: {entry.error}")
        return summary, []

    shot_path = save_screenshot(data.get("screenshot") or {}, out_dir / f"{src.id}.png")
    if shot_path and isinstance(data.get("screenshot"), dict):
        data["screenshot"]["data"] = f"<saved to {shot_path}>"
    (out_dir / f"{src.id}.raw.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))

    md, js = data.get("markdown") or {}, data.get("json") or {}
    if not md.get("success"):
        print(f"   markdown failed: {md.get('error_code')} {md.get('message')}")
    if not js.get("success"):
        print(f"   JSON extraction failed: {js.get('error_code')} {js.get('message')} (HTTP {entry.http_status})")
        return summary, []

    extracted = js["data"] or {}
    errors = schema_errors(extracted, schema)
    summary["schema"] = "OK" if not errors else f"{len(errors)} errors"
    for e in errors[:10]:
        print(f"   schema: {e}")
    if errors:   # the rest of the pipeline assumes the schema holds
        return summary, []

    page_norm = normalize(md.get("data") or "")
    claims = flatten_claims(extracted, src.shape)
    for c in claims:
        c["_source"] = src.id
        c["grounding"], c["grounding_score"] = ground(c.get("evidence_quote", ""), page_norm)
        rules.apply_safety_gate(c)
    exact = sum(c["grounding"] == "exact" for c in claims)
    near = sum(c["grounding"] == "near" for c in claims)
    summary["claims"] = len(claims)
    summary["grounded"] = f"{exact}+{near}~/{len(claims)}"

    header = {k: v for k, v in extracted.items() if k not in ("claims", "posts")}
    if header:
        print("   " + " · ".join(f"{k}={v!r}" for k, v in header.items()))
    if src.shape == "feed":
        for p in extracted.get("posts", []):
            g, _ = ground(p.get("text", ""), page_norm)
            print(f"   post @{(p.get('author_handle') or '').lstrip('@')} · {p.get('posted_at')} · {p.get('account_joined')} · "
                  f"{p.get('follower_count')} followers · {p.get('repost_count')} reposts · text:{g}")

    for i, c in enumerate(claims, 1):
        print_claim(i, c)

    passed, total = run_checks(GENERIC_CHECKS + EXPECTATIONS.get(src.id, []), claims, extracted)
    summary["checks"] = f"{passed}/{total}"

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = [{"source_id": src.id, "source_kind": src.kind, "source_url": url, "scraped_at": stamp,
                "request_id": entry.request_id, **header, **{k: v for k, v in c.items() if k != "_source"}} for c in claims]
    (out_dir / f"{src.id}.claims.json").write_text(json.dumps(records, indent=2, ensure_ascii=False))
    return summary, claims


# ---------------------------------------------------------------- cross-source matching

def print_matches(claims: list[dict], out_dir: Path, source_order: list[str]) -> dict:
    groups = rules.group_claims(claims)
    rank = {s: i for i, s in enumerate(source_order)}
    ordered = sorted(groups.items(), key=lambda kv: (-len({c["_source"] for c in kv[1]}), -len(kv[1]), kv[0]))
    shown = [(k, cs) for k, cs in ordered if len(cs) > 1]

    width = max((len(f"{k[0]} · {k[1]}") for k, _ in shown), default=0)
    for (subject, condition), cs in shown:
        by_source: dict[str, Counter] = {}
        for c in cs:
            by_source.setdefault(c["_source"], Counter())[(c["stance"], c.get("attributed_to"))] += 1
        parts = []
        for src in sorted(by_source, key=lambda s: rank.get(s, 99)):
            items = [f"{st}{f' ← {who}' if who else ''}{f' ×{n}' if n > 1 else ''}" for (st, who), n in by_source[src].items()]
            parts.append(f"{src}: " + ", ".join(items))
        n_src = len(by_source)
        print(f"   {f'{subject} · {condition}':<{width}}  {n_src} source{'s' if n_src > 1 else ' '}  " + " | ".join(parts))
    singles = len(groups) - len(shown)
    print(f"   ({singles} keys with a single claim not shown; claims with condition 'other' are not matched)")

    (out_dir / "matches.json").write_text(json.dumps(
        [{"subject": k[0], "condition": k[1],
          "claims": [{"source": c["_source"], "stance": c["stance"], "attributed_to": c.get("attributed_to"),
                      "proposition": c["proposition"], "evidence_quote": c["evidence_quote"],
                      "author": c.get("_post", {}).get("author_handle")} for c in cs]}
         for k, cs in ordered], indent=2, ensure_ascii=False))
    return groups


# ---------------------------------------------------------------- main

def print_summary(rows: list[dict]) -> None:
    cols = ["source", "kind", "claims", "schema", "grounded", "checks", "latency_ms", "credits", "cache"]
    table = [[str(r.get(c, "-")) for c in cols] for r in rows]
    widths = [max(len(c), *(len(t[i]) for t in table)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for t in table:
        print("  ".join(v.ljust(w) for v, w in zip(t, widths)))


def preflight(url: str) -> str | None:
    """Make sure the page is publicly reachable before spending credits on it."""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "claims-poc-preflight"})
    except requests.RequestException as exc:
        return f"{type(exc).__name__}: {exc}"
    return None if r.ok else f"HTTP {r.status_code}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", help="public URL where fake_site/ is served")
    ap.add_argument("--only", action="append", choices=[s.id for s in HALVIK_SOURCES], help="limit to these Halvik sources")
    ap.add_argument("--url", help="scrape an arbitrary page instead of the Halvik sites")
    ap.add_argument("--kind", default="other", choices=["official", "news", "social", "other"], help="kind for --url")
    ap.add_argument("--shape", default="page", choices=list(SCHEMAS), help="schema for --url")
    ap.add_argument("--screenshot", action="store_true", help="also request a full-page screenshot (evidence)")
    ap.add_argument("--cached", action="store_true", help="allow Context.dev's cache instead of forcing a fresh fetch")
    ap.add_argument("--budget", type=float, default=100, help="stop before a call once this many credits are spent")
    ap.add_argument("--group", action="store_true", help="afterwards, group claims across sources with the LLM (needs ANTHROPIC_API_KEY)")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.environ.get("CONTEXT_DEV_API_KEY")
    if not key:
        print("CONTEXT_DEV_API_KEY is not set (put it in .env or export it).", file=sys.stderr)
        return 2

    if args.url:
        jobs = [(Source("adhoc", args.url, args.kind, args.shape), args.url)]
    elif args.base_url:
        base = args.base_url.rstrip("/") + "/"
        jobs = [(s, base + s.path) for s in HALVIK_SOURCES if not args.only or s.id in args.only]
    else:
        ap.error("give --base-url (Halvik sites) or --url")

    out_dir = ROOT / "out" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    client = ContextClient(key, budget=args.budget)

    summaries, all_claims = [], []
    for src, url in jobs:
        problem = preflight(url)
        if problem:
            print(f"\n━━ {src.id}: skipped, {url} is not reachable ({problem})")
            continue
        try:
            summary, claims = run_source(client, src, url, out_dir, screenshot=args.screenshot, cached=args.cached)
        except BudgetExceeded as exc:
            print(f"\nStopping: {exc}")
            break
        summaries.append(summary)
        all_claims += claims

    if all_claims:
        print("\n══ Cross-source matches (subject · condition)")
        groups = print_matches(all_claims, out_dir, [s.id for s, _ in jobs])
        if {c["_source"] for c in all_claims} == {"rescue", "herald", "board"}:
            passed, total = run_checks(CROSS_CHECKS, groups)
            summaries.append({"source": "cross", "kind": "-", "checks": f"{passed}/{total}"})

    print("\n══ Sources")
    print_summary(summaries)
    print("\n══ Context.dev calls")
    print_log(client.log)
    print(f"\nCredits spent this run: {client.credits_spent:g} · outputs in {out_dir.relative_to(ROOT)}/")
    (out_dir / "api_log.json").write_text(json.dumps([vars(e) for e in client.log], indent=2))
    if args.group and all_claims:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import grouping
        print("\n══ LLM grouping")
        grouping.group_run(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
