#!/usr/bin/env bash
# Deploy FloodGate to a VPS with Docker (e.g. Hostinger KVM 2) and (re)start it.
#
#   First time:  ./deploy.sh root@<vps-ip> <domain>     e.g. ./deploy.sh root@203.0.113.7 srv123456.hstgr.cloud
#   Updates:     ./deploy.sh root@<vps-ip>
#   SSH key:     ~/.ssh/hostinger-floodgate if it exists, or set SSH_KEY=/path/to/key
#
# Copies the code (not .env, data/ or .venv) to /opt/floodgate and runs `docker compose up -d --build` there.
# The first time it also writes the server's .env: your API keys from the local .env plus the hosted settings
# (DOMAIN, a generated ADMIN_TOKEN, a daily credit cap). The server's .env is never overwritten afterwards.
set -euo pipefail
cd "$(dirname "$0")"
HOST="${1:?usage: ./deploy.sh root@<vps-ip> [domain]}"
DOMAIN="${2:-}"
DIR=/opt/floodgate
KEY="${SSH_KEY:-$HOME/.ssh/hostinger-floodgate}"
SSH=(ssh -o BatchMode=yes)
[ -f "$KEY" ] && SSH+=(-o IdentitiesOnly=yes -i "$KEY")

echo ">>> building frontend assets (Tailwind CSS, Leaflet, fonts)"
uv run frontend/build.py
echo ">>> copying code to $HOST:$DIR"
tar czf - --exclude=.venv --exclude=.git --exclude=data --exclude=out --exclude=.env --exclude='*.zip' \
    --exclude=__pycache__ --exclude='fake_site/*' --exclude=serve.sh.new \
    pyproject.toml uv.lock Dockerfile .dockerignore compose.yaml deploy backend frontend scenario fake_site README.md docs |
  "${SSH[@]}" "$HOST" "mkdir -p $DIR && cd $DIR && rm -rf backend frontend scenario deploy docs && tar xzf - && mkdir -p fake_site && touch fake_site/.gitkeep"

if ! "${SSH[@]}" "$HOST" "test -f $DIR/.env"; then
  [ -n "$DOMAIN" ] || { echo "First deploy: pass the domain, e.g. ./deploy.sh $HOST srv123456.hstgr.cloud"; exit 1; }
  TOKEN=$(openssl rand -hex 16)
  echo ">>> writing $DIR/.env on the server (API keys from your local .env + hosted settings)"
  {
    grep -E '^(CONTEXT_DEV_API_KEY|GEMINI_API_KEY|ANTHROPIC_API_KEY|ANTHROPIC_BASE_URL|ELEVENLABS_API_KEY|TIGER_DATABASE_URL|GEMINI_MODEL|LLM_PROVIDER)=' .env || true
    echo "DOMAIN=$DOMAIN"
    echo "ADMIN_TOKEN=$TOKEN"
    echo "# Context.dev credits: per process (high) and per day (auto-pause; raise it for the judging window)"
    echo "CREDIT_BUDGET=100000"
    echo "CREDIT_BUDGET_DAILY=3000"
    echo "ELEVENLABS_CHAR_BUDGET_DAILY=20000"
    # The server already runs Traefik (Hostinger templates)? It routes by the labels in compose.yaml. Else: Caddy.
    if "${SSH[@]}" "$HOST" "docker ps --format '{{.Image}}' | grep -q '^traefik'"; then
      echo "# proxy: the server's Traefik (labels in compose.yaml)"
    else
      echo "COMPOSE_PROFILES=caddy"
    fi
  } | "${SSH[@]}" "$HOST" "umask 077 && cat > $DIR/.env"
  echo ">>> ADMIN TOKEN (needed to control scenarios on /admin.html; also saved in $DIR/.env on the server):"
  echo "    $TOKEN"
fi

echo ">>> building and starting (first build takes a few minutes)"
"${SSH[@]}" "$HOST" "cd $DIR && docker compose up -d --build && docker compose ps"
DOMAIN=$("${SSH[@]}" "$HOST" "grep -E '^DOMAIN=' $DIR/.env | cut -d= -f2")
echo ">>> waiting for https://$DOMAIN/api/health"
for _ in $(seq 1 60); do
  if curl -fsS -m 5 "https://$DOMAIN/api/health" >/dev/null 2>&1; then
    curl -fsS "https://$DOMAIN/api/health"; echo
    echo ">>> live: https://$DOMAIN   (demo: /demo.html, admin: /admin.html)"
    exit 0
  fi
  sleep 3
done
echo ">>> not answering yet; check the logs with: ${SSH[*]} $HOST 'cd $DIR && docker compose logs --tail=80'"
exit 1
