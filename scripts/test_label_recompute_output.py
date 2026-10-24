#!/usr/bin/env python3
from __future__ import annotations

"""
Utility script to run tools/recompute_labels_1m.py for a short window and sanity-check
the resulting ClickHouse rows (counts, positive/negative distribution, average returns).

Usage:
  python scripts/test_label_recompute_output.py --symbols TSLA --start '2025-11-20 09:30:00' \
        --end '2025-11-20 16:00:00' --horizon-min 5 --label-bps 5

Pass --skip-recompute to only read stats from ClickHouse (useful if labels are already written).
"""

import argparse
import subprocess
import sys
from typing import Any, Dict, List

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Recompute 1m labels and inspect ClickHouse output.")
    ap.add_argument("--symbols", required=True, help="Comma-separated symbols (e.g., TSLA,AAPL)")
    ap.add_argument("--start", required=True, help="ISO timestamp (e.g., 2025-11-20 09:30:00)")
    ap.add_argument("--end", required=True, help="ISO timestamp (exclusive)")
    ap.add_argument("--horizon-min", type=int, default=5)
    ap.add_argument("--label-bps", type=float, default=5.0)
    ap.add_argument("--max-abs-ret", type=float, default=0.2)
    ap.add_argument("--out-table", default="trading.features_1m_with_labels")
    ap.add_argument("--active-table", default="trading.features_1m_active")
    ap.add_argument("--skip-recompute", action="store_true", help="Only read stats, do not rerun recompute_labels script.")
    ap.add_argument("--limit", type=int, default=0, help="Optional sample row count to print.")
    return ap.parse_args()


def run_recompute(args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        "tools/recompute_labels_1m.py",
        "--symbols",
        args.symbols,
        "--start",
        args.start,
        "--end",
        args.end,
        "--horizon-min",
        str(args.horizon_min),
        "--label-bps",
        str(args.label_bps),
        "--max-abs-ret",
        str(args.max_abs_ret),
        "--out-table",
        args.out_table,
        "--active-table",
        args.active_table,
    ]
    print("Running:", " ".join(cmd))
    proc = subprocess.run(cmd, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"recompute_labels_1m.py exited with {proc.returncode}")


def ch_client():
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise SystemExit("clickhouse_connect is required for this script. pip install clickhouse-connect") from exc

    from libs.config import config

    cfg = config.clickhouse_config
    return clickhouse_connect.get_client(
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 8123)),
        username=cfg.get("user", "default"),
        password=cfg.get("password", ""),
        database=cfg.get("database", "trading"),
    )


def fetch_summary(cli, args: argparse.Namespace) -> Dict[str, Any]:
    symbols: List[str] = [s.strip() for s in args.symbols.split(",") if s.strip()]
    q = f"""
    SELECT
        count() AS total_rows,
        sum(y = 1) AS positives,
        sum(y = 0) AS negatives,
        avgOrNull(fwd_ret_bps) AS avg_ret_bps,
        avgOrNull(abs(fwd_ret_bps)) AS avg_abs_ret_bps,
        min(ts) AS first_ts,
        max(ts) AS last_ts,
        max(label_bps) AS label_bps,
        any(horizon_min) AS horizon_min
    FROM {args.out_table}
    WHERE symbol IN %(syms)s
      AND ts >= toDateTime(%(start)s)
      AND ts < toDateTime(%(end)s)
      AND horizon_min = %(horizon)s
      AND label_bps = %(label_bps)s
    """
    params = {
        "syms": symbols,
        "start": args.start,
        "end": args.end,
        "horizon": args.horizon_min,
        "label_bps": args.label_bps,
    }
    res = cli.query(q, parameters=params).named_results()
    return res[0] if res else {}


def fetch_samples(cli, args: argparse.Namespace, limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    symbols: List[str] = [s.strip() for s in args.symbols.split(",") if s.strip()]
    q = f"""
    SELECT ts, symbol, close, fwd_ret_bps, y
    FROM {args.out_table}
    WHERE symbol IN %(syms)s
      AND ts >= toDateTime(%(start)s)
      AND ts < toDateTime(%(end)s)
      AND horizon_min = %(horizon)s
      AND label_bps = %(label_bps)s
    ORDER BY ts, symbol
    LIMIT {limit}
    """
    params = {
        "syms": symbols,
        "start": args.start,
        "end": args.end,
        "horizon": args.horizon_min,
        "label_bps": args.label_bps,
    }
    return cli.query(q, parameters=params).named_results()


def main() -> None:
    args = parse_args()
    if not args.skip_recompute:
        run_recompute(args)
    cli = ch_client()
    summary = fetch_summary(cli, args)
    if not summary:
        print("No rows found in ClickHouse for the specified window/parameters.")
        return
    print("=== Label summary ===")
    for k, v in summary.items():
        print(f"{k}: {v}")

    samples = fetch_samples(cli, args, args.limit)
    if samples:
        print(f"\n=== Sample rows (limit {args.limit}) ===")
        for row in samples:
            print(row)


if __name__ == "__main__":
    main()
