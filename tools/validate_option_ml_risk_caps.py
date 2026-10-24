"""
tools/validate_option_ml_risk_caps.py

Validate per-trade risk and daily drawdown caps using option_intraday_perf.

Usage:
  venv/bin/python tools/validate_option_ml_risk_caps.py \
    --start 2025-12-01 --end 2026-01-01 \
    --equity 100000 \
    --backfill-only \
    --output reports/option_ml_risk_caps.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Dict

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _fetch_perf(start: str, end: str, backfill_only: bool) -> pd.DataFrame:
    client = get_client()
    where = [
        "entry_ts >= toDateTime(%(start)s, 'America/New_York')",
        "entry_ts <  toDateTime(%(end)s,   'America/New_York')",
    ]
    if backfill_only:
        where.append("extra_config LIKE '%%\"backfill\"%%'")
    sql = f"""
    SELECT
      entry_ts,
      net_pnl,
      max_trade_risk_pct,
      max_daily_dd_pct,
      max_loss_defined
    FROM trading.option_intraday_perf
    WHERE {" AND ".join(where)}
    """
    rows = client.query(sql, parameters={"start": start, "end": end})
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--equity", type=float, default=100000.0)
    parser.add_argument("--backfill-only", action="store_true")
    parser.add_argument("--output", default="reports/option_ml_risk_caps.csv")
    args = parser.parse_args()

    df = _fetch_perf(args.start, args.end, args.backfill_only)
    if df.empty:
        logger.warning("No perf rows found for validation", start=args.start, end=args.end)
        return

    df["date"] = pd.to_datetime(df["entry_ts"]).dt.date
    df["allowed_trade_loss"] = df["max_trade_risk_pct"].fillna(0.0) * args.equity
    df["trade_violation"] = df["max_loss_defined"] > df["allowed_trade_loss"] + 1e-6

    daily = []
    for date, dfg in df.groupby("date"):
        pnl = dfg["net_pnl"].fillna(0.0).cumsum()
        peak = pnl.cummax()
        dd = (peak - pnl).max()
        max_daily_dd_pct = float(dfg["max_daily_dd_pct"].fillna(0.0).max())
        allowed_dd = args.equity * max_daily_dd_pct
        daily.append(
            {
                "date": date,
                "trades": int(len(dfg)),
                "daily_pnl_sum": float(dfg["net_pnl"].sum()),
                "max_dd_abs": float(dd),
                "allowed_dd_abs": float(allowed_dd),
                "dd_violation": float(dd) > float(allowed_dd) + 1e-6,
                "trade_violations": int(dfg["trade_violation"].sum()),
            }
        )

    out = pd.DataFrame(daily).sort_values("date")
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output, index=False)
    logger.info("Wrote risk cap validation", path=args.output, rows=len(out))


if __name__ == "__main__":
    main()
