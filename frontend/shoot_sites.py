"""Screenshot every fictional site for the demo landing page (demo.html).

Renders each scenario's pages at their last step straight from scenario/ (no server, no scenario has to be running),
opens them in headless Chrome and saves a small WebP per page plus a manifest:

  frontend/img/sites/<scenario>-<page>.webp
  frontend/img/sites/sites.json      [{scenario, region, language, scale, kind, name, href, img, w, h}, ...]

Usage:  uv run frontend/shoot_sites.py        (needs Chrome or Chromium; CHROME=/path/to/chrome to pick one)

Re-run it after changing a scenario's pages. Official and news pages are shot as a desktop window, social feeds as a
narrow one, so the wall on the landing page mixes landscape and portrait tiles.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from websockets.sync.client import connect

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from backend.scenario import load_defs  # noqa: E402

OUT = HERE / "img" / "sites"
OUT_W = 560                                  # tile images are shown ~280 CSS px wide: 2x for sharp screens
VIEWPORT = {"official": (960, 640), "news": (960, 640), "social": (600, 780)}
HIDE = "document.querySelectorAll('.cookie,[aria-label=\"Cookie consent\"]').forEach((e) => e.remove());"
SETTLE = ("document.fonts.ready.then(() => Promise.all([...document.images].map((i) => i.complete ? 0 :"
          " new Promise((r) => { i.onload = i.onerror = r; }))))")


def render_pages(tmp: Path) -> list[dict]:
    """Every scenario page at its last step, written under tmp/ exactly as data/sites/ would hold it."""
    pages = []
    for sid, d in load_defs().items():
        versions = d.versions_at(len(d.steps) - 1)
        images = ROOT / "scenario" / sid / "images"
        if images.is_dir():
            shutil.copytree(images, tmp / sid / "images", dirs_exist_ok=True)
        for p in d.pages:
            target = tmp / p["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(d.content(p["id"], versions[p["id"]]))
            href = os.path.dirname(p["target"]) + "/" if p["target"].endswith("index.html") else p["target"]
            pages.append({"scenario": d.meta.get("name", sid), "region": d.meta.get("region", ""),
                          "language": d.meta.get("language", ""), "scale": d.meta.get("scale", ""),
                          "kind": p["kind"], "name": p["name"], "href": f"sites/{href}",
                          "file": target, "slug": f"{sid}-{p['id']}"})
    return pages


class Chrome:
    """Headless Chrome driven over the DevTools protocol (JPEG/WebP screenshots need it; the CLI only writes PNG)."""

    def __enter__(self) -> "Chrome":
        exe = os.environ.get("CHROME") or next(
            (shutil.which(n) for n in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome")
             if shutil.which(n)), None)
        if not exe:
            sys.exit("Chrome or Chromium not found (set CHROME=/path/to/chrome)")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.profile = tempfile.mkdtemp(prefix="fg-shoot-")
        self.proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", "--remote-allow-origins=*",
                                      f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
                                      "--hide-scrollbars", "--disable-gpu", "--allow-file-access-from-files", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ws = None
        for _ in range(100):
            try:
                tabs = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=1).json()
                url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except (requests.RequestException, StopIteration):
                time.sleep(0.1)
        else:
            self.__exit__()
            sys.exit("Chrome did not start")
        self._conn = connect(url, max_size=None)
        self.ws = self._conn.__enter__()
        self.n = 0
        self.call("Page.enable")
        return self

    def __exit__(self, *exc) -> None:
        if self.ws is not None:
            self._conn.__exit__(None, None, None)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        shutil.rmtree(self.profile, ignore_errors=True)

    def call(self, method: str, wait_event: str | None = None, **params) -> dict:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        result, seen = None, wait_event is None
        while result is None or not seen:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                result = msg.get("result", {})
            elif msg.get("method") == wait_event:
                seen = True
        return result

    def shoot(self, url: str, w: int, h: int) -> bytes:
        self.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
        self.call("Page.navigate", wait_event="Page.loadEventFired", url=url)
        self.call("Runtime.evaluate", expression=HIDE + SETTLE, awaitPromise=True)
        shot = self.call("Page.captureScreenshot", format="webp", quality=80,
                         clip={"x": 0, "y": 0, "width": w, "height": h, "scale": OUT_W / w})
        return base64.b64decode(shot["data"])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    with tempfile.TemporaryDirectory(prefix="fg-sites-") as tmp, Chrome() as chrome:
        for p in render_pages(Path(tmp)):
            w, h = VIEWPORT.get(p["kind"], VIEWPORT["news"])
            img = OUT / f"{p.pop('slug')}.webp"
            img.write_bytes(chrome.shoot(p.pop("file").as_uri(), w, h))
            manifest.append({**p, "img": str(img.relative_to(HERE)), "w": OUT_W, "h": round(h * OUT_W / w)})
            print(f"  {img.name:48} {img.stat().st_size // 1024:4} KB")
    (OUT / "sites.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(manifest)} sites -> {OUT.relative_to(ROOT)}/ ({sum(f.stat().st_size for f in OUT.glob('*.webp')) // 1024} KB)")


if __name__ == "__main__":
    main()
