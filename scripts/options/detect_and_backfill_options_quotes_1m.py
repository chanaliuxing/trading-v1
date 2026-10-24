#!/usr/bin/env python3
"""Detect missing `trading.options_quotes_1m` days and run a backfill skeleton.

Default behavior is non-destructive:
- detect dates where underlying 1m bars exist but options quotes are missing
- print a compact summary
- optionally write a JSON report

Backfill modes:
- `detect`: report only
- `synth`: invoke the existing synthetic backfill script per missing day
- `csv`: ingest normalized CSV files if present
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.ch_pool import get_client

QUOTE_COLUMNS = [
    "ts",
    "underlying",
    "expiry",
    "strike",
    "right",
    "dte",
    "bid",
    "ask",
    "mid",
    "last",
    "volume",
    "open_interest",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "spread_bps",
    "contracts_mult",
    "source",
    "created_at",
]

CSV_REQUIRED_COLUMNS = set(QUOTE_COLUMNS)


@dataclass
class DayCoverage:
    day: str
    bars_count: int
    quote_rows: int
    quote_minutes: int
    status: str


def _query_day_coverage(underlying: str, start: str, end: str) -> List[DayCoverage]:
    ch = get_client()
    sql = """
    WITH bars AS (
        SELECT
            toDate(toTimeZone(ts_start, 'America/New_York')) AS day,
            count(*) AS bars_count
        FROM trading.bars_1m_ibkr
        WHERE symbol = %(u)s
          AND is_rth = 1
          AND ts_start >= %(start)s
          AND ts_start < %(end)s
        GROUP BY day
    ),
    quotes AS (
        SELECT
            toDate(toTimeZone(ts, 'America/New_York')) AS day,
            count(*) AS quote_rows,
            uniqExact(toStartOfMinute(toTimeZone(ts, 'America/New_York'))) AS quote_minutes
        FROM trading.options_quotes_1m
        WHERE underlying = %(u)s
          AND ts >= %(start)s
          AND ts < %(end)s
        GROUP BY day
    )
    SELECT
        b.day AS day,
        b.bars_count AS bars_count,
        ifNull(q.quote_rows, 0) AS quote_rows,
        ifNull(q.quote_minutes, 0) AS quote_minutes
    FROM bars b
    LEFT JOIN quotes q ON b.day = q.day
    ORDER BY day
    """
    rows = ch.query(
        sql,
        {
            "u": underlying.upper(),
            "start": f"{start} 00:00:00",
            "end": f"{end} 00:00:00",
        },
        settings={"use_query_cache": False},
    )
    result: List[DayCoverage] = []
    for row in rows:
        bars_count = int(row.get("bars_count") or 0)
        quote_rows = int(row.get("quote_rows") or 0)
        quote_minutes = int(row.get("quote_minutes") or 0)
        status = "ok"
        if bars_count <= 0:
            status = "no_bars"
        elif quote_rows <= 0:
            status = "missing_quotes"
        elif quote_minutes < 30:
            status = "sparse_quotes"
        result.append(
            DayCoverage(
                day=str(row.get("day")),
                bars_count=bars_count,
                quote_rows=quote_rows,
                quote_minutes=quote_minutes,
                status=status,
            )
        )
    return result


def _load_normalized_quotes_csv(csv_path: Path, underlying: str, source_tag: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    missing = CSV_REQUIRED_COLUMNS.difference(df.columns)
    if missing:
        raise ValueError(f"{csv_path} missing columns: {sorted(missing)}")
    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts"])
    out["created_at"] = pd.to_datetime(out["created_at"])
    out["expiry"] = pd.to_datetime(out["expiry"]).dt.date
    out["underlying"] = underlying.upper()
    out["source"] = source_tag
    numeric_int_cols = ["dte", "volume", "open_interest", "contracts_mult"]
    numeric_float_cols = ["strike", "bid", "ask", "mid", "last", "iv", "delta", "gamma", "theta", "vega", "spread_bps"]
    for col in numeric_int_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)
    for col in numeric_float_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).astype(float)
    out["right"] = out["right"].astype(str).str.upper()
    return out[QUOTE_COLUMNS]


def _insert_quotes_df(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    ch = get_client()
    ch.insert_df("trading.options_quotes_1m", df, column_names=QUOTE_COLUMNS)
    return int(len(df))


def _csv_path_for_day(csv_dir: Path, underlying: str, day: str) -> Path:
    return csv_dir / f"{underlying.upper()}_options_quotes_1m_{day}.csv"


def _run_synth_backfill(underlying: str, day: str, source_tag: str) -> Dict[str, Any]:
    cmd = [
        str(PROJECT_ROOT / "venv" / "bin" / "python"),
        str(PROJECT_ROOT / "scripts" / "options" / "backfill_options_quotes_1m_synth.py"),
        "--underlying",
        underlying.upper(),
        "--start",
        day,
        "--end",
        day,
        "--source",
        source_tag,
        "--replace-source",
    ]
    completed = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False)
    return {
        "day": day,
        "mode": "synth",
        "returncode": int(completed.returncode),
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _run_csv_backfill(csv_dir: Path, underlying: str, day: str, source_tag: str) -> Dict[str, Any]:
    csv_path = _csv_path_for_day(csv_dir, underlying, day)
    if not csv_path.exists():
        return {"day": day, "mode": "csv", "status": "missing_csv", "path": str(csv_path)}
    df = _load_normalized_quotes_csv(csv_path, underlying, source_tag)
    inserted = _insert_quotes_df(df)
    return {"day": day, "mode": "csv", "status": "inserted", "path": str(csv_path), "rows": inserted}


def _write_report(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Detect and backfill missing options_quotes_1m days")
    ap.add_argument("--underlying", default="QQQ")
    ap.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="End date YYYY-MM-DD, exclusive")
    ap.add_argument("--mode", choices=["detect", "synth", "csv"], default="detect")
    ap.add_argument("--status-filter", default="missing_quotes,sparse_quotes", help="Comma-separated statuses to target")
    ap.add_argument("--csv-dir", help="Directory with normalized CSV files named UNDERLYING_options_quotes_1m_YYYY-MM-DD.csv")
    ap.add_argument("--source-tag", default="gap_backfill_skeleton_v1")
    ap.add_argument("--report-json", help="Optional JSON report output path")
    args = ap.parse_args()

    coverage = _query_day_coverage(args.underlying, args.start, args.end)
    target_statuses = {x.strip() for x in args.status_filter.split(",") if x.strip()}
    targets = [row for row in coverage if row.status in target_statuses]

    actions: List[Dict[str, Any]] = []
    if args.mode == "synth":
        for row in targets:
            actions.append(_run_synth_backfill(args.underlying, row.day, args.source_tag))
    elif args.mode == "csv":
        if not args.csv_dir:
            raise SystemExit("--csv-dir is required when --mode csv")
        csv_dir = Path(args.csv_dir)
        for row in targets:
            actions.append(_run_csv_backfill(csv_dir, args.underlying, row.day, args.source_tag))

    payload = {
        "underlying": args.underlying.upper(),
        "start": args.start,
        "end": args.end,
        "mode": args.mode,
        "target_statuses": sorted(target_statuses),
        "coverage": [asdict(row) for row in coverage],
        "targets": [asdict(row) for row in targets],
        "actions": actions,
    }

    print(f"underlying={payload['underlying']} mode={args.mode} days={len(coverage)} targets={len(targets)}")
    for row in coverage:
        print(
            f"{row.day} status={row.status} bars={row.bars_count} "
            f"quote_rows={row.quote_rows} quote_minutes={row.quote_minutes}"
        )
    if actions:
        for action in actions:
            print(json.dumps(action, ensure_ascii=True))

    if args.report_json:
        _write_report(Path(args.report_json), payload)


if __name__ == "__main__":
    main()
