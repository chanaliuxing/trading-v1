#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tool.report.build_stocks_kernel_audit_daily_summary import build_daily_summary


def _load_optional(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_validation_daily_bundle(
    *,
    audit_root: Path,
    date: str,
    parity_path: Path | None = None,
    shadow_path: Path | None = None,
    paper_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "date": date,
        "audit_summary": build_daily_summary(audit_root=audit_root, date=date),
        "parity_report": _load_optional(parity_path),
        "shadow_report": _load_optional(shadow_path),
        "paper_report": _load_optional(paper_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build stocks-kernel validation daily bundle")
    parser.add_argument("--root", default="runtime/audit")
    parser.add_argument("--date", required=True)
    parser.add_argument("--parity")
    parser.add_argument("--shadow")
    parser.add_argument("--paper")
    args = parser.parse_args()
    payload = build_validation_daily_bundle(
        audit_root=Path(args.root),
        date=str(args.date),
        parity_path=Path(args.parity) if args.parity else None,
        shadow_path=Path(args.shadow) if args.shadow else None,
        paper_path=Path(args.paper) if args.paper else None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
