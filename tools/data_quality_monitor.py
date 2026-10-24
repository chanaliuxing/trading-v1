#!/usr/bin/env python3
"""
Data quality monitor with event emission and bad symbol blocking.
Checks recent days for coverage, null ratios, and abnormal bar counts.
Writes results to JSON and emits ml.events for observability.

Usage:
  PYTHONPATH=. venv/bin/python tools/data_quality_monitor.py --symbols TSLA NVDA --days 7
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict

import clickhouse_connect

DEFAULT_SYMBOLS = ["TSLA", "NVDA"]
RTH_EXPECTED_BARS = 390  # 6.5h * 60

# Thresholds for bad symbol detection
COVERAGE_THRESHOLD = 0.8  # <80% coverage = bad
DUPLICATE_THRESHOLD = 0.05  # >5% duplicates = bad
NULL_RATIO_THRESHOLD = 0.1  # >10% nulls = bad


def ch_client():
    try:
        from libs.config import config
        ccfg = config.clickhouse_config
        return clickhouse_connect.get_client(
            host=ccfg.get("host", "localhost"),
            port=int(ccfg.get("port", 8123)),
            database=ccfg.get("database", "trading"),
            username=ccfg.get("user", "default"),
            password=ccfg.get("password", ""),
        )
    except Exception:
        return clickhouse_connect.get_client(host="localhost", port=8123, database="trading")


def check_bars(cli, symbols: List[str], start_day: str, end_day: str) -> List[Dict]:
    """Check bar coverage and detect anomalies."""
    q = """
    SELECT symbol, toDate(ts_end) d, count() bars
    FROM trading.bars_1m_ibkr
    WHERE symbol IN %(syms)s
      AND ts_end >= toDateTime(%(s)s) AND ts_end <= toDateTime(%(e)s)
      AND is_rth = 1
      AND toHour(ts_end) >= 9 AND toHour(ts_end) < 16
    GROUP BY symbol, d
    ORDER BY symbol, d
    """
    rows = cli.query_df(q, parameters={"syms": symbols, "s": start_day, "e": end_day})
    warnings = []
    for _, r in rows.iterrows():
        coverage = r["bars"] / RTH_EXPECTED_BARS
        if coverage < COVERAGE_THRESHOLD:
            warnings.append({
                "component": "bars",
                "symbol": r["symbol"],
                "date": str(r["d"]),
                "bars": int(r["bars"]),
                "coverage": round(coverage, 4),
                "severity": "error"
            })
        elif r["bars"] > RTH_EXPECTED_BARS * 1.3:
            warnings.append({
                "component": "bars",
                "symbol": r["symbol"],
                "date": str(r["d"]),
                "bars": int(r["bars"]),
                "coverage": round(coverage, 4),
                "severity": "warning"
            })
    return warnings


def check_labels(cli, symbols: List[str], start_day: str, end_day: str) -> List[Dict]:
    """Check label quality (null ratios)."""
    q = """
    SELECT symbol,
           count() total,
           countIf(label_h30 IS NULL) nulls
    FROM trading.features_1m_with_labels
    WHERE symbol IN %(syms)s
      AND ts >= toDateTime(%(s)s) AND ts <= toDateTime(%(e)s)
      AND toHour(ts) >= 9 AND toHour(ts) < 16
    GROUP BY symbol
    """
    rows = cli.query_df(q, parameters={"syms": symbols, "s": start_day, "e": end_day})
    warnings = []
    for _, r in rows.iterrows():
        total = int(r["total"])
        nulls = int(r["nulls"])
        null_ratio = nulls / total if total else 1.0
        if null_ratio > NULL_RATIO_THRESHOLD:
            warnings.append({
                "component": "labels",
                "symbol": r["symbol"],
                "null_ratio": round(null_ratio, 4),
                "total": total,
                "severity": "error"
            })
    return warnings


def check_duplicates(cli, symbols: List[str], start_day: str, end_day: str) -> List[Dict]:
    """Check for duplicate bars (same symbol+timestamp with >1 row)."""
    q = """
    SELECT symbol, count() dup_count
    FROM (
        SELECT symbol, ts_end
        FROM trading.bars_1m_ibkr
        WHERE symbol IN %(syms)s
          AND ts_end >= toDateTime(%(s)s) AND ts_end <= toDateTime(%(e)s)
          AND is_rth = 1
        GROUP BY symbol, ts_end
        HAVING count() > 1
    )
    GROUP BY symbol
    """
    rows = cli.query_df(q, parameters={"syms": symbols, "s": start_day, "e": end_day})
    warnings = []
    for _, r in rows.iterrows():
        dup_count = int(r["dup_count"])
        if dup_count > 0:
            warnings.append({
                "component": "duplicates",
                "symbol": r["symbol"],
                "duplicate_bars": dup_count,
                "severity": "warning"
            })
    return warnings


def emit_event(event_type: str, status: str, params: dict):
    """Emit ml.event for observability."""
    try:
        from tools.nightly_oof_flow import publish_ml_event
        publish_ml_event(status, event_type, f"dq_{datetime.now().strftime('%Y%m%d')}", params=params)
    except Exception:
        pass


def write_bad_symbols(bad_symbols: List[str], output_dir: Path):
    """Write bad symbols to file for training blocking."""
    output_dir.mkdir(parents=True, exist_ok=True)
    bad_symbols_file = output_dir / "bad_symbols.json"

    bad_symbols_file.write_text(json.dumps({
        "bad_symbols": bad_symbols,
        "timestamp": datetime.now().isoformat(),
        "reason": "data_quality_check_failed"
    }, indent=2))

    print(f"✅ Bad symbols written to {bad_symbols_file}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--output-dir", default="runs/data_quality", help="Output directory for reports")
    args = ap.parse_args()

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=args.days)
    cli = ch_client()

    # Run all checks
    bars_warn = check_bars(cli, args.symbols, start_dt.strftime("%Y-%m-%d 00:00:00"), end_dt.strftime("%Y-%m-%d 23:59:59"))
    label_warn = check_labels(cli, args.symbols, start_dt.strftime("%Y-%m-%d 00:00:00"), end_dt.strftime("%Y-%m-%d 23:59:59"))
    dup_warn = check_duplicates(cli, args.symbols, start_dt.strftime("%Y-%m-%d 00:00:00"), end_dt.strftime("%Y-%m-%d 23:59:59"))

    all_warnings = bars_warn + label_warn + dup_warn

    # Determine bad symbols (error severity)
    bad_symbols = list(set([w["symbol"] for w in all_warnings if w.get("severity") == "error"]))

    # Write report
    output_dir = Path(args.output_dir) / datetime.now().strftime("%Y%m%d")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / "report.json"

    report = {
        "timestamp": datetime.now().isoformat(),
        "period": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "symbols": args.symbols,
        "warnings": all_warnings,
        "bad_symbols": bad_symbols,
        "summary": {
            "total_warnings": len(all_warnings),
            "bars_warnings": len(bars_warn),
            "label_warnings": len(label_warn),
            "duplicate_warnings": len(dup_warn),
            "bad_symbol_count": len(bad_symbols),
        }
    }

    report_file.write_text(json.dumps(report, indent=2))
    print(f"✅ Report written to {report_file}")

    # Write bad symbols for blocking
    if bad_symbols:
        write_bad_symbols(bad_symbols, output_dir)

    # Emit event
    emit_event(
        "data_quality.check_completed",
        "success" if len(bad_symbols) == 0 else "warning",
        {
            "symbols_checked": args.symbols,
            "bad_symbols": bad_symbols,
            "total_warnings": len(all_warnings),
        }
    )

    # Print summary
    if not all_warnings:
        print("✅ Data quality OK (bars/labels/duplicates)")
    else:
        print(f"⚠️  Data quality warnings ({len(all_warnings)} total):")
        for w in all_warnings:
            severity = w.get("severity", "info")
            symbol = w["symbol"]
            component = w["component"]
            details = {k: v for k, v in w.items() if k not in ["severity", "symbol", "component"]}
            print(f"  [{severity}] [{component}] {symbol}: {details}")

        if bad_symbols:
            print(f"\n🚫 Bad symbols (will be blocked from training): {bad_symbols}")


if __name__ == "__main__":
    main()
