"""Build the frontend's static assets so every page loads from our own server (no CDNs at page load).

  - css/tailwind.css     Tailwind classes used in *.html and js/*.js (standalone Tailwind CLI, no Node needed)
  - vendor/leaflet/      Leaflet 1.9.4 (map)
  - vendor/fonts/        Inter + JetBrains Mono, and the Material Symbols icons the code actually uses

Usage:  uv run frontend/build.py        (run.sh and deploy.sh call it; fast when nothing changed)

Downloads happen once and are cached in .cache/ (the Tailwind binary, verified against the release checksums) and
frontend/vendor/ (committed). The icon subset is recomputed from the code on every build: use a new icon anywhere and
the next build fetches it. Pages link vendor/fonts/fonts.css, vendor/leaflet/* and css/tailwind.css.
"""

from __future__ import annotations

import hashlib
import platform
import re
import subprocess
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = ROOT / ".cache"
VENDOR = HERE / "vendor"

TAILWIND_VERSION = "v3.4.17"
LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4"
LEAFLET_FILES = ["leaflet.min.js", "leaflet.min.css", "images/layers.png", "images/layers-2x.png",
                 "images/marker-icon.png", "images/marker-icon-2x.png", "images/marker-shadow.png"]
TEXT_FONTS = "https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700;800&display=swap"
ICON_FONT = ("https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:"
             "opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&icon_names={names}&display=block")
ICON_LIST = ("https://raw.githubusercontent.com/google/material-design-icons/master/variablefont/"
             "MaterialSymbolsOutlined%5BFILL%2CGRAD%2Copsz%2Cwght%5D.codepoints")
SUBSETS = {"latin", "latin-ext", "greek", "greek-ext"}      # our scenario languages (cs, pl, el, ...)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}


def fetch(url: str) -> bytes:
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return r.content


# ---- Tailwind ---------------------------------------------------------------------------------------------------

def tailwind_binary() -> Path:
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
    system = {"linux": "linux", "darwin": "macos"}[sys.platform if sys.platform != "linux2" else "linux"]
    name = f"tailwindcss-{system}-{arch}"
    path = CACHE / f"{name}-{TAILWIND_VERSION}"
    if path.exists():
        return path
    base = f"https://github.com/tailwindlabs/tailwindcss/releases/download/{TAILWIND_VERSION}"
    print(f"downloading Tailwind {TAILWIND_VERSION} ({name})")
    data = fetch(f"{base}/{name}")
    sums = fetch(f"{base}/sha256sums.txt").decode()
    want = next((line.split()[0] for line in sums.splitlines() if line.strip().endswith(name)), None)
    if want != hashlib.sha256(data).hexdigest():
        raise SystemExit(f"checksum mismatch for {name}: refusing to run it")
    CACHE.mkdir(exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def build_tailwind() -> None:
    out = HERE / "css" / "tailwind.css"
    subprocess.run([str(tailwind_binary()), "-c", str(HERE / "tailwind.config.js"), "-i", str(HERE / "css" / "tailwind.src.css"),
                    "-o", str(out), "--minify"], cwd=HERE, check=True, capture_output=True)
    print(f"css/tailwind.css  {out.stat().st_size // 1024} KB")


# ---- Leaflet ----------------------------------------------------------------------------------------------------

def vendor_leaflet() -> None:
    for f in LEAFLET_FILES:
        dest = VENDOR / "leaflet" / f
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(fetch(f"{LEAFLET}/{f}"))
            print(f"vendor/leaflet/{f}")


# ---- fonts ------------------------------------------------------------------------------------------------------

def used_icons() -> list[str]:
    known_file = CACHE / "material-symbols.codepoints"
    if not known_file.exists():
        CACHE.mkdir(exist_ok=True)
        known_file.write_bytes(fetch(ICON_LIST))
    known = {line.split()[0] for line in known_file.read_text().splitlines() if line.strip()}
    words = set()
    for p in list(HERE.glob("*.html")) + list((HERE / "js").glob("*.js")):
        text = p.read_text()
        words |= set(re.findall(r"[\"'`]([a-z][a-z0-9_]*)[\"'`]", text))       # icon("name"), { icon: "name" }, ...
        words |= set(re.findall(r">([a-z][a-z0-9_]*)<", text))                   # <span class="material-...">name</span>
    return sorted(words & known)


def localise(css: str, folder: Path, keep_subsets: set[str] | None) -> str:
    """Download the font files a Google Fonts stylesheet points at and rewrite it to use the local copies."""
    out, blocks = [], re.split(r"(?=/\* [a-z-]+ \*/)", css) if keep_subsets else [css]
    for block in blocks:
        m = re.match(r"/\* ([a-z-]+) \*/", block)
        if keep_subsets and m and m[1] not in keep_subsets:
            continue
        for url in re.findall(r"url\((https://[^)]+)\)", block):
            name = hashlib.sha1(url.encode()).hexdigest()[:16] + (Path(url).suffix or ".woff2")   # icon URLs have none
            if not (folder / name).exists():
                (folder / name).write_bytes(fetch(url))
            block = block.replace(url, name)
        out.append(block)
    return "".join(out)


def vendor_fonts() -> None:
    folder = VENDOR / "fonts"
    folder.mkdir(parents=True, exist_ok=True)
    icons = used_icons()
    stamp = folder / "icons.txt"
    target = folder / "fonts.css"
    wanted = [TEXT_FONTS] + icons                            # refetch when the typeface or the icon list changes
    if not (target.exists() and stamp.exists() and stamp.read_text().split() == wanted):
        text = localise(fetch(TEXT_FONTS).decode(), folder, SUBSETS)
        icon_css = localise(fetch(ICON_FONT.format(names=",".join(icons))).decode(), folder, None)
        target.write_text("/* Generated by frontend/build.py: local copies of Google Fonts (no request to Google at "
                          "page load). */\n" + text + "\n" + icon_css)
        stamp.write_text("\n".join(wanted) + "\n")
        print(f"vendor/fonts/fonts.css  ({len(icons)} icons)")
    used = set(re.findall(r"url\(([^)]+)\)", target.read_text()))
    for f in folder.iterdir():                               # drop font files no longer referenced
        if f.name not in ("fonts.css", "icons.txt") and f.name not in used:
            f.unlink()


def main() -> int:
    vendor_leaflet()
    vendor_fonts()
    build_tailwind()
    return 0


if __name__ == "__main__":
    sys.exit(main())
