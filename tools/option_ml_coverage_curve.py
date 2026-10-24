"""
tools/option_ml_coverage_curve.py

Generate a daily coverage vs PnL vs drawdown curve for a model version.

Usage:
  venv/bin/python tools/option_ml_coverage_curve.py \
    --start 2025-12-01 --end 2026-01-01 \
    --model-version options_long_v1_20260106_024626 \
    --output reports/option_ml_coverage_curve.csv
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


def _fetch_perf(start: str, end: str, model_version: str) -> pd.DataFrame:
    client = get_client()
    rows = client.query(
        """
        SELECT model_version, entry_ts, net_pnl
        FROM trading.option_intraday_perf
        WHERE model_version = %(model_version)s
          AND entry_ts >= toDateTime(%(start)s, 'America/New_York')
          AND entry_ts <  toDateTime(%(end)s,   'America/New_York')
        """,
        parameters={"model_version": model_version, "start": start, "end": end},
    )
    return pd.DataFrame(rows)


def _fetch_signals(start: str, end: str, model_version: str) -> pd.DataFrame:
    client = get_client()
    rows = client.query(
        """
        SELECT model_version, ts, action, rank
        FROM trading.option_intraday_signals
        WHERE model_version = %(model_version)s
          AND ts >= toDateTime(%(start)s, 'America/New_York')
          AND ts <  toDateTime(%(end)s,   'America/New_York')
        """,
        parameters={"model_version": model_version, "start": start, "end": end},
    )
    return pd.DataFrame(rows)


def _daily_drawdown(pnl_series: pd.Series) -> float:
    if pnl_series.empty:
        return 0.0
    cum = pnl_series.cumsum()
    peak = cum.cummax()
    dd = peak - cum
    return float(dd.max())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--output", default="reports/option_ml_coverage_curve.csv")
    args = parser.parse_args()

    perf = _fetch_perf(args.start, args.end, args.model_version)
    signals = _fetch_signals(args.start, args.end, args.model_version)

    if perf.empty and signals.empty:
        logger.warning("No perf/signals found for curve", model_version=args.model_version)
        return

    if not perf.empty:
        perf["date"] = pd.to_datetime(perf["entry_ts"]).dt.date
    else:
        perf = pd.DataFrame(columns=["date", "net_pnl"])

    if not signals.empty:
        signals["date"] = pd.to_datetime(signals["ts"]).dt.date
        signals["is_actionable"] = (signals["action"].isin(["BUY_CALL", "BUY_PUT"])) & (signals["rank"] > 0)
    else:
        signals = pd.DataFrame(columns=["date", "is_actionable"])

    rows: Dict[object, dict] = {}
    for date, dfg in perf.groupby("date"):
        rows[date] = {
            "date": date,
            "trades": int(len(dfg)),
            "pnl_sum": float(dfg["net_pnl"].sum()),
            "max_dd_abs": _daily_drawdown(dfg["net_pnl"]),
        }

    for date, dfg in signals.groupby("date"):
        total_signals = int(dfg["is_actionable"].sum())
        if date not in rows:
            rows[date] = {"date": date, "trades": 0, "pnl_sum": 0.0, "max_dd_abs": 0.0}
        trades = rows[date]["trades"]
        rows[date]["signals"] = total_signals
        rows[date]["coverage"] = float(trades / total_signals) if total_signals > 0 else 0.0

    out = pd.DataFrame(sorted(rows.values(), key=lambda x: x["date"]))
    if "signals" not in out.columns:
        out["signals"] = 0
        out["coverage"] = 0.0

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output, index=False)
    logger.info("Wrote coverage curve", path=args.output, rows=len(out))


if __name__ == "__main__":
    main()
