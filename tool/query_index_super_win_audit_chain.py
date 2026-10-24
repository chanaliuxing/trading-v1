#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional

from tool.query_audit import query_records


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _symbol_snapshot(snapshot: Dict[str, Any], symbol: Optional[str]) -> Dict[str, Any]:
    symbol_u = str(symbol or "").strip().upper()
    if not symbol_u:
        return {}
    return {
        "symbol": symbol_u,
        "trader_view": ((snapshot.get("trader_view_by_symbol") or {}).get(symbol_u) or {}),
        "chain_report": ((snapshot.get("chain_report_by_symbol") or {}).get(symbol_u) or {}),
        "startup_consistency": ((snapshot.get("startup_consistency_by_symbol") or {}).get(symbol_u) or {}),
        "runtime_health": ((snapshot.get("runtime_health_by_symbol") or {}).get(symbol_u) or {}),
        "conflict_report": ((snapshot.get("conflict_report_by_symbol") or {}).get(symbol_u) or {}),
        "last_preview": ((snapshot.get("last_preview_by_symbol") or {}).get(symbol_u) or {}),
    }


def build_index_audit_chain(
    *,
    audit_root: Path,
    snapshot_path: Path,
    strategy_id: str,
    date: Optional[str] = None,
    hour: Optional[str] = None,
    trace_id: Optional[str] = None,
    order_id: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    symbol: Optional[str] = None,
) -> Dict[str, Any]:
    rows = query_records(
        audit_root,
        date=date,
        hour=hour,
        trace_id=trace_id,
        order_id=order_id,
        broker_order_id=broker_order_id,
        symbol=symbol,
    )
    snapshot = _load_json(snapshot_path)
    symbol_u = str(symbol or "").strip().upper() or None
    if symbol_u is None:
        for row in rows:
            row_symbol = str(row.get("symbol") or "").strip().upper()
            if row_symbol:
                symbol_u = row_symbol
                break
    symbol_state = _symbol_snapshot(snapshot, symbol_u)
    return {
        "query": {
            "strategy_id": strategy_id,
            "audit_root": str(audit_root),
            "snapshot_path": str(snapshot_path),
            "date": date,
            "hour": str(hour).zfill(2) if hour is not None else None,
            "trace_id": trace_id,
            "order_id": order_id,
            "broker_order_id": broker_order_id,
            "symbol": symbol_u,
        },
        "summary": {
            "record_count": len(rows),
            "domains": sorted({str(row.get("domain") or "") for row in rows if str(row.get("domain") or "").strip()}),
            "stages": [str(row.get("stage") or "") for row in rows],
        },
        "latest_symbol_state": symbol_state,
        "rows": rows,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Query index_super_win local audit chain with latest trader snapshot")
    p.add_argument("--audit-root", default="runtime/audit", help="audit root directory")
    p.add_argument(
        "--snapshot-path",
        default="runtime/state/index_super_win_diag_snapshot_index_super_win_001.json",
        help="index_super_win diag snapshot path",
    )
    p.add_argument("--strategy-id", default="index_super_win_001")
    p.add_argument("--date", help="ET date directory, e.g. 2026-03-14")
    p.add_argument("--hour", help="ET hour, e.g. 10")
    p.add_argument("--trace-id")
    p.add_argument("--order-id")
    p.add_argument("--broker-order-id")
    p.add_argument("--symbol")
    p.add_argument("--json", action="store_true", help="emit full JSON")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    payload = build_index_audit_chain(
        audit_root=Path(args.audit_root).expanduser(),
        snapshot_path=Path(args.snapshot_path).expanduser(),
        strategy_id=args.strategy_id,
        date=args.date,
        hour=args.hour,
        trace_id=args.trace_id,
        order_id=args.order_id,
        broker_order_id=args.broker_order_id,
        symbol=args.symbol,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    summary = payload.get("summary") or {}
    state = payload.get("latest_symbol_state") or {}
    print(
        f"records={summary.get('record_count', 0)} "
        f"domains={','.join(summary.get('domains') or []) or '-'} "
        f"symbol={((payload.get('query') or {}).get('symbol')) or '-'} "
        f"final_state={((state.get('trader_view') or {}).get('final_state')) or '-'} "
        f"which_gate_blocked={((state.get('trader_view') or {}).get('which_gate_blocked')) or '-'}"
    )
    for row in payload.get("rows") or []:
        print(
            f"{row.get('ts')} "
            f"{row.get('domain')}/{row.get('component')} "
            f"{row.get('stage')} "
            f"symbol={row.get('symbol') or '-'} "
            f"status={row.get('status') or '-'} "
            f"reason={row.get('reason') or '-'} "
            f"trace_id={row.get('trace_id') or '-'} "
            f"broker_order_id={row.get('broker_order_id') or '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
