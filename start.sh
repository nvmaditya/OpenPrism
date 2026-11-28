#!/usr/bin/env bash
# Run PrismTeX for local development: the API on 127.0.0.1:8000 and the UI on
# 127.0.0.1:3000. The UI proxies /api to the API, so open only the UI.
#
# Needs: Python 3.12+ with the backend installed (pip install -e "backend[dev]"),
# Node 22, and TeX Live or MiKTeX with latexmk for compiling.
# Read from the environment (not .env): PRISMTEX_PORT (API port, default 8000),
# UI_PORT (default 3000) and PRISMTEX_DATA_DIR (default ./data). Both servers
# bind 127.0.0.1; the API reads its other settings from .env as usual.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

API_PORT="${PRISMTEX_PORT:-8000}"
UI_PORT="${UI_PORT:-3000}"
API_URL="http://127.0.0.1:${API_PORT}"
export PRISMTEX_DATA_DIR="${PRISMTEX_DATA_DIR:-$ROOT/data}"
LOG_DIR="$ROOT/logs"

API_PID=""
UI_PID=""

log() { printf '[prismtex] %s\n' "$*"; }
die() {
  log "error: $*"
  exit 1
}

cleanup() {
  local code=$?
  trap - INT TERM EXIT
  for pid in "$UI_PID" "$API_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  exit "$code"
}
trap cleanup INT TERM EXIT

find_python() {
  local candidate
  for candidate in "$ROOT/.venv/bin/python" "$ROOT/.venv/Scripts/python.exe"; do
    [[ -x "$candidate" ]] && { echo "$candidate"; return; }
  done
  for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 && { command -v "$candidate"; return; }
  done
  die "no Python found; install Python 3.12+"
}

wait_for() {
  local url="$1" name="$2" seconds="$3" pid="$4" i
  for ((i = 0; i < seconds; i++)); do
    if "$PY" - "$url" >/dev/null 2>&1 <<'PROBE'
import sys
import urllib.request

# No proxy: a configured HTTP proxy must not intercept 127.0.0.1.
urllib.request.build_opener(urllib.request.ProxyHandler({})).open(sys.argv[1], timeout=2)
PROBE
    then
      log "$name ready at $url"
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || die "$name exited early; see $LOG_DIR"
    sleep 1
  done
  die "$name did not answer at $url within ${seconds}s; see $LOG_DIR"
}

PY="$(find_python)"
"$PY" -c "import prismtex" 2>/dev/null ||
  die "the backend is not installed for $PY; run: $PY -m pip install -e \"backend[dev]\""
command -v npm >/dev/null 2>&1 || die "npm not found; install Node 22"
command -v latexmk >/dev/null 2>&1 ||
  log "warning: latexmk not found; compiles will report that TeX is not installed"

if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  log "installing UI dependencies (npm ci)"
  (cd "$ROOT/frontend" && npm ci)
fi

mkdir -p "$LOG_DIR"

log "starting the API on $API_URL (data: $PRISMTEX_DATA_DIR)"
"$PY" -m prismtex serve --host 127.0.0.1 --port "$API_PORT" >"$LOG_DIR/backend.log" 2>&1 &
API_PID=$!
wait_for "$API_URL/api/health" "API" 30 "$API_PID"

log "starting the UI on http://127.0.0.1:$UI_PORT"
(
  cd "$ROOT/frontend"
  PRISMTEX_API_URL="$API_URL" exec npm run dev -- --port "$UI_PORT"
) >"$LOG_DIR/frontend.log" 2>&1 &
UI_PID=$!
wait_for "http://127.0.0.1:$UI_PORT/" "UI" 90 "$UI_PID"

log "PrismTeX is running at http://127.0.0.1:$UI_PORT/ (logs in $LOG_DIR). Ctrl+C stops it."
while kill -0 "$API_PID" 2>/dev/null && kill -0 "$UI_PID" 2>/dev/null; do
  sleep 2
done
die "a server stopped; see $LOG_DIR"
