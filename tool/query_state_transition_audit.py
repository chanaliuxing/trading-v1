#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

from tool.query_audit import query_records


def _state_machine_from_row(row: Dict[str, Any]) -> str:
    payload = dict(row.get("payload") or {})
    stage = str(row.get("stage") or "").strip()
    if stage == "cross_state_inconsistency_detected":
        return "cross_state"
    return str(
        row.get("state_machine")
        or payload.get("state_machine")
        or stage.replace("_transition", "")
        or "unknown"
    ).strip() or "unknown"


def build_state_transition_chain(
    *,
    audit_root: Path,
    strategy_id: str | None = None,
    symbol: str | None = None,
    trace_id: str | None = None,
    date: str | None = None,
    hour: str | None = None,
    include_inconsistency: bool = True,
    source: str = "local",
    clickhouse_table: str | None = None,
) -> dict[str, Any]:
    rows = query_records(
        audit_root,
        date=date,
        hour=hour,
        trace_id=trace_id,
        symbol=symbol,
        domain="state",
        source=source,
        clickhouse_table=clickhouse_table,
    )
    filtered: list[dict[str, Any]] = []
    by_state_machine = Counter()
    by_stage = Counter()
    by_reason = Counter()
    by_strategy = Counter()
    inconsistency_count = 0
    for row in rows:
        if strategy_id and str(row.get("strategy_id") or "").strip() != str(strategy_id).strip():
            continue
        stage = str(row.get("stage") or "").strip()
        if stage.endswith("_transition") or (include_inconsistency and stage == "cross_state_inconsistency_detected"):
            filtered.append(row)
            if stage.endswith("_transition"):
                by_state_machine[_state_machine_from_row(row)] += 1
            elif stage == "cross_state_inconsistency_detected":
                inconsistency_count += 1
            by_stage[stage] += 1
            by_reason[str(row.get("reason") or "unknown")] += 1
            by_strategy[str(row.get("strategy_id") or "unknown")] += 1
    return {
        "query": {
            "audit_root": str(audit_root),
            "strategy_id": strategy_id,
            "symbol": symbol,
            "trace_id": trace_id,
            "date": date,
            "hour": hour,
            "include_inconsistency": bool(include_inconsistency),
            "source": source,
            "clickhouse_table": clickhouse_table,
        },
        "summary": {
            "record_count": len(filtered),
            "by_state_machine": dict(sorted(by_state_machine.items())),
            "by_stage": dict(sorted(by_stage.items())),
            "cross_state_inconsistency_count": inconsistency_count,
            "top_reasons": by_reason.most_common(20),
            "by_strategy": dict(sorted(by_strategy.items())),
        },
        "rows": filtered,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Query state transition audit chain")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--strategy-id")
    parser.add_argument("--symbol")
    parser.add_argument("--trace-id")
    parser.add_argument("--date")
    parser.add_argument("--hour")
    parser.add_argument("--exclude-inconsistency", action="store_true")
    parser.add_argument("--source", choices=["local", "clickhouse", "auto"], default="local")
    parser.add_argument("--clickhouse-table")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = build_state_transition_chain(
        audit_root=Path(args.root).expanduser(),
        strategy_id=args.strategy_id,
        symbol=args.symbol,
        trace_id=args.trace_id,
        date=args.date,
        hour=args.hour,
        include_inconsistency=not bool(args.exclude_inconsistency),
        source=args.source,
        clickhouse_table=args.clickhouse_table,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    summary = payload.get("summary") or {}
    print(
        f"records={summary.get('record_count', 0)} "
        f"state_machines={','.join(sorted((summary.get('by_state_machine') or {}).keys())) or '-'} "
        f"strategies={','.join(sorted((summary.get('by_strategy') or {}).keys())) or '-'}"
    )
    for row in payload.get("rows") or []:
        print(
            f"{row.get('ts')} "
            f"{row.get('stage')} "
            f"strategy_id={row.get('strategy_id') or '-'} "
            f"symbol={row.get('symbol') or '-'} "
            f"status={row.get('status') or '-'} "
            f"reason={row.get('reason') or '-'} "
            f"trace_id={row.get('trace_id') or '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
