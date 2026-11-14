"""Build backend/meteoalarm_regions.json: MeteoAlarm warning regions (EMMA_ID) -> name, centre and a light outline.

MeteoAlarm's Atom feeds name each warning's regions by EMMA_ID code but carry no shapes. MeteoAlarm's geocodes file
(about 2,000 regions, 31 MB) ships in the MIT-licensed `meteoalarm` Python package; this script downloads that wheel
from PyPI (checksum-pinned), reads the file without installing or running the package, and keeps a simplified outline
per region (largest 3 parts, at most 24 points each), so the result is small enough to commit.

Usage:  uv run deploy/build_meteoalarm_regions.py      (only needed when MeteoAlarm changes its regions)
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import zipfile
from pathlib import Path

import requests

WHEEL = ("https://files.pythonhosted.org/packages/e5/69/267abfa8033212f7efa02fd0c3ea6f276fb9c3515ed66977cc0080eec4b2/"
         "meteoalarm-0.3.0-py3-none-any.whl")
SHA256 = "8f02c764bb86cdd265e5ee92e0cc04be3ea7eeb621be83fbde6102cbd815f8c9"
OUT = Path(__file__).resolve().parent.parent / "backend" / "meteoalarm_regions.json"
MAX_POINTS, MAX_PARTS = 24, 3


def ring_area(ring: list[list[float]]) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]))) / 2


def simplify(ring: list[list[float]]) -> list[list[float]]:
    """GeoJSON [lon, lat] ring -> our [lat, lon], decimated (fine for display at country zoom)."""
    pts = [[round(lat, 3), round(lon, 3)] for lon, lat in ring]
    if len(pts) > MAX_POINTS:
        step = math.ceil(len(pts) / MAX_POINTS)
        pts = pts[::step]
    return pts


def main() -> None:
    data = requests.get(WHEEL, timeout=120).content
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit("checksum mismatch: refusing to use the download")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        geo = json.loads(z.read("meteoalarm/assets/geocodes.json"))
    out = {}
    for f in geo["features"]:
        p, g = f["properties"], f["geometry"]
        parts = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        outers = sorted((part[0] for part in parts), key=ring_area, reverse=True)[:MAX_PARTS]
        polys = [simplify(r) for r in outers]
        main_ring = outers[0]
        lon = sum(x for x, _ in main_ring) / len(main_ring)
        lat = sum(y for _, y in main_ring) / len(main_ring)
        out[p["code"]] = {"name": p["name"], "country": p["country"], "lat": round(lat, 3), "lon": round(lon, 3),
                          "polygons": polys}
    OUT.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    print(f"{OUT.relative_to(OUT.parent.parent)}: {len(out)} regions, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
