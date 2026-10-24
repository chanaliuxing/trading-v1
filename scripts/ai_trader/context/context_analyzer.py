#!/usr/bin/env python3
"""
Market Context Analyzer
- Pulls recent metrics from ClickHouse (VIX, SPY returns/volatility, RVOL proxy)
- Classifies regime: trending | sideways | volatile (simple heuristics)
- Writes runs/ai_trader/YYYY-MM-DD/context.json

Usage:
  python scripts/ai_trader/context/context_analyzer.py --date 2025-10-28 --runs-dir runs/ai_trader

Notes:
  - Best-effort. If ClickHouse not available, falls back to defaults.
"""

from __future__ import annotations
import argparse
import json
import shlex
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Any


def _ch_query(sql: str) -> str:
    cmd = f"docker exec trading_clickhouse clickhouse-client --query \"{sql}\" --format CSVWithNames"
    return subprocess.check_output(cmd, shell=True, text=True)


def _try_float(s: str, d: float = 0.0) -> float:
    try:
        return float(s)
    except Exception:
        return d


def analyze_context(target_date: str) -> Dict[str, Any]:
    # 7-day window ending at target_date
    try:
        d = date.fromisoformat(target_date)
    except Exception:
        d = date.today()
    start = (d - timedelta(days=7)).isoformat() + " 00:00:00"
    end = d.isoformat() + " 23:59:59"

    ctx: Dict[str, Any] = {"date": d.isoformat(), "ok": False}
    try:
        # VIX: daily close average
        sql_vix = (
            "SELECT avg(close) AS vix FROM trading.features_1m WHERE symbol='VIX' "
            f"AND ts BETWEEN toDateTime('{start}') AND toDateTime('{end}')"
        )
        vix_csv = _ch_query(sql_vix).splitlines()
        vix = _try_float(vix_csv[-1].split(',')[-1] if len(vix_csv) >= 2 else "0")

        # SPY simple trend proxy: mean return / stdev
        sql_spy = (
            "SELECT close, lagInFrame(close) OVER (ORDER BY ts) AS prev_close FROM trading.features_1m "
            f"WHERE symbol='SPY' AND ts BETWEEN toDateTime('{start}') AND toDateTime('{end}') ORDER BY ts"
        )
        spy_csv = _ch_query(sql_spy).splitlines()
        rets = []
        for i, line in enumerate(spy_csv[1:]):
            parts = line.split(',')
            if len(parts) < 2:
                continue
            c = _try_float(parts[0])
            p = _try_float(parts[1])
            if p > 0:
                rets.append(c/p - 1.0)
        import math
        mean_r = sum(rets)/len(rets) if rets else 0.0
        stdev = math.sqrt(sum((r-mean_r)**2 for r in rets)/len(rets)) if rets else 1e-9
        trend_score = mean_r / stdev if stdev > 0 else 0.0

        # Regime classification
        if vix >= 22.0:
            regime = "volatile"
        elif abs(trend_score) >= 0.4:
            regime = "trending"
        else:
            regime = "sideways"

        ctx.update({
            "ok": True,
            "vix": round(vix, 2),
            "trend_score": round(trend_score, 3),
            "regime": regime,
            "market_bias": "bull" if trend_score > 0.0 else ("bear" if trend_score < 0 else "neutral"),
        })
    except Exception as e:
        ctx.update({"ok": False, "error": str(e), "regime": "unknown", "vix": None, "market_bias": None})
    return ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--runs-dir", default="runs/ai_trader")
    args = ap.parse_args()

    out = analyze_context(args.date)
    out_dir = Path(args.runs_dir) / out.get("date", date.today().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "context.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

