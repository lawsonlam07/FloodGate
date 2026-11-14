"""Group claims from different sources into "the same claim" using an LLM, with rule-based guardrails.

Division of labour:
  - RULES decide what is allowed to merge: only claims with the same `condition` can share a group, and claims
    whose proposition failed the wording check are quarantined (never grouped).
  - The LLM decides what is the same real-world claim (handles paraphrase, aliases, descriptors, languages).
  - Claims arrive in batches and are ASSIGNED to existing groups or start new ones, so group ids stay stable.
  - Every assignment keeps a one-line reason, so the UI can show why claims were merged.

`propose` is the blocking LLM call and never mutates anything; `apply` mutates the groups. A server can run
`propose` in a worker thread and `apply` on its event loop.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from . import rules

MODEL = os.environ.get("GROUPING_MODEL", "claude-haiku-5-5")   # claude-opus-5-5 | claude-sonnet-5-5 | claude-haiku-5-5
EFFORT = os.environ.get("GROUPING_EFFORT", "low")              # low | medium | high

SYSTEM = """\
You maintain the claim groups of a live emergency situation board. Each claim was extracted from one source \
(official page, news article or social post) and has a fixed `condition` category, a `subject` and a `proposition`.

Your job: decide which claims make the SAME factual claim about the SAME real-world thing, so the board can count \
independent sources for it. Assign every new claim either to an existing group or to a new group.

Same group when:
- they refer to the same specific thing (the same bridge, road, district, building, service, line or area), even if it \
is named or described differently ("the bridge" vs its full name; "residents of X" vs "X residents"), and
- they claim the same state for it. Wording, level of detail, time precision, language, who said it and the stance \
(asserts / relays / denies) do NOT matter: a denial belongs in the same group as the claim it denies.

Separate groups when:
- the scope differs: a whole city vs one district, a district vs one street, all residents vs residents of part of an area;
- they are different things that happen to be nearby or related (a bridge vs the road leading to it);
- for condition "other": unless the propositions state the same fact.

When unsure, keep claims separate. A wrong merge can make a rumour look confirmed; a missed merge only delays \
confirmation.

You may only assign a claim to an existing group with the SAME condition. New claims in the same batch can share a \
new group. Give each new group a short neutral label (a positive proposition) and a canonical subject name. \
Reasons: at most 15 words."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "group": {"type": "string", "description": "An existing group id (e.g. 'G3') or a new-group key from new_groups."},
                    "reason": {"type": "string"},
                },
                "required": ["claim_id", "group", "reason"],
                "additionalProperties": False,
            },
        },
        "new_groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "e.g. 'new:1'"},
                    "label": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["key", "label", "subject"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments", "new_groups"],
    "additionalProperties": False,
}


@dataclass
class Group:
    id: str
    condition: str
    subject: str
    label: str
    members: list[dict] = field(default_factory=list)   # claims, each with "_reason"


@dataclass
class Proposal:
    result: dict | None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = MODEL
    error: str | None = None


def split_quarantine(claims: list[dict]) -> tuple[list[dict], list[dict]]:
    ok, bad = [], []
    for c in claims:
        problems = rules.proposition_problems(c)
        (bad if problems else ok).append({**c, "_problems": problems} if problems else c)
    return ok, bad


def propose(client, groups: dict[str, Group], claims: list[dict], batch: str,
            *, model: str = MODEL, effort: str = EFFORT) -> Proposal:
    """Blocking LLM call. Claims must carry claim_id, source_id, condition, subject, proposition, stance."""
    conditions = {c["condition"] for c in claims}
    existing = [
        {"id": g.id, "condition": g.condition, "subject": g.subject, "label": g.label,
         "examples": [m["proposition"] for m in g.members[:3]]}
        for g in groups.values() if g.condition in conditions
    ]
    new = [
        {"claim_id": c["claim_id"], "condition": c["condition"], "subject": c["subject"],
         "proposition": c["proposition"], "stance": c["stance"], "source": c["source_id"],
         "location": c.get("location")}
        for c in claims
    ]
    prompt = (f"Existing groups:\n{json.dumps(existing, ensure_ascii=False, indent=1)}\n\n"
              f"New claims (batch '{batch}'):\n{json.dumps(new, ensure_ascii=False, indent=1)}")

    # Server-side refusal fallbacks exist for Opus/Sonnet but not Haiku.
    fallback = {} if "haiku" in model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    start = time.monotonic()
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            **fallback,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # network / API errors: caller falls back to one group per claim
        return Proposal(None, round((time.monotonic() - start) * 1000), model=model, error=f"{type(exc).__name__}: {exc}")
    latency = round((time.monotonic() - start) * 1000)
    usage = dict(latency_ms=latency, input_tokens=response.usage.input_tokens,
                 output_tokens=response.usage.output_tokens, model=response.model)
    if response.stop_reason == "refusal":
        return Proposal(None, error=f"refused: {response.stop_details}", **usage)
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return Proposal(json.loads(text), **usage)
    except ValueError:
        return Proposal(None, error="unparseable output", **usage)


def _next_id(groups: dict[str, Group]) -> str:
    nums = [int(g[1:]) for g in groups if g[1:].isdigit()]
    return f"G{max(nums, default=0) + 1}"


def apply(groups: dict[str, Group], claims: list[dict], result: dict | None) -> tuple[list[tuple[dict, str]], list[str]]:
    """Apply assignments, enforcing the rules. Returns ([(claim, group_id)], rule violations that were overridden).

    With result=None (LLM unavailable) every claim gets its own group: missed merges are the safe failure.
    """
    result = result or {"assignments": [], "new_groups": []}
    by_id = {c["claim_id"]: c for c in claims}
    new_meta = {g["key"]: g for g in result["new_groups"]}
    created: dict[tuple[str, str], Group] = {}   # (new key, condition) -> group
    placements, violations, seen = [], [], set()

    for a in result["assignments"]:
        c = by_id.get(a["claim_id"])
        if c is None or a["claim_id"] in seen:
            violations.append(f"unknown or duplicate claim id {a['claim_id']!r}")
            continue
        seen.add(a["claim_id"])
        target = groups.get(a["group"])
        if target is not None and target.condition != c["condition"]:
            violations.append(f"{a['claim_id']} ({c['condition']}) sent to {target.id} ({target.condition}); split off")
            target = None
        if target is None:
            key = (a["group"], c["condition"])     # a new key spanning two conditions is split by the rule
            if key not in created:
                meta = new_meta.get(a["group"], {"label": c["proposition"], "subject": c["subject"]})
                gid = _next_id(groups)
                created[key] = groups[gid] = Group(gid, c["condition"], meta["subject"], meta["label"])
            target = created[key]
        target.members.append({**c, "_reason": a["reason"]})
        placements.append((c, target.id))

    for cid, c in by_id.items():                      # anything the model skipped gets its own group
        if cid not in seen:
            violations.append(f"{cid} not assigned; given its own group")
            gid = _next_id(groups)
            groups[gid] = Group(gid, c["condition"], c["subject"], c["proposition"], [{**c, "_reason": "unassigned"}])
            placements.append((c, gid))
    return placements, violations


class Grouper:
    """Convenience wrapper for scripts: quarantine + propose + apply in one call."""

    def __init__(self, client, effort: str = EFFORT, model: str = MODEL):
        self.client, self.effort, self.model = client, effort, model
        self.groups: dict[str, Group] = {}
        self.quarantined: list[dict] = []
        self.log: list[tuple[str, int, Proposal, list[str]]] = []

    def add_batch(self, name: str, claims: list[dict]) -> None:
        ok, bad = split_quarantine(claims)
        self.quarantined += bad
        if not ok:
            return
        proposal = propose(self.client, self.groups, ok, name, model=self.model, effort=self.effort)
        if proposal.error:
            raise RuntimeError(f"grouping failed: {proposal.error}")
        _, violations = apply(self.groups, ok, proposal.result)
        self.log.append((name, len(ok), proposal, violations))
