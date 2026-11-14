#!/usr/bin/env bash
# Serve the fictional Halvik sites and expose them on a public HTTPS URL so Context.dev can scrape them.
# Uses localhost.run over plain ssh (no install, no account). The URL changes each time you restart.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"

# Static server that labels HTML as UTF-8 (plain `python -m http.server` sends no charset).
python3 - "$PORT" >/dev/null 2>&1 <<'PY' &
import functools, sys, http.server as hs
H = hs.SimpleHTTPRequestHandler
H.extensions_map = {**H.extensions_map, ".html": "text/html; charset=utf-8"}
hs.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), functools.partial(H, directory="fake_site")).serve_forever()
PY
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

echo "Local:  http://127.0.0.1:$PORT/"
echo "Opening public tunnel... copy the https://<id>.lhr.life URL it prints, then in another terminal:"
echo "  ./run.sh    (the backend picks the URL up automatically)"
echo
# localhost.run rotates the free URL now and then; record the latest one where the backend reads it.
mkdir -p data
# The free tunnel drops now and then ("tunnel activity timeout"): reconnect forever; the site server stays up.
while true; do
ssh -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -R 80:localhost:"$PORT" nokey@localhost.run 2>&1 |
while IFS= read -r line; do
  echo "$line"
  url=$(grep -oE 'https://[a-z0-9]+\.(app\.)?lhr\.life' <<<"$line" | head -1 || true)
  [ -n "$url" ] && echo "$url" > data/site_public_url && echo ">>> public URL is now $url (saved to data/site_public_url)"
done || true
echo ">>> tunnel dropped; reconnecting in 3 s (Ctrl-C to stop)"
sleep 3
done
