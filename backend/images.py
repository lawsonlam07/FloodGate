"""Photos from the sources: images found on a captured page, shown on the incident page as unverified context.

Context.dev keeps images in place in the page markdown when asked (`markdownParams.includeImages`), as
`![alt](url)`. We keep the photos (not icons, logos or SVGs) with the source they came from, plus the text just before
each one, so a photo in a social post can be tied to the claims of that post. A photo is never evidence for a verdict:
the board labels it "posted by <source>, unverified".

The fictional scenarios' photos are AI illustrations made once by scenario/generate_images.py; `image_file` gives
their stable names so the page renderer and the generator agree without touching the scenario files.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

from .extraction import normalize

IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_NOT_A_PHOTO = re.compile(r"(?i)(\.svg|\.ico)(\?|$)|/(icons?|logos?|sprites?|emoji|badges?)/|(^|[/_-])(logo|icon|favicon|avatar)[._-]")
MAX_PER_PAGE = 12


def image_file(kind: str, text: str) -> str:
    """Stable file name of a generated illustration: kind 'news' (lead photo, from the headline) or 'photo' (a post)."""
    return f"{kind}-{hashlib.sha1(text.strip().encode()).hexdigest()[:10]}.jpg"


def is_photo(url: str) -> bool:
    return url.startswith(("http://", "https://", "/")) and not _NOT_A_PHOTO.search(urlparse(url).path or url)


def extract(markdown: str, page_url: str, site_base: str = "") -> list[dict]:
    """Photos on a captured page: [{url, local, alt, context}]. `local` is our own /sites/... path for images of the
    fictional sites (whose public address may be a rotating tunnel); `context` is the normalized text before it."""
    out, seen, prev_end = [], set(), 0
    for m in IMG.finditer(markdown or ""):
        alt, url = m[1].strip(), urljoin(page_url, m[2])
        if not is_photo(url) or url in seen:
            continue
        seen.add(url)
        local = None
        if site_base and url.startswith(site_base.rstrip("/") + "/"):
            local = "/sites/" + url[len(site_base.rstrip("/")) + 1:]
        # the post or paragraph the photo belongs to: the last two lines of text just before it
        lines = [ln for ln in markdown[max(prev_end, m.start() - 1500):m.start()].splitlines() if ln.strip() and not IMG.fullmatch(ln.strip())]
        context = normalize(" ".join(lines[-2:]))[-600:]
        out.append({"url": url, "local": local, "alt": alt, "context": context})
        prev_end = m.end()
        if len(out) >= MAX_PER_PAGE:
            break
    return out


def items_for(image: dict, source_id: str, items) -> list[str]:
    """Reports whose quote from this source sits in the text right before the photo (a post and its photo)."""
    ctx = image.get("context") or ""
    if not ctx:
        return []
    ids = []
    for it in items:
        for m in it.group.members:
            q = normalize(m.get("evidence_quote") or "")
            if m.get("source_id") == source_id and len(q) >= 12 and q in ctx:
                ids.append(it.id)
                break
    return ids
