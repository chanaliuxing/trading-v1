#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.report.build_option_active_strategy_evidence_status import build_option_active_strategy_evidence_status


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run active option strategy evidence status report")
    parser.add_argument("--state-root", default=str(ROOT / "runtime" / "state"))
    parser.add_argument("--platform-report", default=str(ROOT / "runtime" / "state" / "option_platform_admission_gate.json"))
    parser.add_argument(
        "--rollout-report",
        default=str(ROOT / "runtime" / "state" / "option_strategy_rollout_evidence_report.json"),
    )
    parser.add_argument("--output", default=str(ROOT / "runtime" / "state" / "option_active_strategy_evidence_status.json"))
    args = parser.parse_args()

    report = build_option_active_strategy_evidence_status(
        _load_json(Path(args.platform_report).expanduser()),
        _load_json(Path(args.rollout_report).expanduser()),
        state_root=args.state_root,
    )
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report_path": str(out), "recommendation": report.get("recommendation")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
