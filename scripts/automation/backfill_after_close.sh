#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p "$PROJECT_ROOT/logs" "$PROJECT_ROOT/runtime/state"

STAMP_FILE="$PROJECT_ROOT/runtime/state/eod_backfill_last_run.txt"
TODAY_ET="$(TZ=America/New_York date +%F)"
if [[ -f "$STAMP_FILE" ]] && [[ "$(cat "$STAMP_FILE" 2>/dev/null || true)" == "$TODAY_ET" ]]; then
  echo "[backfill_after_close] already ran for $TODAY_ET, skip."
  exit 0
fi

PY="$PROJECT_ROOT/venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "[backfill_after_close] missing python: $PY" >&2
  exit 1
fi

echo "[backfill_after_close] start at $(date) / ET_DAY=$TODAY_ET"

"$PY" "$PROJECT_ROOT/tool/load_hist_multi.py" \
  --symbols QQQ TQQQ SQQQ \
  --days 2 \
  --bar-size "1 min" \
  --fill-mode backfill \
  --skip-covered-days \
  --coverage-threshold 0.99

"$PY" "$PROJECT_ROOT/tool/rebuild_qqq_daily_atr_context.py"

echo "$TODAY_ET" > "$STAMP_FILE"
echo "[backfill_after_close] done at $(date)"
