#!/usr/bin/env python3
from __future__ import annotations
"""
Live debug helper for DataHealer.

Usage (from repo root):

  source venv/bin/activate
  PYTHONPATH=. python tools/debug_data_healer_live.py --symbols TSLA NVDA --days 2

This will:
  1) Print current ClickHouse config.
  2) Print bars_1m_ibkr / features_1m row counts for the given symbols over the lookback window.
  3) Run DataHealer.ensure_data_ready(...) once.
  4) Re-check counts and print whether coverage is still missing.
"""

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from libs.db.clickhouse import ClickHouseDB
from services.data.data_healer import DataHealer


def _print_counts(ch: ClickHouseDB, symbols: list[str], days: int) -> None:
    # Use same INTERVAL window logic as DataHealer (_check_coverage)
    days_int = int(days)
    sym_list = ",".join(f"'{s}'" for s in symbols)

    print(f"\n[DEBUG] Checking coverage for last {days_int} days (now() - INTERVAL {days_int} DAY → now())")

    # Bars table counts (bars_1m_ibkr)
    try:
        sql_bars = f"""
        SELECT symbol, count() AS cnt
        FROM trading.bars_1m_ibkr
        WHERE symbol IN ({sym_list})
          AND ts_end >= now() - INTERVAL {days_int} DAY
        GROUP BY symbol
        ORDER BY symbol
        """
        rows_bars = ch.query(sql_bars)
        print("[DEBUG] bars_1m_ibkr counts:")
        for r in rows_bars:
            print(f"  {r['symbol']}: {r['cnt']}")
        if not rows_bars:
            print("  (no rows)")
    except Exception as e:  # pragma: no cover - live debug only
        print(f"[DEBUG] bars_1m_ibkr query failed: {e}")

    # Features table counts (features_1m)
    try:
        sql_feat = f"""
        SELECT symbol, count() AS cnt
        FROM trading.features_1m
        WHERE symbol IN ({sym_list})
          AND ts >= now() - INTERVAL {days_int} DAY
        GROUP BY symbol
        ORDER BY symbol
        """
        rows_feat = ch.query(sql_feat)
        print("[DEBUG] features_1m counts:")
        for r in rows_feat:
            print(f"  {r['symbol']}: {r['cnt']}")
        if not rows_feat:
            print("  (no rows)")
    except Exception as e:  # pragma: no cover - live debug only
        print(f"[DEBUG] features_1m query failed: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live debug for DataHealer")
    ap.add_argument("--symbols", nargs="+", required=True, help="Symbols to check, e.g. TSLA NVDA")
    ap.add_argument("--days", type=int, default=2, help="Lookback days for coverage checks")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]
    days = int(args.days)

    print("=== DataHealer Live Debug ===")
    print(f"Symbols: {symbols}")
    print(f"Lookback days: {days}")

    # 1) Show ClickHouse config
    ch = ClickHouseDB()
    cfg = getattr(ch, "config", {}) or {}
    safe_cfg = {k: ("***" if "password" in k.lower() else v) for k, v in cfg.items()}
    print("\n[DEBUG] ClickHouse config:")
    for k, v in safe_cfg.items():
        print(f"  {k} = {v}")

    # 2) Print current counts before healing
    _print_counts(ch, symbols, days)

    # 3) Run DataHealer.ensure_data_ready once
    runs_dir = Path("runs") / "ai_trader"
    healer = DataHealer(str(runs_dir))
    print("\n[DEBUG] Calling DataHealer.ensure_data_ready(...) once…")
    ok = healer.ensure_data_ready(symbols, lookback_days=days)
    print(f"[DEBUG] DataHealer.ensure_data_ready returned: {ok}")

    # 4) Re-check counts after healing
    ch2 = ClickHouseDB()  # fresh client to avoid session cache issues
    _print_counts(ch2, symbols, days)

    # 5) Show DataHealer's own view of missing symbols
    missing = healer._check_coverage(symbols, days)
    print(f"\n[DEBUG] DataHealer._check_coverage sees missing: {missing}")


if __name__ == "__main__":  # pragma: no cover - manual debug tool
    main()
