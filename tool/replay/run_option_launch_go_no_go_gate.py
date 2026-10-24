#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.report.build_option_launch_go_no_go_gate import build_option_launch_go_no_go_gate


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run option launch GO/BLOCK gate")
    parser.add_argument("--platform-report", default=str(ROOT / "runtime" / "state" / "option_platform_admission_gate.json"))
    parser.add_argument("--rollout-report", default=str(ROOT / "runtime" / "state" / "option_strategy_rollout_evidence_report.json"))
    parser.add_argument(
        "--active-evidence-report",
        default=str(ROOT / "runtime" / "state" / "option_active_strategy_evidence_status.json"),
    )
    parser.add_argument(
        "--qqq-cutover-report",
        default=str(ROOT / "runtime" / "state" / "qqq_state_shadow_acceptance" / "qqq_state_shadow_cutover_recommendation.json"),
    )
    parser.add_argument("--output", default=str(ROOT / "runtime" / "state" / "option_launch_go_no_go_gate.json"))
    args = parser.parse_args()

    report = build_option_launch_go_no_go_gate(
        platform_report=_load_json(Path(args.platform_report).expanduser()),
        rollout_report=_load_json(Path(args.rollout_report).expanduser()),
        active_evidence_report=_load_json(Path(args.active_evidence_report).expanduser()),
        qqq_cutover_report=_load_json(Path(args.qqq_cutover_report).expanduser()),
    )
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report_path": str(out), "recommendation": report.get("recommendation")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
