"""
tools/update_option_ml_models_perf.py

Aggregate option_intraday_perf and write summary into option_ml_models.meta.

Usage:
  venv/bin/python tools/update_option_ml_models_perf.py \
    --start 2025-12-01 --end 2026-01-01 \
    --backfill-only
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
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


def _fetch_perf(start: str | None, end: str | None, backfill_only: bool) -> pd.DataFrame:
    client = get_client()
    where = []
    params: Dict[str, object] = {}
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
    SELECT model_version, entry_ts, net_pnl
    FROM trading.option_intraday_perf
    {where_sql}
    """
    rows = client.query(sql, parameters=params)
    return pd.DataFrame(rows)


def _summarize(df: pd.DataFrame) -> dict[str, dict]:
    if df.empty:
        return {}
    summaries = {}
    for model_version, dfg in df.groupby("model_version"):
        dfg = dfg.sort_values("entry_ts")
        pnl = dfg["net_pnl"].fillna(0.0).to_numpy()
        cum = pnl.cumsum()
        peak = pd.Series(cum).cummax().to_numpy()
        dd = peak - cum
        max_dd_abs = float(dd.max()) if len(dd) else 0.0

        summaries[model_version] = {
            "trades": int(len(dfg)),
            "win_rate": float((dfg["net_pnl"] > 0).mean()),
            "avg_pnl": float(dfg["net_pnl"].mean()),
            "pnl_sum": float(dfg["net_pnl"].sum()),
            "max_dd_abs": max_dd_abs,
        }
    return summaries


def _update_model_meta(model_version: str, summary: dict, source: str) -> None:
    client = get_client()
    rows = client.query(
        """
        SELECT meta
        FROM trading.option_ml_models
        WHERE model_version = %(model_version)s
        LIMIT 1
        """,
        parameters={"model_version": model_version},
    )
    if not rows:
        logger.warning("Model version not found; skipping", model_version=model_version)
        return

    meta_raw = rows[0].get("meta") or "{}"
    try:
        meta = json.loads(meta_raw)
        if not isinstance(meta, dict):
            meta = {}
    except json.JSONDecodeError:
        meta = {}

    meta["perf_summary"] = summary
    meta["perf_source"] = source
    meta["perf_updated_at"] = datetime.now().isoformat()

    client.command(
        """
        ALTER TABLE trading.option_ml_models
        UPDATE
          meta = %(meta)s,
          pnl_per_trade = %(pnl_per_trade)s
        WHERE model_version = %(model_version)s
        """,
        parameters={
            "meta": json.dumps(meta),
            "pnl_per_trade": float(summary.get("avg_pnl", 0.0)),
            "model_version": model_version,
        },
    )
    logger.info("Updated model meta", model_version=model_version)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--backfill-only", action="store_true")
    args = parser.parse_args()

    df = _fetch_perf(args.start, args.end, args.backfill_only)
    summaries = _summarize(df)
    if not summaries:
        logger.warning("No perf rows found for update")
        return

    source = "backfill" if args.backfill_only else "all"
    for model_version, summary in summaries.items():
        _update_model_meta(model_version, summary, source)


if __name__ == "__main__":
    main()
