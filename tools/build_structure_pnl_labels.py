"""
tools/build_structure_pnl_labels.py

Build structure-level PnL labels (TP/SL/time-stop outcomes) from options_quotes_1m.
This is Phase 1b: labels align with multi-leg structure outcomes.

Usage:
  venv/bin/python tools/build_structure_pnl_labels.py \
    --start 2025-12-01 --end 2025-12-02 \
    --symbols SPY,QQQ \
    --horizon-min 30 \
    --tp-pct 0.4 --sl-pct 0.35 \
    --dte 5,7,10 \
    --structure-ids SINGLE_CALL_D0_35,VERTICAL_DEBIT_CALL_W5 \
    --replace
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Dict, List, Tuple

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


def _load_structures(structure_ids: List[str]) -> List[dict]:
    client = get_client()
    sql = """
    SELECT
      structure_id,
      structure_type,
      legs_template_json,
      param_defaults_json
    FROM trading.option_structure_catalog
    WHERE enabled = 1
    ORDER BY structure_id
    """
    rows = client.query(sql)
    out = []
    for row in rows:
        if structure_ids and row["structure_id"] not in structure_ids:
            continue
        out.append(
            {
                "structure_id": row["structure_id"],
                "structure_type": row["structure_type"],
                "legs_template": json.loads(row["legs_template_json"] or "[]"),
                "param_defaults": json.loads(row["param_defaults_json"] or "{}"),
            }
        )
    return out


def _fetch_quotes(
    start: str,
    end: str,
    symbols: List[str],
    dte_candidates: List[int],
    rights: List[str],
) -> pd.DataFrame:
    client = get_client()
    sym_list = ",".join(f"'{s}'" for s in symbols)
    dte_filter = f"AND dte IN ({','.join(str(d) for d in dte_candidates)})" if dte_candidates else ""
    right_filter = ""
    if rights:
        right_list = ",".join(f"'{r}'" for r in rights)
        right_filter = f"AND right IN ({right_list})"

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
    rows = client.query(sql, parameters={"start": start, "end": end})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _select_long_legs(df: pd.DataFrame, delta_target: float) -> pd.DataFrame:
    df = df.copy()
    df["abs_delta"] = df["delta"].abs()
    df["delta_diff"] = (df["abs_delta"] - delta_target).abs()
    idx = df.groupby(["underlying", "ts"])["delta_diff"].idxmin()
    return df.loc[idx].drop(columns=["abs_delta", "delta_diff"])


def _group_quotes(quotes: pd.DataFrame) -> Dict[Tuple[str, str, float, str], pd.DataFrame]:
    grouped: Dict[Tuple[str, str, float, str], pd.DataFrame] = {}
    for key, dfc in quotes.groupby(["underlying", "expiry", "strike", "right"]):
        grouped[key] = dfc.sort_values("ts")
    return grouped


def _build_net_debit_path(
    grouped: Dict[Tuple[str, str, float, str], pd.DataFrame],
    legs: List[dict],
    entry_ts: pd.Timestamp,
    horizon_min: int,
) -> pd.DataFrame:
    horizon_end = entry_ts + timedelta(minutes=horizon_min)
    series_list = []
    for idx, leg in enumerate(legs):
        key = (leg["underlying"], leg["expiry"], leg["strike"], leg["right"])
        df_leg = grouped.get(key)
        if df_leg is None:
            return pd.DataFrame()
        window = df_leg[(df_leg["ts"] > entry_ts) & (df_leg["ts"] <= horizon_end)].copy()
        if window.empty:
            return pd.DataFrame()
        price_col = "bid" if leg["side"] == "BUY" else "ask"
        sign = 1.0 if leg["side"] == "BUY" else -1.0
        window = window[["ts", price_col]].rename(columns={price_col: f"p{idx}"})
        window[f"p{idx}"] = window[f"p{idx}"] * sign
        series_list.append(window)

    merged = series_list[0]
    for s in series_list[1:]:
        merged = merged.merge(s, on="ts", how="inner")
        if merged.empty:
            return pd.DataFrame()
    merged["net_debit"] = merged[[c for c in merged.columns if c.startswith("p")]].sum(axis=1)
    return merged[["ts", "net_debit"]]


def _label_structure_path(
    path: pd.DataFrame,
    entry_ts: pd.Timestamp,
    entry_net_debit: float,
    tp_pct: float,
    sl_pct: float,
) -> Tuple[pd.Timestamp, float, int, float, float]:
    if path.empty:
        return entry_ts, entry_net_debit, 0, 0.0, 0.0
    tp_price = entry_net_debit * (1.0 + tp_pct)
    sl_price = entry_net_debit * (1.0 - sl_pct)
    min_net = entry_net_debit

    for _, row in path.iterrows():
        net = float(row["net_debit"])
        min_net = min(min_net, net)
        if net >= tp_price:
            y_rr = (net - entry_net_debit) / entry_net_debit
            y_maxdd = (min_net - entry_net_debit) / entry_net_debit
            return row["ts"], net, 1, y_rr, y_maxdd
        if net <= sl_price:
            y_rr = (net - entry_net_debit) / entry_net_debit
            y_maxdd = (min_net - entry_net_debit) / entry_net_debit
            return row["ts"], net, 0, y_rr, y_maxdd

    last = path.iloc[-1]
    net = float(last["net_debit"])
    y_rr = (net - entry_net_debit) / entry_net_debit
    y_maxdd = (min_net - entry_net_debit) / entry_net_debit
    return last["ts"], net, 0, y_rr, y_maxdd


def build_labels(
    start: str,
    end: str,
    symbols: List[str],
    horizon_min: int,
    tp_pct: float,
    sl_pct: float,
    dte_candidates: List[int],
    structure_ids: List[str],
    replace: bool,
) -> None:
    structures = _load_structures(structure_ids)
    if not structures:
        logger.warning("No enabled structures found for label build")
        return

    end_dt = datetime.strptime(end, "%Y-%m-%d")
    end_plus = (end_dt + timedelta(minutes=horizon_min)).strftime("%Y-%m-%d %H:%M:%S")

    rights = ["C", "P"]
    quotes = _fetch_quotes(start, end_plus, symbols, dte_candidates, rights)
    if quotes.empty:
        logger.warning("No option quotes fetched for structure label build", start=start, end=end)
        return

    quotes["ts"] = pd.to_datetime(quotes["ts"])
    grouped = _group_quotes(quotes)

    if replace:
        client = get_client()
        client.command(
            """
            ALTER TABLE trading.option_structure_labels_1m
            DELETE WHERE ts >= toDateTime(%(start)s, 'America/New_York')
              AND ts <  toDateTime(%(end)s, 'America/New_York')
            """,
            parameters={"start": start, "end": end},
        )

    rows = []
    for s in structures:
        s_type = s["structure_type"]
        params = s["param_defaults"]
        delta_target = float(params.get("delta_target", 0.35))
        width = float(params.get("width", 5.0))
        dte_list = dte_candidates or params.get("dte_candidates", [])

        if s_type in ("SINGLE_CALL", "SINGLE_PUT"):
            right = "C" if s_type == "SINGLE_CALL" else "P"
            df = quotes[quotes["right"] == right]
            if dte_list:
                df = df[df["dte"].isin(dte_list)]
            if df.empty:
                continue
            long_legs = _select_long_legs(df, delta_target)

            for _, leg in long_legs.iterrows():
                entry_ts = leg["ts"]
                entry_net_debit = float(leg["ask"])
                if entry_net_debit <= 0:
                    continue
                legs = [
                    {
                        "underlying": leg["underlying"],
                        "expiry": leg["expiry"],
                        "strike": float(leg["strike"]),
                        "right": leg["right"],
                        "side": "BUY",
                        "qty": 1,
                        "dte": int(leg["dte"]),
                    }
                ]
                path = _build_net_debit_path(grouped, legs, entry_ts, horizon_min)
                legs_json = [
                    {
                        "underlying": leg["underlying"],
                        "expiry": leg["expiry"].isoformat() if hasattr(leg["expiry"], "isoformat") else str(leg["expiry"]),
                        "strike": float(leg["strike"]),
                        "right": leg["right"],
                        "side": "BUY",
                        "qty": 1,
                        "dte": int(leg["dte"]),
                    }
                ]
                exit_ts, exit_net, y_win, y_rr, y_maxdd = _label_structure_path(
                    path, entry_ts, entry_net_debit, tp_pct, sl_pct
                )
                rows.append(
                    (
                        entry_ts,
                        leg["underlying"],
                        s["structure_id"],
                        s_type,
                        json.dumps(legs_json),
                        horizon_min,
                        tp_pct,
                        sl_pct,
                        entry_net_debit,
                        float(exit_net),
                        exit_ts,
                        int(y_win),
                        float(y_rr),
                        float(y_maxdd),
                        datetime.now(),
                    )
                )

        elif s_type in ("VERTICAL_DEBIT_CALL", "VERTICAL_DEBIT_PUT"):
            right = "C" if s_type == "VERTICAL_DEBIT_CALL" else "P"
            strike_offset = float(width) if s_type == "VERTICAL_DEBIT_CALL" else -float(width)

            df = quotes[quotes["right"] == right]
            if dte_list:
                df = df[df["dte"].isin(dte_list)]
            if df.empty:
                continue
            long_legs = _select_long_legs(df, delta_target)
            long_legs["short_strike"] = long_legs["strike"] + strike_offset

            merged = long_legs.merge(
                df,
                left_on=["underlying", "ts", "expiry", "short_strike", "right"],
                right_on=["underlying", "ts", "expiry", "strike", "right"],
                suffixes=("_long", "_short"),
                how="inner",
            )
            if merged.empty:
                continue

            for _, row in merged.iterrows():
                entry_ts = row["ts"]
                entry_net_debit = float(row["ask_long"]) - float(row["bid_short"])
                if entry_net_debit <= 0:
                    continue

                legs = [
                    {
                        "underlying": row["underlying"],
                        "expiry": row["expiry"],
                        "strike": float(row["strike_long"]),
                        "right": row["right"],
                        "side": "BUY",
                        "qty": 1,
                        "dte": int(row["dte_long"]),
                    },
                    {
                        "underlying": row["underlying"],
                        "expiry": row["expiry"],
                        "strike": float(row["strike_short"]),
                        "right": row["right"],
                        "side": "SELL",
                        "qty": 1,
                        "dte": int(row["dte_short"]),
                    },
                ]
                path = _build_net_debit_path(grouped, legs, entry_ts, horizon_min)
                legs_json = [
                    {
                        "underlying": row["underlying"],
                        "expiry": row["expiry"].isoformat() if hasattr(row["expiry"], "isoformat") else str(row["expiry"]),
                        "strike": float(row["strike_long"]),
                        "right": row["right"],
                        "side": "BUY",
                        "qty": 1,
                        "dte": int(row["dte_long"]),
                    },
                    {
                        "underlying": row["underlying"],
                        "expiry": row["expiry"].isoformat() if hasattr(row["expiry"], "isoformat") else str(row["expiry"]),
                        "strike": float(row["strike_short"]),
                        "right": row["right"],
                        "side": "SELL",
                        "qty": 1,
                        "dte": int(row["dte_short"]),
                    },
                ]
                exit_ts, exit_net, y_win, y_rr, y_maxdd = _label_structure_path(
                    path, entry_ts, entry_net_debit, tp_pct, sl_pct
                )
                rows.append(
                    (
                        entry_ts,
                        row["underlying"],
                        s["structure_id"],
                        s_type,
                        json.dumps(legs_json),
                        horizon_min,
                        tp_pct,
                        sl_pct,
                        entry_net_debit,
                        float(exit_net),
                        exit_ts,
                        int(y_win),
                        float(y_rr),
                        float(y_maxdd),
                        datetime.now(),
                    )
                )

    if not rows:
        logger.warning("No structure labels produced")
        return

    client = get_client()
    client.insert(
        "trading.option_structure_labels_1m",
        rows,
        column_names=[
            "ts",
            "underlying",
            "structure_id",
            "structure_type",
            "legs_json",
            "horizon_min",
            "tp_pct",
            "sl_pct",
            "entry_net_debit",
            "exit_net_debit",
            "exit_ts",
            "y_win",
            "y_rr",
            "y_maxdd",
            "created_at",
        ],
    )
    logger.info("Inserted structure PnL labels", rows=len(rows))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--horizon-min", type=int, default=30)
    parser.add_argument("--tp-pct", type=float, default=0.4)
    parser.add_argument("--sl-pct", type=float, default=0.35)
    parser.add_argument("--dte", default="")
    parser.add_argument("--structure-ids", default="")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    symbols = _parse_list(args.symbols)
    dte_candidates = _parse_dte_list(args.dte) if args.dte else []
    structure_ids = _parse_list(args.structure_ids) if args.structure_ids else []

    build_labels(
        start=args.start,
        end=args.end,
        symbols=symbols,
        horizon_min=args.horizon_min,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        dte_candidates=dte_candidates,
        structure_ids=structure_ids,
        replace=args.replace,
    )


if __name__ == "__main__":
    main()
