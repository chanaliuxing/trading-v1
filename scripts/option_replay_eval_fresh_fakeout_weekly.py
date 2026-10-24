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
    out_root = root / "backtest_results" / "qqq_real_weekly_fresh_fakeout"
    out_root.mkdir(parents=True, exist_ok=True)

    base_cfg = {
        "market_data_session": "extended",
        "indicator_profile": "standard_close_extended_sma",
    }
    variants = {
        "fakeout_v1": {
            "fresh_momentum_fakeout_filter_enabled": True,
            "fresh_momentum_fakeout_min_break_atr": 0.10,
            "fresh_momentum_fakeout_min_close_break_atr": 0.02,
            "fresh_momentum_fakeout_max_body_frac": 0.35,
            "fresh_momentum_fakeout_min_adverse_wick_frac": 0.30,
            "fresh_momentum_fakeout_min_vol_vs_setup": 1.00,
        },
        "fakeout_v2_milder": {
            "fresh_momentum_fakeout_filter_enabled": True,
            "fresh_momentum_fakeout_min_break_atr": 0.08,
            "fresh_momentum_fakeout_min_close_break_atr": 0.01,
            "fresh_momentum_fakeout_max_body_frac": 0.30,
            "fresh_momentum_fakeout_min_adverse_wick_frac": 0.35,
            "fresh_momentum_fakeout_min_vol_vs_setup": 0.95,
        },
        "fakeout_v3_stronger": {
            "fresh_momentum_fakeout_filter_enabled": True,
            "fresh_momentum_fakeout_min_break_atr": 0.12,
            "fresh_momentum_fakeout_min_close_break_atr": 0.03,
            "fresh_momentum_fakeout_max_body_frac": 0.40,
            "fresh_momentum_fakeout_min_adverse_wick_frac": 0.25,
            "fresh_momentum_fakeout_min_vol_vs_setup": 1.05,
        },
    }
    windows = [
        ("w1_0226_0306", "2026-02-26", "2026-03-07"),
        ("w2_0309_0313", "2026-03-09", "2026-03-14"),
        ("w3_0316_0320", "2026-03-16", "2026-03-21"),
        ("w4_0323_0327", "2026-03-23", "2026-03-28"),
        ("full_0226_0327", "2026-02-26", "2026-03-28"),
        ("today_0327", "2026-03-27", "2026-03-28"),
    ]

    base_cfg_path = out_root / "baseline_cfg.json"
    base_cfg_path.write_text(json.dumps(base_cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    for tag, start, end in windows:
        s = run_case(root, out_root / f"{tag}_baseline", start, end, "ma10_reject_0324", base_cfg_path)
        rows.append(
            {
                "variant": "baseline",
                "window": tag,
                "start": start,
                "end": end,
                "trades": s["trades"],
                "win_rate": s["win_rate"],
                "pnl": s["realized_pnl"],
                "mdd_pct": s["max_drawdown_pct"] * 100,
                "out_dir": str(out_root / f"{tag}_baseline"),
            }
        )

    for variant_name, variant_cfg in variants.items():
        cfg = {**base_cfg, **variant_cfg}
        cfg_path = out_root / f"{variant_name}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        for tag, start, end in windows:
            s = run_case(
                root,
                out_root / f"{tag}_{variant_name}",
                start,
                end,
                "ma10_reject_0324_fresh_momentum_fakeout_exp",
                cfg_path,
            )
            rows.append(
                {
                    "variant": variant_name,
                    "window": tag,
                    "start": start,
                    "end": end,
                    "trades": s["trades"],
                    "win_rate": s["win_rate"],
                    "pnl": s["realized_pnl"],
                    "mdd_pct": s["max_drawdown_pct"] * 100,
                    "out_dir": str(out_root / f"{tag}_{variant_name}"),
                }
            )

    df = pd.DataFrame(rows)
    df.to_csv(out_root / "weekly_compare_grid.csv", index=False)

    piv = []
    for window in sorted(df["window"].unique()):
        sub = df[df["window"] == window].copy().sort_values("pnl", ascending=False)
        piv.append(sub)
    ranked = pd.concat(piv, ignore_index=True)
    ranked.to_csv(out_root / "weekly_ranked.csv", index=False)
    print(ranked.to_string(index=False))

    # Find best full-window variant by PnL then MDD.
    full = df[df["window"] == "full_0226_0327"].copy().sort_values(["pnl", "mdd_pct"], ascending=[False, True])
    best_variant = str(full.iloc[0]["variant"])
    (out_root / "best_variant.txt").write_text(best_variant, encoding="utf-8")

    # Export Mar 27 brief trades for baseline and best variant.
    for variant in ["baseline", best_variant]:
        trades_path = out_root / f"today_0327_{variant}" / "trades.csv"
        if not trades_path.exists():
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
        trades[cols].to_csv(out_root / f"today_0327_{variant}_trades_brief.csv", index=False)


if __name__ == "__main__":
    main()

