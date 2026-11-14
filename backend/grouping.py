"""Group claims from different sources into "the same claim" using an LLM, with rule-based guardrails.

Division of labour:
  - RULES decide what is allowed to merge: only claims with the same `condition` can share a group, and claims
    whose proposition failed the wording check are quarantined (never grouped). One exception: an OFFICIAL DENIAL
    may join a group with another condition (rumour and denial are often labelled differently, e.g. "collapsed" vs
    "other"). And an official denial never joins a group that an official source asserts, whatever the condition:
    seen in testing, "another dam has broken" (denied) was merged into "the dam has broken" (true), which would
    have made an official fact look disputed.
  - The LLM decides what is the same real-world claim (handles paraphrase, aliases, descriptors, languages).
  - On by default (DENIAL_CHECK=0 turns it off): official denials get a second, focused LLM check (`_check_denials`, one call, only
    for batches that contain one): which rumour does this "not true" statement deny, if any? It first writes the
    false claim in positive form, which stops "the link is not ours" landing on "the link is a scam". In the
    scenario eval (poc/debunk_eval.py, Flash-Lite) it raised correct denial matches from ~12-20 to ~20-24 and cut
    judged-wrong merges from ~4-8 to ~3-4, for ~+0.5-1 s on those batches. DENIAL_MODEL picks another model for it.
  - Claims arrive in batches and are ASSIGNED to existing groups or start new ones, so group ids stay stable.
  - Every assignment keeps a one-line reason, so the UI can show why claims were merged.

`propose` is the blocking LLM call and never mutates anything; `apply` mutates the groups. A server can run
`propose` in a worker thread and `apply` on its event loop.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field

from . import rules

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
- one says the other is fake, a scam or untrue without being a denial ("Donate at this link" vs "This donation link is \
a scam"; "The water is going down" vs "The water is still rising"): opposite claims, never the same group.

When unsure, keep claims separate. A wrong merge can make a rumour look confirmed; a missed merge only delays \
confirmation.

You may only assign a claim to an existing group with the SAME condition. One exception: a claim with stance \
"denies" from an official source (source_kind "official") may go to an existing group with a different condition when \
it denies exactly the claim that group makes ("Reports of a toxic cloud are false" -> the group "There is a toxic \
cloud over X"; "No second landslide has occurred" -> "A second landslide hit the village"). Same specific thing and \
same state only; when unsure, keep it separate. An official denial never belongs in a group that an official source \
asserts ("No second dam has broken" is NOT the group "The dam has broken"). New claims in the same batch can share a \
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
    model: str = ""
    error: str | None = None


def official_denial(claim: dict) -> bool:
    return claim.get("stance") == "denies" and claim.get("source_kind") == "official"


def officially_asserted(group: Group) -> bool:
    return any(m.get("source_kind") == "official" and m.get("stance") != "denies" for m in group.members)


def split_quarantine(claims: list[dict]) -> tuple[list[dict], list[dict]]:
    ok, bad = [], []
    for c in claims:
        problems = rules.proposition_problems(c)
        (bad if problems else ok).append({**c, "_problems": problems} if problems else c)
    return ok, bad


DENIAL_SYSTEM = """\
You check official denials on a live emergency situation board. A denial is a statement from an official source that \
something is NOT true. Candidates are claim groups made by news or social sources (rumours, reports). For each denial, \
first write `false_claim`: the claim the official says is false, as a plain positive sentence (for "Reports of a \
toxic cloud are untrue" write "There is a toxic cloud"; for "The donation link is not ours" write "The council runs \
this donation link"). Then pick the ONE candidate that makes exactly that false claim, or "none".

Pick a candidate only when it states the false_claim: the same specific thing (same place, object, organisation, \
service or event), the same state, and the same or a NARROWER scope (a denial about the whole town covers a claim \
about the whole town, not a claim about one district). Wording, tense and language do not matter. Examples:
- "Reports that the bridge collapsed are false" -> "The bridge has collapsed": yes.
- "There was never a toxic cloud" -> "A toxic cloud is over the village": yes; -> "Thick smoke over the village": no.
- "No second landslide has occurred" -> "A second landslide hit the village": yes; -> "A landslide hit the road": no.
- "The upstream Las Hoces dam has not broken" -> "Las Hoces dam has collapsed": yes.
- "Ferries will not resume before noon tomorrow" -> "Ferries are cancelled": no (a different claim).
- "No evacuation of the whole town" -> "The whole town is being evacuated": yes; -> "An evacuation is ordered": no.
- "The water is not going down" -> "The water is falling": yes; -> "The dam outflow was reduced": no.
- "The pier was not completely destroyed" -> "The pier is gone": yes; -> "There is some damage": no.
- "The council will not cut the water during the day" -> "There is a water outage": no (different claim).
- "The council collects no donations online" -> "Donate now at this link" (a post presenting it as genuine): yes; \
-> "That donation link is a scam" or "Do not trust donation links": no. They AGREE with the official.
- "The dam has not broken" -> "The dam is stable": no. A denial only matches a claim it says is false.
When unsure, answer "none": a wrong match makes something true look false. Reasons: at most 15 words."""

DENIAL_SCHEMA = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {"claim_id": {"type": "string"},
                       "false_claim": {"type": "string", "description": "What the official says is false, positively."},
                       "target": {"type": "string", "description": "A candidate id that states false_claim, or 'none'."},
                       "reason": {"type": "string"}},
        "required": ["claim_id", "false_claim", "target", "reason"], "additionalProperties": False}}},
    "required": ["decisions"],
    "additionalProperties": False,
}


DEBUG_LOG: list | None = None          # set to a list (evals) to record every denial check
_DENIAL_LLM: list = []


def _denial_llm():
    """DENIAL_MODEL (env) picks a different model for the denial check; default: the grouping model."""
    name = os.environ.get("DENIAL_MODEL", "").strip()
    if not name:
        return None
    if not _DENIAL_LLM:
        from . import llm as llms
        primary = llms.ClaudeLLM(name) if name.startswith("claude") else llms.GeminiLLM(name)
        _DENIAL_LLM.append(llms.WithFallback(primary, llms.make("anthropic") if name.startswith("gemini") else None))
    return _DENIAL_LLM[0]


def _is_rumour(m: dict) -> bool:
    return m.get("source_kind") != "official" and m.get("stance") != "denies"


def _denial_target(members: list[dict]) -> bool:
    """A group an official denial may join: someone non-official claims it, and no official source asserts it."""
    return any(_is_rumour(m) for m in members) and not any(
        m.get("source_kind") == "official" and m.get("stance") != "denies" for m in members)


def _check_denials(llm, groups: dict[str, Group], claims: list[dict], result: dict, effort: str):
    """Second opinion for official denials: rewrite their assignments to the rumour they deny, or their own group.
    Returns (result, LLMResult or None when no call was needed)."""
    denials = {c["claim_id"]: c for c in claims if official_denial(c)}
    if not denials:
        return result, None
    by_id = {c["claim_id"]: c for c in claims}
    batch_members: dict[str, list[dict]] = defaultdict(list)       # new-group key -> claims of this batch
    for a in result["assignments"]:
        if a["group"] not in groups and a["claim_id"] in by_id:
            batch_members[a["group"]].append(by_id[a["claim_id"]])
    labels = {g["key"]: g["label"] for g in result["new_groups"]}
    candidates = {gid: (g.label, [m["proposition"] for m in g.members if _is_rumour(m)][:3])
                  for gid, g in groups.items() if _denial_target(g.members)}
    candidates.update({key: (labels.get(key) or ms[0]["proposition"], [m["proposition"] for m in ms if _is_rumour(m)][:3])
                       for key, ms in batch_members.items() if _denial_target(ms)})
    if not candidates:
        return result, None
    prompt = ("Denials:\n" + json.dumps([{"claim_id": c["claim_id"], "denial": c["proposition"],
                                          "official_words": c.get("evidence_quote")} for c in denials.values()],
                                        ensure_ascii=False, indent=1)
              + "\n\nCandidates:\n" + json.dumps([{"id": k, "claim": label, "examples": ex}
                                                 for k, (label, ex) in candidates.items()], ensure_ascii=False, indent=1))
    checker = _denial_llm() or llm
    r = checker.json(system=DENIAL_SYSTEM, prompt=prompt, schema=DENIAL_SCHEMA, effort=effort, max_tokens=4000)
    if r.data is None:
        return result, r                      # keep the grouping decision; apply() still enforces the rules
    decisions = {d["claim_id"]: d for d in r.data.get("decisions", [])}
    if DEBUG_LOG is not None:
        DEBUG_LOG.append({"denials": list(denials), "candidates": candidates, "decisions": decisions})
    assignments, new_groups = [], list(result["new_groups"])
    for a in result["assignments"]:
        d, c = decisions.get(a["claim_id"]), denials.get(a["claim_id"])
        if c is not None and d is not None:
            if d["target"] in candidates:
                a = {**a, "group": d["target"], "reason": f"denies: {d['reason']}"}
            else:                                  # "none": undo a merge with a rumour the check rejected
                current = groups[a["group"]].members if a["group"] in groups else batch_members.get(a["group"], [])
                if any(_is_rumour(m) for m in current):
                    key = f"own:{a['claim_id']}"
                    a = {**a, "group": key, "reason": f"denies nothing on the board: {d['reason']}"}
                    new_groups.append({"key": key, "label": c["proposition"], "subject": c["subject"]})
        assignments.append(a)
    return {"assignments": assignments, "new_groups": new_groups}, r


def propose(llm, groups: dict[str, Group], claims: list[dict], batch: str, *, effort: str = EFFORT) -> Proposal:
    """Blocking LLM call (llm: backend.llm.make()). Claims must carry claim_id, source_id, condition, subject,
    proposition, stance."""
    conditions = {c["condition"] for c in claims}
    any_condition = any(official_denial(c) for c in claims)       # official denials may cross conditions
    existing = [
        {"id": g.id, "condition": g.condition, "subject": g.subject, "label": g.label,
         "examples": [m["proposition"] for m in g.members[:3]]}
        for g in groups.values() if any_condition or g.condition in conditions
    ]
    new = [
        {"claim_id": c["claim_id"], "condition": c["condition"], "subject": c["subject"],
         "proposition": c["proposition"], "stance": c["stance"], "source": c["source_id"],
         "source_kind": c.get("source_kind"), "location": c.get("location")}
        for c in claims
    ]
    prompt = (f"Existing groups:\n{json.dumps(existing, ensure_ascii=False, indent=1)}\n\n"
              f"New claims (batch '{batch}'):\n{json.dumps(new, ensure_ascii=False, indent=1)}")
    r = llm.json(system=SYSTEM, prompt=prompt, schema=OUTPUT_SCHEMA, effort=effort)
    out = Proposal(r.data, r.latency_ms, r.input_tokens, r.output_tokens, r.model, r.error)
    if out.result is not None and os.environ.get("DENIAL_CHECK", "1") != "0":     # on by default; DENIAL_CHECK=0 skips it
        out.result, check = _check_denials(llm, groups, claims, out.result, effort)
        if check is not None:
            out.latency_ms += check.latency_ms
            out.input_tokens += check.input_tokens
            out.output_tokens += check.output_tokens
    return out


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

    # official denials last, so the rumour they deny is already placed when they join it
    ordered = sorted(result["assignments"], key=lambda a: official_denial(by_id.get(a["claim_id"]) or {}))
    for a in ordered:
        c = by_id.get(a["claim_id"])
        if c is None or a["claim_id"] in seen:
            violations.append(f"unknown or duplicate claim id {a['claim_id']!r}")
            continue
        seen.add(a["claim_id"])
        target = groups.get(a["group"])
        if target is not None and official_denial(c) and officially_asserted(target):
            violations.append(f"{a['claim_id']} (official denial) sent to {target.id}, which an official source "
                              f"asserts; split off")
            target, a = None, {**a, "group": f"split:{a['claim_id']}"}
        elif target is not None and target.condition != c["condition"] and not official_denial(c):
            violations.append(f"{a['claim_id']} ({c['condition']}) sent to {target.id} ({target.condition}); split off")
            target = None
        if target is None:
            key = (a["group"], c["condition"])     # a new key spanning two conditions is split by the rule
            if key not in created and official_denial(c):   # ...except for an official denial joining a rumour
                key = next((k for k, g in created.items() if k[0] == a["group"] and _denial_target(g.members)), key)
            if key in created and official_denial(c) and officially_asserted(created[key]):
                violations.append(f"{a['claim_id']} (official denial) put with an official assertion; split off")
                key = (f"split:{a['claim_id']}", c["condition"])
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

    def __init__(self, llm, effort: str = EFFORT):
        self.llm, self.effort = llm, effort
        self.groups: dict[str, Group] = {}
        self.quarantined: list[dict] = []
        self.log: list[tuple[str, int, Proposal, list[str]]] = []

    def add_batch(self, name: str, claims: list[dict]) -> None:
        ok, bad = split_quarantine(claims)
        self.quarantined += bad
        if not ok:
            return
        proposal = propose(self.llm, self.groups, ok, name, effort=self.effort)
        if proposal.error:
            raise RuntimeError(f"grouping failed: {proposal.error}")
        _, violations = apply(self.groups, ok, proposal.result)
        self.log.append((name, len(ok), proposal, violations))
