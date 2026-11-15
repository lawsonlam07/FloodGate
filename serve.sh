#!/usr/bin/env bash
# Serve the fictional sites and expose them on a public HTTPS URL so Context.dev can scrape them.
# Tunnel: a Cloudflare quick tunnel when `cloudflared` is installed (sturdier under load; no account needed),
# otherwise localhost.run over plain ssh (no install). The URL changes each time the tunnel restarts; the latest
# one is saved to data/site_public_url, where the backend reads it. Force one with TUNNEL=cloudflared|localhost.run.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
TUNNEL="${TUNNEL:-$(command -v cloudflared >/dev/null 2>&1 && echo cloudflared || echo localhost.run)}"

mkdir -p data/sites
# Static server that labels HTML as UTF-8 (plain `python -m http.server` sends no charset).
python3 - "$PORT" >/dev/null 2>&1 <<'PY' &
import functools, sys, http.server as hs
H = hs.SimpleHTTPRequestHandler
H.extensions_map = {**H.extensions_map, ".html": "text/html; charset=utf-8"}
hs.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), functools.partial(H, directory="data/sites")).serve_forever()
PY
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

echo "Local:  http://127.0.0.1:$PORT/"
echo "Opening public tunnel via $TUNNEL... then in another terminal:"
echo "  ./run.sh    (the backend picks the URL up automatically)"
[ "$TUNNEL" = localhost.run ] && echo "  (tip: 'sudo pacman -S cloudflared' gives a sturdier tunnel; serve.sh uses it automatically)"
echo
mkdir -p data

tunnel() {
  if [ "$TUNNEL" = cloudflared ]; then
    cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" 2>&1
  else
    ssh -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
        -R 80:localhost:"$PORT" nokey@localhost.run 2>&1
  fi
}

# Free tunnels drop now and then: reconnect forever; the site server stays up.
while true; do
  tunnel | while IFS= read -r line; do
    echo "$line"
    url=$(grep -oE 'https://[a-z0-9-]+\.(trycloudflare\.com|(app\.)?lhr\.life)' <<<"$line" | grep -v '^https://api\.' | head -1 || true)
    [ -n "$url" ] && echo "$url" > data/site_public_url && echo ">>> public URL is now $url (saved to data/site_public_url)"
  done || true
  echo ">>> tunnel dropped; reconnecting in 3 s (Ctrl-C to stop)"
  sleep 3
done
