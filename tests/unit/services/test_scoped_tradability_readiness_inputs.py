from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService
from services.production.scoped_tradability import (
    FlowType,
    TradabilityScope,
    build_tradability_inputs_from_readiness_artifacts,
)


NOW = datetime.fromisoformat("2026-05-12T10:00:00-04:00")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _scope(*, flow_type: FlowType = FlowType.ENTRY, strategy_instance_id: str = "strategy_a_001") -> TradabilityScope:
    return TradabilityScope(
        account_id="DU123",
        strategy_id="strategy_a",
        strategy_instance_id=strategy_instance_id,
        symbol="NVDA",
        asset_type="STK",
        session="RTH",
        flow_type=flow_type,
        order_role=flow_type.value,
    )


def _write_good_hard_artifacts(root: Path) -> None:
    _write(
        root / "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json",
        {
            "generated_at": NOW.isoformat(),
            "go_no_go": "go",
            "broker_truth_backed": True,
            "live_broker_truth_available": True,
            "lookup_failed": False,
            "stale": False,
            "blockers": [],
        },
    )
    _write(
        root / "runtime/readiness/position_safety/position_safety_latest.json",
        {
            "generated_at": NOW.isoformat(),
            "go_no_go": "go",
            "lookup_failed": False,
            "stale": False,
            "blockers": [],
        },
    )
    _write(
        root / "runtime/readiness/runtime_version_latest.json",
        {
            "generated_at": NOW.isoformat(),
            "freshness": {
                "loaded_code_current": True,
                "runtime_version_loaded_code_stale": False,
            },
            "blockers": [],
        },
    )


def test_readiness_inputs_fail_closed_when_broker_truth_missing(tmp_path: Path) -> None:
    _write_good_hard_artifacts(tmp_path)
    (tmp_path / "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json").unlink()

    inputs = build_tradability_inputs_from_readiness_artifacts(
        repo_root=tmp_path,
        scope=_scope(),
        now=NOW,
    )

    assert "broker_truth_missing" in inputs.global_blockers


def test_readiness_inputs_map_hard_artifact_blockers(tmp_path: Path) -> None:
    _write_good_hard_artifacts(tmp_path)
    _write(
        tmp_path / "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json",
        {
            "generated_at": NOW.isoformat(),
            "go_no_go": "no_go",
            "broker_truth_backed": False,
            "live_broker_truth_available": False,
            "lookup_failed": True,
            "blockers": ["order_truth_active_quarantine"],
        },
    )
    _write(
        tmp_path / "runtime/readiness/position_safety/position_safety_latest.json",
        {"generated_at": NOW.isoformat(), "go_no_go": "no_go", "blockers": ["manual_lock_active"]},
    )
    _write(
        tmp_path / "runtime/readiness/runtime_version_latest.json",
        {
            "generated_at": NOW.isoformat(),
            "freshness": {
                "loaded_code_current": False,
                "runtime_version_loaded_code_stale": True,
            },
        },
    )

    inputs = build_tradability_inputs_from_readiness_artifacts(
        repo_root=tmp_path,
        scope=_scope(),
        now=NOW,
    )

    assert "broker_truth_missing" in inputs.global_blockers
    assert "order_truth_unreconciled" in inputs.global_blockers
    assert "position_safety_no_go" in inputs.global_blockers
    assert "runtime_version_loaded_code_stale" in inputs.global_blockers


def test_readiness_inputs_prefer_production_state_runtime_version_over_stale_legacy_file(tmp_path: Path) -> None:
    _write_good_hard_artifacts(tmp_path)
    _write(
        tmp_path / "runtime/readiness/runtime_version_latest.json",
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "freshness": {
                "loaded_code_current": False,
                "runtime_version_loaded_code_stale": True,
            },
            "blockers": ["runtime_version_loaded_code_stale"],
        },
    )
    _write(
        tmp_path / "runtime/readiness/production_state/production_state_latest.json",
        {
            "generated_at": NOW.isoformat(),
            "go_no_go": "go",
            "global_halt": False,
            "runtime_version": {
                "generated_at": NOW.isoformat(),
                "loaded_code_current": True,
                "freshness": {
                    "loaded_code_current": True,
                    "runtime_version_loaded_code_stale": False,
                    "restart_required": False,
                    "status": "CURRENT",
                },
                "blockers": [],
            },
        },
    )

    inputs = build_tradability_inputs_from_readiness_artifacts(
        repo_root=tmp_path,
        scope=_scope(),
        now=NOW,
    )

    assert "runtime_version_loaded_code_stale" not in inputs.global_blockers


def test_readiness_inputs_map_matching_production_state_scope_only(tmp_path: Path) -> None:
    _write_good_hard_artifacts(tmp_path)
    _write(
        tmp_path / "runtime/readiness/production_state/production_state_latest.json",
        {
            "global_halt": False,
            "blocked_scopes": [
                {
                    "scope_type": "STRATEGY_INSTANCE",
                    "account_id": "DU123",
                    "strategy_id": "strategy_a",
                    "strategy_instance_id": "strategy_a_001",
                    "symbol": "NVDA",
                    "flow_type": "ENTRY",
                    "blockers": ["strategy_manual_lock"],
                }
            ],
        },
    )

    matching = build_tradability_inputs_from_readiness_artifacts(repo_root=tmp_path, scope=_scope(), now=NOW)
    other = build_tradability_inputs_from_readiness_artifacts(
        repo_root=tmp_path,
        scope=_scope(strategy_instance_id="strategy_b_001"),
        now=NOW,
    )

    assert tuple(blocker.blocker for blocker in matching.scoped_blockers) == ("strategy_manual_lock",)
    assert other.scoped_blockers == ()


def test_readiness_inputs_do_not_treat_scoped_manual_lock_as_global_halt(tmp_path: Path) -> None:
    _write_good_hard_artifacts(tmp_path)
    _write(
        tmp_path / "runtime/readiness/production_state/production_state_latest.json",
        {
            "global_halt": False,
            "entry_go_no_go": "partial",
            "blocked_scopes": [
                {
                    "scope_type": "STRATEGY_INSTANCE",
                    "account_id": "DU123",
                    "strategy_id": "strategy_a",
                    "strategy_instance_id": "strategy_a_001",
                    "symbol": "NVDA",
                    "flow_type": "ENTRY",
                    "blockers": ["manual_intervention_lock_active"],
                    "authority": "TradabilityService",
                }
            ],
        },
    )

    locked = build_tradability_inputs_from_readiness_artifacts(repo_root=tmp_path, scope=_scope(), now=NOW)
    other = build_tradability_inputs_from_readiness_artifacts(
        repo_root=tmp_path,
        scope=_scope(strategy_instance_id="strategy_b_001"),
        now=NOW,
    )

    assert locked.global_blockers == ()
    assert tuple(blocker.blocker for blocker in locked.scoped_blockers) == ("manual_intervention_lock_active",)
    assert other.global_blockers == ()
    assert other.scoped_blockers == ()


def test_readiness_inputs_apply_option_trigger_blocker_only_to_required_strategy(tmp_path: Path) -> None:
    _write_good_hard_artifacts(tmp_path)
    _write(
        tmp_path / "runtime/readiness/production_state/production_state_latest.json",
        {
            "global_halt": False,
            "entry_go_no_go": "partial",
            "blocked_scopes": [
                {
                    "scope_type": "STRATEGY_INSTANCE",
                    "account_id": None,
                    "strategy_id": "qqq_intraday_option_v1_001",
                    "strategy_instance_id": "qqq_intraday_option_v1_001",
                    "symbol": None,
                    "flow_type": "ENTRY",
                    "blockers": ["option_trigger_active_without_terminal_verdict"],
                    "authority": "TradabilityService",
                }
            ],
        },
    )

    option_scope = build_tradability_inputs_from_readiness_artifacts(
        repo_root=tmp_path,
        scope=_scope(strategy_instance_id="qqq_intraday_option_v1_001"),
        now=NOW,
    )
    stock_scope = build_tradability_inputs_from_readiness_artifacts(
        repo_root=tmp_path,
        scope=_scope(strategy_instance_id="stocks_kernel_smh_001"),
        now=NOW,
    )

    assert tuple(blocker.blocker for blocker in option_scope.scoped_blockers) == (
        "option_trigger_active_without_terminal_verdict",
    )
    assert stock_scope.scoped_blockers == ()


@pytest.mark.asyncio
async def test_execution_service_uses_readiness_provider_before_risk(tmp_path: Path) -> None:
    _write_good_hard_artifacts(tmp_path)
    _write(
        tmp_path / "runtime/readiness/broker_truth_snapshot/broker_truth_snapshot_latest.json",
        {
            "generated_at": NOW.isoformat(),
            "go_no_go": "no_go",
            "broker_truth_backed": False,
            "live_broker_truth_available": False,
            "lookup_failed": True,
        },
    )
    risk = MagicMock()
    risk.approve = AsyncMock()
    svc = ExecutionService(
        pg=MagicMock(),
        risk_service=risk,
        safety_kernel=MagicMock(),
        enable_execution=False,
        order_event_enabled=False,
        tradability_inputs_provider=lambda context: build_tradability_inputs_from_readiness_artifacts(
            repo_root=tmp_path,
            scope=context["scope"],
            now=NOW,
        ),
    )
    result = PipelineResult(
        status="APPROVED",
        decision_id="d-readiness",
        strategy_id="strategy_a",
        symbol="NVDA",
        plan=ExecPlan(
            order_type="MKT",
            side="BUY",
            qty=1,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"account_id": "DU123", "strategy_instance_id": "strategy_a_001"}},
    )

    out = await svc.execute(result)

    assert out.success is False
    assert out.reason == "scoped_tradability_denied:broker_truth_missing"
    assert out.metadata["tradability_decision"]["blockers"][0] == "broker_truth_missing"
    risk.approve.assert_not_awaited()
