from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from strategies.stocks_kernel.runtime.audit import query_audit_chain


def build_stocks_kernel_audit_chain(
    *,
    audit_root: Path,
    strategy_id: str | None = None,
    symbol: str | None = None,
    position_lot_id: str | None = None,
    order_ref: str | None = None,
    trace_id: str | None = None,
    broker_order_id: str | None = None,
    stage: str | None = None,
    domain: str | None = None,
    date: str | None = None,
    hour: str | None = None,
) -> dict[str, Any]:
    rows = query_audit_chain(
        root_dir=audit_root,
        strategy_id=strategy_id,
        symbol=symbol,
        position_lot_id=position_lot_id,
        order_ref=order_ref,
        trace_id=trace_id,
        broker_order_id=broker_order_id,
        stage=stage,
        domain=domain,
        date=date,
        hour=hour,
    )
    return {
        "query": {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "position_lot_id": position_lot_id,
            "order_ref": order_ref,
            "trace_id": trace_id,
            "broker_order_id": broker_order_id,
            "stage": stage,
            "domain": domain,
            "date": date,
            "hour": hour,
        },
        "count": len(rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Query stocks-kernel audit chain")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--strategy-id")
    parser.add_argument("--symbol")
    parser.add_argument("--position-lot-id")
    parser.add_argument("--order-ref")
    parser.add_argument("--trace-id")
    parser.add_argument("--broker-order-id")
    parser.add_argument("--stage")
    parser.add_argument("--domain")
    parser.add_argument("--date")
    parser.add_argument("--hour")
    args = parser.parse_args()
    payload = build_stocks_kernel_audit_chain(
        audit_root=Path(args.root),
        strategy_id=args.strategy_id,
        symbol=args.symbol,
        position_lot_id=args.position_lot_id,
        order_ref=args.order_ref,
        trace_id=args.trace_id,
        broker_order_id=args.broker_order_id,
        stage=args.stage,
        domain=args.domain,
        date=args.date,
        hour=args.hour,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
