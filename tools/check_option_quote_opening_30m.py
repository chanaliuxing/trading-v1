from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _parse_day(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(str(raw).strip())
    return datetime.now(ET).date()


def _iter_audit_rows(audit_root: Path, day: date) -> Iterable[Dict[str, Any]]:
    day_dir = audit_root.expanduser() / day.isoformat()
    if not day_dir.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(day_dir.glob("strategy-*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _parse_ts(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except Exception:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=ET)
    return ts.astimezone(ET)


def _in_window(ts: datetime, day: date, start_hm: tuple[int, int], window_min: int) -> bool:
    start = datetime.combine(day, time(start_hm[0], start_hm[1]), tzinfo=ET)
    end = start + timedelta(minutes=max(1, int(window_min)))
    return start <= ts < end


def build_opening_30m_report(
    *,
    rows: Iterable[Dict[str, Any]],
    day: date,
    strategy_id: str,
    window_min: int,
    max_quote_age_sec: float,
    max_clickhouse_ratio: float,
    min_heartbeat_ratio: float,
) -> Dict[str, Any]:
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        if str(row.get("strategy_id") or "").strip() != str(strategy_id).strip():
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None or not _in_window(ts, day, (9, 30), window_min):
            continue
        filtered.append(row)

    heartbeat_rows = [r for r in filtered if str(r.get("stage") or "") == "minute_heartbeat"]
    source_counts: Counter[str] = Counter()
    stale_count = 0
    missing_age_count = 0
    for row in heartbeat_rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        source = str(payload.get("quote_source") or "unknown")
        age = payload.get("quote_age_sec")
        source_counts[source] += 1
        if age is None:
            missing_age_count += 1
            continue
        try:
            if float(age) > float(max_quote_age_sec):
                stale_count += 1
        except Exception:
            missing_age_count += 1

    source_total = max(1, sum(source_counts.values()))
    clickhouse_ratio = float(source_counts.get("clickhouse", 0)) / float(source_total)
    expected_heartbeats = max(1, int(window_min))
    heartbeat_ratio = float(len(heartbeat_rows)) / float(expected_heartbeats)
    gate_blocked = [r for r in filtered if str(r.get("stage") or "") == "gate_blocked"]
    quote_pause_events = sum(1 for r in gate_blocked if str(r.get("reason") or "") == "QUOTE_HEARTBEAT_PAUSED")
    coverage_fail_events = sum(1 for r in gate_blocked if str(r.get("reason") or "") == "QUOTE_HEALTH_BLOCK_DAY")

    checks = {
        "heartbeat_continuity_ok": heartbeat_ratio >= float(min_heartbeat_ratio),
        "freshness_ok": stale_count == 0 and missing_age_count == 0,
        "coverage_ok": coverage_fail_events == 0 and quote_pause_events == 0,
        "fallback_ratio_ok": clickhouse_ratio <= float(max_clickhouse_ratio),
    }
    return {
        "report_name": "option_quote_opening_30m",
        "strategy_id": str(strategy_id),
        "day": day.isoformat(),
        "window_start_et": f"{day.isoformat()}T09:30:00-04:00",
        "window_min": int(window_min),
        "heartbeats_total": int(len(heartbeat_rows)),
        "expected_heartbeats": int(expected_heartbeats),
        "heartbeat_ratio": float(round(heartbeat_ratio, 4)),
        "quote_source_counts": dict(source_counts),
        "clickhouse_ratio": float(round(clickhouse_ratio, 4)),
        "freshness_failures": int(stale_count),
        "missing_quote_age_rows": int(missing_age_count),
        "quote_heartbeat_pause_events": int(quote_pause_events),
        "coverage_fail_events": int(coverage_fail_events),
        "checks": checks,
        "go": bool(all(checks.values())),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Opening 30-minute option quote acceptance check from audit events.")
    ap.add_argument("--date", default=None, help="Trading day in YYYY-MM-DD (default: today ET).")
    ap.add_argument("--strategy-id", required=True, help="Target strategy_id.")
    ap.add_argument("--audit-root", default="runtime/audit", help="Audit root directory.")
    ap.add_argument("--window-min", type=int, default=30, help="Opening window length (minutes).")
    ap.add_argument("--max-quote-age-sec", type=float, default=120.0, help="Max allowed quote age for heartbeat rows.")
    ap.add_argument("--max-clickhouse-ratio", type=float, default=0.35, help="Max clickhouse quote-source ratio.")
    ap.add_argument("--min-heartbeat-ratio", type=float, default=0.80, help="Min heartbeat continuity ratio.")
    ap.add_argument("--out", default=None, help="Optional output JSON path.")
    args = ap.parse_args()

    day = _parse_day(args.date)
    rows = _iter_audit_rows(Path(args.audit_root), day)
    report = build_opening_30m_report(
        rows=rows,
        day=day,
        strategy_id=str(args.strategy_id),
        window_min=int(args.window_min),
        max_quote_age_sec=float(args.max_quote_age_sec),
        max_clickhouse_ratio=float(args.max_clickhouse_ratio),
        min_heartbeat_ratio=float(args.min_heartbeat_ratio),
    )
    out_path = Path(args.out) if args.out else Path("runtime/reports") / f"option_quote_opening_30m_{args.strategy_id}_{day.isoformat()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
