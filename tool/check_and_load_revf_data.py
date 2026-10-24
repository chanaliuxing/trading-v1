#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RevF data readiness checker (and loader hints)

Checks ClickHouse for required RevF data:
- 1m bars (trading.bars_1m_ibkr)
- 1m features (trading.features_1m) — produced by FeatureCalculator
- SFS 5s sliding (sfs.sfs_5s) — optional for backtest; live writer produces it

If missing/insufficient, prints ready-to-run load commands using tool/load_hist_multi.py.
"""
import argparse
from datetime import datetime, timedelta, timezone
from typing import List

import os
import clickhouse_connect

try:
    from libs.config import config
except Exception:
    class _C:
        def __init__(self):
            self.universe = []
    config = _C()


def _count_rows(ch, table: str, symbol: str, days: int, ts_col: str = "ts") -> int:
    try:
        sql = f"""
        SELECT count() AS c
        FROM {table} FINAL
        WHERE symbol = {{symbol:String}}
          AND {ts_col} >= now() - INTERVAL {{days:Int32}} DAY
        """
        res = ch.query(sql, parameters={"symbol": symbol, "days": days})
        rows = list(res.named_results())
        return int(rows[0]["c"]) if rows else 0
    except Exception:
        return 0


def main(symbols: List[str], days: int) -> None:
    host = os.getenv('CH_HOST', os.getenv('CH_URL','localhost')).replace('http://','').replace('https://','')
    if ':' in host:
        host_only, port_str = host.split(':', 1)
        port = int(os.getenv('CH_PORT', port_str))
    else:
        host_only = host
        port = int(os.getenv('CH_PORT', '8123'))
    database = os.getenv('CH_DB', 'trading')
    user = os.getenv('CH_USER', 'default')
    password = os.getenv('CH_PASSWORD', '')
    ch = clickhouse_connect.get_client(host=host_only, port=port, username=user, password=password, database=database)
    missing_1m: List[str] = []
    missing_feat_1m: List[str] = []
    low_sfs5s: List[str] = []

    bars_1m_tbl = "trading.bars_1m_ibkr"
    feat_1m_tbl = "trading.features_1m"
    sfs_5s_tbl = "sfs.sfs_5s"

    print(f"Checking last {days} days for symbols: {symbols}")
    for sym in symbols:
        c1 = _count_rows(ch, bars_1m_tbl, sym, days, ts_col="ts_start")
        c2 = _count_rows(ch, feat_1m_tbl, sym, days)
        c3 = _count_rows(ch, sfs_5s_tbl, sym, min(3, days))  # sfs_5s is live; check short window
        print(f"  {sym:6s} bars_1m={c1:6d} features_1m={c2:6d} sfs_5s~3d={c3:6d}")
        if c1 < days * 300:  # heuristic: ~300 1m bars/day (RTH)
            missing_1m.append(sym)
        if c2 < days * 300:
            missing_feat_1m.append(sym)
        if c3 < 1:
            low_sfs5s.append(sym)

    print("\nSummary:")
    if missing_1m:
        print("- Missing 1m bars for:", ",".join(missing_1m))
    if missing_feat_1m:
        print("- Missing 1m features for:", ",".join(missing_feat_1m))
    if low_sfs5s:
        print("- No recent sfs_5s (OK if not live):", ",".join(low_sfs5s))

    if missing_1m:
        syms = ",".join(missing_1m)
        print("\nTo backfill 1m bars via IBKR:")
        print(f"  python tool/load_hist_multi.py --symbols {syms} --bar-size 1m --days {days} --fill-mode backfill")

    # FeatureCalculator will generate features_1m from bars_1m_ibkr; ensure service is running
    if missing_feat_1m and not missing_1m:
        print("\n1m bars exist but features_1m are low — ensure FeatureCalculator(1m) is running.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(config.universe or ["AAPL","TSLA","NVDA","QQQ","SPY"]))
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    main(symbols, args.days)
