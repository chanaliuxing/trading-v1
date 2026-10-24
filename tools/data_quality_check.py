#!/usr/bin/env python3
"""
Lightweight data quality/RTH checker.
Queries ClickHouse for 1m bars coverage and RTH compliance over the last N days.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Any


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["TSLA", "NVDA"], help="Symbols to check")
    parser.add_argument("--days", type=int, default=3, help="Lookback days")
    parser.add_argument("--table", default="trading.bars_1m_ibkr", help="Bars table")
    parser.add_argument("--min_coverage", type=float, default=0.5, help="Minimum coverage ratio to be considered healthy")
    parser.add_argument("--max_off_rth_pct", type=float, default=5.0, help="Maximum off-RTH percentage allowed")
    parser.add_argument("--json", action="store_true", help="Output JSON (for programmatic use)")
    args = parser.parse_args()

    try:
        from libs.db.ch_pool import get_client
    except Exception:
        print("ClickHouse client not available")
        return

    ch = get_client()
    start = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d 00:00:00")
    end = datetime.now().strftime("%Y-%m-%d 23:59:59")

    summary: Dict[str, Any] = {"symbols": []}
    for sym in args.symbols:
        try:
            q = f"""
            SELECT
                count() AS total_rows,
                countIf(toHour(ts_end) < 9 OR toHour(ts_end) >= 16) AS off_rth_rows,
                max(ts_end) AS max_ts
            FROM {args.table}
            WHERE symbol = %(sym)s
              AND ts_end >= toDateTime(%(start)s)
              AND ts_end <= toDateTime(%(end)s)
            """
            res = ch.query(q, {"sym": sym, "start": start, "end": end}, settings={"use_query_cache": False})
            if not res:
                summary["symbols"].append({"symbol": sym, "status": "no_data", "total_rows": 0})
                if not args.json:
                    print(f"{sym}: no data in lookback")
                continue
            row = res[0]
            total = int(row.get("total_rows", 0) or 0)
            off_rth = int(row.get("off_rth_rows", 0) or 0)
            max_ts = row.get("max_ts")
            off_pct = (off_rth / total * 100) if total else 0.0
            status = "healthy"
            if total == 0 or (total < args.min_coverage * 390 * args.days):
                status = "low_coverage"
            if off_pct > args.max_off_rth_pct:
                status = "off_rth_excess"
            summary["symbols"].append({
                "symbol": sym,
                "status": status,
                "total_rows": total,
                "off_rth_pct": off_pct,
                "max_ts": max_ts,
            })
            if not args.json:
                print(f"{sym}: status={status} rows={total}, off_rth={off_rth} ({off_pct:.2f}%), max_ts={max_ts}")
        except Exception as e:
            summary["symbols"].append({"symbol": sym, "status": "error", "error": str(e)})
            if not args.json:
                print(f"{sym}: query failed: {e}")
    if args.json:
        import json as _json
        print(_json.dumps(summary, ensure_ascii=False, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o)))


if __name__ == "__main__":
    main()
