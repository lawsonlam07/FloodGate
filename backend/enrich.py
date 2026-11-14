"""Context.dev reads the official page behind a serious real warning.

The structured feeds (backend/feeds.py) are cheap tripwires: they say THAT a warning exists. When one reaches L2 or above,
Context.dev captures the human-facing official page the feed links to (a GDACS event report, an Environment Agency
flood page, the national weather service's warning page, ...) and extracts what it says about this hazard in this
area: key facts and the official instructions, each with a word-for-word quote. Quotes are checked against the
captured page text, exactly like the scenario pipeline; anything that is not on the page is dropped.

One capture per warning (a JSON extraction, a few credits), capped per day (ENRICH_DAILY), never while paused.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from .extraction import ground, normalize

ENRICH = os.environ.get("ENRICH_WARNINGS", "1") != "0"
ENRICH_DAILY = int(os.environ.get("ENRICH_DAILY", "60"))
ENRICH_MIN_LEVEL = int(os.environ.get("ENRICH_MIN_LEVEL", "2"))

_QUOTED = {"type": "string", "description": "Copied word for word from the page, in the page's own language."}
SCHEMA = {
    "type": "object",
    "properties": {
        "publisher": {"type": "string", "description": "The organisation that publishes this page."},
        "page_title": {"type": "string", "description": "The page's main heading."},
        "about_this_warning": {"type": "boolean", "description": "True if the page says anything about this hazard "
                                                                 "in this area (warning, forecast, impacts or advice)."},
        "facts": {
            "type": "array", "maxItems": 5,
            "items": {"type": "object", "properties": {
                "statement": {"type": "string", "description": "One short, plain English sentence: what is expected, "
                                                               "where, when, or what impact."},
                "evidence_quote": _QUOTED}, "required": ["statement", "evidence_quote"]},
        },
        "instructions": {
            "type": "array", "maxItems": 4,
            "items": {"type": "object", "properties": {
                "instruction": {"type": "string", "description": "What people in the area should do, as one short "
                                                                 "imperative English sentence."},
                "evidence_quote": _QUOTED}, "required": ["instruction", "evidence_quote"]},
        },
    },
    "required": ["about_this_warning", "facts", "instructions"],
}


def host_of(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def prompt(headline: str, areas: list[str], publisher: str) -> str:
    where = ", ".join(areas[:12]) + (f" and {len(areas) - 12} more" if len(areas) > 12 else "")
    return (f"This page is the official source behind this warning: \"{headline}\", issued by {publisher}, for: "
            f"{where or 'the area named in the warning'}.\n"
            "Extract only what the page says about this hazard in this area: up to 5 key facts (what is expected, "
            "when, where, impacts) and up to 4 safety instructions the page gives to the public. Every item needs an "
            "evidence_quote copied word for word from the page. Never write advice the page does not give. If the "
            "page has nothing about this warning, set about_this_warning to false and return empty lists.")


def capture(ctx, url: str, headline: str, areas: list[str], publisher: str, *, step: str) -> tuple[dict, list]:
    """Blocking. Returns (official_page result, Context.dev call logs)."""
    data, call = ctx.scrape(step, url, json_schema=SCHEMA, instructions=prompt(headline, areas, publisher),
                            max_age_ms=30 * 60 * 1000)          # a page captured in the last 30 min is fresh enough
    base = {"url": url, "host": host_of(url), "credits": call.credits_consumed}
    if not isinstance(data, dict):
        return {**base, "status": "failed", "error": call.error or "request failed"}, [call]
    js, md = data.get("json") or {}, (data.get("markdown") or {}).get("data") or ""
    if not js.get("success"):
        return {**base, "status": "failed", "error": f"extraction failed: {js.get('error_code') or js.get('message')}"}, [call]
    got = js.get("data") or {}
    page = normalize(md)

    def grounded(rows: list, key: str) -> list[dict]:
        out, seen = [], set()
        for r in rows or []:
            text, quote = (r.get(key) or "").strip(), (r.get("evidence_quote") or "").strip()
            if not text or not quote or text.lower() in seen:
                continue
            label, score = ground(quote, page)
            if label == "MISSING":                       # not on the page: never shown
                continue
            seen.add(text.lower())
            out.append({"text": text, "quote": quote, "grounding": label})
        return out

    facts, instructions = grounded(got.get("facts"), "statement"), grounded(got.get("instructions"), "instruction")
    relevant = bool(got.get("about_this_warning")) and bool(facts or instructions)
    return {**base, "status": "done" if relevant else "nothing", "publisher": (got.get("publisher") or "").strip(),
            "page_title": (got.get("page_title") or "").strip(), "facts": facts if relevant else [],
            "instructions": instructions if relevant else [],
            "dropped": len(got.get("facts") or []) + len(got.get("instructions") or []) - len(facts) - len(instructions)}, [call]
