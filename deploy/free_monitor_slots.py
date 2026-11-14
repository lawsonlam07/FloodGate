"""List (default) or delete FloodGate's Context.dev monitors, to free the plan's monitor slots (10 on the free plan).

Run this on the laptop before the server takes over, so the two don't fight over slots:
  uv run deploy/free_monitor_slots.py            # list only
  uv run deploy/free_monitor_slots.py --delete   # delete monitors whose name starts with "FloodGate"

Only monitors named "FloodGate ..." are touched. A running local backend recreates its monitors on the next
change, so stop it first (and delete data/state.json's record of them by restarting with a fresh data/ if you like).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.context_client import load_dotenv  # noqa: E402

API = "https://api.context.dev/v1"


def main() -> int:
    load_dotenv(ROOT / ".env")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {os.environ['CONTEXT_DEV_API_KEY']}"
    monitors, cursor = [], None
    while True:
        r = s.get(f"{API}/monitors", params={"limit": 50, **({"cursor": cursor} if cursor else {})}, timeout=30)
        r.raise_for_status()
        body = r.json()
        monitors += body.get("data") or []
        cursor = body.get("next_cursor")
        if not body.get("has_more") or not cursor:
            break
    ours = [m for m in monitors if (m.get("name") or "").lower().startswith("floodgate")]
    print(f"{len(monitors)} monitors on the account, {len(ours)} named FloodGate")
    for m in ours:
        print(f"  {m['id']}  {m.get('name', '')[:60]}  {(m.get('target') or {}).get('url', '')[:70]}")
    if "--delete" not in sys.argv:
        print("(list only; add --delete to remove them)")
        return 0
    for m in ours:
        r = s.delete(f"{API}/monitors/{m['id']}", timeout=30)
        print(f"  deleted {m['id']}" if r.ok or r.status_code == 404 else f"  could not delete {m['id']}: {r.status_code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
