#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from tool.report.build_qqq_state_shadow_readiness import build_qqq_state_shadow_readiness
from tool.query_state_transition_audit import build_state_transition_chain


def build_state_transition_daily_summary(*, audit_root: Path, date: str) -> dict[str, Any]:
    payload = build_state_transition_chain(
        audit_root=audit_root,
        date=date,
        include_inconsistency=True,
    )
    rows = list(payload.get("rows") or [])
    by_symbol = Counter()
    inconsistency_count = 0
    for row in rows:
        by_symbol[str(row.get("symbol") or "UNKNOWN")] += 1
        if str(row.get("stage") or "") == "cross_state_inconsistency_detected":
            inconsistency_count += 1
    return {
        "date": date,
        "row_count": len(rows),
        "state_summary": dict(payload.get("summary") or {}),
        "by_symbol": dict(sorted(by_symbol.items())),
        "cross_state_inconsistency_count": inconsistency_count,
        "qqq_shadow_readiness": build_qqq_state_shadow_readiness(
            audit_root=audit_root,
            date=date,
            strategy_id="qqq_intraday_001",
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build daily state-transition audit summary")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    payload = build_state_transition_daily_summary(audit_root=Path(args.root).expanduser(), date=str(args.date))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
