#!/usr/bin/env python3
"""
Sanity check for ClickHouse trading.options_quotes_1m health (QQQ 0DTE focus).

Usage:
  PYTHONPATH=. ./venv/bin/python scripts/options/check_options_quotes_health.py --underlying QQQ
"""

from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from libs.db.ch_pool import get_client

ET = ZoneInfo("America/New_York")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="QQQ")
    ap.add_argument("--max-age-min", type=float, default=2.0)
    args = ap.parse_args()

    u = str(args.underlying).strip().upper()
    ch = get_client()

    now = datetime.now(ET)
    rows = ch.query(
        """
        SELECT
          max(ts) AS max_ts,
          dateDiff('minute', max(ts), now()) AS age_min,
          countIf(dte=0) AS n_dte0,
          countIf(dte=1) AS n_dte1
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
        """,
        parameters={"u": u},
        settings={"use_query_cache": False},
    )
    if not rows:
        raise SystemExit("[health] no rows")

    max_ts = rows[0].get("max_ts")
    if isinstance(max_ts, datetime):
        max_ts = max_ts.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")
    age_min = float(rows[0].get("age_min") or 9999.0)
    n0 = int(rows[0].get("n_dte0") or 0)
    n1 = int(rows[0].get("n_dte1") or 0)

    print(f"[health] underlying={u} now={now.strftime('%Y-%m-%d %H:%M:%S')} max_ts={max_ts} age_min={age_min:.1f} n_dte0={n0} n_dte1={n1}")

    if age_min > float(args.max_age_min):
        raise SystemExit(f"[health] FAIL stale options_quotes_1m age_min={age_min:.1f} > {args.max_age_min:.1f}")
    if n0 <= 0:
        raise SystemExit("[health] FAIL missing dte=0 rows")

    # Spot/ATM coverage check: verify we can find at least one near-ATM contract with bid/ask/mid>0 for both rights.
    spot_rows = ch.query(
        """
        SELECT anyLast(close) AS spot
        FROM trading.bars_1m_ibkr
        WHERE symbol = {u:String}
          AND ts_start >= now() - INTERVAL 10 MINUTE
          AND is_rth = 1
        """,
        parameters={"u": u},
        settings={"use_query_cache": False},
    )
    spot = float((spot_rows[0] or {}).get("spot") or 0.0) if spot_rows else 0.0
    if spot <= 0:
        print("[health] WARN no recent spot from bars_1m_ibkr; skipping ATM coverage check")
        return

    atm_rows = ch.query(
        """
        SELECT
          right,
          min(abs(strike - {spot:Float64})) AS best_dist,
          argMin(strike, abs(strike - {spot:Float64})) AS best_strike,
          any(mid) AS mid_any,
          any(bid) AS bid_any,
          any(ask) AS ask_any
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
          AND ts = (SELECT max(ts) FROM trading.options_quotes_1m WHERE underlying={u:String})
          AND dte = 0
          AND bid > 0 AND ask > 0 AND mid > 0
        GROUP BY right
        """,
        parameters={"u": u, "spot": spot},
        settings={"use_query_cache": False},
    )
    rights = {str(r.get("right") or "") for r in atm_rows}
    print(f"[health] spot={spot:.2f} rights_ok={sorted(rights)} atm_rows={atm_rows}")
    if "C" not in rights or "P" not in rights:
        raise SystemExit("[health] FAIL missing either CALL or PUT near-ATM quotes for dte=0")

    print("[health] OK")


if __name__ == "__main__":
    main()
