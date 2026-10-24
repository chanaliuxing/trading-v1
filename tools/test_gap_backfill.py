#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gap-aware backfill tester.

用途：
- 分析某个 symbol 在最近 N 天内 1m/bars 的每日覆盖率
- 根据当前 load_hist_multi 的逻辑，模拟哪些 chunk 会被真正回补、哪些会被跳过

示例：
  python tools/test_gap_backfill.py \
    --symbols TSLA AAPL \
    --days 60 \
    --bar-size 1m \
    --threshold 0.98 \
    --chunk-days 1
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
import sys


# 保证 project root 在 sys.path 中，便于导入 libs / tool
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tool.load_hist_multi import (  # type: ignore[import]
    HistoricalDataLoader,
    normalize_bar_size,
    table_name_for_bar,
)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Test gap-aware backfill coverage and chunk plan.")
    ap.add_argument(
        "--symbols",
        nargs="+",
        required=False,
        help="Symbols to inspect (default: config.universe or TSLA).",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=60,
        help="Calendar days window (today backwards, inclusive). Default: 60.",
    )
    ap.add_argument(
        "--bar-size",
        dest="bar_size",
        type=str,
        default="1m",
        help="Bar size, e.g. 1m/5m/1h/1d. Default: 1m.",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.98,
        help="Per-day coverage threshold (0-1) to consider a day complete. Default: 0.98.",
    )
    ap.add_argument(
        "--chunk-days",
        type=int,
        default=1,
        help="Simulated chunk size in days (per IB request). Default: 1.",
    )
    return ap.parse_args()


def _compute_daily_counts(
    loader: HistoricalDataLoader,
    symbol: str,
    table: str,
    start_day: date,
    end_day: date,
) -> Dict[date, int]:
    """Query ClickHouse for bar counts per calendar day in [start_day, end_day]."""
    start_ts = f"{start_day.isoformat()} 00:00:00"
    end_ts = f"{(end_day + timedelta(days=1)).isoformat()} 00:00:00"
    sql = f"""
    SELECT
        toDate(ts_start) AS d,
        count() AS c
    FROM {table}
    WHERE symbol = %(sym)s
      AND ts_start >= toDateTime(%(s)s)
      AND ts_start < toDateTime(%(e)s)
    GROUP BY d
    ORDER BY d
    """
    rows = loader.ch.query(sql, {"sym": symbol, "s": start_ts, "e": end_ts})
    out: Dict[date, int] = {}
    for r in rows:
        d_val = r.get("d")
        if isinstance(d_val, datetime):
            d_key = d_val.date()
        else:
            d_key = d_val  # type: ignore[assignment]
        try:
            c = int(r.get("c", 0) or 0)
        except Exception:
            c = 0
        if isinstance(d_key, date):
            out[d_key] = c
    return out


def _plan_chunks(
    start_day: date,
    end_day: date,
    chunk_days: int,
) -> List[Tuple[date, date]]:
    """Simulate loader 的 chunk 切分逻辑（不考虑 gap，只根据天数切分）。"""
    chunks: List[Tuple[date, date]] = []
    cur = start_day
    while cur <= end_day:
        remaining = (end_day - cur).days + 1
        span = max(1, min(remaining, chunk_days))
        ce = cur + timedelta(days=span - 1)
        chunks.append((cur, ce))
        cur = ce + timedelta(days=1)
    return chunks


def main() -> None:
    args = _parse_args()

    # 初始化 loader（不连接 IB，只用 ClickHouse + 时间信息）
    loader = HistoricalDataLoader(
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=9999,
    )

    try:
        from libs.config import config as _cfg  # type: ignore[import]
        default_universe = [str(s) for s in (_cfg.universe or [])]
    except Exception:
        default_universe = []

    symbols = args.symbols or default_universe or ["TSLA"]

    bar_size_norm = normalize_bar_size(args.bar_size)
    table = table_name_for_bar(bar_size_norm)

    today_local = datetime.now(loader.ib_timezone).date()
    start_day = (today_local - timedelta(days=args.days - 1)) if args.days > 0 else today_local
    end_day = today_local

    print("=" * 80)
    print(f"Gap-aware backfill test")
    print(f"Window: {start_day} ~ {end_day} ({args.days} days)")
    print(f"Bar size: {bar_size_norm} (table={table})")
    print(f"Coverage threshold: {args.threshold}")
    print(f"Simulated chunk_days: {args.chunk_days}")
    print("=" * 80)
    print()

    for sym in symbols:
        sym_u = sym.upper().strip()
        print("-" * 80)
        print(f"Symbol: {sym_u}")

        # 1. 重新跑一遍 loader 内部的 gap 扫描，得到需要补的日期集合
        incomplete = loader._get_incomplete_dates(  # type: ignore[attr-defined]
            symbol=sym_u,
            table=table,
            bar_size=bar_size_norm,
            start_day=start_day,
            end_day=end_day,
            min_coverage=args.threshold,
        )

        # 2. 取每日条数，算 coverage
        counts = _compute_daily_counts(loader, sym_u, table, start_day, end_day)

        print("Per-day coverage:")
        print("  date       bars   expected   coverage   status")
        for i in range((end_day - start_day).days + 1):
            d = start_day + timedelta(days=i)
            expected = loader._expected_bars_for_day(bar_size_norm, d)  # type: ignore[attr-defined]
            # 非交易日（RTH 且周末）就只打印标记
            got = counts.get(d, 0)
            if expected <= 0:
                status = "non-trading"
                cov = 0.0
            else:
                cov = (got / expected) if expected > 0 else 0.0
                if d in incomplete:
                    status = "INCOMPLETE"
                else:
                    status = "ok" if cov >= args.threshold else "low"
            print(f"  {d}  {got:5d}  {expected:8d}  {cov:8.3f}   {status}")

        print()
        print(f"Incomplete trading days (coverage < {args.threshold}):")
        incomplete_sorted = sorted([d for d in incomplete if d >= start_day and d <= end_day])
        if not incomplete_sorted:
            print("  (none – 数据已经完整，启用 --skip-covered-days 时不会发任何 IB 请求)")
        else:
            for d in incomplete_sorted:
                print(f"  {d}")

        # 3. 模拟 chunk 级别的 backfill 计划（与 loader.skip_covered_days 逻辑对齐）
        print()
        print("Chunk plan (what load_hist_multi will do with --skip-covered-days):")
        chunks = _plan_chunks(start_day, end_day, args.chunk_days)
        for cs, ce in chunks:
            days_span = (ce - cs).days + 1
            # 只看交易日
            trading_days = [d for d in (cs + timedelta(days=i) for i in range(days_span)) if not (loader.use_rth and d.weekday() >= 5)]
            if not trading_days:
                action = "SKIP (non-trading only)"
            elif any(d in incomplete for d in trading_days):
                action = "FETCH (has gap)"
            else:
                action = "SKIP (already covered)"
            print(f"  {cs} ~ {ce}  [{days_span} days]  -> {action}")

        print()


if __name__ == "__main__":
    main()

