#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.reports.option_live_parity_report import generate_option_live_parity_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate option live parity report from snapshots/audit/replay output.")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--date", required=True, help="ET day, e.g. 2026-03-25")
    parser.add_argument("--snapshot-root", default="runtime/state/decision_snapshots")
    parser.add_argument("--audit-root", default="runtime/audit")
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    report = generate_option_live_parity_report(
        strategy_id=args.strategy_id,
        report_date=args.date,
        snapshot_root=args.snapshot_root,
        audit_root=args.audit_root,
        replay_dir=args.replay_dir,
    )
    payload = report.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.out:
        path = Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
