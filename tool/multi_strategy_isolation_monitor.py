#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.build_multi_strategy_isolation_snapshot import _audit_rows_for_day, _load_json, build_snapshot


ET = ZoneInfo("America/New_York")
HIGH_VALUE_STAGES = {
    "entry_isolation_blocked_pending_order",
    "entry_isolation_blocked_live_position",
    "fill_attribution_fallback_broker_order_id",
    "fill_attribution_ambiguous",
    "broker_open_orders_quarantined",
    "broker_position_quarantined",
}


def _parse_ts(value: Any) -> str:
    text = str(value or "").strip()
    return text


def _row_key(row: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(row.get("stage") or ""),
        str(row.get("symbol") or "").strip().upper(),
        str(row.get("reason") or ""),
        str(row.get("trace_id") or ""),
    )


def collect_high_value_alerts(rows: Iterable[Dict[str, Any]], *, limit: int = 20) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for row in sorted(list(rows), key=lambda item: str(item.get("ts") or ""), reverse=True):
        stage = str(row.get("stage") or "")
        severity = str(row.get("severity") or "").lower()
        if stage not in HIGH_VALUE_STAGES and severity not in {"warning", "error", "critical"}:
            continue
        key = _row_key(row)
        if key in seen:
            continue
        seen.add(key)
        selected.append(
            {
                "ts": _parse_ts(row.get("ts")),
                "stage": stage,
                "severity": str(row.get("severity") or "info"),
                "status": row.get("status"),
                "reason": row.get("reason"),
                "symbol": str(row.get("symbol") or "").strip().upper() or None,
                "strategy_id": row.get("strategy_id"),
                "trace_id": row.get("trace_id"),
                "payload": row.get("payload") or {},
            }
        )
        if len(selected) >= int(limit):
            break
    return selected


def build_alerts_payload(*, day: str, rows: Iterable[Dict[str, Any]], limit: int = 20) -> Dict[str, Any]:
    alerts = collect_high_value_alerts(rows, limit=limit)
    counter: Dict[str, int] = {}
    for item in alerts:
        stage = str(item.get("stage") or "")
        counter[stage] = int(counter.get(stage, 0)) + 1
    return {
        "generated_at_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "day": day,
        "alert_count": len(alerts),
        "stage_counter": counter,
        "alerts": alerts,
    }


def write_monitor_outputs(
    *,
    day: str,
    audit_root: Path,
    qqq_obs_path: Path,
    index_diag_path: Path,
    snapshot_out: Path,
    alerts_out: Path,
    alerts_limit: int,
) -> Dict[str, Any]:
    audit_rows = _audit_rows_for_day(audit_root, day)
    snapshot = build_snapshot(
        qqq_obs=_load_json(qqq_obs_path),
        index_diag=_load_json(index_diag_path),
        audit_rows=audit_rows,
        day=day,
    )
    alerts = build_alerts_payload(day=day, rows=audit_rows, limit=alerts_limit)
    snapshot_out.parent.mkdir(parents=True, exist_ok=True)
    alerts_out.parent.mkdir(parents=True, exist_ok=True)
    snapshot_out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    alerts_out.write_text(json.dumps(alerts, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "snapshot_out": str(snapshot_out),
        "alerts_out": str(alerts_out),
        "alert_count": int(alerts.get("alert_count", 0) or 0),
    }


def _read_checkpoint(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_checkpoint(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _latest_relevant_ts(rows: Iterable[Dict[str, Any]]) -> str:
    relevant = [str(row.get("ts") or "") for row in rows if str(row.get("stage") or "") in HIGH_VALUE_STAGES]
    return max(relevant) if relevant else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Low-noise multi-strategy isolation monitor")
    parser.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--qqq-obs", default=str(ROOT / "runtime" / "state" / "qqq_intraday_observability_qqq_intraday_001.json"))
    parser.add_argument("--index-diag", default=str(ROOT / "runtime" / "state" / "index_super_win_diag_snapshot_index_super_win_001.json"))
    parser.add_argument("--snapshot-out", default=str(ROOT / "runtime" / "state" / "multi_strategy_isolation_snapshot.json"))
    parser.add_argument("--alerts-out", default=str(ROOT / "runtime" / "state" / "multi_strategy_isolation_alerts.json"))
    parser.add_argument("--checkpoint-out", default=str(ROOT / "runtime" / "state" / "multi_strategy_isolation_monitor_checkpoint.json"))
    parser.add_argument("--snapshot-interval-sec", type=float, default=60.0)
    parser.add_argument("--poll-interval-sec", type=float, default=5.0)
    parser.add_argument("--alerts-limit", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    day = str(args.day)
    audit_root = Path(args.audit_root)
    qqq_obs_path = Path(args.qqq_obs)
    index_diag_path = Path(args.index_diag)
    snapshot_out = Path(args.snapshot_out)
    alerts_out = Path(args.alerts_out)
    checkpoint_out = Path(args.checkpoint_out)
    snapshot_interval_sec = max(5.0, float(args.snapshot_interval_sec))
    poll_interval_sec = max(1.0, float(args.poll_interval_sec))
    alerts_limit = max(1, int(args.alerts_limit))

    iterations = 1 if bool(args.once) else max(1, int(args.iterations))
    checkpoint = _read_checkpoint(checkpoint_out)
    last_snapshot_mono = 0.0
    latest_seen_ts = str(checkpoint.get("latest_relevant_ts") or "")

    for idx in range(iterations):
        rows = _audit_rows_for_day(audit_root, day)
        latest_ts = _latest_relevant_ts(rows)
        should_refresh = False
        now_mono = time.monotonic()
        if idx == 0 or (now_mono - last_snapshot_mono) >= snapshot_interval_sec:
            should_refresh = True
        if latest_ts and latest_ts > latest_seen_ts:
            should_refresh = True
            latest_seen_ts = latest_ts
        if should_refresh:
            write_monitor_outputs(
                day=day,
                audit_root=audit_root,
                qqq_obs_path=qqq_obs_path,
                index_diag_path=index_diag_path,
                snapshot_out=snapshot_out,
                alerts_out=alerts_out,
                alerts_limit=alerts_limit,
            )
            last_snapshot_mono = now_mono
            _write_checkpoint(
                checkpoint_out,
                {
                    "day": day,
                    "latest_relevant_ts": latest_seen_ts,
                    "last_snapshot_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
                },
            )
        if idx < (iterations - 1):
            time.sleep(poll_interval_sec)
    print(str(snapshot_out))
    print(str(alerts_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
