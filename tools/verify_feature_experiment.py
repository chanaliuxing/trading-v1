#!/usr/bin/env python3
"""
Verify ClickHouse feature columns over a small time window.

This is intended as a lightweight integration check for feature engineering:
- Given symbols, feature columns, and a time range, compute coverage and
  basic statistics directly from ClickHouse (no pandas dependency).

Usage:
  venv/bin/python tools/verify_feature_experiment.py \
    --symbols TSLA,NVDA \
    --feature-cols ret_5_atr14_ratio,corr_spy_20 \
    --start 2025-11-20 \
    --end   2025-11-25 \
    --table trading.features_1m \
    --out   runs/ai_trader/feature_checks/ret5_atr14_corr_spy20.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List
import sys


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_sys_path() -> None:
    root = _project_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols, e.g. TSLA,NVDA",
    )
    ap.add_argument(
        "--feature-cols",
        required=True,
        help="Comma-separated feature column names to inspect, e.g. ret_5_atr14_ratio,corr_spy_20",
    )
    ap.add_argument(
        "--start",
        required=True,
        help="Start timestamp (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    ap.add_argument(
        "--end",
        required=True,
        help="End timestamp (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    ap.add_argument(
        "--table",
        default="trading.features_1m",
        help="ClickHouse table to query (default: trading.features_1m)",
    )
    ap.add_argument(
        "--out",
        help="Optional output JSON file; if omitted, print to stdout",
    )
    return ap.parse_args()


def _compute_stats(values: List[float]) -> Dict[str, Any]:
    n = len(values)
    if n == 0:
        return {
            "non_null": 0,
            "non_null_ratio": 0.0,
            "min": None,
            "max": None,
            "mean": None,
            "std": None,
            "all_zero": False,
            "all_constant": False,
        }
    v_min = min(values)
    v_max = max(values)
    s = sum(values)
    mean = s / n
    sq = sum(v * v for v in values)
    var = max(0.0, (sq / n) - (mean * mean)) if n > 1 else 0.0
    std = math.sqrt(var)
    all_zero = all(abs(v) < 1e-15 for v in values)
    all_constant = (n > 0 and v_min == v_max)
    return {
        "non_null": n,
        "non_null_ratio": None,  # filled by caller once total_rows known
        "min": v_min,
        "max": v_max,
        "mean": mean,
        "std": std,
        "all_zero": all_zero,
        "all_constant": all_constant,
    }


def main() -> int:
    # Keep machine-readable JSON on real stdout; redirect incidental library logs
    # (some modules write startup logs to stdout) to stderr.
    orig_stdout = sys.stdout
    sys.stdout = sys.stderr
    _ensure_sys_path()
    args = _parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cols = [c.strip() for c in args.feature_cols.split(",") if c.strip()]
    start = args.start
    end = args.end
    table = args.table

    try:
        from libs.db.ch_pool import get_client
    except Exception as exc:  # pragma: no cover - defensive
        print(f"Failed to import ClickHouse client: {exc}", file=sys.stderr)
        return 1

    cli = get_client()
    results: Dict[str, Dict[str, Any]] = {sym: {} for sym in symbols}

    for sym in symbols:
        for col in cols:
            # Use a simple query selecting only the feature column as v
            sql = f"""
            SELECT {col} AS v
            FROM {table}
            WHERE symbol = {{sym:String}}
              AND ts >= {{start:DateTime}}
              AND ts <  {{end:DateTime}}
            """
            try:
                rows = cli.query(
                    sql,
                    parameters={"sym": sym, "start": start, "end": end},
                )
            except Exception as exc:
                results[sym][col] = {
                    "non_null": 0,
                    "non_null_ratio": 0.0,
                    "min": None,
                    "max": None,
                    "mean": None,
                    "std": None,
                    "all_zero": False,
                    "all_constant": False,
                    "symbol": sym,
                    "feature": col,
                    "total_rows": 0,
                    "error": f"query_failed:{exc}",
                }
                continue

            total_rows = len(rows)
            non_null_values: List[float] = []
            for r in rows:
                v = r.get("v")
                if v is None:
                    continue
                try:
                    fv = float(v)
                except Exception:
                    continue
                non_null_values.append(fv)

            stats = _compute_stats(non_null_values)
            stats["symbol"] = sym
            stats["feature"] = col
            stats["total_rows"] = total_rows
            if total_rows > 0:
                stats["non_null_ratio"] = (
                    float(stats["non_null"]) / float(total_rows)
                )
            else:
                stats["non_null_ratio"] = 0.0
            results[sym][col] = stats

    out_str = json.dumps(results, indent=2, ensure_ascii=False)
    # Restore stdout for final JSON emission.
    sys.stdout = orig_stdout
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_str, encoding="utf-8")
        print(f"Wrote feature check report to {out_path}", file=sys.stderr)
    else:
        print(out_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
