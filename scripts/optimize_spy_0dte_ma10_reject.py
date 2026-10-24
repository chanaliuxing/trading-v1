#!/usr/bin/env python3
from __future__ import annotations

import itertools
import json
import os
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.backtest_spy_0dte_ma10_reject import load_spy_1m, add_features, Backtester


def main() -> None:
    start = "2026-01-01"
    end = "2026-03-05"
    out_dir = Path("backtest_results/spy_0dte_ma10_reject_opt")
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = load_spy_1m(start, end)
    df = add_features(raw)

    base_cfg = {
        "starting_cash": 100000.0,
        "risk_per_trade_pct": 0.005,
        "daily_stop_pct": 0.015,
        "max_trades_per_day": 2,
        "entry_start": "09:40",
        "entry_end": "15:20",
        "vol_gate_threshold": 0.50,
        "reject_wick_ratio_min": 0.35,
        "reject_close_low_ratio_max": 0.35,
        "time_stop_bars": 8,
        "strike_step": 1.0,
    }

    grid = {
        "vol_gate_threshold": [0.45, 0.50, 0.60],
        "time_stop_bars": [6, 8, 10],
        "risk_per_trade_pct": [0.004, 0.005],
        "reject_wick_ratio_min": [0.30, 0.35, 0.40],
        "max_trades_per_day": [2, 3],
    }

    keys = list(grid.keys())
    rows = []

    for vals in itertools.product(*(grid[k] for k in keys)):
        cfg = dict(base_cfg)
        cfg.update(dict(zip(keys, vals)))

        bt = Backtester(df, cfg)
        res = bt.run()["summary"]

        rows.append(
            {
                **dict(zip(keys, vals)),
                "n_trades": res["n_trades"],
                "win_rate": res["win_rate"],
                "realized_pnl": res["realized_pnl"],
                "return_pct": res["return_pct"],
                "max_drawdown_pct": res["max_drawdown_pct"],
                "avg_trade_pnl": res["avg_trade_pnl"],
            }
        )

    out = pd.DataFrame(rows)
    out = out.sort_values(["realized_pnl", "max_drawdown_pct"], ascending=[False, True]).reset_index(drop=True)
    out.to_csv(out_dir / "grid_results.csv", index=False)

    # Conservative selection: maximize pnl with MDD <= 1.0%
    filt = out[out["max_drawdown_pct"] <= 0.01]
    best = (filt if not filt.empty else out).iloc[0].to_dict()

    best_cfg = {k: best[k] for k in base_cfg.keys() if k in best}
    best_cfg.update({k: base_cfg[k] for k in base_cfg.keys() if k not in best_cfg})

    (out_dir / "best_config.json").write_text(json.dumps(best_cfg, ensure_ascii=True, indent=2), encoding="utf-8")
    (out_dir / "best_row.json").write_text(json.dumps(best, ensure_ascii=True, indent=2), encoding="utf-8")

    print("best", json.dumps(best, ensure_ascii=True))


if __name__ == "__main__":
    main()
