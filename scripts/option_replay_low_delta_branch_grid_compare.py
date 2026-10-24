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


def trade_details(trades_path: Path) -> dict:
    if not trades_path.exists() or trades_path.stat().st_size <= 1:
        return {
            "entries": 0,
            "win_rate": 0.0,
            "pnl_total": 0.0,
            "avg_pnl": 0.0,
            "reason_pnl": {},
            "worst5": [],
            "best5": [],
        }
    t = pd.read_csv(trades_path)
    if t.empty:
        return {
            "entries": 0,
            "win_rate": 0.0,
            "pnl_total": 0.0,
            "avg_pnl": 0.0,
            "reason_pnl": {},
            "worst5": [],
            "best5": [],
        }
    reason_pnl = (
        t.groupby("reason", dropna=False)["pnl"].sum().sort_values(ascending=True).to_dict()
        if "reason" in t.columns
        else {}
    )
    best5 = (
        t.sort_values("pnl", ascending=False)
        .head(5)[["entry_ts", "exit_ts", "side", "signal_kind", "reason", "pnl"]]
        .to_dict("records")
    )
    worst5 = (
        t.sort_values("pnl", ascending=True)
        .head(5)[["entry_ts", "exit_ts", "side", "signal_kind", "reason", "pnl"]]
        .to_dict("records")
    )
    return {
        "entries": int(len(t)),
        "win_rate": float((t["pnl"] > 0).mean()),
        "pnl_total": float(t["pnl"].sum()),
        "avg_pnl": float(t["pnl"].mean()),
        "reason_pnl": reason_pnl,
        "worst5": worst5,
        "best5": best5,
    }


def main() -> None:
    root = Path(".")
    out_root = root / "backtest_results" / "qqq_low_delta_branch_grid_compare"
    out_root.mkdir(parents=True, exist_ok=True)

    start = "2026-03-01"
    end = "2026-03-28"  # inclusive to Mar-27 because end is exclusive in engine.

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

    # Grid for conditional low-delta branch (only oracle signal kind).
    delta_grid = [
        (0.28, 0.38),
        (0.30, 0.40),
        (0.32, 0.42),
        (0.30, 0.45),
        (0.35, 0.45),
    ]

    rows = []

    # Fixed strategies.
    fixed = [
        ("baseline", "ma10_reject_0324", cfg_base),
        ("layered_softstop", "ma10_reject_0324_trend_cont_put_layered_softstop_exp", cfg_base),
        ("fakeout_v3", "ma10_reject_0324_fresh_momentum_fakeout_exp", cfg_fakeout_v3),
        ("oracle_base", "ma10_reject_0324_oracle0327_fallback_exp", cfg_base),
    ]
    for label, strategy, cfg in fixed:
        cfg_path = out_root / f"{label}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        out_dir = out_root / label
        s = run_case(root, out_dir, start, end, strategy, cfg_path)
        rows.append(
            {
                "label": label,
                "strategy": strategy,
                "low_delta_range": "",
                "trades": s["trades"],
                "win_rate": s["win_rate"],
                "pnl": s["realized_pnl"],
                "mdd_pct": s["max_drawdown_pct"] * 100,
                "skips": s["skipped_signals"],
                "out_dir": str(out_dir),
            }
        )

    # Oracle + conditional low-delta branch grid.
    for dmin, dmax in delta_grid:
        label = f"oracle_lowdelta_{dmin:.2f}_{dmax:.2f}".replace(".", "")
        cfg = {
            **cfg_base,
            "low_delta_experiment_enabled": True,
            "low_delta_signal_kinds_csv": "oracle0327_fallback_put",
            "low_delta_require_trend_regime": True,
            "low_delta_delta_min": dmin,
            "low_delta_delta_max": dmax,
            "low_delta_no_fallback": True,
        }
        cfg_path = out_root / f"{label}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        out_dir = out_root / label
        s = run_case(root, out_dir, start, end, "ma10_reject_0324_oracle0327_fallback_exp", cfg_path)
        rows.append(
            {
                "label": label,
                "strategy": "ma10_reject_0324_oracle0327_fallback_exp",
                "low_delta_range": f"{dmin:.2f}-{dmax:.2f}",
                "trades": s["trades"],
                "win_rate": s["win_rate"],
                "pnl": s["realized_pnl"],
                "mdd_pct": s["max_drawdown_pct"] * 100,
                "skips": s["skipped_signals"],
                "out_dir": str(out_dir),
            }
        )

    df = pd.DataFrame(rows)
    df.to_csv(out_root / "summary_compare.csv", index=False)

    grid = df[df["label"].str.startswith("oracle_lowdelta_")].copy()
    grid = grid.sort_values(["pnl", "win_rate", "mdd_pct"], ascending=[False, False, True])
    grid.to_csv(out_root / "oracle_lowdelta_grid_ranked.csv", index=False)
    best = grid.iloc[0]
    best_label = str(best["label"])
    (out_root / "best_lowdelta_label.txt").write_text(best_label, encoding="utf-8")

    # Final strategy set for detailed compare.
    final_labels = ["baseline", "layered_softstop", "fakeout_v3", "oracle_base", best_label]
    final = df[df["label"].isin(final_labels)].copy().sort_values("pnl", ascending=False)
    final.to_csv(out_root / "final_strategy_compare.csv", index=False)

    details = {}
    for _, r in final.iterrows():
        label = str(r["label"])
        trades_path = Path(str(r["out_dir"])) / "trades.csv"
        details[label] = trade_details(trades_path)
    (out_root / "final_strategy_pnl_details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("=== FINAL COMPARE ===")
    print(final.to_string(index=False))
    print(f"best_lowdelta={best_label}")


if __name__ == "__main__":
    main()

