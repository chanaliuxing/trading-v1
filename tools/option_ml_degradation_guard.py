"""
tools/option_ml_degradation_guard.py

Apply degradation rules to option_ml_controls based on recent performance.

Usage:
  venv/bin/python tools/option_ml_degradation_guard.py \
    --equity 100000 \
    --rules config/option_ml_degradation_rules.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
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


def _load_rules(path: str) -> dict:
    data = json.loads(Path(path).read_text())
    return data


def _fetch_perf(start_ts: datetime, model_version: str) -> pd.DataFrame:
    client = get_client()
    rows = client.query(
        """
        SELECT entry_ts, net_pnl
        FROM trading.option_intraday_perf
        WHERE entry_ts >= %(start_ts)s
          AND model_version = %(model_version)s
        """,
        parameters={"start_ts": start_ts, "model_version": model_version},
    )
    return pd.DataFrame(rows)


def _compute_metrics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"trades": 0, "win_rate": 0.0, "pnl_sum": 0.0, "max_dd_abs": 0.0}
    pnl = df["net_pnl"].fillna(0.0).to_numpy()
    cum = pd.Series(pnl).cumsum()
    peak = cum.cummax()
    dd = (peak - cum).max()
    return {
        "trades": int(len(df)),
        "win_rate": float((df["net_pnl"] > 0).mean()),
        "pnl_sum": float(df["net_pnl"].sum()),
        "max_dd_abs": float(dd),
    }


def _load_controls(strategy_name: str) -> dict:
    client = get_client()
    rows = client.query(
        """
        SELECT *
        FROM trading.option_ml_controls
        WHERE strategy_name = %(strategy_name)s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        parameters={"strategy_name": strategy_name},
    )
    return rows[0] if rows else {}


def _insert_controls(base: dict, updates: dict) -> None:
    client = get_client()
    payload = dict(base)
    payload.update(updates)
    client.command(
        """
        INSERT INTO trading.option_ml_controls
        (strategy_name, updated_at, enabled, mode, active_model_version, theta_dir, theta_move,
         max_trade_risk_pct, max_daily_dd_pct, max_positions_per_symbol, max_total_positions,
         stop_loss_pct_on_premium, take_profit_pct_on_premium, cooldown_min_after_loss,
         max_trades_per_day, max_consecutive_losses, extra_config, allowed_structures,
         structure_param_caps_json, max_leg_count, top_n)
        VALUES
        (%(strategy_name)s, now('America/New_York'), %(enabled)s, %(mode)s, %(active_model_version)s, %(theta_dir)s, %(theta_move)s,
         %(max_trade_risk_pct)s, %(max_daily_dd_pct)s, %(max_positions_per_symbol)s, %(max_total_positions)s,
         %(stop_loss_pct_on_premium)s, %(take_profit_pct_on_premium)s, %(cooldown_min_after_loss)s,
         %(max_trades_per_day)s, %(max_consecutive_losses)s, %(extra_config)s, %(allowed_structures)s,
         %(structure_param_caps_json)s, %(max_leg_count)s, %(top_n)s)
        """,
        parameters={
            "strategy_name": payload["strategy_name"],
            "enabled": int(payload["enabled"]),
            "mode": payload["mode"],
            "active_model_version": payload["active_model_version"],
            "theta_dir": float(payload["theta_dir"]),
            "theta_move": float(payload["theta_move"]),
            "max_trade_risk_pct": float(payload["max_trade_risk_pct"]),
            "max_daily_dd_pct": float(payload["max_daily_dd_pct"]),
            "max_positions_per_symbol": int(payload["max_positions_per_symbol"]),
            "max_total_positions": int(payload["max_total_positions"]),
            "stop_loss_pct_on_premium": float(payload["stop_loss_pct_on_premium"]),
            "take_profit_pct_on_premium": float(payload["take_profit_pct_on_premium"]),
            "cooldown_min_after_loss": int(payload["cooldown_min_after_loss"]),
            "max_trades_per_day": int(payload["max_trades_per_day"]),
            "max_consecutive_losses": int(payload["max_consecutive_losses"]),
            "extra_config": payload["extra_config"],
            "allowed_structures": payload["allowed_structures"],
            "structure_param_caps_json": payload["structure_param_caps_json"],
            "max_leg_count": int(payload["max_leg_count"]),
            "top_n": int(payload["top_n"]),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--equity", type=float, default=100000.0)
    parser.add_argument("--rules", default="config/option_ml_degradation_rules.json")
    parser.add_argument("--strategy-name", default="intraday_options_long")
    args = parser.parse_args()

    rules = _load_rules(args.rules)
    base = _load_controls(args.strategy_name)
    if not base:
        logger.warning("No controls found", strategy=args.strategy_name)
        return

    lookback_days = int(rules.get("lookback_days", 5))
    start_ts = datetime.now() - timedelta(days=lookback_days)
    df = _fetch_perf(start_ts, base["active_model_version"])
    metrics = _compute_metrics(df)

    min_trades = int(rules.get("min_trades", 20))
    if metrics["trades"] < min_trades:
        logger.info("Not enough trades for degradation check", trades=metrics["trades"])
        return

    min_win_rate = float(rules.get("min_win_rate", 0.45))
    max_dd_abs_pct = float(rules.get("max_dd_abs_pct", 0.02))
    risk_downscale = float(rules.get("risk_downscale_factor", 0.5))
    min_risk_pct = float(rules.get("min_risk_pct", 0.001))
    severe_mult = float(rules.get("severe_dd_multiplier", 2.0))

    max_dd_abs = metrics["max_dd_abs"]
    dd_limit = args.equity * max_dd_abs_pct
    severe_dd_limit = dd_limit * severe_mult

    degrade = metrics["win_rate"] < min_win_rate or max_dd_abs > dd_limit
    severe = max_dd_abs > severe_dd_limit
    if not degrade:
        logger.info("No degradation action", metrics=metrics)
        return

    new_risk = float(base["max_trade_risk_pct"]) * risk_downscale
    enabled = int(base["enabled"])
    action = "risk_downscale"
    if severe or new_risk < min_risk_pct:
        enabled = 0
        action = "disabled"

    extra = base.get("extra_config") or "{}"
    try:
        extra_obj = json.loads(extra)
        if not isinstance(extra_obj, dict):
            extra_obj = {}
    except json.JSONDecodeError:
        extra_obj = {}

    extra_obj["degradation_action"] = action
    extra_obj["degradation_metrics"] = metrics
    extra_obj["degradation_rules"] = rules
    extra_obj["degradation_at"] = datetime.now().isoformat()

    updates = {
        "enabled": enabled,
        "max_trade_risk_pct": new_risk,
        "extra_config": json.dumps(extra_obj),
    }
    _insert_controls(base, updates)
    logger.info("Applied degradation action", action=action, new_risk=new_risk, metrics=metrics)


if __name__ == "__main__":
    main()
