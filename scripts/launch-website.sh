#!/usr/bin/env bash
# Launches (or reloads) the DASHR AI dev stack: Postgres/Redis/MinIO via
# docker compose, the FastAPI backend (uvicorn), and the Next.js frontend.
#
# Safe to re-run: any previously launched API/web process (tracked via PID
# file, falling back to whatever is bound to the port) is stopped first, so
# this always ends with exactly one fresh instance of each picking up the
# latest code.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$REPO_ROOT/apps/api"
WEB_DIR="$REPO_ROOT/apps/web"

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"

RUN_DIR="/tmp/dashr-dev"
mkdir -p "$RUN_DIR"
API_LOG="$RUN_DIR/api.log"
WEB_LOG="$RUN_DIR/web.log"
API_PID_FILE="$RUN_DIR/api.pid"
WEB_PID_FILE="$RUN_DIR/web.pid"

stop_if_running() {
  local pid_file="$1" label="$2"
  if [ -f "$pid_file" ]; then
    local pid
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "Stopping existing $label (pid $pid)..."
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
  fi
}

kill_port() {
  local port="$1"
  local pids
  pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "Killing stray process(es) already bound to port $port: $pids"
    kill $pids 2>/dev/null || true
    sleep 1
    kill -9 $pids 2>/dev/null || true
  fi
}

echo "== Reloading any running backend/frontend =="
stop_if_running "$API_PID_FILE" "API (uvicorn)"
stop_if_running "$WEB_PID_FILE" "Web (next dev)"
kill_port "$API_PORT"
kill_port "$WEB_PORT"

echo "== Ensuring Postgres/Redis/MinIO are up =="
if command -v docker >/dev/null 2>&1; then
  docker compose -f "$REPO_ROOT/infra/docker-compose.yml" up -d postgres redis minio
  echo "Waiting for Postgres to accept connections..."
  for _ in $(seq 1 30); do
    docker exec infra-postgres-1 pg_isready -U dashr >/dev/null 2>&1 && break
    sleep 1
  done
else
  echo "docker not found on PATH — skipping infra bring-up, assuming Postgres/Redis/MinIO are already reachable"
fi

echo "== Starting API (uvicorn) on :$API_PORT =="
(
  cd "$API_DIR"
  source .venv/bin/activate
  nohup uvicorn app.main:app --host 0.0.0.0 --port "$API_PORT" > "$API_LOG" 2>&1 &
  echo $! > "$API_PID_FILE"
)

for _ in $(seq 1 30); do
  curl -sf "http://localhost:$API_PORT/docs" >/dev/null && break
  sleep 1
done
if ! curl -sf "http://localhost:$API_PORT/docs" >/dev/null; then
  echo "API failed to become ready — last 40 lines of $API_LOG:"
  tail -n 40 "$API_LOG"
  exit 1
fi
echo "API up at http://localhost:$API_PORT (pid $(cat "$API_PID_FILE"), logs: $API_LOG)"

echo "== Starting Web (next dev) on :$WEB_PORT =="
(
  cd "$WEB_DIR"
  nohup env API_INTERNAL_URL="http://localhost:$API_PORT" npm run dev -- --port "$WEB_PORT" > "$WEB_LOG" 2>&1 &
  echo $! > "$WEB_PID_FILE"
)

for _ in $(seq 1 45); do
  curl -sf "http://localhost:$WEB_PORT" >/dev/null && break
  sleep 1
done
if ! curl -sf "http://localhost:$WEB_PORT" >/dev/null; then
  echo "Web failed to become ready — last 40 lines of $WEB_LOG:"
  tail -n 40 "$WEB_LOG"
  exit 1
fi
echo "Web up at http://localhost:$WEB_PORT (pid $(cat "$WEB_PID_FILE"), logs: $WEB_LOG)"

echo
echo "DASHR AI is running:"
echo "  Frontend: http://localhost:$WEB_PORT"
echo "  API:      http://localhost:$API_PORT  (docs at /docs)"
echo "  Logs:     $API_LOG"
echo "            $WEB_LOG"
echo "  Stop with: kill \$(cat $API_PID_FILE) \$(cat $WEB_PID_FILE)"
