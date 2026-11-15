# Fictional disaster scenarios (format)

Each scenario is one fictional disaster somewhere in Europe, told as a storyline of steps. A step publishes new versions of
up to three fictional web pages (an official authority page, a local news article, a public social feed). FloodGate's
monitors notice the change, Context.dev captures the pages, and the board verifies the claims, exactly as for real pages.

Files: `scenario/<id>/scenario.json`. The backend renders the pages to `data/sites/<id>/<source>/index.html` (same visual
style as the Halvik pages) and serves them at `/sites/<id>/<source>/` and through the tunnel. The hand-written Halvik
storyline (`scenario/halvik/steps.json` + HTML files) stays as it is.

## Hard rules
- **Everything is fictional**: town, organisations, newspapers, people, accounts. Never use a real organisation, agency,
  newspaper, brand or person (no real fire services, civil-protection agencies, broadcasters, police forces). Real
  countries/regions may be named only in `region` (for the map); the town itself is invented.
- Links in posts only use `.example` domains (e.g. `https://brennholt-alerts.example/live`).
- Plausible but not graphic. Casualty claims, if any, are vague ("several people injured"), never gory.
- Coordinates: a plausible spot in the named region, not on top of a real city centre.

## scenario.json
```json
{
  "id": "brennholt-forest-fire",                 // kebab-case, unique, = folder name
  "name": "Brennholt forest fire",
  "hazard": "fire",                              // flood | storm | storm_surge | fire | earthquake | landslide | hazmat |
                                                 // power_outage | heatwave | avalanche | tsunami | dam_failure | other
  "scale": "moderate",                           // minor | moderate | major | catastrophic (author intent)
  "language": "de",                              // language of all three pages: en de fr it es nl pl sv pt cs el ...
  "region": "Black Forest, Germany",             // real region (for humans/map placement); the town is fictional
  "area": {"lat": 48.12, "lon": 8.21, "radius_km": 5, "label": "Brennholt (fictional)"},
  "summary": "A forest fire spreads towards two villages; a fake 'all clear' circulates before officials debunk it.",
  "sources": {
    "official": {"name": "Brennholt Feuerwehr und Rettung", "kind": "official"},
    "news":     {"name": "Schwarzwald-Kurier", "kind": "news"},
    "social":   {"name": "Brennholt Board", "kind": "social"}
  },
  "steps": [
    {
      "clock": "14:05",
      "title": "First smoke reports",
      "description": "Residents post about smoke; no official statement yet.",
      "expect": "Unverified social reports only; low severity.",
      "pages": { "official": { }, "news": { }, "social": { } }   // FULL page content at this step; omit a page = unchanged
    }
  ]
}
```

A step's page object is the **complete** content of that page at that moment (not a delta). Omit a page from a step to
leave it unchanged. The first step must include all three pages.

### official page
```json
{
  "title": "Waldbrand bei Brennholt",
  "updated": "Lagebericht Nr. 2 · 14. November 2026, 15:10 Uhr",
  "alert": {"level": "orange", "heading": "Orange Warnung: Waldbrand", "text": "…"},   // level: yellow|orange|red, or null
  "sections": [
    {"heading": "Evakuierung", "paragraphs": ["…"], "items": ["…"]},                  // paragraphs and/or bullet items
    {"heading": "Was Sie tun sollten", "items": ["…", "…"]}
  ],
  "not_true": ["Berichte, dass … sind falsch."],                                       // official corrections, may be []
  "timeline": [{"time": "14:40", "text": "…"}]                                        // earlier updates, newest first
}
```
Use the colour words of Europe's common warning scale (yellow / orange / red, in the page's language) for official
warnings: the board derives severity from them (yellow = advisory, orange = severe, red = critical; evacuation orders,
collapses and casualties are critical).

### news page
```json
{
  "kicker": "Live",
  "headline": "…",
  "standfirst": "…",
  "byline": "Von unserer Redaktion",
  "published": "14.11.2026, 14:30", "updated": "15:20",
  "paragraphs": ["…"],                           // may relay rumours ("Posts claim …") and official statements
  "quotes": [{"text": "…", "by": "eine Anwohnerin"}],
  "most_read": ["unrelated story 1", "unrelated story 2", "unrelated story 3"]
}
```

### social page
```json
{
  "feed_name": "#brennholtfeuer",
  "posts": [                                     // newest first
    {"name": "Anna K.", "handle": "anna_k", "joined": "March 2019", "followers": 412, "time": "14:28",
     "text": "…", "links": [], "reposts": 18, "likes": 64, "media": null}               // media: "photo: …" or null
  ]
}
```

## What makes a good scenario for the demo
Each storyline should exercise some of: a rumour that is later **confirmed**; a rumour that is **debunked** in the official
`not_true` list; **near-identical bot posts** (same text, same link, new accounts, many reposts) and a **scam link**; a risky
**reassurance** ("water is going down, I'm going home") that must be held back; a news article that **relays** an
unverified claim; an official **update that changes a fact** (a number, a deadline, a closed road reopening); escalation
or de-escalation, ending with an official all-clear or stand-down where it fits.
