#!/usr/bin/env bash
# Start OpenPrism backend (FastAPI) + frontend (Next.js) for local dev.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

BACKEND_HOST="${PRISM_HOST:-127.0.0.1}"
BACKEND_PORT="${PRISM_PORT:-8000}"
FRONTEND_PORT="${PRISM_FRONTEND_PORT:-3000}"
API_URL="${NEXT_PUBLIC_API_URL:-http://${BACKEND_HOST}:${BACKEND_PORT}}"

BACKEND_PID=""
FRONTEND_PID=""

log() { printf '[prism] %s\n' "$*"; }

die() {
  log "ERROR: $*"
  exit 1
}

cleanup() {
  local code=$?
  log "shutting down..."
  if [[ -n "${FRONTEND_PID}" ]] && kill -0 "${FRONTEND_PID}" 2>/dev/null; then
    kill "${FRONTEND_PID}" 2>/dev/null || true
    wait "${FRONTEND_PID}" 2>/dev/null || true
  fi
  if [[ -n "${BACKEND_PID}" ]] && kill -0 "${BACKEND_PID}" 2>/dev/null; then
    kill "${BACKEND_PID}" 2>/dev/null || true
    wait "${BACKEND_PID}" 2>/dev/null || true
  fi
  exit "${code}"
}
trap cleanup INT TERM EXIT

resolve_python() {
  if [[ -x "${ROOT}/.venv/Scripts/python.exe" ]]; then
    echo "${ROOT}/.venv/Scripts/python.exe"
  elif [[ -x "${ROOT}/.venv/bin/python" ]]; then
    echo "${ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    die "no Python found (expected .venv or python3 on PATH)"
  fi
}

wait_http() {
  local url="$1"
  local name="$2"
  local attempts="${3:-60}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if command -v curl >/dev/null 2>&1; then
      if curl -sf --max-time 2 "${url}" >/dev/null 2>&1; then
        log "${name} ready: ${url}"
        return 0
      fi
    else
      # Fallback: Python one-liner
      if "${PY}" -c "import urllib.request; urllib.request.urlopen('${url}', timeout=2)" 2>/dev/null; then
        log "${name} ready: ${url}"
        return 0
      fi
    fi
    sleep 1
  done
  die "${name} did not become ready at ${url} within ${attempts}s"
}

PY="$(resolve_python)"
log "python: ${PY}"
log "root:   ${ROOT}"

# Ensure frontend deps if missing
if [[ ! -d "${ROOT}/frontend/node_modules" ]]; then
  log "installing frontend deps (npm install)..."
  (cd "${ROOT}/frontend" && npm install)
fi

mkdir -p "${ROOT}/data/projects" "${ROOT}/data/previews" "${ROOT}/data/skill-home" "${ROOT}/logs"

log "starting backend on http://${BACKEND_HOST}:${BACKEND_PORT} ..."
(
  cd "${ROOT}"
  exec "${PY}" -m uvicorn app.main:app \
    --app-dir backend \
    --host "${BACKEND_HOST}" \
    --port "${BACKEND_PORT}"
) >"${ROOT}/logs/backend.log" 2>&1 &
BACKEND_PID=$!

wait_http "http://${BACKEND_HOST}:${BACKEND_PORT}/health" "backend" 45

log "starting frontend on http://127.0.0.1:${FRONTEND_PORT} ..."
(
  cd "${ROOT}/frontend"
  export NEXT_PUBLIC_API_URL="${API_URL}"
  exec npm run dev -- --port "${FRONTEND_PORT}" --hostname 127.0.0.1
) >"${ROOT}/logs/frontend.log" 2>&1 &
FRONTEND_PID=$!

wait_http "http://127.0.0.1:${FRONTEND_PORT}/" "frontend" 90

log ""
log "OpenPrism is running"
log "  API:  http://${BACKEND_HOST}:${BACKEND_PORT}/health"
log "  UI:   http://127.0.0.1:${FRONTEND_PORT}/"
log "  logs: ${ROOT}/logs/backend.log"
log "        ${ROOT}/logs/frontend.log"
log "Press Ctrl+C to stop."
log ""

# Keep foreground until interrupted
while true; do
  if ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
    die "backend exited early; see logs/backend.log"
  fi
  if ! kill -0 "${FRONTEND_PID}" 2>/dev/null; then
    die "frontend exited early; see logs/frontend.log"
  fi
  sleep 2
done
