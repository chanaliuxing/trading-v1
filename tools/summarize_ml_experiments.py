#!/usr/bin/env python3
from __future__ import annotations
"""
Summarize ML experiment results recorded in ml_experiments.jsonl.

Groups OOF experiments by (window_days, horizon_min, label_bps) and reports:
  - experiment count
  - mean AUC / IC
  - positive-edge count (AUC>=0.52 and |IC|>=0.02)

Usage:
  venv/bin/python tools/summarize_ml_experiments.py --days 7
"""

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Tuple


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days",
        type=int,
        default=7,
        help="回溯最近 N 天的实验 (默认 7)",
    )
    ap.add_argument(
        "--runs-dir",
        default="runs/ai_trader",
        help="runs 根目录 (默认 runs/ai_trader)",
    )
    return ap.parse_args()


def _iter_experiments(runs_dir: Path, days: int):
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    cur = end_date
    while cur >= start_date:
        day_dir = runs_dir / cur.isoformat()
        exp_file = day_dir / "ml_experiments.jsonl"
        if exp_file.exists():
            for line in exp_file.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                yield cur, row
        cur -= timedelta(days=1)


def main() -> None:
    args = _parse_args()
    runs_dir = Path(args.runs_dir)

    print("=" * 70)
    print(f"ML 实验结果汇总 (最近 {args.days} 天)")
    print("=" * 70)

    groups: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    fam_stats: Dict[str, Dict[str, Any]] = {}
    total_oof = 0

    for day, row in _iter_experiments(runs_dir, args.days):
        if str(row.get("job_type") or "").lower() != "oof":
            continue
        metrics = row.get("metrics") or {}
        params = row.get("params") or {}
        # horizon/label/window_days may be at metrics or params level
        horizon = params.get("horizon_min", metrics.get("horizon_min"))
        label_bps = params.get("label_bps")
        window_days = params.get("window_days")
        try:
            h = int(horizon) if horizon is not None else None
            b = int(label_bps) if label_bps is not None else None
            w = int(window_days) if window_days is not None else None
        except Exception:
            continue
        if h is None or b is None or w is None:
            continue
        auc = metrics.get("fusion_auc", metrics.get("auc"))
        ic = metrics.get("fusion_ic", metrics.get("ic"))
        if not isinstance(auc, (int, float)) or not isinstance(ic, (int, float)):
            continue

        key = (w, h, b)
        g = groups.setdefault(
            key,
            {"count": 0, "sum_auc": 0.0, "sum_ic": 0.0, "positive": 0},
        )
        g["count"] += 1
        g["sum_auc"] += float(auc)
        g["sum_ic"] += float(ic)
        if float(auc) >= 0.52 and abs(float(ic)) >= 0.02:
            g["positive"] += 1
        total_oof += 1

        # Feature family mask stats (if present)
        ff = row.get("feature_families") or {}
        enabled_fams = ff.get("enabled") or []
        if isinstance(enabled_fams, list):
            mask = ",".join(sorted(str(x) for x in enabled_fams)) or "default"
            fs = fam_stats.setdefault(mask, {"count": 0})
            fs["count"] += 1

    if not groups:
        print("⚠️  在指定时间窗口内找不到任何带有 horizon_min/label_bps/window_days 的 OOF 实验记录。")
        return

    print(f"\n总 OOF 实验数: {total_oof}")
    print("\n按 (window_days, horizon_min, label_bps) 分组:")
    for (w, h, b), g in sorted(groups.items()):
        mean_auc = g["sum_auc"] / g["count"]
        mean_ic = g["sum_ic"] / g["count"]
        print(
            f"  - window={w}d, horizon={h}m, label_bps={b}: "
            f"n={g['count']}, positive={g['positive']}, "
            f"mean_auc={mean_auc:.3f}, mean_ic={mean_ic:.4f}"
        )

    if fam_stats:
        print("\n按特征家族组合分组 (基于 feature_families.enabled):")
        for mask, stats in sorted(fam_stats.items(), key=lambda kv: kv[0]):
            print(f"  - families={mask}: n={stats['count']}")


if __name__ == "__main__":
    main()
