"""Turn one web page into claims via Context.dev: scrape + JSON extraction, then validate and check grounding.

Pure functions plus `extract_source`, which does the (blocking) API call; callers run it in a thread.
"""

from __future__ import annotations

import base64
import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import rules
from .claims_schema import SCHEMAS
from .context_client import CallLog

try:
    from jsonschema import Draft202012Validator
except ImportError:  # validation is optional
    Draft202012Validator = None


# ---------------------------------------------------------------- grounding

_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", " ": " "})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _MD_LINK.sub(r"\1", text)
    text = text.translate(_PUNCT)
    text = text.replace("\\", "")          # markdown escapes like \] or \*
    text = re.sub(r"[*_`#>|]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def ground(quote: str, page_norm: str) -> tuple[str, float]:
    """Is the quote really on the page? Returns (exact|near|MISSING, score 0..1)."""
    q = normalize(quote)
    if not q or not page_norm:
        return "MISSING", 0.0
    if q in page_norm:
        return "exact", 1.0
    m = difflib.SequenceMatcher(None, page_norm, q, autojunk=False).find_longest_match(0, len(page_norm), 0, len(q))
    lo = max(0, m.a - m.b - 20)
    hi = min(len(page_norm), m.a + (len(q) - m.b) + 20)
    blocks = difflib.SequenceMatcher(None, page_norm[lo:hi], q, autojunk=False).get_matching_blocks()
    score = sum(b.size for b in blocks) / len(q)
    return ("near" if score >= 0.9 else "MISSING"), round(score, 2)


# ---------------------------------------------------------------- validation and shaping

def schema_errors(data, schema) -> list[str]:
    if Draft202012Validator is None:
        return []
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in Draft202012Validator(schema).iter_errors(data)]


def flatten_claims(extracted: dict, shape: str) -> list[dict]:
    if shape == "page":
        return [dict(c) for c in extracted.get("claims", [])]
    claims = []
    for post in extracted.get("posts", []):
        meta = {k: v for k, v in post.items() if k != "claims"}
        meta["author_handle"] = (meta.get("author_handle") or "").lstrip("@")
        for c in post.get("claims", []):
            claims.append({**c, "_post": meta})
    return claims


# Phrases that mean a page is NOT calm. Used to overrule the extractor when it claims a page has no emergency
# content (seen live: a "fictional exercise" banner made it skip a page with a red warning on it).
_STRONG_HAZARD = re.compile(r"\b(evacuat\w*|(red|orange) warning|closed to|collaps\w*|must leave|storm surge warning)\b", re.I)


def silently_empty(data, shape: str) -> bool:
    """Seen in testing: extraction 'succeeds' with an empty list on a page full of text."""
    if not isinstance(data, dict):
        return False
    md, js = data.get("markdown") or {}, data.get("json") or {}
    if not (js.get("success") and isinstance(js.get("data"), dict)):
        return False
    items = js["data"].get("claims" if shape == "page" else "posts") or []
    if items:
        return False
    text = md.get("data") or ""
    if js["data"].get("has_emergency_content") is False:
        # Trust "calm" only if the page text agrees (rules beat the model's self-report).
        return bool(_STRONG_HAZARD.search(text))
    return len(text) > 500


def save_screenshot(shot: dict, dest: Path) -> str | None:
    data = shot.get("data")
    if not (shot.get("success") and isinstance(data, str)):
        return None
    if data.startswith("http"):
        return data
    b64 = data.split(",", 1)[1] if data.startswith("data:") else data
    dest.write_bytes(base64.b64decode(b64))
    return str(dest)


# ---------------------------------------------------------------- one source

@dataclass
class Extraction:
    ok: bool
    calls: list[CallLog] = field(default_factory=list)
    header: dict = field(default_factory=dict)       # publisher / headline / feed_name ...
    claims: list[dict] = field(default_factory=list)
    raw: dict | None = None
    error: str | None = None
    schema_errors: list[str] = field(default_factory=list)
    calm: bool = False                                 # extractor says: no emergency content on this page


def extract_source(client, url: str, shape: str, *, step: str, screenshot: bool = False, max_age_ms: int | None = 0) -> Extraction:
    """Blocking. Scrape + extract one page, retrying once if extraction silently returns nothing."""
    schema, instructions = SCHEMAS[shape]
    result = Extraction(ok=False)
    data = None
    for attempt in (1, 2):
        data, entry = client.scrape(step + (" (retry)" if attempt > 1 else ""), url, json_schema=schema,
                                    instructions=instructions, screenshot=screenshot, max_age_ms=max_age_ms)
        result.calls.append(entry)
        if attempt == 1 and silently_empty(data, shape):
            continue
        break
    result.raw = data

    if not isinstance(data, dict):
        result.error = result.calls[-1].error or "request failed"
        return result
    if silently_empty(data, shape):        # still empty after the retry: a failure, never "the page says nothing"
        result.error = "extraction returned no items for a page with text (after retry)"
        return result
    js, md = data.get("json") or {}, data.get("markdown") or {}
    if not js.get("success"):
        result.error = f"extraction failed: {js.get('error_code')} {js.get('message')}"
        return result
    extracted = js.get("data") or {}
    result.schema_errors = schema_errors(extracted, schema)
    if result.schema_errors:
        result.error = f"{len(result.schema_errors)} schema errors"
        return result

    page_norm = normalize(md.get("data") or "")
    claims = flatten_claims(extracted, shape)
    for c in claims:
        c["grounding"], c["grounding_score"] = ground(c.get("evidence_quote", ""), page_norm)
        rules.apply_safety_gate(c)
    result.header = {k: v for k, v in extracted.items() if k not in ("claims", "posts", "has_emergency_content")}
    result.calm = extracted.get("has_emergency_content") is False and not claims
    result.claims = claims
    result.ok = True
    return result


# ---------------------------------------------------------------- fast incremental updates
#
# For a page we already captured: Context.dev fetches it as markdown (fast, 1 credit), we diff it against the last
# capture, and a small LLM (Gemini Flash-Lite or Claude Haiku, see backend/llm.py) extracts claims from only the
# changed lines. Quotes are still grounded against the full
# Context.dev markdown, and withdrawals are decided by rule (a quote that is no longer on the page). Anything that
# looks unusual (no baseline, big change, LLM error) asks the caller for a full Context.dev JSON extraction instead.

_LINK_TARGET = re.compile(r"\]\([^)]*\)")         # link URLs change with the tunnel; compare link text only

FAST_NOTE = """
You receive only the lines that are NEW on this page since the last check, plus the page's headings for context.
Extract claims only from the new lines; every evidence_quote must be copied from the new lines.
A statement that something is fake, a scam, a hoax or untrue DENIES that thing: proposition = what it rejects, stance \
denies ("This donation link is a scam" -> "This donation link is a genuine collection." | denies). A post that pushes \
the thing ("Donate here!") asserts it."""


def markdown_lines(md: str) -> list[str]:
    return [_LINK_TARGET.sub("]", line).strip() for line in md.splitlines() if line.strip()]


def diff_markdown(old: str, new: str) -> list[str]:
    """Lines added (or rewritten) on the new page, in page order."""
    o, n = markdown_lines(old), markdown_lines(new)
    added = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, o, n, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            added += n[j1:j2]
    return added


@dataclass
class Update(Extraction):
    markdown: str = ""
    added_lines: list[str] = field(default_factory=list)
    needs_full: bool = False                       # caller should fall back to Context.dev JSON extraction
    llm_latency_ms: int = 0
    llm_model: str = ""


def extract_update(ctx, llm, url: str, shape: str, old_markdown: str, *, step: str) -> Update:
    """Blocking. Markdown scrape via Context.dev + LLM extraction of the changed lines (llm: backend.llm.make())."""
    result = Update(ok=False)
    data, entry = ctx.scrape(step + " (markdown)", url, markdown=True, max_age_ms=0)
    result.calls.append(entry)
    md = ((data or {}).get("markdown") or {}) if isinstance(data, dict) else {}
    if not md.get("success") or not md.get("data"):
        result.error = entry.error or f"markdown scrape failed: {md.get('error_code')} {md.get('message')}"
        return result
    result.markdown = new_md = md["data"]
    result.added_lines = added = diff_markdown(old_markdown, new_md)
    if not added:                                   # e.g. only link targets changed: nothing new to extract
        result.ok = True
        return result
    if len("\n".join(added)) > 0.8 * len("\n".join(markdown_lines(new_md))):
        result.needs_full = True                    # nearly the whole page changed: let Context.dev re-read it all
        return result

    schema, instructions = SCHEMAS[shape]
    headings = [line for line in markdown_lines(new_md) if line.startswith("#")][:12]
    prompt = f"Page: {url}\nHeadings:\n" + "\n".join(headings) + "\n\nNew lines:\n" + "\n".join(added)
    r = llm.json(system=instructions + FAST_NOTE, prompt=prompt, schema=schema, effort="low", max_tokens=8000)
    result.llm_latency_ms, result.llm_model = r.latency_ms, r.model
    if r.data is None:
        result.error, result.needs_full = (r.error or "no output")[:200], True
        return result
    extracted = r.data
    result.schema_errors = schema_errors(extracted, schema)
    if result.schema_errors:
        result.error, result.needs_full = f"{len(result.schema_errors)} schema errors", True
        return result

    page_norm = normalize(new_md)
    claims = flatten_claims(extracted, shape)
    for c in claims:
        c["grounding"], c["grounding_score"] = ground(c.get("evidence_quote", ""), page_norm)
        rules.apply_safety_gate(c)
    result.claims = [c for c in claims if c["grounding"] != "MISSING"]     # never keep a quote that isn't on the page
    result.header = {k: v for k, v in extracted.items() if k not in ("claims", "posts", "has_emergency_content")}
    result.ok = True
    return result
