#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd


def run(
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
        raise RuntimeError(p.stderr or p.stdout)
    return json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))["summary"]


def main() -> None:
    root = Path(".")
    out_root = root / "backtest_results" / "qqq_real_weekly_fmq_gate_grid"
    out_root.mkdir(parents=True, exist_ok=True)

    base = {
        "market_data_session": "extended",
        "indicator_profile": "standard_close_extended_sma",
    }
    variants = {
        "fmq_a_relaxed": {
            "fresh_momentum_confirm_quality_enabled": True,
            "fresh_momentum_confirm_require_close_break": False,
            "fresh_momentum_confirm_min_body_frac": 0.45,
            "fresh_momentum_confirm_min_dir_close_strength": 0.60,
        },
        "fmq_b_mid": {
            "fresh_momentum_confirm_quality_enabled": True,
            "fresh_momentum_confirm_require_close_break": False,
            "fresh_momentum_confirm_min_body_frac": 0.50,
            "fresh_momentum_confirm_min_dir_close_strength": 0.65,
        },
        "fmq_c_strict_closebreak": {
            "fresh_momentum_confirm_quality_enabled": True,
            "fresh_momentum_confirm_require_close_break": True,
            "fresh_momentum_confirm_min_body_frac": 0.50,
            "fresh_momentum_confirm_min_dir_close_strength": 0.70,
        },
    }
    windows = [
        ("full", "2026-02-26", "2026-03-27"),
        ("w4", "2026-03-23", "2026-03-27"),
        ("today", "2026-03-27", "2026-03-27"),
    ]

    rows = []
    base_cfg_path = out_root / "baseline_cfg.json"
    base_cfg_path.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")

    for w, s, e in windows:
        sm = run(
            root,
            out_root / f"baseline_{w}",
            s,
            e,
            "ma10_reject_0324",
            base_cfg_path,
        )
        rows.append(
            {
                "variant": "baseline",
                "window": w,
                "trades": sm["trades"],
                "win_rate": sm["win_rate"],
                "pnl": sm["realized_pnl"],
                "mdd_pct": sm["max_drawdown_pct"] * 100,
            }
        )

    for name, v in variants.items():
        cfg = {**base, **v}
        cfg_path = out_root / f"{name}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        for w, s, e in windows:
            sm = run(
                root,
                out_root / f"{name}_{w}",
                s,
                e,
                "ma10_reject_0324_fresh_momentum_quality_exp",
                cfg_path,
            )
            rows.append(
                {
                    "variant": name,
                    "window": w,
                    "trades": sm["trades"],
                    "win_rate": sm["win_rate"],
                    "pnl": sm["realized_pnl"],
                    "mdd_pct": sm["max_drawdown_pct"] * 100,
                }
            )

    df = pd.DataFrame(rows)
    df.to_csv(out_root / "grid_compare.csv", index=False)
    print(df.sort_values(["window", "pnl"], ascending=[True, False]).to_string(index=False))


if __name__ == "__main__":
    main()

