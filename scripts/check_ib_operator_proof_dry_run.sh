#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PY="$ROOT_DIR/venv/bin/python"
else
  PY="python3"
fi

TMP_OUTPUT="$(mktemp)"
TMP_ARTIFACT="$(mktemp)"
trap 'rm -f "$TMP_OUTPUT" "$TMP_ARTIFACT"' EXIT

export TRADING_TEST_ALLOW_LIVE_IBKR=0

"$PY" scripts/operator_ib_gateway_recovery_proof.py \
  --source operator_cli \
  --host 127.0.0.1 \
  --port 4002 \
  --client-id 98 \
  --artifact-out "$TMP_ARTIFACT" \
  > "$TMP_OUTPUT"

"$PY" - "$TMP_OUTPUT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("result") != "DRY_RUN":
    raise SystemExit(f"expected DRY_RUN, got {payload.get('result')!r}")
if payload.get("live_mode") is not False:
    raise SystemExit("dry-run reported live_mode=true")
if payload.get("lease_acquired") is not False:
    raise SystemExit("dry-run unexpectedly acquired a lease")
if payload.get("coordinator_status") != "DRY_RUN":
    raise SystemExit(f"expected coordinator_status DRY_RUN, got {payload.get('coordinator_status')!r}")
proof = payload.get("proof") or {}
for key in ("api_handshake", "current_time", "positions", "open_orders"):
    if proof.get(key) != "not_run":
        raise SystemExit(f"dry-run proof {key} unexpectedly ran: {proof.get(key)!r}")
PY

if rg -n "Connected to IBKR|Connecting to 127\\.0\\.0\\.1|account snapshot|position snapshot|execution snapshot|execDetails|updatePortfolio" "$TMP_OUTPUT" >/dev/null; then
  echo "dry-run output contains live connection evidence" >&2
  exit 1
fi

echo "operator proof dry-run smoke: DRY_RUN no-live ok"
