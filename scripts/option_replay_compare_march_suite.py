#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd


def run_case(
    root: Path,
    out_dir: Path,
    start: str,
    end: str,
    strategy: str,
    cfg_path: Path,
) -> dict:
    cmd = [
        "venv/bin/python",
        "scripts/backtest_spy_option_0324.py",
        "--start",
        start,
        "--end",
        end,
        "--symbol",
        "QQQ",
        "--strategy",
        strategy,
        "--quote-mode",
        "real",
        "--warmup-trading-days",
        "5",
        "--config-json",
        str(cfg_path),
        "--out-dir",
        str(out_dir),
    ]
    p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"run failed: {' '.join(cmd)}\nstdout={p.stdout}\nstderr={p.stderr}")
    return json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))["summary"]


def main() -> None:
    root = Path(".")
    out_root = root / "backtest_results" / "qqq_march_suite_compare"
    out_root.mkdir(parents=True, exist_ok=True)

    cfg_base = {
        "market_data_session": "extended",
        "indicator_profile": "standard_close_extended_sma",
    }
    cfg_fakeout_v3 = {
        **cfg_base,
        "fresh_momentum_fakeout_filter_enabled": True,
        "fresh_momentum_fakeout_min_break_atr": 0.12,
        "fresh_momentum_fakeout_min_close_break_atr": 0.03,
        "fresh_momentum_fakeout_max_body_frac": 0.40,
        "fresh_momentum_fakeout_min_adverse_wick_frac": 0.25,
        "fresh_momentum_fakeout_min_vol_vs_setup": 1.05,
    }

    cfg_paths = {
        "baseline": out_root / "cfg_baseline.json",
        "layered_softstop": out_root / "cfg_layered_softstop.json",
        "fakeout_v3": out_root / "cfg_fakeout_v3.json",
        "oracle0327_fallback": out_root / "cfg_oracle0327_fallback.json",
    }
    cfg_paths["baseline"].write_text(json.dumps(cfg_base, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_paths["layered_softstop"].write_text(json.dumps(cfg_base, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_paths["fakeout_v3"].write_text(json.dumps(cfg_fakeout_v3, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_paths["oracle0327_fallback"].write_text(json.dumps(cfg_base, ensure_ascii=False, indent=2), encoding="utf-8")

    strategy_defs = [
        ("baseline", "ma10_reject_0324", "baseline"),
        ("layered_softstop", "ma10_reject_0324_trend_cont_put_layered_softstop_exp", "layered_softstop"),
        ("fakeout_v3", "ma10_reject_0324_fresh_momentum_fakeout_exp", "fakeout_v3"),
        ("oracle0327_fallback", "ma10_reject_0324_oracle0327_fallback_exp", "oracle0327_fallback"),
    ]

    windows = [
        ("same_window_0226_0327", "2026-02-26", "2026-03-28"),
        ("march_full_0301_0327", "2026-03-01", "2026-03-28"),
        ("march_w1_0302_0306", "2026-03-02", "2026-03-07"),
        ("march_w2_0309_0313", "2026-03-09", "2026-03-14"),
        ("march_w3_0316_0320", "2026-03-16", "2026-03-21"),
        ("march_w4_0323_0327", "2026-03-23", "2026-03-28"),
        ("today_0327", "2026-03-27", "2026-03-28"),
    ]

    rows = []
    for window_name, start, end in windows:
        for label, strategy_name, cfg_key in strategy_defs:
            out_dir = out_root / f"{window_name}_{label}"
            s = run_case(root, out_dir, start, end, strategy_name, cfg_paths[cfg_key])
            rows.append(
                {
                    "window": window_name,
                    "start": start,
                    "end": end,
                    "strategy_label": label,
                    "strategy_name": strategy_name,
                    "trades": s["trades"],
                    "win_rate": s["win_rate"],
                    "pnl": s["realized_pnl"],
                    "mdd_pct": s["max_drawdown_pct"] * 100,
                    "skipped_signals": s["skipped_signals"],
                    "out_dir": str(out_dir),
                }
            )

    df = pd.DataFrame(rows)
    df.to_csv(out_root / "suite_compare.csv", index=False)

    # A/B table for request #1: fakeout_v3 vs layered_softstop on same windows.
    ab = df[df["strategy_label"].isin(["fakeout_v3", "layered_softstop"])].copy()
    ab.to_csv(out_root / "ab_fakeout_v3_vs_layered.csv", index=False)

    # Rank each window.
    ranked = df.sort_values(["window", "pnl", "win_rate"], ascending=[True, False, False]).copy()
    ranked.to_csv(out_root / "suite_ranked_by_window.csv", index=False)
    print(ranked.to_string(index=False))

    # Export Mar 27 trade details for all strategies.
    for label, _, _ in strategy_defs:
        trades_path = out_root / f"today_0327_{label}" / "trades.csv"
        if not trades_path.exists():
            continue
        if trades_path.stat().st_size <= 1:
            continue
        trades = pd.read_csv(trades_path)
        cols = [
            c
            for c in [
                "entry_ts",
                "exit_ts",
                "side",
                "signal_kind",
                "reason",
                "pnl",
                "entry_underlying",
                "exit_underlying",
                "qty",
            ]
            if c in trades.columns
        ]
        trades[cols].to_csv(out_root / f"today_0327_{label}_trades_brief.csv", index=False)


if __name__ == "__main__":
    main()
