#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CH_DATA_DIR="${HOME}/Documents/clickhouse_data"
CH_LOG_DIR="${HOME}/Documents/clickhouse_logs"

echo "== AirMac migration setup =="
echo "Project: ${ROOT_DIR}"
echo ""

echo "[1/5] Prepare ClickHouse host directories"
mkdir -p "${CH_DATA_DIR}" "${CH_LOG_DIR}"
echo "  OK: ${CH_DATA_DIR}"
echo "  OK: ${CH_LOG_DIR}"
echo ""

echo "[2/5] Check Docker CLI/daemon"
if ! command -v docker >/dev/null 2>&1; then
  echo "  WARN: docker command not found"
  echo "  Install Docker Desktop: brew install --cask docker"
  echo "  Then open Docker.app once and retry"
else
  if docker info >/dev/null 2>&1; then
    echo "  OK: docker is ready"
  else
    echo "  WARN: docker found but daemon not ready"
    echo "  Start Docker Desktop and wait until green status"
  fi
fi
echo ""

echo "[3/5] Check PostgreSQL service"
if command -v pg_isready >/dev/null 2>&1 && pg_isready -h localhost -p 5432 >/dev/null 2>&1; then
  echo "  OK: PostgreSQL is accepting connections on localhost:5432"
else
  echo "  WARN: PostgreSQL not ready on localhost:5432"
  echo "  Try: brew services start postgresql@15"
fi
echo ""

echo "[4/5] Test PostgreSQL login"
PG_CANDIDATES=("${PG_USER:-}" "postgres" "${USER:-}")
FOUND_USER=""
for cand in "${PG_CANDIDATES[@]}"; do
  [ -n "${cand}" ] || continue
  if psql -h localhost -p 5432 -U "${cand}" -d postgres -Atqc "select current_user" >/dev/null 2>&1; then
    FOUND_USER="${cand}"
    break
  fi
done

if [ -n "${FOUND_USER}" ]; then
  echo "  OK: login success with user=${FOUND_USER}"
else
  echo "  WARN: unable to login with tested users (${PG_CANDIDATES[*]})"
  echo "  Check pg_hba.conf and role/password setup"
fi
echo ""

echo "[5/5] Suggested next commands"
LATEST_CH_BACKUP="$(find "${ROOT_DIR}/backup/clickhouse" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1 || true)"
LATEST_PG_BACKUP="$(find "${ROOT_DIR}/backup/pg" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1 || true)"

echo "  cd ${ROOT_DIR}"
echo "  make up"
if [ -n "${LATEST_CH_BACKUP}" ]; then
  echo "  cd ${LATEST_CH_BACKUP} && bash restore.sh"
fi
if [ -n "${LATEST_PG_BACKUP}" ]; then
  echo "  cd ${LATEST_PG_BACKUP}"
  echo "  gunzip -c 00_globals.sql.gz | psql -U \${PG_USER:-postgres} -h localhost -p 5432"
  echo "  for d in */; do [ -f \"\${d}restore.sh\" ] && (cd \"\${d}\" && PG_USER=\${PG_USER:-postgres} bash restore.sh); done"
fi

