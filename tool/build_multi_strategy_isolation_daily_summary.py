#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.build_multi_strategy_isolation_snapshot import _audit_rows_for_day
from tool.multi_strategy_isolation_monitor import HIGH_VALUE_STAGES, collect_high_value_alerts


ET = ZoneInfo("America/New_York")


def build_daily_summary(*, day: str, audit_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(audit_rows)
    stage_counter: Dict[str, int] = {}
    symbol_counter: Dict[str, int] = {}
    for row in rows:
        stage = str(row.get("stage") or "")
        if stage not in HIGH_VALUE_STAGES:
            continue
        symbol = str(row.get("symbol") or "").strip().upper() or "UNKNOWN"
        stage_counter[stage] = int(stage_counter.get(stage, 0)) + 1
        symbol_counter[symbol] = int(symbol_counter.get(symbol, 0)) + 1
    return {
        "generated_at_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "day": day,
        "high_value_event_count": sum(stage_counter.values()),
        "high_value_stage_counter": stage_counter,
        "high_value_symbol_counter": symbol_counter,
        "recent_alerts": collect_high_value_alerts(rows, limit=10),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build daily multi-strategy isolation summary")
    parser.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--out", default=str(ROOT / "runtime" / "state" / "multi_strategy_isolation_daily_summary.json"))
    args = parser.parse_args()

    payload = build_daily_summary(
        day=str(args.day),
        audit_rows=_audit_rows_for_day(Path(args.audit_root), str(args.day)),
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
