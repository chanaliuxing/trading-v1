#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

export PATH="$PROJECT_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/.env"
  set +a
fi

if [[ -f "$PROJECT_ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/venv/bin/activate"
fi

cd "$PROJECT_ROOT"

MANIFEST_PATH="${MANIFEST_PATH:-$PROJECT_ROOT/configs/index_super_win/qqq_replica_manifest.example.yml}"
RUNTIME_SNAPSHOT_PATH="${RUNTIME_SNAPSHOT_PATH:-$PROJECT_ROOT/runtime/state/index_super_win_replica_runtime_index_super_win_replica_001.json}"
BROKER_POSITIONS_PATH="${BROKER_POSITIONS_PATH:-$PROJECT_ROOT/runtime/state/index_super_win_replica_broker_positions_flat.json}"
PREFLIGHT_OUTPUT_PATH="${PREFLIGHT_OUTPUT_PATH:-$PROJECT_ROOT/runtime/state/index_super_win_replica_preflight.json}"

if [[ ! -f "$RUNTIME_SNAPSHOT_PATH" ]]; then
  printf '{"strategy_id":"index_super_win_replica_001","generated_at_et":null,"replica_runtime":{}}\n' >"$RUNTIME_SNAPSHOT_PATH"
fi

"$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/build_replica_flat_broker_snapshot.py" \
  --manifest "$MANIFEST_PATH" \
  --output-path "$BROKER_POSITIONS_PATH" >/dev/null

exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/tool/index_super_win_replica_preflight.py" \
  --manifest "$MANIFEST_PATH" \
  --runtime-snapshot "$RUNTIME_SNAPSHOT_PATH" \
  --broker-positions "$BROKER_POSITIONS_PATH" \
  --output-path "$PREFLIGHT_OUTPUT_PATH" \
  --json
