#!/usr/bin/env python3
"""
QQQ put spread data check + decision preview.

Purposes:
- Verify ClickHouse connectivity and availability of QQQ bars/daily data.
- Compute MarketSnapshot (price, pct_change_today, MA50/100/200).
- Run rule-based qqq_put_spread.suggest to preview strikes/widths.

Usage (example):
    PYTHONPATH=. python scripts/options/qqq_put_spread_check.py \
      --bars-table market.bars_1m --daily-table market.bars_1d --symbol QQQ

Notes:
- Bars/daily table names are configurable; queries are conservative (LIMIT).
- Trend heuristic: pct_change_today >=0 => up, else down; down_fast if pct_change_today<=-3% and price < min(ma50, ma200)*0.98.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, date
from typing import Dict, Any, Optional, Tuple

try:
    from libs.db.ch_pool import get_client as get_ch_client  # type: ignore
except Exception:
    get_ch_client = None

from strategies.options.qqq_put_spread import suggest as qqq_suggest


def _fetch_snapshot(
    symbol: str,
    bars_table: str,
    daily_table: Optional[str] = None,
    client=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch latest price/pct_change and moving averages from ClickHouse."""
    if client is None:
        return None, "clickhouse_client_missing"
    try:
        # Latest 1m price and today's close
        q = f"""
        SELECT
          toDate(time) AS d,
          anyLast(close) AS last_close,
          anyLast(open) AS last_open
        FROM {bars_table}
        WHERE symbol = %(sym)s
        GROUP BY d
        ORDER BY d DESC
        LIMIT 2
        """
        rows = client.query(q, parameters={"sym": symbol}).result_rows
        if not rows:
            return None, "no_bars_found"
        today = rows[0]
        last_close = float(today[1])
        last_open = float(today[2]) if today[2] is not None else last_close
        pct_change = (last_close - last_open) / last_open if last_open else 0.0
    except Exception as exc:
        return None, f"bars_query_failed: {exc}"

    ma50 = ma100 = ma200 = None
    if daily_table:
        try:
            q_ma = f"""
            SELECT
              avg(close) OVER (ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS ma50,
              avg(close) OVER (ROWS BETWEEN 99 PRECEDING AND CURRENT ROW) AS ma100,
              avg(close) OVER (ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS ma200
            FROM (
              SELECT close
              FROM {daily_table}
              WHERE symbol = %(sym)s
              ORDER BY time DESC
              LIMIT 200
            )
            ORDER BY ma50 DESC
            LIMIT 1
            """
            res = client.query(q_ma, parameters={"sym": symbol}).result_rows
            if res:
                ma50, ma100, ma200 = [float(x) if x is not None else None for x in res[0]]
        except Exception as exc:
            return None, f"ma_query_failed: {exc}"

    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "price": last_close,
        "pct_change_today": pct_change,
        "ma50": ma50,
        "ma100": ma100,
        "ma200": ma200,
    }
    return snap, None


def _infer_trend(snapshot: Dict[str, Any]) -> str:
    pct = snapshot.get("pct_change_today") or 0.0
    price = snapshot.get("price") or 0.0
    ma50 = snapshot.get("ma50") or price
    ma200 = snapshot.get("ma200") or price
    if pct <= -0.03 and price < min(ma50, ma200) * 0.98:
        return "down_fast"
    return "up" if pct >= 0 else "down"


def main() -> None:
    ap = argparse.ArgumentParser(description="Check QQQ data availability and preview put spread decision.")
    ap.add_argument("--symbol", default="QQQ", help="Symbol to check (default QQQ)")
    ap.add_argument("--bars-table", required=True, help="ClickHouse bars table (e.g., market.bars_1m)")
    ap.add_argument("--daily-table", help="ClickHouse daily bars table for MAs (optional)")
    args = ap.parse_args()

    client = None
    if get_ch_client:
        try:
            client = get_ch_client()
        except Exception as exc:
            print(json.dumps({"status": "error", "error": f"clickhouse_connect_failed: {exc}"}))
            return

    snap, err = _fetch_snapshot(args.symbol, args.bars_table, args.daily_table, client=client)
    if err or not snap:
        print(json.dumps({"status": "error", "error": err or "unknown"}))
        return

    trend_state = _infer_trend(snap)
    dec = qqq_suggest(
        price=float(snap["price"]),
        ma50=float(snap["ma50"] or snap["price"]),
        ma100=float(snap["ma100"] or snap["price"]),
        ma200=float(snap["ma200"] or snap["price"]),
        trend="up" if trend_state == "up" else "down",
        time_et="10:00",
        long_leg_quotes={},
    )

    out = {
        "status": "ok",
        "market_snapshot": snap,
        "trend_state": trend_state,
        "spread_decision": dec.to_dict(),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
