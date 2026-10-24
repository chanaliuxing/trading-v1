#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.replay.export_strategy_traces_for_shadow_session import export_strategy_traces_for_shadow_session


def export_replica_traces_for_shadow_session(*, sample_dir: Path, audit_root: Path, output_dir: Path, strategy_id: str):
    return export_strategy_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        strategy_id=strategy_id,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Export replica audit traces for one captured shadow session")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--strategy-id", required=True)
    args = parser.parse_args()

    report = export_replica_traces_for_shadow_session(
        sample_dir=Path(args.sample_dir),
        audit_root=Path(args.audit_root),
        output_dir=Path(args.output_dir),
        strategy_id=str(args.strategy_id),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if int(report.get("missing_count", 0)) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
