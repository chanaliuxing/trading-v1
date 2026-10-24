#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from scripts.ai_trader.research.agent import run_research_daily_cycle_once
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main
from scripts.ai_trader.research.preflight_check import build_preflight_check
from scripts.ai_trader.research.readiness_dashboard import build_readiness_dashboard
from scripts.ai_trader.research.launch_checklist import build_launch_checklist
from scripts.ai_trader.research.launch_verdict import build_launch_verdict
from scripts.ai_trader.research.evidence_integrity import build_evidence_integrity_report
from scripts.ai_trader.research.launch_batch import build_launch_batch
from scripts.ai_trader.research.ops_summary import (
    build_ops_summary,
    persist_launch_registry,
    persist_system_status,
)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _load_json(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except Exception:
        return {}
    return {}


def _cycle_default() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _cycle_dir(cycle_id: str) -> Path:
    return Path("runs/ai_trader/research/cycles") / cycle_id


def _state_root() -> Path:
    return Path("runs/ai_trader/research/state")


def _run_preflight_bundle(*, cycle_id: str) -> Dict[str, str]:
    cycle_dir = _cycle_dir(cycle_id)
    state_root = _state_root()

    operator_controls_path = state_root / "operator_controls.json"
    capital_governor_path = cycle_dir / "capital_governor_report.json"
    session_control_path = cycle_dir / "session_control_report.json"
    admission_gate_path = cycle_dir / "admission_gate.json"
    health_monitor_path = cycle_dir / "health_monitor_report.json"
    execution_feedback_path = cycle_dir / "execution_feedback.json"
    lifecycle_path = state_root / "candidate_lifecycle_index.json"
    active_registry_path = state_root / "active_registry.json"
    live_feedback_index_path = state_root / "live_feedback_index.json"
    auto_halt_path = cycle_dir / "auto_halt_report.json"
    lifecycle_report_path = cycle_dir / "candidate_lifecycle_report.json"
    allocation_plan_path = cycle_dir / "allocation_plan.json"
    portfolio_selection_path = cycle_dir / "portfolio_selection.json"

    evidence_integrity = build_evidence_integrity_report(
        cycle_id=cycle_id,
        evidence_paths={
            "admission_gate": str(admission_gate_path),
            "health_monitor": str(health_monitor_path),
            "operator_controls": str(operator_controls_path),
            "capital_governor": str(capital_governor_path),
            "session_control": str(session_control_path),
            "execution_feedback": str(execution_feedback_path),
            "candidate_lifecycle": str(lifecycle_path),
        },
    )
    preflight = build_preflight_check(
        cycle_id=cycle_id,
        operator_controls=_load_json(operator_controls_path),
        capital_governor_report=_load_json(capital_governor_path),
        session_control_report=_load_json(session_control_path),
        admission_gate=_load_json(admission_gate_path),
        health_monitor_report=_load_json(health_monitor_path),
        active_registry=_load_json(active_registry_path),
        candidate_lifecycle_index=_load_json(lifecycle_path),
        live_feedback_index=_load_json(live_feedback_index_path),
        required_artifacts=[
            str(operator_controls_path),
            str(capital_governor_path),
            str(session_control_path),
            str(admission_gate_path),
            str(health_monitor_path),
            str(active_registry_path),
            str(lifecycle_path),
            str(live_feedback_index_path),
            str(execution_feedback_path),
        ],
        evidence_integrity_report=evidence_integrity,
    )
    dashboard = build_readiness_dashboard(
        cycle_id=cycle_id,
        preflight_check=preflight,
        admission_gate=_load_json(admission_gate_path),
        health_monitor_report=_load_json(health_monitor_path),
        active_registry=_load_json(active_registry_path),
    )
    checklist = build_launch_checklist(
        cycle_id=cycle_id,
        preflight_check=preflight,
        readiness_dashboard=dashboard,
    )
    verdict = build_launch_verdict(
        cycle_id=cycle_id,
        preflight_check=preflight,
        launch_checklist=checklist,
        readiness_dashboard=dashboard,
        admission_gate=_load_json(admission_gate_path),
    )
    launch_batch = build_launch_batch(
        cycle_id=cycle_id,
        mode="small_live" if verdict.get("verdict") == "go_small_live" else "paper",
        launch_verdict=verdict,
        admission_gate=_load_json(admission_gate_path),
        allocation_plan=_load_json(allocation_plan_path),
        portfolio_selection=_load_json(portfolio_selection_path),
        evidence_integrity_report=evidence_integrity,
        min_candidates=2,
    )
    ops_summary = build_ops_summary(
        cycle_id=cycle_id,
        active_registry=_load_json(active_registry_path),
        auto_halt_report=_load_json(auto_halt_path),
        candidate_lifecycle_report=_load_json(lifecycle_report_path),
        live_feedback=_load_json(cycle_dir / "live_feedback.json"),
    )

    preflight_path = cycle_dir / "preflight_check.json"
    checklist_path = cycle_dir / "launch_checklist.json"
    dashboard_path = cycle_dir / "readiness_dashboard.json"
    verdict_path = cycle_dir / "launch_verdict.json"
    evidence_path = cycle_dir / "evidence_integrity_report.json"
    batch_path = cycle_dir / "launch_batch.json"
    ops_path = cycle_dir / "ops_summary.json"
    for path, payload in [
        (preflight_path, preflight),
        (checklist_path, checklist),
        (dashboard_path, dashboard),
        (verdict_path, verdict),
        (evidence_path, evidence_integrity),
        (batch_path, launch_batch),
        (ops_path, ops_summary),
    ]:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    launch_registry_path = persist_launch_registry(
        cycle_id=cycle_id,
        launch_verdict=verdict,
        launch_batch=launch_batch,
        active_registry=_load_json(active_registry_path),
        state_root=state_root,
    )
    system_status_path = persist_system_status(
        cycle_id=cycle_id,
        operator_controls=_load_json(operator_controls_path),
        risk_guard_report=_load_json(cycle_dir / "risk_guard_report.json"),
        launch_verdict=verdict,
        state_root=state_root,
    )
    return {
        "cycle_dir": str(cycle_dir),
        "preflight_check_json": str(preflight_path),
        "launch_checklist_json": str(checklist_path),
        "readiness_dashboard_json": str(dashboard_path),
        "launch_verdict_json": str(verdict_path),
        "evidence_integrity_report_json": str(evidence_path),
        "launch_batch_json": str(batch_path),
        "ops_summary_json": str(ops_path),
        "launch_registry_json": str(launch_registry_path),
        "system_status_json": str(system_status_path),
        "verdict": str(verdict.get("verdict") or ""),
    }


async def _run_research_mode(cycle_id: str) -> Dict[str, Any]:
    result = await run_research_daily_cycle_once(cycle_id=cycle_id, rerun=True, resume=False)
    return _as_dict(_as_dict(result.get("execution")).get("artifacts"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified launch-control entrypoint")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["fixture", "research", "preflight", "paper_launch_check", "live_launch_check"],
    )
    parser.add_argument("--cycle-id", default=None)
    args = parser.parse_args()
    cycle_id = str(args.cycle_id or _cycle_default())

    if args.mode == "fixture":
        run_fixture_cycle_main()
        print("mode: fixture")
        print("cycle_id: fixture-2026-04-05")
        print("cycle_dir:", _cycle_dir("fixture-2026-04-05"))
        return

    if args.mode == "research":
        artifacts = asyncio.run(_run_research_mode(cycle_id))
        print("mode: research")
        print("cycle_id:", cycle_id)
        print("cycle_dir:", _cycle_dir(cycle_id))
        print("preflight_check.json:", artifacts.get("preflight_check_json"))
        print("launch_verdict.json:", artifacts.get("launch_verdict_json"))
        print("launch_batch.json:", artifacts.get("launch_batch_json"))
        print("ops_summary.json:", artifacts.get("ops_summary_json"))
        return

    bundle = _run_preflight_bundle(cycle_id=cycle_id)
    print("mode:", args.mode)
    print("cycle_id:", cycle_id)
    print("cycle_dir:", bundle["cycle_dir"])
    print("preflight_check.json:", bundle["preflight_check_json"])
    print("launch_checklist.json:", bundle["launch_checklist_json"])
    print("readiness_dashboard.json:", bundle["readiness_dashboard_json"])
    print("launch_verdict.json:", bundle["launch_verdict_json"])
    print("evidence_integrity_report.json:", bundle["evidence_integrity_report_json"])
    print("launch_batch.json:", bundle["launch_batch_json"])
    print("ops_summary.json:", bundle["ops_summary_json"])
    print("launch_registry.json:", bundle["launch_registry_json"])
    print("system_status.json:", bundle["system_status_json"])
    if args.mode == "paper_launch_check":
        print("paper_launch_verdict:", bundle["verdict"])
    elif args.mode == "live_launch_check":
        print("live_launch_verdict:", bundle["verdict"])


if __name__ == "__main__":
    main()

