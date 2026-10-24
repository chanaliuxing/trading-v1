#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from strategies.stocks_kernel.runtime.audit import query_audit_chain


def build_daily_summary(*, audit_root: Path, date: str) -> dict[str, Any]:
    rows = query_audit_chain(root_dir=audit_root, date=date)
    by_domain = Counter()
    by_stage = Counter()
    by_reason = Counter()
    by_strategy = Counter()
    for row in rows:
        by_domain[str(row.get("domain") or "unknown")] += 1
        by_stage[str(row.get("stage") or "unknown")] += 1
        by_reason[str(row.get("reason") or "unknown")] += 1
        by_strategy[str(row.get("strategy_id") or "unknown")] += 1
    return {
        "date": date,
        "row_count": len(rows),
        "by_domain": dict(sorted(by_domain.items())),
        "top_stages": by_stage.most_common(20),
        "top_reasons": by_reason.most_common(20),
        "by_strategy": dict(sorted(by_strategy.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build stocks-kernel daily audit summary")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    payload = build_daily_summary(audit_root=Path(args.root), date=str(args.date))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
