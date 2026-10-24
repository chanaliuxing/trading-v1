"""
tools/backfill_option_intraday_perf.py

Backfill option_intraday_perf from historical signals and options_quotes_1m.

Usage:
  venv/bin/python tools/backfill_option_intraday_perf.py \
    --start 2025-12-01 --end 2026-01-01 \
    --equity 100000 \
    --replace
"""

from __future__ import annotations

import argparse
from datetime import datetime, date, timedelta
import json
from pathlib import Path
import sys
from typing import List

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
          max_trade_risk_pct
        FROM trading.option_ml_controls
        WHERE strategy_name = %(strategy_name)s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        parameters={"strategy_name": strategy_name},
    )
    if not rows:
        return {"max_trade_risk_pct": 0.005}
    return rows[0]


def _fetch_signals(start: str, end: str) -> List[dict]:
    client = get_client()
    # Fetch signals for a specific range (usually 1 day in batch mode)
    return client.query(
        """
        SELECT *
        FROM trading.option_intraday_signals
        WHERE ts >= toDateTime(%(start)s, 'America/New_York')
          AND ts <  toDateTime(%(end)s,   'America/New_York')
          AND action != 'NO_TRADE'
          AND rank > 0
          AND legs_spec_json != '{}'
        ORDER BY ts ASC
        """,
        parameters={"start": start, "end": end},
    )


def _fetch_quotes_at_ts(underlying: str, ts: datetime, right: str, dte_candidates: List[int]) -> List[dict]:
    client = get_client()
    dte_filter = ""
    if dte_candidates:
        dte_filter = f"AND dte IN ({','.join(str(d) for d in dte_candidates)})"
    # Limit results to prevent massive fetch if data is duplicated or unpartitioned
    sql = f"""
    SELECT expiry, strike, right, dte, bid, ask, mid, delta
    FROM trading.options_quotes_1m
    WHERE underlying = %(underlying)s
      AND ts = %(ts)s
      AND right = %(right)s
      {dte_filter}
      AND bid > 0 AND ask > 0 AND mid > 0
    LIMIT 500
    """
    return client.query(sql, parameters={"underlying": underlying, "ts": ts, "right": right})


def _select_by_delta(quotes: List[dict], delta_target: float) -> dict | None:
    best = None
    best_diff = 1e9
    for row in quotes:
        diff = abs(abs(float(row["delta"])) - delta_target)
        if diff < best_diff:
            best = row
            best_diff = diff
    return best


def _select_by_strike(quotes: List[dict], expiry: date | None, target_strike: float, dte_target: int | None = None) -> dict | None:
    best = None
    best_diff = 1e9
    for row in quotes:
        if expiry is not None and row["expiry"] != expiry:
            continue
        if dte_target is not None and int(row["dte"]) != dte_target:
            continue
        diff = abs(float(row["strike"]) - target_strike)
        if diff < best_diff:
            best = row
            best_diff = diff
    return best


def _estimate_max_loss_per_contract(legs: List[dict], net_debit: float) -> float:
    if net_debit >= 0:
        return float(net_debit)
    credit = abs(float(net_debit))
    widths = []
    for right in ("C", "P"):
        buys = [l for l in legs if l["right"] == right and l["side"] == "BUY"]
        sells = [l for l in legs if l["right"] == right and l["side"] == "SELL"]
        if not buys or not sells:
            continue
        strikes_buy = [float(l["strike"]) for l in buys]
        strikes_sell = [float(l["strike"]) for l in sells]
        width = max(abs(b - s) for b in strikes_buy for s in strikes_sell)
        widths.append(width)
    if widths:
        max_loss = max(widths) - credit
        if max_loss > 0:
            return float(max_loss)
    return float(abs(net_debit))


def _resolve_legs_from_spec(underlying: str, ts: datetime, legs_spec_json: str) -> dict | None:
    if not legs_spec_json or legs_spec_json == "{}":
        return None
    try:
        spec = json.loads(legs_spec_json)
    except json.JSONDecodeError:
        return None

    legs_template = spec.get("legs_template", [])
    params = spec.get("params", {})
    if not legs_template:
        return None

    dte_candidates = params.get("dte_candidates", [])
    delta_target = float(params.get("delta_target", 0.35))

    resolved = []
    anchors: dict[str, dict] = {}

    # First pass: resolve anchor legs
    for leg in legs_template:
        if not leg.get("anchor"):
            continue
        right = leg.get("right")
        side = leg.get("side")
        anchor_id = leg.get("anchor_id") or f"{right}_{side}"
        leg_delta = float(leg.get("delta_target", delta_target))
        dte_target = leg.get("dte_target")
        dte_list = [int(dte_target)] if dte_target is not None else dte_candidates
        quotes = _fetch_quotes_at_ts(underlying, ts, right, dte_list)
        anchor_leg = _select_by_delta(quotes, leg_delta)
        if not anchor_leg:
            return None
        anchor = {
            "underlying": underlying,
            "expiry": anchor_leg["expiry"],
            "strike": float(anchor_leg["strike"]),
            "right": right,
            "side": side,
            "qty": int(leg.get("qty", 1)),
            "dte": int(anchor_leg["dte"]),
            "bid": float(anchor_leg["bid"]),
            "ask": float(anchor_leg["ask"]),
        }
        anchors[anchor_id] = anchor
        resolved.append(anchor)

    if not anchors:
        return None

    # Second pass: resolve non-anchor legs
    for leg in legs_template:
        if leg.get("anchor"):
            continue
        right = leg.get("right")
        side = leg.get("side")
        anchor_ref = leg.get("anchor_ref") or next(iter(anchors.keys()))
        anchor = anchors.get(anchor_ref)
        if not anchor:
            return None

        target_strike = float(anchor["strike"])
        if leg.get("strike_offset") is not None:
            target_strike = float(anchor["strike"]) + float(leg["strike_offset"])

        dte_target = None
        if leg.get("dte_target") is not None:
            dte_target = int(leg["dte_target"])
        elif leg.get("dte_offset") is not None:
            dte_target = int(anchor["dte"]) + int(leg["dte_offset"])

        dte_candidates_leg = [dte_target] if dte_target is not None else dte_candidates
        quotes = _fetch_quotes_at_ts(underlying, ts, right, dte_candidates_leg)
        target_expiry = anchor["expiry"] if dte_target is None else None
        leg_row = _select_by_strike(quotes, target_expiry, target_strike, dte_target)
        if not leg_row:
            return None
        resolved.append(
            {
                "underlying": underlying,
                "expiry": leg_row["expiry"],
                "strike": float(leg_row["strike"]),
                "right": right,
                "side": side,
                "qty": int(leg.get("qty", 1)),
                "dte": int(leg_row["dte"]),
                "bid": float(leg_row["bid"]),
                "ask": float(leg_row["ask"]),
            }
        )

    if not resolved:
        return None

    net_debit = 0.0
    for leg in resolved:
        if leg["side"] == "BUY":
            net_debit += leg["ask"] * leg["qty"]
        else:
            net_debit -= leg["bid"] * leg["qty"]

    return {"legs": resolved, "net_debit": float(net_debit)}


def _compute_size(equity: float, max_trade_risk_pct: float, max_loss_per_contract: float) -> int:
    budget = max_trade_risk_pct * equity
    if max_loss_per_contract <= 0:
        return 0
    return int(budget // (max_loss_per_contract * 100.0))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--equity", type=float, default=100000.0)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    controls = _load_controls()
    max_trade_risk_pct = float(controls.get("max_trade_risk_pct", 0.005))

    if args.replace:
        client = get_client()
        client.command(
            """
            ALTER TABLE trading.option_intraday_perf
            DELETE WHERE entry_ts >= toDateTime(%(start)s, 'America/New_York')
              AND entry_ts <  toDateTime(%(end)s,   'America/New_York')
              AND extra_config LIKE '%%\"backfill\"%%'
            """,
            parameters={"start": args.start, "end": args.end},
        )

    # Convert strings to dates for iteration
    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    
    current_date = start_date
    while current_date < end_date:
        next_date = current_date + timedelta(days=1)
        
        s_str = current_date.strftime("%Y-%m-%d")
        e_str = next_date.strftime("%Y-%m-%d")
        
        logger.info("Processing backfill batch", start=s_str, end=e_str)
        
        signals = _fetch_signals(s_str, e_str)
        if not signals:
            current_date = next_date
            continue

        rows = []
        for s in signals:
            ts = s["ts"]
            underlying = s["symbol"]
            resolved = _resolve_legs_from_spec(underlying, ts, s["legs_spec_json"])
            if not resolved:
                continue

            net_debit = float(resolved["net_debit"])
            max_loss_per_contract = _estimate_max_loss_per_contract(resolved["legs"], net_debit)
            size = _compute_size(args.equity, max_trade_risk_pct, max_loss_per_contract)
            if size <= 0:
                continue

            legs_json = []
            for leg in resolved["legs"]:
                legs_json.append(
                    {
                        "underlying": leg["underlying"],
                        "expiry": leg["expiry"].isoformat() if hasattr(leg["expiry"], "isoformat") else str(leg["expiry"]),
                        "strike": float(leg["strike"]),
                        "right": leg["right"],
                        "side": leg["side"],
                        "qty": int(leg["qty"]),
                        "dte": int(leg["dte"]),
                        "bid": float(leg["bid"]),
                        "ask": float(leg["ask"]),
                    }
                )

            trade_id = f"{underlying}_{ts.strftime('%Y%m%d_%H%M%S_%f')}"
            primary = resolved["legs"][0]
            side = "CALL" if str(primary.get("right", "C")) == "C" else "PUT"
            max_loss = max_loss_per_contract * 100.0 * size

            rows.append(
                (
                    trade_id,
                    f"{underlying}_OPT",
                    underlying,
                    side,
                    primary["expiry"],
                    float(primary["strike"]),
                    primary["right"],
                    int(primary["dte"]),
                    int(size),
                    net_debit,
                    ts,
                    ts,
                    net_debit,
                    net_debit,
                    0.0,
                    0.0,
                    0.0,
                    s["model_version"],
                    float(s["p_up"]),
                    float(s["p_down"]),
                    float(s["p_move"]),
                    int(s["horizon_min"]),
                    max_trade_risk_pct,
                    0.01,
                    "OPEN",
                    json.dumps({"backfill": True}),
                    s.get("structure_id", ""),
                    s.get("structure_type", ""),
                    json.dumps(legs_json),
                    net_debit,
                    0.0,
                    max_loss,
                    0.0,
                    0.0,
                    datetime.now(),
                )
            )

        if rows:
            client = get_client()
            client.insert(
                "trading.option_intraday_perf",
                rows,
                column_names=[
                    "trade_id",
                    "symbol",
                    "underlying",
                    "side",
                    "expiry",
                    "strike",
                    "right",
                    "dte",
                    "contracts",
                    "notional_per_contract",
                    "entry_ts",
                    "exit_ts",
                    "entry_price",
                    "exit_price",
                    "gross_pnl",
                    "net_pnl",
                    "max_drawdown_pct",
                    "model_version",
                    "p_up_entry",
                    "p_down_entry",
                    "p_move_entry",
                    "horizon_min",
                    "max_trade_risk_pct",
                    "max_daily_dd_pct",
                    "exit_reason",
                    "extra_config",
                    "structure_id",
                    "structure_type",
                    "legs_json",
                    "net_debit_entry",
                    "net_debit_exit",
                    "max_loss_defined",
                    "fees",
                    "slippage_est",
                    "created_at",
                ],
            )
            logger.info("Backfilled option_intraday_perf batch", date=s_str, rows=len(rows))
        
        current_date = next_date



if __name__ == "__main__":
    main()
