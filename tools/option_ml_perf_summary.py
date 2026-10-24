"""
tools/option_ml_perf_summary.py

Summarize option ML performance from trading.option_intraday_perf and
optionally write a Markdown report.

Usage:
  venv/bin/python tools/option_ml_perf_summary.py \
    --start 2025-12-01 --end 2026-01-01 \
    --output reports/option_ml_perf_summary.md
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _fetch_perf(start: str | None, end: str | None, backfill_only: bool) -> pd.DataFrame:
    client = get_client()
    where = []
    params = {}
    if start:
        where.append("entry_ts >= toDateTime(%(start)s, 'America/New_York')")
        params["start"] = start
    if end:
        where.append("entry_ts < toDateTime(%(end)s, 'America/New_York')")
        params["end"] = end
    if backfill_only:
        where.append("extra_config LIKE '%%\"backfill\"%%'")
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    sql = f"""
    SELECT
      model_version,
      entry_ts,
      net_pnl,
      exit_reason
    FROM trading.option_intraday_perf
    {where_sql}
    """
    rows = client.query(sql, parameters=params)
    return pd.DataFrame(rows)


def _summarize(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    df["win"] = df["net_pnl"] > 0
    summary = df.groupby("model_version").agg(
        trades=("net_pnl", "count"),
        win_rate=("win", "mean"),
        avg_pnl=("net_pnl", "mean"),
        median_pnl=("net_pnl", "median"),
        pnl_sum=("net_pnl", "sum"),
    ).reset_index()
    return summary


def _write_markdown(path: Path, summary: pd.DataFrame, start: str | None, end: str | None) -> None:
    lines: List[str] = []
    lines.append("# Option ML Performance Summary")
    if start or end:
        lines.append("")
        lines.append(f"Range: {start or 'ALL'} → {end or 'ALL'}")

    lines.append("")
    if summary.empty:
        lines.append("No trades found for the specified range.")
    else:
        lines.append("| model_version | trades | win_rate | avg_pnl | median_pnl | pnl_sum |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for _, row in summary.iterrows():
            lines.append(
                f"| {row['model_version']} | {int(row['trades'])} | "
                f"{row['win_rate']:.3f} | {row['avg_pnl']:.6f} | "
                f"{row['median_pnl']:.6f} | {row['pnl_sum']:.6f} |"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    logger.info("Wrote performance summary", path=str(path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--backfill-only", action="store_true")
    parser.add_argument("--output", default="reports/option_ml_perf_summary.md")
    args = parser.parse_args()

    df = _fetch_perf(args.start, args.end, args.backfill_only)
    summary = _summarize(df)
    _write_markdown(Path(args.output), summary, args.start, args.end)


if __name__ == "__main__":
    main()
