#!/usr/bin/env python3
"""
Gateway Watchdog - Layer B Real-time Self-Healing

Monitors ML Gateway decision logs for data integrity errors and automatically
triggers repair actions (auto-seed) before AI Brain even notices the issue.

Usage:
    # Run in background
    nohup venv/bin/python tools/gateway_watchdog.py > logs/watchdog.log 2>&1 &

    # With custom symbols
    WATCHDOG_SYMBOLS=TSLA,NVDA,SPY python tools/gateway_watchdog.py

    # Stop
    ps aux | grep gateway_watchdog
    kill <PID>

Features:
    - Monitors gateway decision logs in real-time
    - Detects [data_integrity] and invalid_features errors
    - Automatically triggers auto-seed repair
    - Cooldown mechanism to prevent repair loops
    - Logs all actions for post-mortem analysis
"""

import time
import json
import subprocess
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from libs.logger import get_logger
    logger = get_logger("watchdog")
except Exception:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("watchdog")

# ============================================================================
# Configuration
# ============================================================================

# Symbols to monitor (can override with env var WATCHDOG_SYMBOLS)
DEFAULT_SYMBOLS = os.getenv("WATCHDOG_SYMBOLS", "TSLA,NVDA,SPY,AAPL,MSFT,META,AMD,GOOGL,QQQ,AMZN")
PYTHON_EXEC = str(PROJECT_ROOT / "venv/bin/python")
REPAIR_SCRIPT = str(PROJECT_ROOT / "tools/check_active_completeness.py")

# Detection keywords
WATCH_KEYWORDS = ["[data_integrity]", "invalid_features"]

# Cooldown to prevent repair loops (seconds)
COOLDOWN_SECONDS = 300  # 5 minutes

# Max repairs per hour (circuit breaker)
MAX_REPAIRS_PER_HOUR = 10

# ============================================================================
# State Management
# ============================================================================

last_repair_time = {}
repair_count_hourly = []


def get_today_log_path():
    """Automatically locate today's gateway decision log"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_path = PROJECT_ROOT / "runs" / "ai_trader" / today_str / "ml_gateway_decisions.jsonl"
    return str(log_path)


def should_trigger_repair(symbol: str) -> bool:
    """Check if repair should be triggered based on cooldown and circuit breaker"""
    global last_repair_time, repair_count_hourly

    now = time.time()

    # Cooldown check (per symbol)
    if symbol in last_repair_time:
        elapsed = now - last_repair_time[symbol]
        if elapsed < COOLDOWN_SECONDS:
            logger.info(
                "watchdog_cooldown",
                symbol=symbol,
                elapsed_sec=int(elapsed),
                cooldown_sec=COOLDOWN_SECONDS
            )
            return False

    # Circuit breaker check (global)
    # Remove repairs older than 1 hour
    repair_count_hourly = [t for t in repair_count_hourly if now - t < 3600]

    if len(repair_count_hourly) >= MAX_REPAIRS_PER_HOUR:
        logger.error(
            "watchdog_circuit_breaker",
            repairs_last_hour=len(repair_count_hourly),
            max_allowed=MAX_REPAIRS_PER_HOUR,
            message="Too many repairs in last hour - possible systemic issue!"
        )
        return False

    return True


def trigger_repair(symbol: str, error_reason: str = ""):
    """Execute auto-seed repair for a specific symbol"""
    global last_repair_time, repair_count_hourly

    if not should_trigger_repair(symbol):
        return

    now = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")

    logger.warning(
        "watchdog_repair_triggered",
        symbol=symbol,
        error_reason=error_reason,
        action="auto_seed"
    )

    print(f"\n{'='*80}")
    print(f"[Watchdog] 🚨 Data Integrity Error Detected!")
    print(f"[Watchdog] Symbol: {symbol}")
    print(f"[Watchdog] Reason: {error_reason}")
    print(f"[Watchdog] 🚑 Triggering Auto-Seed Repair...")
    print(f"{'='*80}\n")

    cmd = [
        PYTHON_EXEC, REPAIR_SCRIPT,
        "--symbols", symbol,
        "--date", today_str,
        "--auto-seed-active",
        "--min-coverage", "0.95"
    ]

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=120
        )

        print(f"[Watchdog] ✅ Repair completed for {symbol}")
        print(f"[Watchdog] Output:\n{result.stdout}")

        logger.info(
            "watchdog_repair_success",
            symbol=symbol,
            output=result.stdout[:500]  # Truncate for logging
        )

        # Update state
        last_repair_time[symbol] = now
        repair_count_hourly.append(now)

    except subprocess.TimeoutExpired:
        logger.error("watchdog_repair_timeout", symbol=symbol, timeout_sec=120)
        print(f"[Watchdog] ⏱️ Repair timed out for {symbol}")

    except subprocess.CalledProcessError as e:
        logger.error(
            "watchdog_repair_failed",
            symbol=symbol,
            exit_code=e.returncode,
            stderr=e.stderr
        )
        print(f"[Watchdog] ❌ Repair failed for {symbol}")
        print(f"[Watchdog] Error: {e.stderr}")


def parse_decision_line(line_str: str) -> dict | None:
    """Parse a single line from gateway decision log"""
    try:
        data = json.loads(line_str)
        return data
    except json.JSONDecodeError:
        return None


def should_repair(decision: dict) -> tuple[bool, str]:
    """Determine if this decision requires repair

    Returns:
        (should_repair: bool, reason: str)
    """
    if not decision:
        return False, ""

    # Check if decision is blocked
    if decision.get("decision") != "blocked":
        return False, ""

    # Check block reason
    block_reason = decision.get("block_reason", "")
    error_reason = decision.get("error_reason", "")

    # Detect data integrity issues
    for keyword in WATCH_KEYWORDS:
        if keyword in block_reason or keyword in error_reason:
            return True, block_reason or error_reason

    return False, ""


def monitor_log_file():
    """Main monitoring loop using tail -F"""
    log_path = get_today_log_path()

    logger.info(
        "watchdog_started",
        log_path=log_path,
        symbols=DEFAULT_SYMBOLS,
        cooldown_sec=COOLDOWN_SECONDS
    )

    print(f"{'='*80}")
    print(f"[Watchdog] Gateway Watchdog Started")
    print(f"[Watchdog] Monitoring: {log_path}")
    print(f"[Watchdog] Symbols: {DEFAULT_SYMBOLS}")
    print(f"[Watchdog] Cooldown: {COOLDOWN_SECONDS}s per symbol")
    print(f"[Watchdog] Circuit Breaker: {MAX_REPAIRS_PER_HOUR} repairs/hour")
    print(f"{'='*80}\n")

    # Wait for log file to be created
    while not os.path.exists(log_path):
        print(f"[Watchdog] Waiting for log file: {log_path}")
        time.sleep(5)

    print(f"[Watchdog] Log file found, starting monitoring...\n")

    # Use tail -F to follow log file (handles rotation)
    try:
        process = subprocess.Popen(
            ['tail', '-F', log_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        for line in process.stdout:
            if not line.strip():
                continue

            # Parse decision
            decision = parse_decision_line(line)
            if not decision:
                continue

            # Check if repair is needed
            should_fix, reason = should_repair(decision)
            if should_fix:
                symbol = decision.get("symbol", "UNKNOWN")
                trigger_repair(symbol, reason)

    except KeyboardInterrupt:
        logger.info("watchdog_stopped", reason="keyboard_interrupt")
        print("\n[Watchdog] Stopping (Ctrl+C received)...")

    except Exception as e:
        logger.error("watchdog_error", error=str(e), exc_info=True)
        print(f"\n[Watchdog] Error: {e}")
        raise


def main():
    """Entry point"""
    try:
        monitor_log_file()
    except KeyboardInterrupt:
        print("\n[Watchdog] Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\n[Watchdog] Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
