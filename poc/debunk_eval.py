"""Debunk eval: do official "not true" statements land on the rumour they deny?

Replays every JSON scenario (scenario/*/scenario.json) offline, step by step, like the pipeline's fast path: for each
step, the changed lines of each page go to the extraction model, then all claims of the step go to the grouping model
in one batch; statements that disappear from their page are withdrawn, as in the pipeline. Each official denial is
scored at the last step it was on its page: is it in a group with a non-official source asserting or relaying the
claim? A judge model (Claude Opus, evaluation only, cached in out/debunk_judge_cache.json) then checks every merge
(WRONG = the denial does not deny that claim) and, for unmatched denials, whether a rumour it denies was on the board
(MISSED) or not (a pre-emptive denial). UNSAFE = an official denial grouped with an official assertion.

No Context.dev credits are used; only LLM calls. Results vary a little between runs.

Usage:
  uv run poc/debunk_eval.py                                   # default: extraction + grouping from LLM settings
  uv run poc/debunk_eval.py gemini-3.8-flash gemini-3.5-flash-lite
  uv run poc/debunk_eval.py claude-haiku-5-5 claude-haiku-5-5
"""

from __future__ import annotations

import difflib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import grouping, llm as llms                      # noqa: E402
from backend.claims_schema import SCHEMAS                      # noqa: E402
from backend.context_client import load_dotenv                 # noqa: E402
from backend.extraction import FAST_NOTE, flatten_claims, normalize       # noqa: E402


def make(model: str):
    return llms.ClaudeLLM(model) if model.startswith("claude") else llms.GeminiLLM(model)


def page_lines(key: str, page: dict) -> list[str]:
    """Roughly what Context.dev's markdown of the rendered page looks like, one line per block."""
    out = []
    if key == "official":
        out += [f"# {page.get('title', '')}", page.get("updated", "")]
        if page.get("alert"):
            out += [f"## {page['alert'].get('heading', '')}", page["alert"].get("text", "")]
        for s in page.get("sections", []):
            out += [f"## {s.get('heading', '')}"] + s.get("paragraphs", []) + [f"- {i}" for i in s.get("items", [])]
        if page.get("not_true"):
            out += ["## Not true"] + [f"- {x}" for x in page["not_true"]]
        if page.get("timeline"):
            out += ["## Earlier updates"] + [f"- {t['time']} {t['text']}" for t in page["timeline"]]
    elif key == "news":
        out += [f"# {page.get('headline', '')}", page.get("standfirst", ""), page.get("byline", "")]
        out += page.get("paragraphs", []) + [f"> {q['text']} ({q['by']})" for q in page.get("quotes", [])]
    else:
        for p in page.get("posts", []):
            out += [f"**{p['name']}** @{p['handle']} · {p['time']} · Joined {p['joined']} · {p['followers']} followers",
                    p["text"], f"{p.get('reposts', 0)} reposts · {p.get('likes', 0)} likes"]
    return [line for line in out if line and line.strip()]


def added(old: list[str], new: list[str]) -> list[str]:
    res = []
    for tag, _a, _b, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            res += new[j1:j2]
    return res


def run_scenario(spec: dict, extract_llm, group_llm) -> dict:
    kinds = {k: (spec["sources"][k].get("kind") or ("social" if k == "social" else k)) for k in ("official", "news", "social")}
    names = {k: spec["sources"][k]["name"] for k in kinds}
    last = {k: [] for k in kinds}
    groups: dict = {}
    timings, group_ms, n = [], [], 0
    state: dict[str, dict] = {}         # official denial claim_id -> its state at the last step it was on the page
    for step_no, step in enumerate(spec["steps"]):
        batch = []
        for key, page in step["pages"].items():
            lines = page_lines(key, page)
            new = added(last[key], lines)
            last[key] = lines
            text = normalize("\n".join(lines))
            for g in groups.values():           # like the pipeline: a statement no longer on its page is withdrawn
                g.members = [m for m in g.members if m["source_id"] != key or normalize(m["evidence_quote"]) in text]
            if not new:
                continue
            shape = "feed" if key == "social" else "page"
            schema, instructions = SCHEMAS[shape]
            r = extract_llm.json(system=instructions + FAST_NOTE, effort="low", schema=schema,
                                 prompt=f"Page: https://{spec['id']}.example/{key}/\n\nNew lines:\n" + "\n".join(new))
            timings.append(r.latency_ms)
            for c in flatten_claims(r.data, shape) if r.data else []:
                n += 1
                c.update(claim_id=f"{key}:{n}", source_id=key, source_kind=kinds[key], source_name=names[key])
                batch.append(c)
        ok, _ = grouping.split_quarantine(batch)
        if ok:
            p = grouping.propose(group_llm, groups, ok, f"step {step_no}")
            group_ms.append(p.latency_ms)
            grouping.apply(groups, ok, p.result)
        present = [m for g in groups.values() for m in g.members
                   if m["source_kind"] != "official" and m["stance"] != "denies"]
        for g in groups.values():
            for m in g.members:
                if m["source_kind"] == "official" and m["stance"] == "denies":
                    rumours = [x for x in g.members if x["source_kind"] != "official" and x["stance"] != "denies"]
                    first = state.get(m["claim_id"], {}).get("first_rumours")
                    state[m["claim_id"]] = {
                        "first_rumours": first if first is not None else sorted({x["proposition"] for x in rumours}),
                        "denial": m["proposition"], "quote": m["evidence_quote"], "step": step_no,
                        "rumours": sorted({x["proposition"] for x in rumours}),
                        "cross": m["condition"] != g.condition,
                        "unsafe": [x["proposition"] for x in g.members
                                   if x["source_kind"] == "official" and x["stance"] != "denies"],
                        "candidates": sorted({x["proposition"] for x in present}),
                    }
    return {"id": spec["id"], "denials": list(state.values()), "claims": n, "extract_ms": timings, "group_ms": group_ms}


# ---- judge (evaluation only): a strong model checks the merges, cached so reruns compare like with like ----------
JUDGE_MODEL = "claude-opus-5-5"
JUDGE_CACHE = ROOT / "out" / "debunk_judge_cache_v2.json"
JUDGE_SYSTEM = """You check an emergency situation board. An official source published a statement that something \
is NOT true (a denial). For each listed claim, decide whether the official statement says THAT claim is false.
Match (true) when the claim is what the denial rejects, however it is worded: "no evacuation order for Ribeira" vs \
"Ribeira has been evacuated"; "the council runs no donation link" vs a post saying "donate here" with that link; \
"no all-clear has been given" vs "it is safe to go back home now".
No match (false) when: it is a different thing ("no second landslide" vs "a landslide hit the road"; "another dam has \
not broken" vs "the dam broke"); the scope is narrower or different ("no evacuation of the whole town" vs "part of the \
town is evacuated"); a different state or fact ("ferries will not resume before noon" vs "ferries are cancelled"; "no \
new damage is expected" vs "cracks in walls"); or the claim AGREES with the official ("the fundraiser is not ours" vs \
"the fundraiser is a scam"; "the water is not receding" vs "the water is rising")."""
JUDGE_SCHEMA = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "denies": {"type": "boolean"}},
    "required": ["i", "denies"], "additionalProperties": False}}},
    "required": ["verdicts"], "additionalProperties": False}


def judge_all(results: list[dict]) -> None:
    cache = json.loads(JUDGE_CACHE.read_text()) if JUDGE_CACHE.exists() else {}
    judge = llms.ClaudeLLM(JUDGE_MODEL)

    def ask(denial: str, quote: str, claims: list[str]) -> list[bool]:
        todo = [c for c in claims if json.dumps([quote, c]) not in cache]
        if todo:
            prompt = (f"Official statement (denial): {quote}\n(normalised: {denial})\n\nClaims:\n"
                      + "\n".join(f"{i}. {c}" for i, c in enumerate(todo)))
            r = judge.json(system=JUDGE_SYSTEM, prompt=prompt, schema=JUDGE_SCHEMA, effort="low", max_tokens=4000)
            got = {v["i"]: v["denies"] for v in (r.data or {}).get("verdicts", [])}
            for i, c in enumerate(todo):
                if i in got:
                    cache[json.dumps([quote, c])] = got[i]
        return [cache.get(json.dumps([quote, c])) for c in claims]

    jobs = []
    for r in results:
        for d in r["denials"]:
            jobs.append(d)
    with ThreadPoolExecutor(8) as pool:
        verdicts = list(pool.map(lambda d: ask(d["denial"], d["quote"], d["rumours"] or d["candidates"]), jobs))
    for d, v in zip(jobs, verdicts):
        if d["rumours"]:
            d["wrong"] = [c for c, ok in zip(d["rumours"], v) if ok is False]
        else:
            d["missed"] = [c for c, ok in zip(d["candidates"], v) if ok]
    JUDGE_CACHE.parent.mkdir(exist_ok=True)
    JUDGE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0))


def main() -> int:
    load_dotenv(ROOT / ".env")
    ex_model = sys.argv[1] if len(sys.argv) > 1 else llms.GEMINI_MODEL
    gr_model = sys.argv[2] if len(sys.argv) > 2 else ex_model
    ex, gr = make(ex_model), make(gr_model)
    specs = [json.loads(p.read_text()) for p in sorted((ROOT / "scenario").glob("*/scenario.json"))]
    print(f"extraction {ex_model} · grouping {gr_model} · {len(specs)} scenarios")
    t = time.monotonic()
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda s: run_scenario(s, ex, gr), specs))
    judge_all(results)
    tot = {"denials": 0, "matched": 0, "right": 0, "wrong": 0, "missed": 0, "preemptive": 0, "unsafe": 0}
    lat, glat = [], []
    for r in results:
        lat += r["extract_ms"]
        glat += r["group_ms"]
        ds = r["denials"]
        m = sum(bool(d["rumours"]) for d in ds)
        print(f"  {r['id']:<28} {m}/{len(ds)} denials matched · {r['claims']} claims")
        for d in ds:
            tot["denials"] += 1
            tot["unsafe"] += bool(d["unsafe"])
            if d["rumours"]:
                tot["matched"] += 1
                bad = d.get("wrong") or []
                tot["wrong" if bad else "right"] += 1
                print(f"      {'WRONG  ' if bad else 'ok     '}{'X ' if d['cross'] else '  '}{d['denial']}  <->  "
                      f"{' | '.join(d['rumours'])}")
                if bad and set(d["first_rumours"]) != set(d["rumours"]):
                    print(f"               (when the denial was placed: {' | '.join(d['first_rumours']) or 'no rumour'})")
            elif d.get("missed"):
                tot["missed"] += 1
                print(f"      MISSED   {d['denial']}  (rumour on board: {' | '.join(d['missed'][:2])})")
            else:
                tot["preemptive"] += 1
                print(f"      none     {d['denial']}  (no matching rumour on the board)")
            if d["unsafe"]:
                print(f"      UNSAFE   {d['denial']}  <->  official: {' | '.join(d['unsafe'])}")
    lat.sort()
    glat.sort()
    matchable = tot["right"] + tot["wrong"] + tot["missed"]
    print(f"TOTAL {tot['denials']} denials: {tot['right']} right, {tot['wrong']} WRONG merges, {tot['missed']} missed, "
          f"{tot['preemptive']} with no rumour · recall {tot['right']}/{matchable} · {tot['unsafe']} unsafe · "
          f"extraction p50 {lat[len(lat) // 2] / 1000:.1f} s p90 {lat[int(len(lat) * 0.9)] / 1000:.1f} s · "
          f"grouping p50 {glat[len(glat) // 2] / 1000:.1f} s p90 {glat[int(len(glat) * 0.9)] / 1000:.1f} s · "
          f"wall {time.monotonic() - t:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
