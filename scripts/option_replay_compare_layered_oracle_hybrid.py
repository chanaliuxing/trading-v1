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


def summarize_trades(path: Path) -> dict:
    if not path.exists() or path.stat().st_size <= 1:
        return {
            "entries": 0,
            "win_rate": 0.0,
            "pnl_total": 0.0,
            "avg_pnl": 0.0,
            "reason_pnl": {},
        }
    t = pd.read_csv(path)
    if t.empty:
        return {
            "entries": 0,
            "win_rate": 0.0,
            "pnl_total": 0.0,
            "avg_pnl": 0.0,
            "reason_pnl": {},
        }
    reason_pnl = t.groupby("reason", dropna=False)["pnl"].sum().to_dict() if "reason" in t.columns else {}
    return {
        "entries": int(len(t)),
        "win_rate": float((t["pnl"] > 0).mean()),
        "pnl_total": float(t["pnl"].sum()),
        "avg_pnl": float(t["pnl"].mean()),
        "reason_pnl": reason_pnl,
    }


def main() -> None:
    root = Path(".")
    out_root = root / "backtest_results" / "qqq_layered_oracle_hybrid_compare"
    out_root.mkdir(parents=True, exist_ok=True)

    start = "2026-03-01"
    end = "2026-03-28"  # include 2026-03-27

    cfg_base = {
        "market_data_session": "extended",
        "indicator_profile": "standard_close_extended_sma",
    }
    cfg_layered_path = out_root / "cfg_layered.json"
    cfg_layered_path.write_text(json.dumps(cfg_base, ensure_ascii=False, indent=2), encoding="utf-8")

    # Fixed layered baseline.
    layered_summary = run_case(
        root,
        out_root / "layered",
        start,
        end,
        "ma10_reject_0324_trend_cont_put_layered_softstop_exp",
        cfg_layered_path,
    )

    delta_grid = [
        (0.28, 0.38),
        (0.30, 0.40),
        (0.32, 0.42),
        (0.30, 0.45),
        (0.35, 0.45),
    ]

    def branch_cfg(dmin: float, dmax: float) -> dict:
        return {
            **cfg_base,
            "low_delta_experiment_enabled": True,
            "low_delta_signal_kinds_csv": "oracle0327_fallback_put",
            "low_delta_require_trend_regime": True,
            "low_delta_require_5m_trend_gate": True,
            "low_delta_min_vol_ratio": 1.0,
            "low_delta_delta_min": dmin,
            "low_delta_delta_max": dmax,
            "low_delta_no_fallback": True,
        }

    rows = [
        {
            "family": "layered",
            "label": "layered",
            "strategy": "ma10_reject_0324_trend_cont_put_layered_softstop_exp",
            "delta_range": "",
            "trades": layered_summary["trades"],
            "win_rate": layered_summary["win_rate"],
            "pnl": layered_summary["realized_pnl"],
            "mdd_pct": layered_summary["max_drawdown_pct"] * 100,
            "skips": layered_summary["skipped_signals"],
            "out_dir": str(out_root / "layered"),
        }
    ]

    for fam, strategy in [
        ("oracle_lowdelta", "ma10_reject_0324_oracle0327_fallback_exp"),
        ("hybrid_lowdelta", "ma10_reject_0324_layered_plus_oracle_fallback_exp"),
    ]:
        for dmin, dmax in delta_grid:
            label = f"{fam}_{dmin:.2f}_{dmax:.2f}".replace(".", "")
            cfg = branch_cfg(dmin, dmax)
            cfg_path = out_root / f"{label}.json"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            out_dir = out_root / label
            s = run_case(root, out_dir, start, end, strategy, cfg_path)
            rows.append(
                {
                    "family": fam,
                    "label": label,
                    "strategy": strategy,
                    "delta_range": f"{dmin:.2f}-{dmax:.2f}",
                    "trades": s["trades"],
                    "win_rate": s["win_rate"],
                    "pnl": s["realized_pnl"],
                    "mdd_pct": s["max_drawdown_pct"] * 100,
                    "skips": s["skipped_signals"],
                    "out_dir": str(out_dir),
                }
            )

    df = pd.DataFrame(rows)
    df.to_csv(out_root / "all_results.csv", index=False)

    oracle_best = (
        df[df["family"] == "oracle_lowdelta"]
        .sort_values(["pnl", "win_rate", "mdd_pct"], ascending=[False, False, True])
        .iloc[0]
    )
    hybrid_best = (
        df[df["family"] == "hybrid_lowdelta"]
        .sort_values(["pnl", "win_rate", "mdd_pct"], ascending=[False, False, True])
        .iloc[0]
    )

    final = pd.DataFrame(
        [
            df[df["label"] == "layered"].iloc[0].to_dict(),
            oracle_best.to_dict(),
            hybrid_best.to_dict(),
        ]
    ).sort_values("pnl", ascending=False)
    final.to_csv(out_root / "final_compare.csv", index=False)

    details = {}
    for _, r in final.iterrows():
        details[str(r["label"])] = summarize_trades(Path(str(r["out_dir"])) / "trades.csv")
    (out_root / "final_pnl_breakdown.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")

    # Save best labels.
    (out_root / "best_oracle_label.txt").write_text(str(oracle_best["label"]), encoding="utf-8")
    (out_root / "best_hybrid_label.txt").write_text(str(hybrid_best["label"]), encoding="utf-8")

    print("=== FINAL ===")
    print(final.to_string(index=False))
    print("best_oracle:", oracle_best["label"])
    print("best_hybrid:", hybrid_best["label"])


if __name__ == "__main__":
    main()

