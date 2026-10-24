#!/bin/bash
# =============================================================================
# One‑Click Setup: Full Daily Automation via launchd (macOS)
# - Installs launch agents for: hourly health, morning check, premarket LLM,
#   market LLM, evening review, overnight LLM, and 16:01 recon-all.
# - Every task gates on docs/daily/system_health_check.py (must be healthy).
# =============================================================================
set -e

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This one-click automation uses macOS launchd. Detected: $(uname -s)"
  echo "Use crontab on non-macOS or ask me to generate a Linux systemd setup."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AUTOMATION_DIR="$PROJECT_ROOT/scripts/automation"
LAUNCH_DIR="$HOME/Library/LaunchAgents"

mkdir -p "$AUTOMATION_DIR" "$PROJECT_ROOT/logs" "$LAUNCH_DIR"

cat <<'SH' > "$AUTOMATION_DIR/common.sh"
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
SH
chmod +x "$AUTOMATION_DIR/common.sh"

# ---- Wrapper scripts ---------------------------------------------------------

cat <<'SH' > "$AUTOMATION_DIR/morning_health.sh"
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/docs/daily"
./morning_check.sh || true
SH
chmod +x "$AUTOMATION_DIR/morning_health.sh"

cat <<'SH' > "$AUTOMATION_DIR/premarket_llm.sh"
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/llm_trader"
if [[ -x ./run_premarket.sh ]]; then
  ./run_premarket.sh
else
  # Fallback to a safe default with ET gating
  python run_single_bot.py \
    --provider deepseek_nvidia \
    --symbols TSLA \
    --interval 600 \
    --cycles 33 \
    --close-at "09:35" \
    --forbid-new-positions-before 9 \
    --wait-for-premarket
fi
SH
chmod +x "$AUTOMATION_DIR/premarket_llm.sh"

cat <<'SH' > "$AUTOMATION_DIR/market_llm.sh"
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/llm_trader"
if [[ -x ./run_all_providers.sh ]]; then
  ./run_all_providers.sh market
else
  # Fallback to first-hour fast strategy
  python run_single_bot.py \
    --provider deepseek \
    --symbols TSLA \
    --interval 60 \
    --wait-for-market \
    --start-trading-at "9:40" \
    --stop-trading-at "10:30" \
    --close-at "10:45"
fi
SH
chmod +x "$AUTOMATION_DIR/market_llm.sh"

cat <<'SH' > "$AUTOMATION_DIR/evening_review.sh"
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/docs/daily"
./evening_review.sh || true
SH
chmod +x "$AUTOMATION_DIR/evening_review.sh"

cat <<'SH' > "$AUTOMATION_DIR/ai_trader_learn.sh"
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
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
SH
chmod +x "$AUTOMATION_DIR/ai_trader_learn.sh"

cat <<'SH' > "$AUTOMATION_DIR/daily_candidate_factory.sh"
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

export PATH="$PROJECT_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

# Candidate factory is strictly offline review production: no order execution.
export TRADING_MODE="${TRADING_MODE:-paper}"

PY_BIN="$PROJECT_ROOT/venv/bin/python"
if [[ -x "$PROJECT_ROOT/venv_ml/bin/python" ]]; then
  PY_BIN="$PROJECT_ROOT/venv_ml/bin/python"
fi

"$PY_BIN" "$PROJECT_ROOT/scripts/daily_candidate_factory.py" --top-n 5
SH
chmod +x "$AUTOMATION_DIR/daily_candidate_factory.sh"

cat <<'SH' > "$AUTOMATION_DIR/overnight_llm.sh"
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
"$SCRIPT_DIR/common.sh"
cd "$PROJECT_ROOT/llm_trader"
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 50 \
  --wait-for-overnight \
  --close-at "03:50" \
  --forbid-new-positions-before 5
SH
chmod +x "$AUTOMATION_DIR/overnight_llm.sh"

cat <<'SH' > "$AUTOMATION_DIR/hourly_health.sh"
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Only run health report; do not gate out
export PATH="$PROJECT_ROOT/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$PROJECT_ROOT/docs/daily"
python ./system_health_check.py || true
SH
chmod +x "$AUTOMATION_DIR/hourly_health.sh"

# ---- Helper to write plist ---------------------------------------------------
write_plist() {
  local label="$1"; shift
  local program="$1"; shift
  local log_name="$1"; shift
  local calendar_xml="$1"; shift
  local plist="$LAUNCH_DIR/${label}.plist"
  cat > "$plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${program}</string>
  </array>
  ${calendar_xml}
  <key>StandardOutPath</key><string>${PROJECT_ROOT}/logs/${log_name}.log</string>
  <key>StandardErrorPath</key><string>${PROJECT_ROOT}/logs/${log_name}_error.log</string>
  <key>WorkingDirectory</key><string>${PROJECT_ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:${PROJECT_ROOT}/venv/bin</string>
  </dict>
  <key>RunAtLoad</key><false/>
</dict>
 </plist>
PL
}

unload_if_loaded() {
  local label="$1"; local plist="$LAUNCH_DIR/${label}.plist"
  if launchctl list | grep -q "$label"; then
    launchctl unload "$plist" 2>/dev/null || true
  fi
}

load_plist() {
  local label="$1"; local plist="$LAUNCH_DIR/${label}.plist"
  launchctl load "$plist"
}

# ---- Create all plists -------------------------------------------------------

# Hourly health at minute 0
write_plist "com.trading.health.hourly" \
  "$AUTOMATION_DIR/hourly_health.sh" \
  "health_cron" \
  "<key>StartCalendarInterval</key><dict><key>Minute</key><integer>0</integer></dict>"

# 07:00 ET equivalent (local time) morning check
write_plist "com.trading.morning.health" \
  "$AUTOMATION_DIR/morning_health.sh" \
  "morning" \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>"

# 07:30 premarket (wrapper will wait-for-premarket in ET)
write_plist "com.trading.premarket" \
  "$AUTOMATION_DIR/premarket_llm.sh" \
  "premarket" \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>"

# 09:30 market (wrapper may use run_all_providers.sh market)
write_plist "com.trading.market" \
  "$AUTOMATION_DIR/market_llm.sh" \
  "market" \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>30</integer></dict>"

# 16:30 evening review
write_plist "com.trading.evening.review" \
  "$AUTOMATION_DIR/evening_review.sh" \
  "evening_review" \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>30</integer></dict>"

# 17:05 Super Win nightly research (auto backtest + parameter optimization)
write_plist "com.trading.superwin.research" \
  "$AUTOMATION_DIR/ai_trader_learn.sh" \
  "superwin_research" \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>5</integer></dict>"

# 17:20 Daily candidate factory (offline top5 review bundle)
write_plist "com.trading.candidate.factory" \
  "$AUTOMATION_DIR/daily_candidate_factory.sh" \
  "candidate_factory" \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>20</integer></dict>"

# 20:00 overnight LLM
write_plist "com.trading.overnight" \
  "$AUTOMATION_DIR/overnight_llm.sh" \
  "overnight" \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>"

# 16:01 recon-all (reuse existing recon script)
if [[ -x "$PROJECT_ROOT/scripts/setup_daily_automation.sh" ]]; then
  bash "$PROJECT_ROOT/scripts/setup_daily_automation.sh"
fi

# ---- Unload/reload all -------------------------------------------------------
for LABEL in \
  com.trading.health.hourly \
  com.trading.morning.health \
  com.trading.premarket \
  com.trading.market \
  com.trading.evening.review \
  com.trading.superwin.research \
  com.trading.candidate.factory \
  com.trading.overnight \
  com.trading.recon.daily
do
  unload_if_loaded "$LABEL"
done

for LABEL in \
  com.trading.health.hourly \
  com.trading.morning.health \
  com.trading.premarket \
  com.trading.market \
  com.trading.evening.review \
  com.trading.superwin.research \
  com.trading.candidate.factory \
  com.trading.overnight \
  com.trading.recon.daily
do
  load_plist "$LABEL"
done

echo ""
echo "=============================================================="
echo "✓ One‑Click Automation installed (launchd)."
echo "Jobs:"
echo "  - Hourly Health (on the hour)"
echo "  - Morning Check (07:00)"
echo "  - Premarket LLM (07:30; waits for 04:00 ET)"
echo "  - Market LLM (09:30)"
echo "  - Evening Review (16:30)"
echo "  - Super Win Research (17:05; backtest + optimize)"
echo "  - Daily Candidate Factory (17:20; publish Top5 review bundle)"
echo "  - Overnight LLM (20:00; closes 03:50 ET)"
echo "  - Recon-All (16:01)"
echo "Logs in: $PROJECT_ROOT/logs/*.log"
echo "Manage with: launchctl list | grep trading"
echo "=============================================================="
