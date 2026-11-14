"""Render the fictional scenario pages (official / news / social) from scenario.json content into standalone HTML.

Same visual language as the hand-written Halvik pages, so Context.dev sees realistic pages. Every page carries a
"FICTIONAL EXERCISE" banner. Deliberately NOT "contains no real emergency information": that phrase made the
extractor skip whole pages (see the comment on _STRONG_HAZARD in extraction.py).
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from .images import image_file

e = html.escape
SCENARIO_ROOT = Path(__file__).resolve().parent.parent / "scenario"


def _photo(spec: dict, kind: str, text: str) -> str | None:
    """Relative URL of the scenario's generated illustration for this text (served at /sites/<id>/images/), if made."""
    name = image_file(kind, text)
    return f"../images/{name}" if (SCENARIO_ROOT / spec["id"] / "images" / name).exists() else None

_BASE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="exercise-banner" role="note">{banner}</div>
{body}
</body>
</html>
"""

_BANNER_CSS = ".exercise-banner{background:#ffe14d;color:#000;font:600 14px/1.4 system-ui,sans-serif;text-align:center;padding:8px 16px}"

OFFICIAL_CSS = _BANNER_CSS + """
:root{--navy:#0b2e59;--ink:#1b1f24;--muted:#5b6470;--line:#d9dee5;--bg:#f4f6f9}
*{box-sizing:border-box}body{margin:0;font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--navy);color:#fff}.wrap{max-width:960px;margin:0 auto;padding:0 16px}
.brand{display:flex;align-items:center;gap:12px;padding:16px 0 8px;font-weight:700;font-size:20px}
.brand .mark{width:34px;height:34px;border-radius:6px;background:#fff;color:var(--navy);display:grid;place-items:center;font-size:18px}
nav ul{list-style:none;display:flex;flex-wrap:wrap;gap:20px;margin:0;padding:8px 0 14px;font-size:15px}
nav a{color:#cfe0ff;text-decoration:none}
main{background:#fff;margin:24px auto;padding:24px;border:1px solid var(--line);border-radius:8px}
h1{margin:0 0 6px;font-size:30px;line-height:1.2}.updated{color:var(--muted);margin:0 0 20px}
h2{font-size:21px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:6px}
.alert{border-left:6px solid;padding:12px 16px;border-radius:4px}.alert h2{border:0;margin:0 0 4px}
.alert.red{border-color:#b3141c;background:#fdeced}.alert.red h2{color:#b3141c}
.alert.orange{border-color:#d9640c;background:#fff1e6}.alert.orange h2{color:#b24f05}
.alert.yellow{border-color:#d4a600;background:#fff9db}.alert.yellow h2{color:#7a5d00}
.notice{border-left:6px solid #6b7280;background:#f1f2f4;padding:4px 16px;border-radius:4px}
li{margin:4px 0}.timeline li strong{display:inline-block;min-width:52px}
footer{color:var(--muted);font-size:14px;padding:16px 0 40px}
"""

NEWS_CSS = _BANNER_CSS + """
*{box-sizing:border-box}body{margin:0;font:18px/1.6 Georgia,"Times New Roman",serif;color:#161616;background:#fbfaf7}
.masthead{border-bottom:3px double #161616;text-align:center;padding:18px 16px 10px}
.masthead .name{font-size:40px;font-weight:700;letter-spacing:.5px}
.masthead nav{font:15px/1 system-ui,sans-serif;display:flex;flex-wrap:wrap;justify-content:center;gap:18px;margin-top:12px}
.masthead nav a{color:#161616;text-decoration:none}
.page{max-width:1080px;margin:0 auto;padding:24px 16px;display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:40px}
@media (max-width:820px){.page{grid-template-columns:1fr}}
.kicker{font:700 13px/1 system-ui,sans-serif;color:#c0151b;text-transform:uppercase;letter-spacing:1px}
h1{font-size:36px;line-height:1.15;margin:8px 0 12px}.standfirst{font-size:21px;color:#333;margin:0 0 14px}
.meta{font:14px/1.5 system-ui,sans-serif;color:#666;border-top:1px solid #ddd;border-bottom:1px solid #ddd;padding:8px 0;margin-bottom:20px}
blockquote{margin:16px 0;padding-left:16px;border-left:3px solid #c0151b;font-style:italic}
aside h3{font:700 14px/1 system-ui,sans-serif;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid #161616;padding-bottom:6px}
aside ol{padding-left:20px;font-size:16px}aside li{margin:10px 0}
.cookie{position:fixed;left:16px;right:16px;bottom:16px;max-width:640px;margin:0 auto;background:#161616;color:#fff;padding:14px 16px;border-radius:6px;font:14px/1.4 system-ui,sans-serif;display:flex;gap:12px;align-items:center;justify-content:space-between}
.cookie button{background:#fff;border:0;padding:8px 12px;border-radius:4px;font-weight:600}
footer{max-width:1080px;margin:0 auto;padding:16px 16px 120px;font:14px system-ui,sans-serif;color:#666;border-top:1px solid #ddd}
"""

SOCIAL_CSS = _BANNER_CSS + """
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#0f1419;background:#eef1f4}
.top{background:#fff;border-bottom:1px solid #dfe3e8}.top .in{max-width:620px;margin:0 auto;padding:12px 16px;display:flex;justify-content:space-between;align-items:baseline}
.logo{font-weight:800;font-size:20px;color:#1463d6}.feed{max-width:620px;margin:0 auto;padding:16px}
.feed h1{font-size:20px;margin:4px 0 14px}
.post{background:#fff;border:1px solid #dfe3e8;border-radius:12px;padding:14px 16px;margin-bottom:12px}
.who{display:flex;flex-wrap:wrap;gap:6px;align-items:baseline}.who .name{font-weight:700}.who .handle,.who .time{color:#536471}
.acct{color:#536471;font-size:13px;margin:2px 0 8px}.text{white-space:pre-wrap;margin:0 0 8px}.text a{color:#1463d6}
.media{background:#d6dde4;color:#3d4a57;border-radius:8px;padding:28px;text-align:center;font-size:13px;margin-bottom:8px}
.photo{width:100%;height:auto;display:block;border-radius:10px;margin-bottom:8px}
.stats{color:#536471;font-size:13px;display:flex;gap:18px}
"""


def town_of(spec: dict) -> str:
    return re.sub(r"\s*\(fictional\)\s*$", "", spec["area"]["label"], flags=re.I)


def _banner(spec: dict, org: str) -> str:
    return (f"FICTIONAL EXERCISE: {e(town_of(spec))} is not a real place and {e(org)} is not a real organisation. "
            "Made for a hackathon demo.")


def _page(spec: dict, org: str, title: str, css: str, body: str) -> str:
    return _BASE.format(lang=e(spec.get("language", "en")), title=e(title), css=css, banner=_banner(spec, org), body=body)


def render_official(spec: dict, page: dict) -> str:
    org = spec["sources"]["official"]["name"]
    parts = [f'<p class="updated">{e(page.get("updated", ""))}</p>']
    alert = page.get("alert")
    if alert:
        level = alert.get("level") if alert.get("level") in ("yellow", "orange", "red") else "yellow"
        parts.append(f'<section class="alert {level}"><h2>{e(alert.get("heading", ""))}</h2><p>{e(alert.get("text", ""))}</p></section>')
    for sec in page.get("sections", []):
        parts.append(f'<h2>{e(sec.get("heading", ""))}</h2>')
        parts += [f"<p>{e(p)}</p>" for p in sec.get("paragraphs", [])]
        if sec.get("items"):
            parts.append("<ul>" + "".join(f"<li>{e(i)}</li>" for i in sec["items"]) + "</ul>")
    if page.get("not_true"):
        parts.append('<h2>Not true</h2><div class="notice"><ul>'
                     + "".join(f"<li>{e(i)}</li>" for i in page["not_true"]) + "</ul></div>")
    if page.get("timeline"):
        parts.append('<h2>Earlier updates</h2><ul class="timeline">'
                     + "".join(f"<li><strong>{e(t.get('time', ''))}</strong> {e(t.get('text', ''))}</li>" for t in page["timeline"])
                     + "</ul>")
    body = f"""<header><div class="wrap"><div class="brand"><span class="mark">{e(org[:1])}</span> {e(org)}</div>
<nav aria-label="Main"><ul><li><a href="#">Home</a></li><li><a href="#">Situation</a></li><li><a href="#">Preparedness</a></li><li><a href="#">Contact</a></li></ul></nav></div></header>
<div class="wrap"><main><h1>{e(page.get("title", ""))}</h1>
{chr(10).join(parts)}
</main><footer>© 2026 {e(org)} (fictional)</footer></div>"""
    return _page(spec, org, f'{page.get("title", "")} – {org}', OFFICIAL_CSS, body)


def render_news(spec: dict, page: dict) -> str:
    outlet = spec["sources"]["news"]["name"]
    paras = "".join(f"<p>{e(p)}</p>" for p in page.get("paragraphs", []))
    quotes = "".join(f'<blockquote>"{e(q.get("text", ""))}" — {e(q.get("by", ""))}</blockquote>' for q in page.get("quotes", []))
    most = "".join(f'<li><a href="#">{e(m)}</a></li>' for m in page.get("most_read", []))
    updated = f' · Updated {e(page["updated"])}' if page.get("updated") else ""
    src = _photo(spec, "news", page.get("headline", ""))
    lead = (f'<figure class="lead"><img src="{src}" alt="{e(page.get("headline", ""))}" width="1200" height="675">'
            f"<figcaption>AI illustration · fictional</figcaption></figure>") if src else ""
    body = f"""<header class="masthead"><div class="name">{e(outlet)}</div>
<nav aria-label="Sections"><a href="#">News</a><a href="#">Local</a><a href="#">Sport</a><a href="#">Culture</a><a href="#">Opinion</a></nav></header>
<div class="page"><article>
<div class="kicker">{e(page.get("kicker", ""))}</div>
<h1>{e(page.get("headline", ""))}</h1>
<p class="standfirst">{e(page.get("standfirst", ""))}</p>
<div class="meta">{e(page.get("byline", ""))} · {e(page.get("published", ""))}{updated}</div>
{paras}{quotes}
</article>
<aside><h3>Most read</h3><ol>{most}</ol></aside></div>
<footer>© 2026 {e(outlet)} (fictional)</footer>
<div class="cookie" role="dialog" aria-label="Cookie consent">We use cookies to improve your reading experience. <button type="button">Accept all</button></div>"""
    return _page(spec, outlet, f'{page.get("headline", "")} | {outlet}', NEWS_CSS, body)


def _linkify(text: str, links: list[str]) -> str:
    out = e(text)
    for url in links:
        shown = re.sub(r"^https?://", "", url)
        if e(shown) in out:
            out = out.replace(e(shown), f'<a href="{e(url)}">{e(shown)}</a>', 1)
        elif e(url) in out:
            out = out.replace(e(url), f'<a href="{e(url)}">{e(shown)}</a>', 1)
        else:
            out += f' <a href="{e(url)}">{e(shown)}</a>'
    return out


def render_social(spec: dict, page: dict) -> str:
    board = spec["sources"]["social"]["name"]
    posts = []
    for p in page.get("posts", []):
        joined = str(p.get("joined", ""))
        joined = joined if joined.lower().startswith("joined") else f"Joined {joined}"
        photo = _photo(spec, "photo", str(p["media"])) if p.get("media") else None
        media = (f'<img class="photo" src="{photo}" alt="{e(str(p["media"]))}" width="1200" height="675">' if photo
                 else f'<div class="media">[{e(str(p["media"]))}]</div>' if p.get("media") else "")
        posts.append(f"""<article class="post">
<div class="who"><span class="name">{e(p.get("name", ""))}</span><span class="handle">@{e(str(p.get("handle", "")).lstrip("@"))}</span><span class="time">· {e(str(p.get("time", "")))}</span></div>
<div class="acct">{e(joined)} · {e(str(p.get("followers", 0)))} followers</div>
<p class="text">{_linkify(p.get("text", ""), p.get("links") or [])}</p>{media}
<div class="stats"><span>↻ {e(str(p.get("reposts", 0)))} reposts</span><span>♥ {e(str(p.get("likes", 0)))}</span></div>
</article>""")
    body = f"""<div class="top"><div class="in"><span class="logo">{e(board)}</span><span>Public posts</span></div></div>
<main class="feed"><h1>{e(page.get("feed_name", ""))}</h1>
{chr(10).join(posts)}
</main>"""
    return _page(spec, board, f'{page.get("feed_name", "")} · {board}', SOCIAL_CSS, body)


RENDERERS = {"official": render_official, "news": render_news, "social": render_social}


def render(spec: dict, key: str, page: dict) -> str:
    return RENDERERS[key](spec, page)
