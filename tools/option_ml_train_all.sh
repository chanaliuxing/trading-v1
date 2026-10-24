#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON_BIN:-venv/bin/python}"

ARGS=("$@")
HAS_START=0
HAS_END=0
for arg in "${ARGS[@]}"; do
  if [[ "$arg" == "--start" ]]; then
    HAS_START=1
  fi
  if [[ "$arg" == "--end" ]]; then
    HAS_END=1
  fi
done

if [[ $HAS_START -eq 0 || $HAS_END -eq 0 ]]; then
  read -r DEFAULT_START DEFAULT_END < <("$PY" - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo("America/New_York"))
start = now - timedelta(days=60)
print(start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S"))
PY
)
  if [[ $HAS_START -eq 0 ]]; then
    ARGS+=(--start "$DEFAULT_START")
  fi
  if [[ $HAS_END -eq 0 ]]; then
    ARGS+=(--end "$DEFAULT_END")
  fi
fi

echo "[option_ml_train_all] Training + auto-promotion..."
$PY tools/option_ml_train_and_promote.py "${ARGS[@]}"

echo "[option_ml_train_all] Done."
