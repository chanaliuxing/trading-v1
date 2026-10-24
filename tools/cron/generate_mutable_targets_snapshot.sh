#!/usr/bin/env bash
# Simple cron-friendly wrapper to refresh mutable target health snapshot.
# Usage (cron):
#   */30 * * * * cd . && PYTHONPATH=. venv/bin/python scripts/ai_trader/research/mutable_targets_snapshot.py

set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

PYTHONPATH=. venv/bin/python scripts/ai_trader/research/mutable_targets_snapshot.py
