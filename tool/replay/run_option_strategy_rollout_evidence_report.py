#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.report.build_option_strategy_rollout_evidence_report import build_option_strategy_rollout_evidence_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run option rollout evidence report")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--output", default=str(ROOT / "runtime" / "state" / "option_strategy_rollout_evidence_report.json"))
    args = parser.parse_args()
    report = build_option_strategy_rollout_evidence_report(args.config)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if int(report.get("summary", {}).get("active_rollout_contract_fail_count", 0) or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
