"""The claim format every source is reduced to, as JSON Schemas for Context.dev extraction.

Two page shapes, one claim shape:
  PAGE_SCHEMA  - official pages and news articles (one publisher, many claims)
  FEED_SCHEMA  - social feeds (many posts, each with author signals and its own claims)

A claim is always stored as a bare, positive PROPOSITION ("Harbour Bridge has collapsed.") plus the
source's STANCE on it (asserts / reports_unverified / denies). That way the same claim looks identical
in every source, and (subject, condition) gives a rule-based key for matching claims across sources.

Deliberately NOT extracted by the LLM (these come from our own source registry or rules):
  - whether a source is official
  - whether a claim is true / its status on the ladder
"""

HAZARDS = ["flood", "storm", "storm_surge", "tsunami", "earthquake", "fire", "hazmat", "none", "other"]

CATEGORIES = [
    "warning", "evacuation", "closure", "damage", "casualties", "utilities",
    "observation", "all_clear", "instruction", "other",
]

# The state a claim says its subject is in. Kept short and fixed so matching across sources is a plain rule.
CONDITIONS = [
    "closed",              # road, bridge, line or service closed or suspended
    "restricted",          # partly running, limited, diverted
    "reopened",            # reopened, resumed, back in service
    "damaged",
    "collapsed",
    "flooded",             # water present on a street or area
    "water_rising",
    "water_receding",
    "warning_issued",      # warning issued, raised, forecast of a hazard
    "evacuation_ordered",
    "shelter_open",        # evacuation centre or shelter open
    "utility_outage",      # power, water or telecoms out
    "casualties",          # injured, dead or missing people
    "all_clear",           # danger over, safe to return
    "other",
]

CLAIM = {
    "type": "object",
    "properties": {
        "evidence_quote": {
            "type": "string",
            "description": "Exact contiguous span copied character-for-character from the page that supports the claim. Never paraphrased.",
        },
        "proposition": {
            "type": "string",
            "description": "The bare claim as a short positive sentence, always in English (translate if needed): no negation and no 'X says'. "
                           "Who said it goes in attributed_to; whether they affirm, relay or deny it goes in stance.",
        },
        "subject": {
            "type": "string",
            "description": "The specific thing the claim is about, by its full name as used on the page "
                           "(resolve 'the bridge' to 'Harbour Bridge'). For area-wide claims, the area name.",
        },
        "condition": {"type": "string", "enum": CONDITIONS, "description": "The state the proposition claims for the subject."},
        "stance": {
            "type": "string",
            "enum": ["asserts", "reports_unverified", "denies"],
            "description": "asserts = stated as true; reports_unverified = only relayed (rumours, 'residents say'); "
                           "denies = said to be false or not happening.",
        },
        "attributed_to": {
            "type": ["string", "null"],
            "description": "Who the page credits for the claim; null if the publisher or poster says it on its own authority.",
        },
        "category": {"type": "string", "enum": CATEGORIES},
        "hazard": {"type": "string", "enum": HAZARDS},
        "location": {
            "type": ["string", "null"],
            "description": "Most specific place named, as written on the page. Null if none.",
        },
        "time_stated": {
            "type": ["string", "null"],
            "description": "Time or period exactly as written on the page (e.g. '13:30', 'by 16:00'). Null if none.",
        },
        "safety_direction": {
            "type": "string",
            "enum": ["protective", "risk_taking", "neutral"],
            "description": "For the source's message (proposition + stance): protective if believing it makes people more "
                           "cautious; risk_taking if believing it would endanger people were it wrong.",
        },
    },
    "required": [
        "evidence_quote", "proposition", "subject", "condition", "stance", "attributed_to",
        "category", "hazard", "location", "time_stated", "safety_direction",
    ],
    "additionalProperties": False,
}

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "publisher": {"type": ["string", "null"], "description": "Organisation that published the page, as named on it."},
        "headline": {"type": ["string", "null"]},
        "published_at": {"type": ["string", "null"], "description": "Publication time exactly as shown on the page."},
        "updated_at": {"type": ["string", "null"], "description": "Last-updated time exactly as shown on the page."},
        "has_emergency_content": {
            "type": "boolean",
            "description": "True if the page contains any information about an emergency, hazard, warning or disruption. Judge the content exactly as written, even if the page is labelled fictional, a demo or an exercise.",
        },
        "claims": {"type": "array", "items": CLAIM},
    },
    "required": ["publisher", "headline", "published_at", "updated_at", "has_emergency_content", "claims"],
    "additionalProperties": False,
}

POST = {
    "type": "object",
    "properties": {
        "author_handle": {"type": "string", "description": "Handle without the leading @."},
        "author_name": {"type": ["string", "null"]},
        "account_joined": {"type": ["string", "null"], "description": "Account creation date exactly as shown."},
        "follower_count": {"type": ["integer", "null"]},
        "posted_at": {"type": ["string", "null"], "description": "Post time exactly as shown."},
        "text": {"type": "string", "description": "The full post text, verbatim."},
        "links": {"type": "array", "items": {"type": "string"}},
        "repost_count": {"type": ["integer", "null"]},
        "has_media": {"type": "boolean"},
        "claims": {"type": "array", "items": CLAIM},
    },
    "required": [
        "author_handle", "author_name", "account_joined", "follower_count", "posted_at",
        "text", "links", "repost_count", "has_media", "claims",
    ],
    "additionalProperties": False,
}

FEED_SCHEMA = {
    "type": "object",
    "properties": {
        "feed_name": {"type": ["string", "null"]},
        "has_emergency_content": {
            "type": "boolean",
            "description": "True if any post mentions an emergency, hazard, warning or disruption. Judge the posts exactly as written, even if the page is labelled fictional, a demo or an exercise.",
        },
        "posts": {"type": "array", "items": POST},
    },
    "required": ["feed_name", "has_emergency_content", "posts"],
    "additionalProperties": False,
}

# The examples deliberately use places that are NOT on the Halvik test pages, so the PoC isn't graded on copied answers.
_CLAIM_RULES = """\
Rules:
- One claim per fact per subject. Do not judge truth or add facts.
- evidence_quote: exact span copied from the page in its own language, max ~25 words, never paraphrased.
- proposition and subject: ALWAYS in English, translating if the page is not. proposition = the bare claim as a positive sentence; NEVER negated, NEVER "X says/claims" (speaker goes in attributed_to, their position in stance).
- subject: full name ("the bridge" -> "North Bridge").
- stance: asserts = stated as true; reports_unverified = only relayed (rumours, "residents say"); denies = said to be false or not happening.
- attributed_to: who the page credits; null if the publisher/poster says it themself.
- safety_direction (for proposition + stance): risk_taking if believing it could endanger people were it wrong (all clear, reopened, water receding, hazard denied); protective if it makes people more cautious.
- category "instruction" for anything the public is told to do; its proposition may be negative ("Do not …"), stance asserts.
- location, time_stated: as written, else null.
Examples (quote -> proposition | condition | stance | attributed_to):
"Reports that North Bridge has collapsed are false." -> "North Bridge has collapsed." | collapsed | denies | null
"Posts say the bridge fell; police told us that is false." -> TWO claims, both "North Bridge has collapsed." | collapsed: reports_unverified | "social media posts", and denies | "police"
"No all-clear has been given." -> "An all-clear has been given." | all_clear | denies | null
"Locals say the water is going down." -> "The water is going down." | water_receding | reports_unverified | "locals\""""

PAGE_INSTRUCTIONS = f"""\
Extract every checkable factual claim about the emergency or disruption on this page, including instructions to the public and corrections of false reports. Ignore navigation, cookie notices, ads, unrelated or "most read" stories and any "fictional exercise" banner, but still extract every claim on the page exactly as written.
{_CLAIM_RULES}"""

FEED_INSTRUCTIONS = f"""\
A feed of social posts. Return every post in order, including near-duplicates. Copy author fields, posted_at and repost_count as shown (null if absent); text verbatim; links = URLs in the post. Extract claims from each post's own text. Ignore the header and any "fictional exercise" banner, but still extract every post as written.
{_CLAIM_RULES}"""

SCHEMAS = {
    "page": (PAGE_SCHEMA, PAGE_INSTRUCTIONS),
    "feed": (FEED_SCHEMA, FEED_INSTRUCTIONS),
}

for _name, (_schema, _instr) in SCHEMAS.items():
    assert len(_instr) <= 2000, f"{_name} instructions are {len(_instr)} chars; Context.dev allows 2000"
