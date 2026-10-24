#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_ROOT="${ROOT_DIR}/backup/pg"
BACKUP_DIR="${1:-$(find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)}"

PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-postgres}"

if [ -z "${BACKUP_DIR}" ] || [ ! -d "${BACKUP_DIR}" ]; then
  echo "ERROR: backup directory not found: ${BACKUP_DIR}"
  exit 1
fi

echo "== PostgreSQL restore =="
echo "Backup: ${BACKUP_DIR}"
echo "Target: ${PG_USER}@${PG_HOST}:${PG_PORT}"
echo ""

if [ -f "${BACKUP_DIR}/00_globals.sql.gz" ]; then
  echo "[1/2] Restoring globals"
  gunzip -c "${BACKUP_DIR}/00_globals.sql.gz" | psql -U "${PG_USER}" -h "${PG_HOST}" -p "${PG_PORT}" -d postgres || true
else
  echo "[1/2] Globals not found, skipping"
fi

echo "[2/2] Restoring databases"
for db_dir in "${BACKUP_DIR}"/*/; do
  [ -d "${db_dir}" ] || continue
  db_name="$(basename "${db_dir}")"
  full_sql="${db_dir}/${db_name}_full.sql.gz"
  schema_sql="${db_dir}/${db_name}_schema.sql.gz"
  data_sql="${db_dir}/${db_name}_data.sql.gz"

  echo "  -> ${db_name}"
  psql -U "${PG_USER}" -h "${PG_HOST}" -p "${PG_PORT}" -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='${db_name}'" | grep -q 1 || \
    createdb -U "${PG_USER}" -h "${PG_HOST}" -p "${PG_PORT}" "${db_name}"

  if [ -f "${full_sql}" ]; then
    gunzip -c "${full_sql}" | psql -U "${PG_USER}" -h "${PG_HOST}" -p "${PG_PORT}" -d "${db_name}" || true
    continue
  fi

  if [ -f "${schema_sql}" ]; then
    gunzip -c "${schema_sql}" | psql -U "${PG_USER}" -h "${PG_HOST}" -p "${PG_PORT}" -d "${db_name}" || true
  fi
  if [ -f "${data_sql}" ]; then
    gunzip -c "${data_sql}" | psql -U "${PG_USER}" -h "${PG_HOST}" -p "${PG_PORT}" -d "${db_name}" || true
  fi

  if [ ! -f "${full_sql}" ] && [ ! -f "${schema_sql}" ] && [ ! -f "${data_sql}" ]; then
    echo "     skip: no dump files for ${db_name}"
  fi
done

echo ""
echo "Done. Database table counts:"
for db in $(psql -U "${PG_USER}" -h "${PG_HOST}" -p "${PG_PORT}" -d postgres -Atqc "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"); do
  count=$(psql -U "${PG_USER}" -h "${PG_HOST}" -p "${PG_PORT}" -d "${db}" -Atqc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null || echo "N/A")
  echo "  ${db}: ${count}"
done

