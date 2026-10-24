#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.production.ib_restart_drill_artifact import build_ib_restart_drill_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a paper IB restart go/no-go artifact from runtime audit JSONL")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--day", default=None)
    parser.add_argument("--start-ts", default=None)
    parser.add_argument("--end-ts", default=None)
    parser.add_argument("--drill-id", default="paper_ib_restart_latest")
    parser.add_argument("--output-dir", default="runtime/readiness/drills")
    args = parser.parse_args()

    artifact = build_ib_restart_drill_artifact(
        repo_root=Path(args.repo_root),
        day=args.day,
        start_ts=args.start_ts,
        end_ts=args.end_ts,
        drill_id=args.drill_id,
        output_dir=args.output_dir,
    )
    print(json.dumps(artifact, ensure_ascii=False, sort_keys=True))
    return 0 if artifact.get("go_no_go") == "go" else 2


if __name__ == "__main__":
    raise SystemExit(main())
