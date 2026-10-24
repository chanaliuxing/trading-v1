#!/usr/bin/env bash
# Quick-launch helper to run the Meta Brain loop locally with minimal setup.
# - Starts auto_loop in the background (idempotent via pid file)
# - Triggers one research cycle + one sleep loop pass to seed lessons/skills

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT}/runs/meta_brain_local"
PYTHON_BIN="${PYTHON_BIN:-python}"

mkdir -p "${LOG_DIR}"
export PYTHONPATH="${ROOT}"

start_auto_loop() {
  local pid_file="${LOG_DIR}/auto_loop.pid"
  if [[ -f "${pid_file}" ]]; then
    local pid
    pid="$(cat "${pid_file}")" || true
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "[meta-brain] auto_loop already running (pid ${pid})"
      return
    fi
  fi
  echo "[meta-brain] starting auto_loop..."
  nohup "${PYTHON_BIN}" "${ROOT}/scripts/ai_trader/auto_loop.py" \
    > "${LOG_DIR}/auto_loop.log" 2>&1 &
  echo $! > "${pid_file}"
  echo "[meta-brain] auto_loop started (pid $(cat "${pid_file}")) → ${LOG_DIR}/auto_loop.log"
}

run_research_cycle_once() {
  echo "[meta-brain] triggering research cycle once..."
  "${PYTHON_BIN}" - <<'PY'
import asyncio
from scripts.ai_trader.research.agent import run_research_cycle_once

asyncio.run(run_research_cycle_once())
PY
}

run_sleep_loop_once() {
  echo "[meta-brain] triggering sleep_loop once..."
  "${PYTHON_BIN}" "${ROOT}/scripts/ai_trader/memory/sleep_loop.py"
}

start_auto_loop
run_research_cycle_once
run_sleep_loop_once

echo "[meta-brain] done. Check logs under ${LOG_DIR} and runs/ai_trader for outputs."
