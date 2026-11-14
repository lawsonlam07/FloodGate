"""Group claims from different sources into "the same claim" using an LLM, with rule-based guardrails.

Division of labour:
  - RULES decide what is allowed to merge: only claims with the same `condition` can share a group, and claims
    whose proposition failed the wording check are quarantined (never grouped).
  - The LLM decides what is the same real-world claim (handles paraphrase, aliases, descriptors, languages).
  - Claims arrive in batches (one per source scrape) and are ASSIGNED to existing groups or start new ones,
    so group ids stay stable as new evidence arrives.
  - Every assignment stores a one-line reason, so the UI can show why claims were merged.

Usage:
  uv run poc/grouping.py out/<run>                          # group the claims saved by extract.py
  uv run poc/grouping.py out/<run> gemini [model]           # or: anthropic [model]; default = LLM_PROVIDER setting
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import llm as llms
from backend.context_client import load_dotenv
from backend.grouping import EFFORT, Grouper

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- evaluation on the Halvik scenario

def _text(c: dict) -> str:
    return f"{c['subject']} {c['proposition']}".lower()


def _pick(cond: str, *words: str):
    return lambda c: c["condition"] == cond and all(w in _text(c) for w in words)


SHOULD_MERGE = [
    ("Harbour Bridge collapse", _pick("collapsed", "bridge")),
    ("Harbour Bridge closure", _pick("closed", "harbour bridge")),
    ("Saltmarsh evacuation order", _pick("evacuation_ordered", "saltmarsh")),
    ("Northgate School shelter", _pick("shelter_open", "northgate")),
    ("Lindholm power cut", _pick("utility_outage", "lindholm")),
    ("Lindholm ferry suspension", _pick("closed", "ferry serv")),     # not "ferry terminal" (Coast Road)
    ("Tram line 4 restriction", _pick("restricted", "tram")),
    ("Old Harbour 2.1 m peak", lambda c: c["condition"] == "water_rising" and "2.1" in c["proposition"]),
    ("'Cars in the water' bot posts", lambda c: "cars" in c["proposition"].lower() and "water" in c["proposition"].lower()),
]

SHOULD_SPLIT = [
    ("'all of Halvik evacuated' vs Saltmarsh evacuation", _pick("evacuation_ordered", "all of halvik"), _pick("evacuation_ordered", "saltmarsh")),
    ("Coast Road closure vs Harbour Bridge closure", _pick("closed", "coast road"), _pick("closed", "harbour bridge")),
    ("ferry suspension vs Coast Road closure", _pick("closed", "ferry serv"), _pick("closed", "coast road")),
]


def evaluate(grouper: Grouper) -> tuple[int, int]:
    where = {m["claim_id"]: g.id for g in grouper.groups.values() for m in g.members}
    claims = [m for g in grouper.groups.values() for m in g.members]
    passed = total = 0
    for name, pred in SHOULD_MERGE:
        hits = [c for c in claims if pred(c)]
        gids = {where[c["claim_id"]] for c in hits}
        srcs = {c["source_id"] for c in hits}
        ok = len(hits) >= 2 and len(gids) == 1
        detail = f"{len(hits)} claims from {sorted(srcs)} -> {len(gids)} group(s) {sorted(gids)}"
        passed += ok; total += 1
        print(f"   {'PASS' if ok else 'FAIL'}  merge: {name:<40} {detail}")
    for name, pa, pb in SHOULD_SPLIT:
        ga = {where[c["claim_id"]] for c in claims if pa(c)}
        gb = {where[c["claim_id"]] for c in claims if pb(c)}
        ok = bool(ga) and bool(gb) and not (ga & gb)
        passed += ok; total += 1
        print(f"   {'PASS' if ok else 'FAIL'}  split: {name:<40} {sorted(ga)} vs {sorted(gb)}")
    return passed, total


# ---------------------------------------------------------------- output

SOURCE_ORDER = ["rescue", "herald", "board"]


def print_groups(grouper: Grouper) -> None:
    rank = {s: i for i, s in enumerate(SOURCE_ORDER)}
    groups = sorted(grouper.groups.values(), key=lambda g: (-len({m["source_id"] for m in g.members}), -len(g.members), g.id))
    for g in groups:
        if len(g.members) < 2:
            continue
        by_src: dict[str, Counter] = {}
        for m in g.members:
            by_src.setdefault(m["source_id"], Counter())[(m["stance"], m.get("attributed_to"))] += 1
        parts = []
        for s in sorted(by_src, key=lambda s: rank.get(s, 99)):
            parts.append(f"{s}: " + ", ".join(
                f"{st}{f' ← {who}' if who else ''}{f' ×{n}' if n > 1 else ''}" for (st, who), n in by_src[s].items()))
        print(f"   {g.id:<4} [{g.condition}] {g.label}")
        print(f"        {' | '.join(parts)}")
        for m in g.members:
            print(f"          · {m['claim_id']:<10} {m['proposition'][:70]:<70}  — {m['_reason']}")
    singles = sum(len(g.members) == 1 for g in grouper.groups.values())
    print(f"   ({singles} single-claim groups not shown)")
    if grouper.quarantined:
        print("\n   Quarantined (failed the wording check, not grouped):")
        for c in grouper.quarantined:
            print(f"   ⚠ {c['claim_id']:<10} {c['proposition']}  ({', '.join(c['_problems'])})")


def load_run(run_dir: Path) -> list[tuple[str, list[dict]]]:
    batches = []
    for src in ["board", "herald", "rescue", "adhoc"]:      # social first: roughly the order things surface in reality
        f = run_dir / f"{src}.claims.json"
        if not f.exists():
            continue
        claims = json.loads(f.read_text())
        for i, c in enumerate(claims, 1):
            c["claim_id"] = f"{src}:{i}"
        batches.append((src, claims))
    return batches


def make_llm(provider: str | None, model: str | None):
    if provider == "gemini":
        return llms.GeminiLLM(model or llms.GEMINI_MODEL)
    if provider == "anthropic":
        return llms.ClaudeLLM(model or llms.CLAUDE_MODEL)
    return llms.make()


def group_run(run_dir: Path, effort: str = EFFORT, provider: str | None = None, model: str | None = None) -> Grouper:
    load_dotenv(ROOT / ".env")
    llm = make_llm(provider, model)
    if llm is None:
        raise SystemExit("No LLM key set (GEMINI_API_KEY or ANTHROPIC_API_KEY in .env).")
    grouper = Grouper(llm, effort)
    print(f"   {llm.provider} {llm.model} · effort {effort}")
    for name, claims in load_run(run_dir):
        print(f"   grouping batch '{name}' ({len(claims)} claims)...", flush=True)
        grouper.add_batch(name, claims)

    print("\n══ Claim groups (LLM-assigned, rule-checked)")
    print_groups(grouper)
    if {b for b, _ in load_run(run_dir)} >= {"rescue", "herald", "board"}:
        print("\n══ Grouping checks (Halvik scenario)")
        passed, total = evaluate(grouper)
        print(f"   {passed}/{total} passed")
    print("\n══ LLM calls")
    for name, n, prop, violations in grouper.log:
        print(f"   {name:<7} {n:>3} claims · {prop.latency_ms / 1000:.1f} s · {prop.input_tokens} in / "
              f"{prop.output_tokens} out tokens · {prop.model}" + (f" · {len(violations)} rule overrides" if violations else ""))
        for v in violations:
            print(f"      rule override: {v}")

    (run_dir / f"groups-{llm.model}-{effort}.json").write_text(json.dumps(
        [{"id": g.id, "condition": g.condition, "subject": g.subject, "label": g.label,
          "members": [{"claim_id": m["claim_id"], "source": m["source_id"], "stance": m["stance"],
                       "attributed_to": m.get("attributed_to"), "proposition": m["proposition"],
                       "evidence_quote": m["evidence_quote"], "reason": m["_reason"]} for m in g.members]}
         for g in grouper.groups.values()], indent=2, ensure_ascii=False))
    return grouper


if __name__ == "__main__":
    if not 2 <= len(sys.argv) <= 4:
        raise SystemExit(__doc__)
    group_run(Path(sys.argv[1]), provider=sys.argv[2] if len(sys.argv) > 2 else None,
              model=sys.argv[3] if len(sys.argv) > 3 else None)
