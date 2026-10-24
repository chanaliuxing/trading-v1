#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
LOG_DIR="${LOG_DIR:-$REPO_DIR/logs}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_DIR/infra/compose.yml}"
DOCKER_SERVICES="${DOCKER_SERVICES:-}"
START_ALL_SERVICES="${START_ALL_SERVICES:-1}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

env_get() {
  local key="$1"
  local file="$2"
  local line value
  line="$(grep -E "^${key}=" "$file" | tail -n1 || true)"
  value="${line#*=}"
  if [[ "$value" != \"*\" ]]; then
    value="${value%%#*}"
  fi
  value="${value%"${value##*[![:space:]]}"}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "$value"
}

wait_for() {
  local timeout="$1"
  shift
  local start now
  start="$(date +%s)"
  while true; do
    if "$@"; then
      return 0
    fi
    now="$(date +%s)"
    if (( now - start >= timeout )); then
      return 1
    fi
    sleep 2
  done
}

ensure_docker_ready() {
  if docker info >/dev/null 2>&1; then
    log "Docker daemon is ready."
    return 0
  fi

  log "Docker daemon not ready. Opening Docker Desktop..."
  open -ga Docker || true
  wait_for 150 docker info >/dev/null 2>&1 || die "Docker Desktop did not become ready in time."
  log "Docker daemon is ready."
}

start_docker_services() {
  [[ -f "$COMPOSE_FILE" ]] || die "Compose file not found: $COMPOSE_FILE"
  local services=""

  if [[ "$START_ALL_SERVICES" == "1" ]]; then
    services="$(docker compose -f "$COMPOSE_FILE" config --services | tr '\n' ' ' | xargs)"
  else
    services="$DOCKER_SERVICES"
  fi

  [[ -n "$services" ]] || die "No docker services resolved from compose."
  log "Starting docker services: $services"
  docker compose -f "$COMPOSE_FILE" up -d $services

  local not_running
  not_running="$(docker compose -f "$COMPOSE_FILE" ps --services --filter status=running | tr '\n' ' ')"
  [[ -n "$not_running" ]] || die "No compose service is running after startup."

  wait_for 120 docker exec trading_clickhouse clickhouse-client -q "SELECT 1" >/dev/null 2>&1 || die "ClickHouse is not ready."
  wait_for 60 redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1 || die "Redis is not ready."
  log "Docker core services are up."
}

ensure_postgres() {
  local pg_host pg_port pg_user pg_db formula
  pg_host="$(env_get PG_HOST "$ENV_FILE")"; [[ -n "$pg_host" ]] || pg_host="localhost"
  pg_port="$(env_get PG_PORT "$ENV_FILE")"; [[ -n "$pg_port" ]] || pg_port="5432"
  pg_user="$(env_get PG_USER "$ENV_FILE")"; [[ -n "$pg_user" ]] || pg_user="postgres"
  pg_db="$(env_get PG_DB "$ENV_FILE")"; [[ -n "$pg_db" ]] || pg_db="postgres"

  if pg_isready -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$pg_db" >/dev/null 2>&1; then
    log "PostgreSQL is ready at ${pg_host}:${pg_port}."
    return 0
  fi

  if [[ "$pg_host" != "localhost" && "$pg_host" != "127.0.0.1" ]]; then
    die "PostgreSQL is not reachable at ${pg_host}:${pg_port}."
  fi

  log "PostgreSQL is down. Trying to start via Homebrew..."
  formula="$(brew services list 2>/dev/null | awk '$1 ~ /^postgresql(@[0-9]+)?$/ {print $1; exit}')"
  if [[ -z "$formula" ]]; then
    formula="$(brew list --formula 2>/dev/null | awk '/^postgresql(@[0-9]+)?$/ {print; exit}')"
  fi
  [[ -n "$formula" ]] || die "No Homebrew PostgreSQL formula found."

  brew services start "$formula" >/dev/null
  wait_for 60 pg_isready -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$pg_db" >/dev/null 2>&1 || die "PostgreSQL failed to start."
  log "PostgreSQL started (${formula}) and is ready."
}

ensure_ibkr_client() {
  local ib_host ib_port mode
  ib_host="$(env_get IB_GATEWAY_HOST "$ENV_FILE")"; [[ -n "$ib_host" ]] || ib_host="$(env_get IB_HOST "$ENV_FILE")"
  [[ -n "$ib_host" ]] || ib_host="127.0.0.1"

  ib_port="$(env_get IB_GATEWAY_PORT "$ENV_FILE")"
  if [[ -z "$ib_port" ]]; then
    mode="$(env_get TRADING_MODE "$ENV_FILE")"
    if [[ "$mode" == "live" ]]; then
      ib_port="$(env_get IB_LIVE_PORT "$ENV_FILE")"
    else
      ib_port="$(env_get IB_PAPER_PORT "$ENV_FILE")"
    fi
  fi
  [[ -n "$ib_port" ]] || ib_port="4002"

  if nc -z "$ib_host" "$ib_port" >/dev/null 2>&1; then
    log "IBKR client port is open at ${ib_host}:${ib_port}."
  else
    die "IBKR client is not started or not listening at ${ib_host}:${ib_port}. Please open IB Gateway/TWS first."
  fi

  local ib_ok=0
  for _ in {1..8}; do
    if "$REPO_DIR/venv/bin/python" - "$ib_host" "$ib_port" >/dev/null 2>&1 <<'PY'
import asyncio
import random
import sys
from ib_insync import IB

host = sys.argv[1]
port = int(sys.argv[2])

async def main():
    ib = IB()
    try:
        await ib.connectAsync(host, port, clientId=random.randint(2000, 9000), timeout=8)
        return ib.isConnected()
    except Exception:
        return False
    finally:
        if ib.isConnected():
            ib.disconnect()

ok = asyncio.run(main())
raise SystemExit(0 if ok else 1)
PY
    then
      ib_ok=1
      break
    fi
    sleep 2
  done

  [[ "$ib_ok" -eq 1 ]] || die "IBKR API handshake failed at ${ib_host}:${ib_port} (port open but API not ready)."
  log "IBKR API handshake is ready at ${ib_host}:${ib_port}."
}

start_run_system() {
  local py entry pid_file
  py="$REPO_DIR/venv/bin/python"
  entry="$REPO_DIR/run_system.py"
  pid_file="$LOG_DIR/run_system.pid"

  [[ -x "$py" ]] || die "venv python not found: $py"
  [[ -f "$entry" ]] || die "Entry not found: $entry"

  mkdir -p "$LOG_DIR"

  if pgrep -f "python.*run_system.py" >/dev/null 2>&1; then
    log "run_system.py is already running."
    return 0
  fi

  log "Starting run_system.py..."
  nohup "$py" "$entry" >>"$LOG_DIR/run_system.out.log" 2>>"$LOG_DIR/run_system.err.log" &
  echo $! > "$pid_file"
  sleep 2

  if ! pgrep -f "python.*run_system.py" >/dev/null 2>&1; then
    die "run_system.py exited early. Check $LOG_DIR/run_system.err.log"
  fi

  log "run_system.py started. PID: $(cat "$pid_file")"
  log "Logs: $LOG_DIR/run_system.out.log / $LOG_DIR/run_system.err.log"
}

main() {
  [[ -f "$ENV_FILE" ]] || die ".env file not found: $ENV_FILE"
  ensure_docker_ready
  start_docker_services
  ensure_postgres
  ensure_ibkr_client
  start_run_system
}

main "$@"
