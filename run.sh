#!/usr/bin/env bash
# Start the FloodGate backend (API + monitors + frontend) on http://127.0.0.1:8000
#   live:    ./serve.sh in another terminal first (fictional sites + tunnel), then ./run.sh
#   replay:  CONTEXT_REPLAY_DIR=out/<run> ./run.sh   (saved Context.dev responses, no credits; UI development only)
set -euo pipefail
# Ctrl-C: open live-update connections get 2 s, then the server saves its state and exits.
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  exec uv run uvicorn backend.app:app --host 127.0.0.1 --port "${PORT:-8000}" --timeout-graceful-shutdown 2
else
  echo "uv not found; falling back to .venv (install uv: sudo pacman -S uv)" >&2
  exec .venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port "${PORT:-8000}" --timeout-graceful-shutdown 2
fi
