"""
tools/close_option_intraday_trades.py

Close OPEN option_intraday_perf trades using options_quotes_1m and TP/SL/time-stop.

Usage:
  venv/bin/python tools/close_option_intraday_trades.py \
    --max-trades 50
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
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


def _load_controls(strategy_name: str = "intraday_options_long") -> dict:
    client = get_client()
    rows = client.query(
        """
        SELECT
          stop_loss_pct_on_premium,
          take_profit_pct_on_premium
        FROM trading.option_ml_controls
        WHERE strategy_name = %(strategy_name)s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        parameters={"strategy_name": strategy_name},
    )
    if not rows:
        return {"stop_loss_pct_on_premium": 0.35, "take_profit_pct_on_premium": 0.4}
    return rows[0]


def _fetch_open_trades(max_trades: int, start: str | None, end: str | None) -> List[dict]:
    client = get_client()
    where = ["exit_reason = 'OPEN'"]
    params = {"limit": max_trades}
    if start:
        where.append("entry_ts >= toDateTime(%(start)s, 'America/New_York')")
        params["start"] = start
    if end:
        where.append("entry_ts < toDateTime(%(end)s, 'America/New_York')")
        params["end"] = end

    sql = f"""
    SELECT *
    FROM trading.option_intraday_perf
    WHERE {' AND '.join(where)}
    ORDER BY entry_ts ASC
    LIMIT %(limit)s
    """
    return client.query(sql, parameters=params)


def _fetch_leg_quotes(underlying: str, expiry: str, strike: float, right: str, start_ts: datetime, end_ts: datetime) -> pd.DataFrame:
    client = get_client()
    rows = client.query(
        """
        SELECT ts, bid, ask
        FROM trading.options_quotes_1m
        WHERE underlying = %(underlying)s
          AND expiry = %(expiry)s
          AND strike = %(strike)s
          AND right = %(right)s
          AND ts > %(start_ts)s
          AND ts <= %(end_ts)s
        ORDER BY ts
        """,
        parameters={
            "underlying": underlying,
            "expiry": expiry,
            "strike": strike,
            "right": right,
            "start_ts": start_ts,
            "end_ts": end_ts,
        },
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def _build_net_debit_path(legs: List[dict], entry_ts: datetime, horizon_min: int) -> pd.DataFrame:
    end_ts = entry_ts + timedelta(minutes=int(horizon_min))
    series = []
    for idx, leg in enumerate(legs):
        df = _fetch_leg_quotes(
            underlying=leg["underlying"],
            expiry=leg["expiry"],
            strike=float(leg["strike"]),
            right=leg["right"],
            start_ts=entry_ts,
            end_ts=end_ts,
        )
        if df.empty:
            return pd.DataFrame()
        price_col = "bid" if leg["side"] == "BUY" else "ask"
        sign = 1.0 if leg["side"] == "BUY" else -1.0
        df = df[["ts", price_col]].rename(columns={price_col: f"p{idx}"})
        df[f"p{idx}"] = df[f"p{idx}"] * sign * float(leg.get("qty", 1))
        series.append(df)

    merged = series[0]
    for s in series[1:]:
        merged = merged.merge(s, on="ts", how="inner")
        if merged.empty:
            return pd.DataFrame()
    merged["net_debit"] = merged[[c for c in merged.columns if c.startswith("p")]].sum(axis=1)
    return merged[["ts", "net_debit"]]


def _resolve_exit(path: pd.DataFrame, entry_net: float, tp_pct: float, sl_pct: float) -> tuple[datetime, float, str]:
    tp_price = entry_net * (1.0 + tp_pct)
    sl_price = entry_net * (1.0 - sl_pct)

    for _, row in path.iterrows():
        net = float(row["net_debit"])
        if net >= tp_price:
            return row["ts"], net, "TP"
        if net <= sl_price:
            return row["ts"], net, "SL"

    last = path.iloc[-1]
    return last["ts"], float(last["net_debit"]), "TIME_STOP"


def _update_trade(trade_id: str, exit_ts: datetime, exit_net: float, entry_net: float, contracts: int, exit_reason: str) -> None:
    gross_pnl = (exit_net - entry_net) * 100.0 * contracts
    client = get_client()
    client.command(
        """
        ALTER TABLE trading.option_intraday_perf
        UPDATE
          exit_ts = %(exit_ts)s,
          exit_price = %(exit_price)s,
          net_debit_exit = %(net_debit_exit)s,
          gross_pnl = %(gross_pnl)s,
          net_pnl = %(net_pnl)s,
          exit_reason = %(exit_reason)s
        WHERE trade_id = %(trade_id)s
        """,
        parameters={
            "exit_ts": exit_ts,
            "exit_price": exit_net,
            "net_debit_exit": exit_net,
            "gross_pnl": gross_pnl,
            "net_pnl": gross_pnl,
            "exit_reason": exit_reason,
            "trade_id": trade_id,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-trades", type=int, default=50)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    controls = _load_controls()
    tp_pct = float(controls.get("take_profit_pct_on_premium", 0.4))
    sl_pct = float(controls.get("stop_loss_pct_on_premium", 0.35))

    trades = _fetch_open_trades(args.max_trades, args.start, args.end)
    if not trades:
        logger.info("No OPEN trades to close")
        return

    closed = 0
    for t in trades:
        legs = json.loads(t["legs_json"] or "[]")
        if not legs:
            continue
        entry_ts = t["entry_ts"]
        entry_net = float(t["net_debit_entry"] or t["entry_price"])
        horizon_min = int(t["horizon_min"] or 30)

        path = _build_net_debit_path(legs, entry_ts, horizon_min)
        if path.empty:
            logger.warning("No exit quotes found; skipping trade", trade_id=t["trade_id"])
            continue
        exit_ts, exit_net, exit_reason = _resolve_exit(path, entry_net, tp_pct, sl_pct)
        _update_trade(
            trade_id=t["trade_id"],
            exit_ts=exit_ts,
            exit_net=exit_net,
            entry_net=entry_net,
            contracts=int(t["contracts"]),
            exit_reason=exit_reason,
        )
        logger.info("Closed trade", trade_id=t["trade_id"], exit_reason=exit_reason)
        closed += 1

    logger.info("Close job complete", closed=closed, total=len(trades))


if __name__ == "__main__":
    main()
