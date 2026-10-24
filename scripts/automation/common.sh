#!/bin/bash
set -e

# Resolve project root (scripts/automation -> project)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Environment
export PATH="$PROJECT_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Load .env if present (export vars)
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/.env"
  set +a
fi

# Activate venv if available
if [[ -f "$PROJECT_ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/venv/bin/activate"
fi

# Health gate: only continue if overall_status == healthy (exit 0)
cd "$PROJECT_ROOT/docs/daily"
python ./system_health_check.py >/dev/null 2>&1 || {
  echo "[GATE] Health check not healthy. Abort task at $(date)" >&2
  exit 1
}
