#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd


def run_case(
    root: Path,
    out_root: Path,
    tag: str,
    start: str,
    end: str,
    strategy: str,
    cfg_path: Path,
    label: str,
) -> dict:
    out_dir = out_root / f"{tag}_{label}"
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
        return {
            "ok": False,
            "out_dir": str(out_dir),
            "stdout": p.stdout,
            "stderr": p.stderr,
        }
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))["summary"]
    return {"ok": True, "summary": summary, "out_dir": str(out_dir)}


def main() -> None:
    root = Path(".")
    out_root = root / "backtest_results" / "qqq_real_weekly_fmq_gate"
    out_root.mkdir(parents=True, exist_ok=True)

    cfg_base = {
        "market_data_session": "extended",
        "indicator_profile": "standard_close_extended_sma",
    }
    cfg_opt = {
        "market_data_session": "extended",
        "indicator_profile": "standard_close_extended_sma",
        "fresh_momentum_confirm_quality_enabled": True,
        "fresh_momentum_confirm_require_close_break": True,
        "fresh_momentum_confirm_min_body_frac": 0.50,
        "fresh_momentum_confirm_min_dir_close_strength": 0.70,
    }
    base_cfg_path = out_root / "cfg_baseline.json"
    opt_cfg_path = out_root / "cfg_fmq_gate.json"
    base_cfg_path.write_text(json.dumps(cfg_base, ensure_ascii=False, indent=2), encoding="utf-8")
    opt_cfg_path.write_text(json.dumps(cfg_opt, ensure_ascii=False, indent=2), encoding="utf-8")

    windows = [
        ("w1_0226_0306", "2026-02-26", "2026-03-06"),
        ("w2_0309_0313", "2026-03-09", "2026-03-13"),
        ("w3_0316_0320", "2026-03-16", "2026-03-20"),
        ("w4_0323_0327", "2026-03-23", "2026-03-27"),
        ("full_0226_0327", "2026-02-26", "2026-03-27"),
        ("today_0327", "2026-03-27", "2026-03-27"),
    ]

    rows = []
    for tag, start, end in windows:
        base = run_case(root, out_root, tag, start, end, "ma10_reject_0324", base_cfg_path, "baseline")
        opt = run_case(
            root,
            out_root,
            tag,
            start,
            end,
            "ma10_reject_0324_fresh_momentum_quality_exp",
            opt_cfg_path,
            "fmq",
        )
        if not base["ok"] or not opt["ok"]:
            raise RuntimeError(f"run failed: {tag}\nbase={base}\nopt={opt}")

        bs = base["summary"]
        os = opt["summary"]
        rows.append(
            {
                "window": tag,
                "start": start,
                "end": end,
                "baseline_trades": bs["trades"],
                "baseline_win_rate": bs["win_rate"],
                "baseline_pnl": bs["realized_pnl"],
                "baseline_mdd_pct": bs["max_drawdown_pct"] * 100,
                "opt_trades": os["trades"],
                "opt_win_rate": os["win_rate"],
                "opt_pnl": os["realized_pnl"],
                "opt_mdd_pct": os["max_drawdown_pct"] * 100,
                "pnl_delta": os["realized_pnl"] - bs["realized_pnl"],
                "mdd_delta_pct": (os["max_drawdown_pct"] - bs["max_drawdown_pct"]) * 100,
                "baseline_out_dir": base["out_dir"],
                "opt_out_dir": opt["out_dir"],
            }
        )

    df = pd.DataFrame(rows)
    df.to_csv(out_root / "weekly_compare.csv", index=False)
    print(df.to_string(index=False))

    for label in ("baseline", "fmq"):
        trades_path = out_root / f"today_0327_{label}" / "trades.csv"
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
        trades[cols].to_csv(out_root / f"today_0327_{label}_trades_brief.csv", index=False)


if __name__ == "__main__":
    main()

