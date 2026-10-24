#!/usr/bin/env python3
from __future__ import annotations
"""
Quick test script for the ML experiment plumbing.

This script does NOT run real HPO/OOF via ClickHouse. Instead it:
  1) Synthesizes a few OOF experiment lines into
     runs/ai_trader/<today>/ml_experiments.jsonl
  2) Prints a local summary by (window_days, horizon_min, label_bps)
  3) Attempts to run the JSONL→ClickHouse importer (ml_experiment_store)
     and reports whether insert succeeded or was skipped
  4) Shows a simple before/after of feature_family_filter on a toy column list

Usage (from repo root, inside venv):
  venv/bin/python tools/test_ml_experiment_pipeline.py
"""

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Ensure project root is on sys.path so that "tools.*" imports work when the
# script is invoked as `python tools/test_ml_experiment_pipeline.py`.
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _runs_dir() -> Path:
    return Path("runs/ai_trader") / date.today().isoformat()


def _write_synthetic_experiments() -> Path:
    """Append a few synthetic OOF experiments for today's date."""
    runs = _runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    exp_file = runs / "ml_experiments.jsonl"
    exp_file.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now().isoformat()
    rows: List[Dict[str, Any]] = []

    # Combo 1: weak/no edge
    rows.append({
        "ts": now,
        "job_id": "test-oof-exp-001",
        "job_type": "oof",
        "params": {"window_days": 30, "horizon_min": 5, "label_bps": 10},
        "metrics": {
            "fusion_auc": 0.51,
            "fusion_ic": 0.0005,
            "coverage": 0.7,
            "n_bars": 5000,
        },
        "feature_families": {
            "enabled": ["price", "volume", "volatility", "time"],
            "disabled": [],
        },
    })

    # Combo 2: positive edge
    rows.append({
        "ts": now,
        "job_id": "test-oof-exp-002",
        "job_type": "oof",
        "params": {"window_days": 60, "horizon_min": 10, "label_bps": 10},
        "metrics": {
            "fusion_auc": 0.55,
            "fusion_ic": 0.025,
            "coverage": 0.8,
            "n_bars": 8000,
        },
        "feature_families": {
            "enabled": ["price", "volume"],
            "disabled": ["volatility", "time"],
        },
    })

    # Combo 3: inverse / weak
    rows.append({
        "ts": now,
        "job_id": "test-oof-exp-003",
        "job_type": "oof",
        "params": {"window_days": 30, "horizon_min": 15, "label_bps": 10},
        "metrics": {
            "fusion_auc": 0.48,
            "fusion_ic": -0.015,
            "coverage": 0.65,
            "n_bars": 4500,
        },
        "feature_families": {
            "enabled": ["price"],
            "disabled": ["volume", "volatility", "time"],
        },
    })

    with exp_file.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[test] Appended {len(rows)} synthetic experiments to {exp_file}")
    return exp_file


def _local_summary(exp_file: Path) -> None:
    """Local grouping by (window_days, horizon_min, label_bps) and family masks."""
    print("\n[test] Local summary from ml_experiments.jsonl:")
    groups: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    fam_stats: Dict[str, int] = defaultdict(int)
    lines = exp_file.read_text("utf-8").splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if (row.get("job_type") or "").lower() != "oof":
            continue
        metrics = row.get("metrics") or {}
        params = row.get("params") or {}
        try:
            w = int(params.get("window_days", metrics.get("window_days")))
            h = int(params.get("horizon_min", metrics.get("horizon_min")))
            b = int(params.get("label_bps", metrics.get("label_bps")))
        except Exception:
            continue
        auc = metrics.get("fusion_auc", metrics.get("auc"))
        ic = metrics.get("fusion_ic", metrics.get("ic"))
        if not isinstance(auc, (int, float)) or not isinstance(ic, (int, float)):
            continue
        key = (w, h, b)
        g = groups.setdefault(key, {"count": 0, "sum_auc": 0.0, "sum_ic": 0.0})
        g["count"] += 1
        g["sum_auc"] += float(auc)
        g["sum_ic"] += float(ic)

        ff = row.get("feature_families") or {}
        enabled = ff.get("enabled") or []
        if isinstance(enabled, list):
            mask = ",".join(sorted(str(x) for x in enabled)) or "default"
            fam_stats[mask] += 1

    if not groups:
        print("  ⚠️ No OOF experiments found in file.")
    else:
        for (w, h, b), g in sorted(groups.items()):
            mean_auc = g["sum_auc"] / g["count"]
            mean_ic = g["sum_ic"] / g["count"]
            print(
                f"  - window={w}d, horizon={h}m, label_bps={b}: "
                f"n={g['count']}, mean_auc={mean_auc:.3f}, mean_ic={mean_ic:.4f}"
            )

    if fam_stats:
        print("\n  Family masks (enabled combinations):")
        for mask, cnt in sorted(fam_stats.items(), key=lambda kv: kv[0]):
            print(f"    • {mask}: n={cnt}")


def _test_feature_family_filter() -> None:
    """Show before/after of feature_family_filter on a toy list."""
    print("\n[test] Feature family filter smoke-test:")
    try:
        from tools.feature_family_filter import filter_feature_columns  # type: ignore
    except Exception as e:
        print(f"  ⚠️ feature_family_filter not importable: {e}")
        return

    cols = ["open", "high", "low", "close", "volume", "day_of_week", "unknown_feature"]
    filtered = filter_feature_columns(cols)
    print(f"  input:   {cols}")
    print(f"  filtered:{filtered}")


def _test_clickhouse_store(days: int = 1) -> None:
    """Attempt to run tools/ml_experiment_store.py main() to insert into ClickHouse."""
    print("\n[test] ClickHouse store (tools/ml_experiment_store.py):")
    try:
        import tools.ml_experiment_store as store  # type: ignore
    except Exception as e:
        print(f"  ⚠️ failed to import ml_experiment_store: {e}")
        return

    try:
        # Temporarily patch sys.argv so store.main() sees our args
        import sys

        argv_backup = list(sys.argv)
        sys.argv = ["ml_experiment_store.py", "--days", str(days)]
        store.main()
        sys.argv = argv_backup
    except SystemExit:
        # main() may call sys.exit; ignore for test purposes
        sys.argv = argv_backup  # type: ignore[name-defined]
    except Exception as e:
        print(f"  ⚠️ ml_experiment_store.main() raised: {repr(e)}")


def main() -> None:
    exp_file = _write_synthetic_experiments()
    _local_summary(exp_file)
    _test_feature_family_filter()
    _test_clickhouse_store(days=1)


if __name__ == "__main__":
    main()
