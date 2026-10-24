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

from tool.report.build_qqq_state_shadow_acceptance import build_qqq_state_shadow_acceptance

DEFAULT_QQQ_OPTION_STRATEGY_ID = "qqq_intraday_option_v1_001"
DEFAULT_QQQ_OPTION_BASELINE_PATH = ROOT / "docs" / "baselines" / "qqq_intraday_option_v1_001_replica_baseline.json"
LEGACY_QQQ_BASELINE_PATH = ROOT / "docs" / "baselines" / "qqq_intraday_001_replica_baseline.json"


def _default_baseline_path() -> Path:
    if DEFAULT_QQQ_OPTION_BASELINE_PATH.exists():
        return DEFAULT_QQQ_OPTION_BASELINE_PATH
    return LEGACY_QQQ_BASELINE_PATH


def run_qqq_state_shadow_acceptance(
    *,
    audit_root: Path,
    output_root: Path,
    date: str,
    strategy_id: str = DEFAULT_QQQ_OPTION_STRATEGY_ID,
    baseline_path: Path,
    state_instrumentation_start_date: str | None = "2026-04-03",
    source: str = "local",
    clickhouse_table: str | None = None,
) -> Dict[str, Any]:
    output_dir = output_root / date
    output_dir.mkdir(parents=True, exist_ok=True)
    acceptance = build_qqq_state_shadow_acceptance(
        audit_root=audit_root,
        date=date,
        strategy_id=strategy_id,
        baseline_path=baseline_path,
        state_instrumentation_start_date=state_instrumentation_start_date,
        source=source,
        clickhouse_table=clickhouse_table,
    )
    report_path = output_dir / "qqq_state_shadow_acceptance.json"
    report_path.write_text(json.dumps(acceptance, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    acceptance["report_path"] = str(report_path)
    return acceptance


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one-click QQQ state shadow acceptance pipeline")
    parser.add_argument("--root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-root", default=str(ROOT / "runtime" / "state" / "qqq_state_shadow_acceptance"))
    parser.add_argument("--date", required=True)
    parser.add_argument("--strategy-id", default=DEFAULT_QQQ_OPTION_STRATEGY_ID)
    parser.add_argument("--baseline-path", default=str(_default_baseline_path()))
    parser.add_argument("--state-instrumentation-start-date", default="2026-04-03")
    parser.add_argument("--source", choices=["local", "clickhouse", "auto"], default="local")
    parser.add_argument("--clickhouse-table")
    args = parser.parse_args()

    report = run_qqq_state_shadow_acceptance(
        audit_root=Path(args.root).expanduser(),
        output_root=Path(args.output_root).expanduser(),
        date=str(args.date),
        strategy_id=str(args.strategy_id),
        baseline_path=Path(args.baseline_path).expanduser(),
        state_instrumentation_start_date=str(args.state_instrumentation_start_date or "").strip() or None,
        source=str(args.source),
        clickhouse_table=str(args.clickhouse_table or "").strip() or None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if str(report.get("acceptance_status")) == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
