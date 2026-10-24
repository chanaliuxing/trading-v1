"""
tools/build_option_pnl_labels.py

Build option PnL labels (TP/SL/time-stop) from options_quotes_1m.
This is Phase 1b: labels align with actual option trade outcomes.

Usage:
  venv/bin/python tools/build_option_pnl_labels.py \
    --start 2025-12-01 --end 2025-12-02 \
    --symbols SPY,QQQ \
    --horizon-min 30 \
    --tp-pct 0.4 --sl-pct 0.35 \
    --dte 5,7,10 \
    --delta-target 0.35 \
    --right BOTH \
    --replace
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys
from typing import List, Dict, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _parse_list(s: str) -> List[str]:
    return [x.strip().upper() for x in s.split(",") if x.strip()]


def _parse_dte_list(s: str) -> List[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def _fetch_quotes(start: str, end: str, symbols: List[str], dte_candidates: List[int], right: str | None) -> pd.DataFrame:
    client = get_client()
    sym_list = ",".join(f"'{s}'" for s in symbols)
    dte_filter = f"AND dte IN ({','.join(str(d) for d in dte_candidates)})" if dte_candidates else ""
    right_filter = "AND right = {right:String}" if right in ("C", "P") else ""

    sql = f"""
    SELECT
      ts,
      underlying,
      expiry,
      strike,
      right,
      dte,
      bid,
      ask,
      mid,
      delta
    FROM trading.options_quotes_1m
    WHERE underlying IN ({sym_list})
      AND ts >= toDateTime(%(start)s, 'America/New_York')
      AND ts <  toDateTime(%(end)s,   'America/New_York')
      {dte_filter}
      {right_filter}
      AND bid > 0 AND ask > 0 AND mid > 0
    ORDER BY underlying, ts
    """
    params = {"start": start, "end": end}
    if right in ("C", "P"):
        params["right"] = right
    rows = client.query(sql, parameters=params)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _select_contracts_at_ts(df: pd.DataFrame, delta_target: float) -> pd.DataFrame:
    """
    Select one contract per (underlying, right, ts) using delta proximity.
    Uses abs(delta) distance to target for both calls/puts.
    """
    df = df.copy()
    df["abs_delta"] = df["delta"].abs()
    df["delta_diff"] = (df["abs_delta"] - delta_target).abs()

    idx = df.groupby(["underlying", "right", "ts"])["delta_diff"].idxmin()
    return df.loc[idx].drop(columns=["abs_delta", "delta_diff"])


def _label_contract_path(
    quotes: pd.DataFrame,
    entry_ts: pd.Timestamp,
    horizon: int,
    tp_pct: float,
    sl_pct: float,
    entry_ask: float,
) -> Tuple[pd.Timestamp, float, int, float]:
    """
    Walk forward within horizon, checking TP/SL on bid.
    Returns: exit_ts, exit_bid, y_win, y_rr
    """
    horizon_end = entry_ts + timedelta(minutes=horizon)
    window = quotes[(quotes["ts"] > entry_ts) & (quotes["ts"] <= horizon_end)]

    tp_price = entry_ask * (1.0 + tp_pct)
    sl_price = entry_ask * (1.0 - sl_pct)

    for _, row in window.iterrows():
        bid = float(row["bid"])
        if bid >= tp_price:
            y_rr = (bid - entry_ask) / entry_ask
            return row["ts"], bid, 1, y_rr
        if bid <= sl_price:
            y_rr = (bid - entry_ask) / entry_ask
            return row["ts"], bid, 0, y_rr

    # Time stop
    if not window.empty:
        last = window.iloc[-1]
        bid = float(last["bid"])
        y_rr = (bid - entry_ask) / entry_ask
        return last["ts"], bid, 0, y_rr

    # No future quotes; fallback to entry
    return entry_ts, entry_ask, 0, 0.0


def build_labels(
    start: str,
    end: str,
    symbols: List[str],
    horizon: int,
    tp_pct: float,
    sl_pct: float,
    dte_candidates: List[int],
    delta_target: float,
    right: str | None,
    replace: bool,
) -> None:
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    end_plus = (end_dt + timedelta(minutes=horizon)).strftime("%Y-%m-%d %H:%M:%S")

    quotes = _fetch_quotes(start, end_plus, symbols, dte_candidates, right)
    if quotes.empty:
        logger.warning("No option quotes fetched for label build", start=start, end=end)
        return

    quotes["ts"] = pd.to_datetime(quotes["ts"])

    if replace:
        client = get_client()
        client.command(
            """
            ALTER TABLE trading.options_pnl_labels_1m
            DELETE WHERE ts >= toDateTime(%(start)s, 'America/New_York')
              AND ts <  toDateTime(%(end)s, 'America/New_York')
            """,
            parameters={"start": start, "end": end},
        )

    selected = _select_contracts_at_ts(quotes, delta_target)

    # Group quotes by contract for fast lookup
    grouped: Dict[Tuple[str, str, float, str], pd.DataFrame] = {}
    for key, dfc in quotes.groupby(["underlying", "expiry", "strike", "right"]):
        grouped[key] = dfc.sort_values("ts")

    rows = []
    for _, row in selected.iterrows():
        key = (row["underlying"], row["expiry"], row["strike"], row["right"])
        contract_quotes = grouped.get(key)
        if contract_quotes is None:
            continue

        entry_ts = row["ts"]
        entry_ask = float(row["ask"])
        exit_ts, exit_bid, y_win, y_rr = _label_contract_path(
            contract_quotes, entry_ts, horizon, tp_pct, sl_pct, entry_ask
        )

        rows.append(
            (
                entry_ts,
                row["underlying"],
                row["expiry"],
                float(row["strike"]),
                row["right"],
                int(row["dte"]),
                horizon,
                tp_pct,
                sl_pct,
                float(entry_ask),
                float(row["mid"]),
                float(exit_bid),
                exit_ts,
                int(y_win),
                float(y_rr),
                datetime.now(),
            )
        )

    if not rows:
        logger.warning("No labels produced from quotes")
        return

    client = get_client()
    client.insert(
        "trading.options_pnl_labels_1m",
        rows,
        column_names=[
            "ts",
            "underlying",
            "expiry",
            "strike",
            "right",
            "dte",
            "horizon_min",
            "tp_pct",
            "sl_pct",
            "entry_ask",
            "entry_mid",
            "exit_bid",
            "exit_ts",
            "y_win",
            "y_rr",
            "created_at",
        ],
    )
    logger.info("Inserted option PnL labels", rows=len(rows))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", default="SPY,QQQ")
    parser.add_argument("--horizon-min", type=int, default=30)
    parser.add_argument("--tp-pct", type=float, default=0.4)
    parser.add_argument("--sl-pct", type=float, default=0.35)
    parser.add_argument("--dte", default="5,7,10")
    parser.add_argument("--delta-target", type=float, default=0.35)
    parser.add_argument("--right", default="BOTH")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    symbols = _parse_list(args.symbols)
    dte_candidates = _parse_dte_list(args.dte)
    right = args.right if args.right in ("C", "P") else None

    build_labels(
        start=args.start,
        end=args.end,
        symbols=symbols,
        horizon=args.horizon_min,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        dte_candidates=dte_candidates,
        delta_target=args.delta_target,
        right=right,
        replace=args.replace,
    )


if __name__ == "__main__":
    main()

