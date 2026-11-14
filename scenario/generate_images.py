"""Make the photos of the fictional scenarios with Gemini's image model ("Nano Banana"). Run once; the output is committed.

  uv run scenario/generate_images.py                 # every scenario (skips images that already exist)
  uv run scenario/generate_images.py halvik ...      # only these scenarios

One lead photo per news article version (from its headline and standfirst) and one per social post that has a
"media": "photo: ..." description; Halvik's hand-written pages are read the same way. Every image is an invented scene,
stamped "AI illustration · fictional", ~1200 px JPEG, saved as scenario/<id>/images/<backend.images.image_file name>.
The page renderer shows them on the fictional sites; Context.dev captures them with the page (see backend/images.py).
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.context_client import load_dotenv  # noqa: E402
from backend.images import image_file  # noqa: E402

MODEL = "gemini-3.1-flash-image"     # "Nano Banana 2": most photographic in a 4-model test, kept "no text", ~12 s/image
STAMP = "AI illustration · fictional"
RULES = ("Rules: an invented scene in an invented small town; no text, captions, signs with writing, logos or brands; "
         "no real landmarks; no recognisable people (people only small, at a distance or from behind); "
         "nothing graphic, no injured people.")


def news_prompt(scene: str, region: str, hazard: str) -> str:
    return (f"A realistic documentary press photograph used as the lead image of a local news article. "
            f"The article: {scene} Setting: a small town, {region}. Event type: {hazard.replace('_', ' ')}. "
            f"Natural light, photojournalism, 35mm, wide shot, 16:9. {RULES}")


def post_prompt(description: str, text: str, region: str, hazard: str) -> str:
    return (f"A realistic smartphone photo posted on social media by a local resident, slightly imperfect framing. "
            f"The photo shows: {description.removeprefix('photo:').strip()}. The post said: \"{text[:220]}\" "
            f"Setting: a small town, {region}. Event type: {hazard.replace('_', ' ')}. 16:9. {RULES}")


def jobs_for(sid: str) -> list[tuple[Path, str]]:
    out_dir = ROOT / "scenario" / sid / "images"
    jobs: dict[str, tuple[Path, str]] = {}
    if sid == "halvik":
        region, hazard = "on the Bothnian Sea coast of Finland", "storm surge"
        for f in sorted((ROOT / "scenario" / "halvik").glob("herald-*.html")):
            t = f.read_text()
            h1 = html.unescape(re.search(r"<h1>(.*?)</h1>", t, re.S)[1].strip())
            sf = re.search(r'class="standfirst">(.*?)</', t, re.S)
            name = image_file("news", h1)
            jobs[name] = (out_dir / name, news_prompt(f"{h1}. {html.unescape(sf[1].strip()) if sf else ''}", region,
                                                      "local news" if "festival" in h1.lower() else hazard))
        for f in sorted((ROOT / "scenario" / "halvik").glob("board-*.html")):
            for post in re.findall(r'<article class="post">(.*?)</article>', f.read_text(), re.S):
                media = re.search(r'<div class="media">\[(.*?)\]</div>', post)
                if not media:
                    continue
                desc = html.unescape(media[1])
                text = html.unescape(re.sub(r"<[^>]+>", "", (re.search(r'<p class="text">(.*?)</p>', post, re.S) or ["", ""])[1]))
                name = image_file("photo", desc)
                jobs[name] = (out_dir / name, post_prompt(desc, text, region, hazard))
        return list(jobs.values())
    spec = json.loads((ROOT / "scenario" / sid / "scenario.json").read_text())
    region, hazard = spec.get("region", "Europe"), spec.get("hazard", "emergency")
    for step in spec["steps"]:
        news = step["pages"].get("news")
        if news and news.get("headline"):
            name = image_file("news", news["headline"])
            jobs[name] = (out_dir / name, news_prompt(f"{news['headline']}. {news.get('standfirst', '')}", region, hazard))
        for post in (step["pages"].get("social") or {}).get("posts", []):
            if post.get("media"):
                name = image_file("photo", str(post["media"]))
                jobs[name] = (out_dir / name, post_prompt(str(post["media"]), post.get("text", ""), region, hazard))
    return list(jobs.values())


def stamp_and_save(raw: bytes, dest: Path) -> None:
    """~1200 px JPEG with the label bottom-right (ffmpeg drawtext; the label goes in a file to avoid escaping)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src, label = Path(tmp) / "in.img", Path(tmp) / "label.txt"
        src.write_bytes(raw)
        label.write_text(STAMP)
        vf = (f"scale=1200:-2,drawtext=textfile={label}:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.55:"
              f"boxborderw=8:x=w-tw-18:y=h-th-18")
        out = dest.with_suffix(".tmp.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", vf, "-q:v", "5", str(out)], check=True)
        out.replace(dest)


def generate(client, prompt: str) -> bytes:
    from google.genai import types
    last = None
    for attempt in range(3):
        try:
            r = client.models.generate_content(model=MODEL, contents=prompt, config=types.GenerateContentConfig(
                response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="16:9")))
            parts = [p for p in (r.candidates[0].content.parts if r.candidates else []) if p.inline_data]
            if parts:
                return parts[0].inline_data.data
            last = f"no image ({r.candidates[0].finish_reason if r.candidates else 'no candidates'})"
        except Exception as exc:     # rate limits / timeouts: back off and retry
            last = f"{type(exc).__name__}: {exc}"[:200]
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(last)


def main() -> int:
    load_dotenv(ROOT / ".env")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=120_000))
    wanted = sys.argv[1:] or ["halvik"] + sorted(p.parent.name for p in (ROOT / "scenario").glob("*/scenario.json"))
    todo = [(sid, dest, prompt) for sid in wanted for dest, prompt in jobs_for(sid) if not dest.exists()]
    print(f"{len(todo)} images to make ({MODEL})")

    def one(job):
        sid, dest, prompt = job
        t = time.monotonic()
        try:
            stamp_and_save(generate(client, prompt), dest)
            return f"  {sid}/{dest.name}  {dest.stat().st_size // 1024} KB  {time.monotonic() - t:.0f}s"
        except Exception as exc:
            return f"  FAILED {sid}/{dest.name}: {exc}"

    with ThreadPoolExecutor(4) as pool:
        for line in pool.map(one, todo):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
