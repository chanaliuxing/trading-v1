#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _iter_jsonl(paths: Iterable[str]) -> Iterable[Dict[str, Any]]:
    for fp in paths:
        p = Path(fp)
        if not p.exists() or not p.is_file():
            continue
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    if lo == hi:
        return float(values[lo])
    frac = pos - lo
    return float(values[lo] * (1.0 - frac) + values[hi] * frac)


def _summarize(values: list[float]) -> Dict[str, float]:
    if not values:
        return {"count": 0, "min": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0, "avg": 0.0}
    vals = sorted(float(v) for v in values if v is not None)
    return {
        "count": float(len(vals)),
        "min": float(vals[0]),
        "p50": _percentile(vals, 0.50),
        "p90": _percentile(vals, 0.90),
        "max": float(vals[-1]),
        "avg": float(statistics.fmean(vals)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="QQQ stock->option signal latency diagnostics")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--audit-dir", default="runtime/audit", help="audit root directory")
    ap.add_argument("--plugin-log-glob", default=str(Path.home() / "grafana-stack/logs/trader.log*"))
    ap.add_argument("--stock-strategy-id", default="qqq_intraday_001")
    ap.add_argument("--option-strategy-id", default="qqq_intraday_option_v1_001")
    args = ap.parse_args()

    day = str(args.date)
    audit_files = sorted(glob.glob(str(Path(args.audit_dir) / day / "strategy-*.jsonl")))
    plugin_files = sorted(glob.glob(str(args.plugin_log_glob)))

    # stock publish evidence
    publish_by_trigger: Dict[str, datetime] = {}
    for row in _iter_jsonl(audit_files):
        if str(row.get("strategy_id") or "") != str(args.stock_strategy_id):
            continue
        if str(row.get("stage") or "") != "stock_signal_publish_ok":
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts.date().isoformat() != day:
            continue
        payload = dict(row.get("payload") or {})
        trigger_id = str(row.get("trigger_id") or payload.get("trigger_id") or "").strip()
        if trigger_id:
            publish_by_trigger[trigger_id] = ts

    # option consume evidence
    consume_age_sec: list[float] = []
    e2e_latency_sec: list[float] = []
    consumed = 0
    for row in _iter_jsonl(plugin_files):
        if str(row.get("event") or "") != "qqq_intraday_option_signal":
            continue
        if str(row.get("strategy_id") or "") != str(args.option_strategy_id):
            continue
        row_ts = _parse_ts(row.get("timestamp") or row.get("ts"))
        if row_ts is None or row_ts.date().isoformat() != day:
            continue
        consumed += 1
        age = row.get("signal_source_age_sec")
        try:
            if age is not None:
                consume_age_sec.append(float(age))
        except Exception:
            pass
        payload = dict(row.get("signal_payload") or {})
        trigger_id = str(payload.get("source_trigger_id") or "").strip()
        src_trigger_ts = _parse_ts(payload.get("source_trigger_ts"))
        base_ts = publish_by_trigger.get(trigger_id) if trigger_id else None
        if base_ts is None:
            base_ts = src_trigger_ts
        if base_ts is not None:
            e2e_latency_sec.append(max(0.0, (row_ts - base_ts).total_seconds()))

    out = {
        "date": day,
        "stock_strategy_id": str(args.stock_strategy_id),
        "option_strategy_id": str(args.option_strategy_id),
        "counts": {
            "stock_publish_ok_rows": len(publish_by_trigger),
            "option_signal_rows": consumed,
        },
        "signal_source_age_sec": _summarize(consume_age_sec),
        "stock_to_option_e2e_latency_sec": _summarize(e2e_latency_sec),
        "match_coverage_ratio": float(len(e2e_latency_sec) / consumed) if consumed > 0 else 0.0,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

