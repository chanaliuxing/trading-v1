#!/bin/bash
set -e

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
if [[ ! -d "$SCRIPT_DIR" ]]; then
  SCRIPT_DIR="$PWD"
fi
cd "$SCRIPT_DIR"
SCRIPT_DIR="$(pwd)"
PROJECT_ROOT="$(cd ../.. && pwd)"

cd "$PROJECT_ROOT"

# Lightweight bootstrap (no trading health hard-gate): this job is for backtest/ML research.
export PATH="$PROJECT_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/.env"
  set +a
fi

# Prefer ML venv for nightly optimization/learning jobs.
PY_BIN="$PROJECT_ROOT/venv/bin/python"
if [[ -x "$PROJECT_ROOT/venv_ml/bin/python" ]]; then
  PY_BIN="$PROJECT_ROOT/venv_ml/bin/python"
fi

# Nightly super-win optimization (safe by default: no live config apply unless enabled).
if [[ "${ENABLE_SUPER_WIN_OPTIMIZE:-1}" = "1" ]]; then
  OPT_ARGS=()
  if [[ "${SUPER_WIN_APPLY_LIVE:-0}" = "1" ]]; then
    OPT_ARGS+=(--apply-live)
  fi
  if [[ "${SUPER_WIN_USE_BRAIN:-1}" = "1" ]]; then
    OPT_ARGS+=(--use-brain)
  fi
  if [[ "${SUPER_WIN_AUTO_BACKFILL:-1}" = "1" ]]; then
    OPT_ARGS+=(--auto-backfill)
  else
    OPT_ARGS+=(--no-auto-backfill)
  fi
  if [[ -n "${SUPER_WIN_MAX_DAYS:-}" ]]; then
    OPT_ARGS+=(--max-days "${SUPER_WIN_MAX_DAYS}")
  fi
  if [[ -n "${SUPER_WIN_LOOKBACK_DAYS:-}" ]]; then
    OPT_ARGS+=(--lookback-days "${SUPER_WIN_LOOKBACK_DAYS}")
  fi
  if [[ -n "${SUPER_WIN_MIN_BARS_PER_SYMBOL:-}" ]]; then
    OPT_ARGS+=(--min-bars-per-symbol "${SUPER_WIN_MIN_BARS_PER_SYMBOL}")
  fi
  if [[ -n "${SUPER_WIN_BACKFILL_RETRIES:-}" ]]; then
    OPT_ARGS+=(--backfill-retries "${SUPER_WIN_BACKFILL_RETRIES}")
  fi
  if [[ -n "${SUPER_WIN_BACKFILL_WAIT_SEC:-}" ]]; then
    OPT_ARGS+=(--backfill-wait-sec "${SUPER_WIN_BACKFILL_WAIT_SEC}")
  fi
  if [[ "${SUPER_WIN_BACKFILL_BLOCKING:-0}" = "1" ]]; then
    OPT_ARGS+=(--backfill-blocking)
  else
    OPT_ARGS+=(--no-backfill-blocking)
  fi
  if [[ -n "${SUPER_WIN_BACKFILL_TIMEOUT_SEC:-}" ]]; then
    OPT_ARGS+=(--backfill-timeout-sec "${SUPER_WIN_BACKFILL_TIMEOUT_SEC}")
  fi
  if [[ "${SUPER_WIN_PROCEED_PARTIAL_DATA:-1}" = "1" ]]; then
    OPT_ARGS+=(--proceed-on-partial-data)
  else
    OPT_ARGS+=(--no-proceed-on-partial-data)
  fi
  if [[ -n "${SUPER_WIN_MIN_TRADES:-}" ]]; then
    OPT_ARGS+=(--min-trades "${SUPER_WIN_MIN_TRADES}")
  fi
  if [[ -n "${SUPER_WIN_COMMISSION_BPS:-}" ]]; then
    OPT_ARGS+=(--commission-bps "${SUPER_WIN_COMMISSION_BPS}")
  fi
  "$PY_BIN" scripts/super_win_nightly_optimize.py "${OPT_ARGS[@]}" || true
fi

# Optional: keep old ai_trader learning flow behind an explicit switch.
if [[ "${ENABLE_AI_TRADER_LEARN:-0}" = "1" ]]; then
  "$PY_BIN" scripts/ai_trader/cli.py learn || true
fi
