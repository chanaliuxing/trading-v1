from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.exec.execution_service import ExecutionResult
from services.health import SystemHealthController
from services.orders import UnifiedOrderManager
from services.production.recovery_drill import RuntimeRecoveryDrillRunner
from services.production.recovery_readiness import get_recovery_readiness_service
from services.reconciliation.runtime_reconciler import RuntimeReconciler
from services.reconciliation.strategy_recovery_policy import default_strategy_recovery_policy_registry


@dataclass
class _Contract:
    symbol: str
    secType: str = "STK"


@dataclass
class _Position:
    symbol: str
    position: float

    @property
    def contract(self) -> _Contract:
        return _Contract(symbol=self.symbol)


class _FakeAdapter:
    def __init__(self, *, symbol: str, qty: float) -> None:
        self.symbol = symbol
        self.qty = float(qty)

    async def open_orders(self, *, force_refresh: bool = False) -> list[Any]:
        return []

    def positions(self) -> list[_Position]:
        if abs(self.qty) <= 1e-9:
            return []
        return [_Position(symbol=self.symbol, position=self.qty)]


class _FakePg:
    async def fetchall(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM core.core_order_quarantine" in query:
            return []
        if "FROM core.core_order" in query:
            return []
        if "FROM core.position_ledger_current" in query:
            return []
        return []

    async def fetchone(self, query: str, *args: Any) -> dict[str, Any] | None:
        return None

    async def execute(self, query: str, *args: Any) -> None:
        return None


class _FakeHealthController:
    state = "recovering"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, **event: Any) -> None:
        self.events.append(dict(event))
        if event.get("event_type") == "system_resumed":
            self.state = "running"
        elif event.get("event_type") == "system_degraded":
            self.state = "degraded"


class _FakeExecutionService:
    pg = None

    def __init__(self, adapter: _FakeAdapter) -> None:
        self.adapter = adapter
        self.submitted: list[dict[str, Any]] = []

    async def execute(self, result: Any) -> ExecutionResult:
        metadata = dict(getattr(result, "metadata", {}) or {})
        extras = dict(metadata.get("envelope_extras") or {})
        self.submitted.append(
            {
                "decision_id": str(getattr(result, "decision_id", "")),
                "strategy_id": str(getattr(result, "strategy_id", "")),
                "symbol": str(getattr(result, "symbol", "")),
                "stage": str(getattr(result, "stage", "")),
                "runtime_recovery_action": bool(extras.get("runtime_recovery_action")),
                "recovery_intent_id": str(extras.get("recovery_intent_id") or ""),
            }
        )
        # Simulate broker fill before the next reconciler cycle.
        self.adapter.qty = 0.0
        return ExecutionResult(
            success=True,
            decision_id=str(getattr(result, "decision_id", "")),
            order_id=1001,
            reason="fake_recovery_flatten_submitted",
            stage="execution",
            idempotency_key=str(extras.get("recovery_intent_id") or getattr(result, "decision_id", "")),
        )


class _FlatLedger:
    async def current_position(self, *, strategy_id: str, symbol: str, lot_id: str | None = None) -> dict[str, Any]:
        return {
            "qty": 0.0,
            "updated_at": "2026-05-05T09:30:00-04:00",
            "artifact_stale": True,
        }


class _StaleArtifactExecutionService(_FakeExecutionService):
    def __init__(self, adapter: _FakeAdapter) -> None:
        super().__init__(adapter)
        self.trading_gate_provider = lambda _symbol: {
            "exec_ready": True,
            "market_ready": False,
            "master_truth_complete": True,
            "trading_enabled": False,
            "stale_data_artifact": True,
            "stale_readiness_artifact": True,
        }


@pytest.mark.asyncio
async def test_broker_safety_flatten_ignores_stale_ledger_data_and_halted_state(tmp_path: Path) -> None:
    (tmp_path / "runtime/readiness/production_state_latest.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime/readiness/production_state_latest.json").write_text(
        '{"go_no_go":"no_go","generated_at":"2026-05-05T09:30:00-04:00","reason":"stale_artifact"}',
        encoding="utf-8",
    )
    adapter = _FakeAdapter(symbol="TQQQ", qty=10.0)
    health = _FakeHealthController()
    health.state = "halted"
    execution = _StaleArtifactExecutionService(adapter)
    uom = UnifiedOrderManager(
        execution_service=execution,
        system_health_controller=health,
        position_ledger=_FlatLedger(),
    )
    result = SimpleNamespace(
        decision_id="runtime_recovery:qqq_intraday_001:TQQQ:long:flatten",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=10.0),
        metadata={
            "envelope_extras": {
                "flatten_action": True,
                "close_only": True,
                "runtime_recovery_action": True,
                "broker_position_qty_hint": 10.0,
                "recovery_intent_id": "runtime_recovery:qqq_intraday_001:TQQQ:long:flatten",
            }
        },
    )

    outcome = await uom.submit_flatten(result)

    assert outcome.success is True
    assert outcome.reason == "fake_recovery_flatten_submitted"
    assert adapter.qty == 0.0
    extras = result.metadata["envelope_extras"]
    assert extras["uom_safety_lane"] is True
    assert extras["uom_safety_lane_market_ready"] is False
    assert extras["uom_ledger_gate_bypassed_for_broker_truth_safety"] is True
    assert extras["uom_ledger_repair_from_position_hint"] is True
    assert execution.submitted[0]["runtime_recovery_action"] is True


@pytest.mark.asyncio
async def test_runtime_recovery_drill_generates_go_artifact(tmp_path: Path) -> None:
    adapter = _FakeAdapter(symbol="TQQQ", qty=10.0)
    health = _FakeHealthController()
    execution = _FakeExecutionService(adapter)
    uom = UnifiedOrderManager(execution_service=execution, system_health_controller=health)
    reconciler = RuntimeReconciler(
        pg=_FakePg(),
        ib_adapter=adapter,
        system_health_controller=health,
        strategy_recovery_policy_registry=default_strategy_recovery_policy_registry({"qqq_intraday_001"}),
        recovery_order_manager=uom,
        recovery_action_cooldown_seconds=0.0,
        query_offhours=True,
    )
    reconciler.symbol_strategy_owner_map = {"TQQQ": "qqq_intraday_001"}
    reconciler._audit_positions = lambda **kwargs: None
    reconciler._audit_flatten_lifecycle = lambda **kwargs: None
    reconciler._maybe_push_alert = lambda **kwargs: None

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    reconciler._audit_positions = _noop
    reconciler._audit_flatten_lifecycle = _noop
    reconciler._maybe_push_alert = _noop

    artifact = await RuntimeRecoveryDrillRunner(artifact_dir=tmp_path).run(
        reconciler=reconciler,
        drill_id="ib_restart_broker_only_recovery",
        cycles=2,
    )

    assert artifact["go_no_go"] == "go"
    assert artifact["final_truth"]["position_mismatches"] == 0
    assert [event["event_type"] for event in artifact["health_events"]] == [
        "system_degraded",
        "system_resumed",
    ]
    assert len(artifact["submitted_orders"]) == 1
    assert artifact["submitted_orders"][0]["runtime_recovery_action"] is True
    assert artifact["duplicate_order_count"] == 0
    assert (tmp_path / "ib_restart_broker_only_recovery.json").exists()


@pytest.mark.asyncio
async def test_ib_recovery_health_fanout_waits_for_market_data_readiness() -> None:
    readiness = get_recovery_readiness_service()
    readiness.clear()
    controller = SystemHealthController()
    required_components = [
        "market_data_service",
        "market_data_ib",
        "market_data_subscriptions",
        "bar_pipeline",
        "tick_producer",
    ]
    reconciler = RuntimeReconciler(
        pg=_FakePg(),
        ib_adapter=_FakeAdapter(symbol="TQQQ", qty=0.0),
        system_health_controller=controller,
        recovery_required_components=required_components,
    )
    reconciler._running = True
    reconciler._event_loop = asyncio.get_running_loop()
    reconciler._register_recovery_readiness_listener()
    await controller.subscribe("runtime_reconciler", reconciler)

    async def _clean_once() -> None:
        await reconciler._publish_runtime_health_if_needed(
            ghosts=0,
            unresolved_orphans=0,
            mismatches=0,
            position_mismatches=0,
            active_quarantine_count=0,
        )

    reconciler.run_once = _clean_once

    for component in required_components:
        readiness.mark(component, ready=False, reason="ib_reconnect_in_progress")
    await controller.on_ib_connection_event(
        {
            "event_type": "broker_api_ready",
            "reason": "ib_runtime_recovery_api_ready",
            "source": "ib_connection_manager.runtime_recovery",
            "requires_runtime_reconciler": True,
        }
    )
    if reconciler._event_reconcile_task is not None:
        await reconciler._event_reconcile_task

    assert controller.state == "recovering"
    assert controller.last_event["event_type"] == "system_recovering"
    assert controller.last_event["reason"] == "platform_recovery_readiness_not_met"
    blockers = set(controller.last_event["metadata"]["recovery_readiness"]["blockers"])
    assert "market_data_service_not_ready" in blockers
    assert "bar_pipeline_not_ready" in blockers

    for component in required_components:
        readiness.mark(
            component,
            ready=True,
            reason=f"{component}_ready",
            metadata={"ttl_sec": 30},
        )
    await asyncio.sleep(0)
    if reconciler._event_reconcile_task is not None:
        await reconciler._event_reconcile_task

    assert controller.state == "running"
    assert controller.last_event["event_type"] == "system_resumed"
    assert controller.last_event["metadata"]["recovery_readiness"]["ready"] is True
    await reconciler.stop()
    readiness.clear()
