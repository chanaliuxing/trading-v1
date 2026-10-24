import pytest

from libs.strategy.base import StrategyAlgorithm, StrategyParams
from services.health import SystemHealthController
from services.exec.execution_service import ExecutionResult
from services.orders import UnifiedOrderManager
from services.runtime.lifecycle_controller import RuntimeLifecycleController
from services.strategy.engine import StrategyEngine
from libs.strategy.registry import get_registry
from types import SimpleNamespace


def _resume_metadata(**overrides):
    data = {
        "broker_connected": True,
        "broker_truth_complete": True,
        "order_truth_clean": True,
        "position_ledger_clean": True,
        "strategies_paused_ack": True,
        "platform_recovery_ready": True,
        "recovery_readiness": {"ready": True, "blockers": []},
    }
    data.update(overrides)
    return data


class _SubscribedTestStrategy(StrategyAlgorithm):
    async def initialize(self) -> None:
        await self.subscribe_system_health()

    async def on_features(self, symbol: str, features: dict) -> None:
        return None


class _UnsubscribedTestStrategy(StrategyAlgorithm):
    async def initialize(self) -> None:
        return None

    async def on_features(self, symbol: str, features: dict) -> None:
        return None


@pytest.mark.asyncio
async def test_system_health_controller_requires_explicit_subscription(monkeypatch):
    class _FakePostgresDB:
        pass

    monkeypatch.setattr("libs.db.postgres.PostgresDB", _FakePostgresDB)

    strategy_name = "_subscribed_health_test_strategy"
    get_registry().register(strategy_name, _SubscribedTestStrategy)
    engine = StrategyEngine(system_health_controller=SystemHealthController(), dry_run=True)

    await engine.load_strategy(
        strategy_id="subscribed_strategy_001",
        name=strategy_name,
        params={"cadence_s": 5, "extras": {}},
    )

    assert "subscribed_strategy_001" in engine._strategies
    assert engine.system_health_controller.state == "starting"


@pytest.mark.asyncio
async def test_system_health_controller_rejects_strategy_without_subscription(monkeypatch):
    class _FakePostgresDB:
        pass

    monkeypatch.setattr("libs.db.postgres.PostgresDB", _FakePostgresDB)

    strategy_name = "_unsubscribed_health_test_strategy"
    get_registry().register(strategy_name, _UnsubscribedTestStrategy)
    engine = StrategyEngine(system_health_controller=SystemHealthController(), dry_run=True)

    with pytest.raises(RuntimeError, match="system_health_subscription_required:unsubscribed_strategy_001"):
        await engine.load_strategy(
            strategy_id="unsubscribed_strategy_001",
            name=strategy_name,
            params={"cadence_s": 5, "extras": {}},
        )


@pytest.mark.asyncio
async def test_system_health_controller_pauses_and_resumes_subscribed_strategy() -> None:
    controller = SystemHealthController()
    strategy = _SubscribedTestStrategy(ctx=type("Ctx", (), {"log": None, "metrics": None, "engine": None, "strategy_id": "demo", "system_health": controller})(), params=StrategyParams())

    await controller.register_strategy("demo", strategy)
    await controller.subscribe("demo", strategy)
    await controller.publish(event_type="ib_disconnected", reason="disconnect", source="test")
    assert strategy._system_paused is True

    await controller.publish(event_type="ib_connected", reason="reconnected", source="test")
    assert controller.state == "recovering"
    assert strategy._system_paused is True

    await controller.publish(
        event_type="system_resumed",
        reason="broker_truth_ready",
        source="test",
        metadata=_resume_metadata(),
    )
    assert strategy._system_paused is False


@pytest.mark.asyncio
async def test_system_health_controller_lifecycle_projection_clears_strategy_pause() -> None:
    controller = SystemHealthController()
    strategy = _SubscribedTestStrategy(ctx=type("Ctx", (), {"log": None, "metrics": None, "engine": None, "strategy_id": "demo", "system_health": controller})(), params=StrategyParams())

    await controller.register_strategy("demo", strategy)
    await controller.subscribe("demo", strategy)
    await controller.publish(event_type="ib_disconnected", reason="disconnect", source="test")
    assert strategy._system_paused is True

    lifecycle = RuntimeLifecycleController()
    snapshot = lifecycle.mark_running(reason="ready", source="unit_test")
    await controller.publish_lifecycle_snapshot(snapshot, source="unit_test")

    assert controller.state == "running"
    assert strategy._system_paused is False
    assert controller.last_event["metadata"]["runtime_lifecycle_state"] == "RUNNING"


@pytest.mark.asyncio
async def test_system_health_controller_lifecycle_degraded_pauses_strategy() -> None:
    controller = SystemHealthController()
    strategy = _SubscribedTestStrategy(ctx=type("Ctx", (), {"log": None, "metrics": None, "engine": None, "strategy_id": "demo", "system_health": controller})(), params=StrategyParams())

    await controller.register_strategy("demo", strategy)
    await controller.subscribe("demo", strategy)

    lifecycle = RuntimeLifecycleController()
    snapshot = lifecycle.transition(
        "DEGRADED_CLOSE_ONLY",
        reason="broker_truth_unavailable",
        source="unit_test",
    )
    await controller.publish_lifecycle_snapshot(snapshot, source="unit_test")

    assert controller.state == "degraded"
    assert strategy._system_paused is True


@pytest.mark.asyncio
async def test_system_health_controller_rejects_resume_until_all_conditions_met() -> None:
    controller = SystemHealthController()
    await controller.publish(event_type="ib_disconnected", reason="disconnect", source="test")
    await controller.publish(
        event_type="system_resumed",
        reason="premature",
        source="test",
        metadata=_resume_metadata(broker_truth_complete=False),
    )

    assert controller.state == "recovering"
    assert controller.last_event["event_type"] == "system_recovering"


@pytest.mark.asyncio
async def test_system_health_controller_rejects_resume_without_platform_readiness() -> None:
    controller = SystemHealthController()
    await controller.publish(event_type="ib_disconnected", reason="disconnect", source="test")

    await controller.publish(
        event_type="system_resumed",
        reason="legacy_resume_without_platform_readiness",
        source="test",
        metadata={
            "broker_connected": True,
            "broker_truth_complete": True,
            "order_truth_clean": True,
            "position_ledger_clean": True,
            "strategies_paused_ack": True,
        },
    )

    assert controller.state == "recovering"
    assert controller.last_event["event_type"] == "system_recovering"
    assert controller.last_event["reason"] == "resume_conditions_not_met"


@pytest.mark.asyncio
async def test_system_health_controller_rejects_runtime_resume_while_production_state_no_go() -> None:
    controller = SystemHealthController()
    await controller.publish(
        event_type="system_degraded",
        reason="production_state_no_go",
        source="production_state_emitter",
        metadata={"production_state_go_no_go": "no_go", "production_state_failure_count": 1},
    )

    await controller.publish(
        event_type="system_resumed",
        reason="runtime_reconciliation_clean",
        source="runtime_reconciler",
        metadata={
            "platform_recovery_ready": True,
            "recovery_readiness": {"ready": True},
            "broker_connected": True,
            "broker_truth_complete": True,
            "order_truth_clean": True,
            "position_ledger_clean": True,
            "strategies_paused_ack": True,
        },
    )

    assert controller.state == "recovering"
    assert controller.last_event["event_type"] == "system_recovering"
    assert controller.last_event["reason"] == "resume_conditions_not_met"


@pytest.mark.asyncio
async def test_system_health_controller_allows_resume_with_soft_production_state_blockers() -> None:
    controller = SystemHealthController()
    await controller.publish(
        event_type="system_degraded",
        reason="production_state_no_go",
        source="production_state_emitter",
        metadata={
            "production_state_go_no_go": "no_go",
            "production_state_failure_count": 3,
            "production_state_failures": [
                {"issue_key": "strategy_tradability:qqq_intraday_001:system_paused"},
                {
                    "issue_key": (
                        "data_availability:qqq_intraday_001:QQQ:"
                        "strategy_tradability:entry:stable_entry_guard:data_integrity_fail_closed"
                    )
                },
                {"issue_key": "system_health:degraded"},
                {"issue_key": "ib_restart_drill_artifact_stale"},
                {"issue_key": "ib_restart_drill_no_go"},
            ],
        },
    )

    await controller.publish(
        event_type="system_resumed",
        reason="runtime_reconciliation_clean",
        source="runtime_reconciler",
        metadata={
            "platform_recovery_ready": True,
            "recovery_readiness": {"ready": True},
            "broker_connected": True,
            "broker_truth_complete": True,
            "order_truth_clean": True,
            "position_ledger_clean": True,
            "strategies_paused_ack": True,
        },
    )

    assert controller.state == "running"
    assert controller.last_event["event_type"] == "system_resumed"


@pytest.mark.asyncio
async def test_system_health_controller_emits_transition_audit() -> None:
    class _AuditWriter:
        def __init__(self) -> None:
            self.records = []

        def emit(self, **kwargs):
            self.records.append(kwargs)
            return True

    audit = _AuditWriter()
    controller = SystemHealthController(audit_writer=audit)

    await controller.publish(event_type="system_recovering", reason="ib_restart", source="test")

    assert audit.records
    record = audit.records[-1]
    assert record["domain"] == "system"
    assert record["stage"] == "health_transition"
    assert record["status"] == "recovering"
    assert record["payload"]["event_type"] == "system_recovering"


@pytest.mark.asyncio
async def test_system_health_controller_heartbeat_refreshes_running_audit() -> None:
    class _AuditWriter:
        def __init__(self) -> None:
            self.records = []

        def emit(self, **kwargs):
            self.records.append(kwargs)
            return True

    audit = _AuditWriter()
    controller = SystemHealthController(audit_writer=audit)
    await controller.publish(
        event_type="system_resumed",
        reason="clean",
        source="test",
        metadata={
            "broker_connected": True,
            "broker_truth_complete": True,
            "order_truth_clean": True,
            "position_ledger_clean": True,
            "strategies_paused_ack": True,
            "platform_recovery_ready": True,
            "recovery_readiness": {"ready": True, "blockers": []},
        },
    )

    controller.emit_heartbeat(reason="test_heartbeat", source="test")

    record = audit.records[-1]
    assert record["status"] == "running"
    assert record["reason"] == "test_heartbeat"
    assert record["payload"]["event_type"] == "system_resumed"


@pytest.mark.asyncio
async def test_system_health_controller_heartbeat_accepts_production_metadata() -> None:
    class _AuditWriter:
        def __init__(self) -> None:
            self.records = []

        def emit(self, **kwargs):
            self.records.append(kwargs)
            return True

    audit = _AuditWriter()
    controller = SystemHealthController(audit_writer=audit)
    await controller.publish(
        event_type="system_resumed",
        reason="clean",
        source="test",
        metadata={
            "broker_connected": True,
            "broker_truth_complete": True,
            "order_truth_clean": True,
            "position_ledger_clean": True,
            "strategies_paused_ack": True,
            "platform_recovery_ready": True,
            "recovery_readiness": {"ready": True, "blockers": []},
        },
    )

    controller.emit_heartbeat(
        reason="production_state_soft_no_go",
        source="production_state_emitter",
        metadata={
            "production_state_go_no_go": "no_go",
            "production_state_failure_count": 1,
            "production_state_failures": [{"issue_key": "strategy_tradability:paper_001:system_paused"}],
            "production_state_soft_no_go": True,
        },
    )

    record = audit.records[-1]
    assert record["status"] == "running"
    assert record["reason"] == "production_state_soft_no_go"
    assert record["payload"]["metadata"]["production_state_go_no_go"] == "no_go"
    assert record["payload"]["metadata"]["production_state_soft_no_go"] is True


@pytest.mark.asyncio
async def test_late_strategy_subscription_receives_current_halted_state() -> None:
    controller = SystemHealthController()
    strategy = _SubscribedTestStrategy(ctx=type("Ctx", (), {"log": None, "metrics": None, "engine": None, "strategy_id": "late", "system_health": controller})(), params=StrategyParams())

    await controller.publish(event_type="system_halted", reason="startup_ib_not_ready", source="test")
    await controller.subscribe("late", strategy)

    assert controller.state == "halted"
    assert strategy._system_paused is True


@pytest.mark.asyncio
async def test_ib_disconnect_recovering_and_resume_gate_uom_and_alerts() -> None:
    alerts = []

    async def _alert_sink(**kwargs):
        alerts.append(kwargs)
        return True

    class _FakeExecutionService:
        pg = None

        def __init__(self):
            self.calls = 0
            self._gate = {
                "exec_ready": True,
                "market_ready": True,
                "master_truth_complete": True,
                "trading_enabled": True,
            }

        def trading_gate_provider(self, _symbol):
            return dict(self._gate)

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    controller = SystemHealthController(alert_sink=_alert_sink)
    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc, system_health_controller=controller)
    result = SimpleNamespace(
        decision_id="decision-recover-chaos",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="BUY", qty=100.0),
        metadata={"envelope_extras": {}},
    )

    await controller.publish(event_type="ib_disconnected", reason="socket_closed", source="test.ib")
    blocked = await manager.submit_entry(result)
    assert controller.state == "paused"
    assert blocked.success is False
    assert blocked.reason == "system_health_gate_blocked:paused"

    await controller.publish(event_type="ib_connected", reason="socket_reconnected", source="test.ib")
    blocked = await manager.submit_entry(result)
    assert controller.state == "recovering"
    assert blocked.success is False
    assert blocked.reason == "system_health_gate_blocked:recovering"

    await controller.publish(
        event_type="system_resumed",
        reason="premature",
        source="test.reconciler",
        metadata=_resume_metadata(broker_truth_complete=False),
    )
    blocked = await manager.submit_entry(result)
    assert controller.state == "recovering"
    assert blocked.success is False
    assert blocked.reason == "system_health_gate_blocked:recovering"

    await controller.publish(
        event_type="system_resumed",
        reason="broker_truth_ready",
        source="test.reconciler",
        metadata=_resume_metadata(),
    )
    outcome = await manager.submit_entry(result)

    assert controller.state == "running"
    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1
    assert [row["error_type"] for row in alerts] == [
        "ib_disconnected",
        "system_recovering",
        "system_resumed",
    ]


@pytest.mark.asyncio
async def test_ib_gateway_api_unhealthy_halts_system_and_alerts() -> None:
    alerts = []

    async def _alert_sink(**kwargs):
        alerts.append(kwargs)
        return True

    controller = SystemHealthController(alert_sink=_alert_sink)

    await controller.on_ib_connection_event(
        {
            "event_type": "ib_gateway_api_unhealthy",
            "reason": "gateway_api_half_open",
            "source": "market:pre_connect_api_probe",
            "detail": "TimeoutError: ib_api_handshake_not_ready",
            "action": "manual_gateway_api_recovery_required",
        }
    )

    assert controller.state == "halted"
    assert controller.last_event["event_type"] == "system_halted"
    assert controller.last_event["reason"] == "gateway_api_half_open"
    assert alerts[-1]["error_type"] == "system_halted"
    assert alerts[-1]["reason"] == "gateway_api_half_open"


@pytest.mark.asyncio
async def test_ib_connection_manager_broker_api_ready_stays_recovering() -> None:
    controller = SystemHealthController()

    await controller.on_ib_connection_event(
        {
            "event_type": "broker_api_ready",
            "reason": "ib_runtime_recovery_api_ready",
            "source": "ib_connection_manager.runtime_recovery",
            "resume_ready": False,
            "broker_connected": True,
            "broker_truth_complete": False,
            "order_truth_clean": False,
            "position_ledger_clean": False,
            "strategies_paused_ack": True,
        }
    )

    assert controller.state == "recovering"
    assert controller.last_event["event_type"] == "system_recovering"
    assert controller.last_event["reason"] == "ib_runtime_recovery_api_ready"
