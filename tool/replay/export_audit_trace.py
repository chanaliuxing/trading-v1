#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.query_audit import query_records


def build_trace_payload(
    *,
    root: Path,
    date: str,
    domain: str,
    hour: str | None,
    trace_id: str | None,
    symbol: str | None,
    stage: str | None,
) -> Dict[str, Any]:
    rows = query_records(
        root,
        date=date,
        domain=domain,
        hour=hour,
        trace_id=trace_id,
        symbol=symbol,
        stage=stage,
    )
    return {
        "meta": {
            "date": date,
            "domain": domain,
            "hour": hour,
            "trace_id": trace_id,
            "symbol": symbol,
            "stage": stage,
            "row_count": len(rows),
        },
        "events": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export audit records into replay trace JSON")
    parser.add_argument("--root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--date", required=True)
    parser.add_argument("--domain", default="exec")
    parser.add_argument("--hour")
    parser.add_argument("--trace-id")
    parser.add_argument("--symbol")
    parser.add_argument("--stage")
    parser.add_argument("--out")
    args = parser.parse_args()

    payload = build_trace_payload(
        root=Path(args.root),
        date=str(args.date),
        domain=str(args.domain),
        hour=str(args.hour) if args.hour is not None else None,
        trace_id=str(args.trace_id) if args.trace_id is not None else None,
        symbol=str(args.symbol) if args.symbol is not None else None,
        stage=str(args.stage) if args.stage is not None else None,
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
