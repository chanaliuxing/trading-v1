"""
tools/option_ml_train_and_promote.py

One-click training + promotion:
- Train direction model + structure ranker.
- Compare new direction model vs current active model.
- Promote only if improved by a minimum score.
- Update structure_model_version if improved by R2.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, Tuple

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class ModelMetrics:
    acc_dir: float
    auc_move: float
    coverage: float

    def score(self) -> float:
        return 0.5 * self.auc_move + 0.3 * self.acc_dir + 0.2 * self.coverage


def _default_window(days: int = 60) -> Tuple[str, str]:
    now = datetime.now(ET)
    start = now - timedelta(days=days)
    return start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def _run(cmd: list[str]) -> None:
    logger.info("Running", cmd=" ".join(cmd))
    subprocess.run(cmd, check=True)


def _load_latest_controls(strategy_name: str) -> dict:
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
    if not rows:
        raise RuntimeError(f"No controls found for strategy: {strategy_name}")
    return dict(rows[0])


def _fetch_model_metrics(model_version: str) -> Optional[ModelMetrics]:
    client = get_client()
    rows = client.query(
        """
        SELECT val_accuracy_dir, val_auc_move, val_coverage
        FROM trading.option_ml_models
        WHERE model_version = %(model_version)s
        LIMIT 1
        """,
        parameters={"model_version": model_version},
    )
    if not rows:
        return None
    row = rows[0]
    return ModelMetrics(
        acc_dir=float(row.get("val_accuracy_dir") or 0.0),
        auc_move=float(row.get("val_auc_move") or 0.0),
        coverage=float(row.get("val_coverage") or 0.0),
    )


def _fetch_structure_r2(model_version: str) -> Optional[float]:
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
        return None
    try:
        meta = json.loads(rows[0].get("meta") or "{}")
        metrics = meta.get("metrics") or {}
        return float(metrics.get("r2")) if "r2" in metrics else None
    except Exception:
        return None


def _latest_model(prefix: str, since_ts: str) -> Optional[str]:
    client = get_client()
    rows = client.query(
        """
        SELECT model_version
        FROM trading.option_ml_models
        WHERE model_version LIKE %(prefix)s
          AND created_at >= toDateTime(%(since)s, 'America/New_York')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        parameters={"prefix": f"{prefix}%", "since": since_ts},
    )
    if not rows:
        return None
    return str(rows[0]["model_version"])


def _insert_controls(row: dict) -> None:
    client = get_client()
    client.command(
        """
        INSERT INTO trading.option_ml_controls
        (strategy_name, updated_at, enabled, mode, active_model_version,
         theta_dir, theta_move, max_trade_risk_pct, max_daily_dd_pct,
         max_positions_per_symbol, max_total_positions,
         allowed_structures, structure_param_caps_json, max_leg_count, top_n,
         stop_loss_pct_on_premium, take_profit_pct_on_premium, cooldown_min_after_loss,
         max_trades_per_day, max_consecutive_losses, extra_config)
        VALUES
        (%(strategy_name)s, now('America/New_York'), %(enabled)s, %(mode)s, %(active_model_version)s,
         %(theta_dir)s, %(theta_move)s, %(max_trade_risk_pct)s, %(max_daily_dd_pct)s,
         %(max_positions_per_symbol)s, %(max_total_positions)s,
         %(allowed_structures)s, %(structure_param_caps_json)s, %(max_leg_count)s, %(top_n)s,
         %(stop_loss_pct_on_premium)s, %(take_profit_pct_on_premium)s, %(cooldown_min_after_loss)s,
         %(max_trades_per_day)s, %(max_consecutive_losses)s, %(extra_config)s)
        """,
        parameters=row,
    )


def _update_controls(
    *,
    strategy_name: str,
    mode: str,
    new_model_version: Optional[str],
    new_structure_version: Optional[str],
    enable: Optional[int],
    dry_run: bool,
) -> None:
    row = _load_latest_controls(strategy_name)
    row["mode"] = mode
    if new_model_version:
        row["active_model_version"] = new_model_version
    if enable is not None:
        row["enabled"] = int(enable)

    extra_config = row.get("extra_config") or "{}"
    try:
        extra = json.loads(extra_config)
    except Exception:
        extra = {}
    if new_structure_version:
        extra["structure_model_version"] = new_structure_version
    row["extra_config"] = json.dumps(extra, ensure_ascii=False)

    if dry_run:
        logger.info("Controls promotion dry-run", row=json.dumps(row, ensure_ascii=False))
        return

    _insert_controls(row)
    logger.info(
        "Controls promoted",
        strategy_name=strategy_name,
        model_version=row.get("active_model_version"),
        structure_model_version=extra.get("structure_model_version"),
        mode=mode,
        enabled=row.get("enabled"),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--universe", default="SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA")
    ap.add_argument("--horizon-min", type=int, default=30)
    ap.add_argument("--mode", choices=["paper", "live", "sandbox"], default="paper")
    ap.add_argument("--min-improve", type=float, default=0.01)
    ap.add_argument("--enable", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start, end = args.start, args.end
    if not start or not end:
        start, end = _default_window()

    run_started = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")

    base_args = [
        "--start", start,
        "--end", end,
        "--universe", args.universe,
        "--horizon-min", str(args.horizon_min),
    ]

    py = sys.executable
    _run([py, "ml/train_intraday_long_options.py", *base_args])
    _run([py, "ml/train_structure_ranker.py", *base_args])

    new_dir = _latest_model("options_long_v1_", run_started)
    new_struct = _latest_model("options_struct_v1_", run_started)
    if not new_dir:
        logger.error("No new direction model found; aborting promotion")
        return

    ctrl = _load_latest_controls("intraday_options_long")
    current_dir = str(ctrl.get("active_model_version") or "")
    current_struct = ""
    try:
        extra = json.loads(ctrl.get("extra_config") or "{}")
        current_struct = str(extra.get("structure_model_version") or "")
    except Exception:
        current_struct = ""

    new_metrics = _fetch_model_metrics(new_dir)
    old_metrics = _fetch_model_metrics(current_dir) if current_dir else None
    if not new_metrics:
        logger.error("No metrics for new model; aborting promotion", model_version=new_dir)
        return

    promote_dir = False
    if not old_metrics:
        promote_dir = True
        logger.info("No prior model metrics; promoting new model", model_version=new_dir)
    else:
        new_score = new_metrics.score()
        old_score = old_metrics.score()
        improve = (new_score - old_score) / max(old_score, 1e-9)
        logger.info(
            "Model score comparison",
            new_model=new_dir,
            old_model=current_dir,
            new_score=new_score,
            old_score=old_score,
            improve=improve,
        )
        promote_dir = improve >= args.min_improve

    promote_struct = False
    if new_struct:
        new_r2 = _fetch_structure_r2(new_struct)
        old_r2 = _fetch_structure_r2(current_struct) if current_struct else None
        if new_r2 is not None and (old_r2 is None or new_r2 >= old_r2):
            promote_struct = True
        logger.info(
            "Structure r2 comparison",
            new_model=new_struct,
            old_model=current_struct,
            new_r2=new_r2,
            old_r2=old_r2,
            promote=promote_struct,
        )

    if not promote_dir:
        logger.warning("New model did not beat current; keeping old", new_model=new_dir, old_model=current_dir)
        new_dir = None

    if not promote_struct:
        new_struct = None

    if new_dir or new_struct:
        _update_controls(
            strategy_name="intraday_options_long",
            mode=args.mode,
            new_model_version=new_dir,
            new_structure_version=new_struct,
            enable=args.enable,
            dry_run=args.dry_run,
        )
    else:
        logger.info("No promotion applied")


if __name__ == "__main__":
    main()
