from types import SimpleNamespace

import pytest

from services.exec.execution_service import ExecutionResult
from services.exec.flip_guard import FlipGuardRegistry
from services.orders import UnifiedOrderManager, build_protection_metadata


def test_build_protection_metadata_is_stable_for_same_band() -> None:
    left = build_protection_metadata(
        account_id="DU123",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        lot_id="lot-1",
        protection_kind="stop_loss",
        side="SELL",
        qty=100,
        price=50.001,
        tick_size=0.01,
    )
    right = build_protection_metadata(
        account_id="DU123",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        lot_id="lot-1",
        protection_kind="stop_loss",
        side="SELL",
        qty=100.0,
        price=50.004,
        tick_size=0.01,
    )

    assert left["price_band_id"] == right["price_band_id"]
    assert left["protection_key"] == right["protection_key"]


@pytest.mark.asyncio
async def test_unified_order_manager_noops_duplicate_protective_stop(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class _FakePG:
        async def fetchone(self, sql, *args):
            return {
                "order_id": "uuid-open-protection",
                "idempotency_key": "existing-protection-idem",
                "broker_order_id": "9001",
            }

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegate_called")

    manager = UnifiedOrderManager(execution_service=_FakeExecutionService())
    result = SimpleNamespace(
        decision_id="decision-1",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0, stop_px=49.5),
        metadata={
            "envelope_extras": {
                "protective_stop": True,
                "position_id": "lot-1",
                "ref_px": 49.5,
            }
        },
    )

    outcome = await manager.execute(result)

    assert outcome.success is True
    assert outcome.reason == "uom_duplicate_protection_noop"
    assert outcome.stage == "uom"
    assert manager.execution_service.calls == 0


@pytest.mark.asyncio
async def test_unified_order_manager_registers_protection_after_success(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class _FakePG:
        def __init__(self):
            self.executed = []

        async def fetchone(self, sql, *args):
            return None

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "1"

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()

        async def execute(self, result):
            return ExecutionResult(success=True, decision_id=result.decision_id, order_id=9002, reason="ok")

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc)
    result = SimpleNamespace(
        decision_id="decision-protection",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0, stop_px=49.5),
        metadata={
            "envelope_extras": {
                "protective_stop": True,
                "position_id": "lot-2",
                "ref_px": 49.5,
                "skip_uom_ledger_gate": True,
            }
        },
    )

    outcome = await manager.ensure_protection(result)

    assert outcome.success is True
    assert any("core_protection_order_registry" in call[0] for call in svc.pg.executed)
    registry_args = svc.pg.executed[-1][1]
    assert registry_args[0]
    assert registry_args[1] == "DU123"
    assert registry_args[11] == 9002


@pytest.mark.asyncio
async def test_unified_order_manager_locks_when_protection_registry_write_fails(monkeypatch) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class _FakePG:
        async def fetchone(self, sql, *args):
            return None

        async def execute(self, sql, *args):
            raise RuntimeError("registry down")

    class _FakeLockStore:
        def __init__(self):
            self.calls = []

        async def lock(self, **kwargs):
            self.calls.append(kwargs)
            return {"active": True}

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()
            self.manual_intervention_lock_store = _FakeLockStore()

        async def execute(self, result):
            return ExecutionResult(success=True, decision_id=result.decision_id, order_id=9002, reason="ok")

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc)
    result = SimpleNamespace(
        decision_id="decision-protection",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0, stop_px=49.5),
        metadata={
            "envelope_extras": {
                "protective_stop": True,
                "position_id": "lot-2",
                "ref_px": 49.5,
                "skip_uom_ledger_gate": True,
            }
        },
    )

    outcome = await manager.ensure_protection(result)

    assert outcome.success is True
    assert svc.manual_intervention_lock_store.calls
    lock_call = svc.manual_intervention_lock_store.calls[0]
    assert lock_call["strategy_id"] == "qqq_intraday_001"
    assert lock_call["symbol"] == "TQQQ"
    assert lock_call["reason"] == "protection_registry_write_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protection_kind", "plan_price_attr", "price"),
    [
        ("protective_stop", "stop_px", 49.5),
        ("take_profit", "limit_px", 55.25),
    ],
)
async def test_unified_order_manager_protection_registry_stress_noops_100_duplicate_triggers(
    monkeypatch,
    protection_kind,
    plan_price_attr,
    price,
) -> None:
    monkeypatch.setenv("IB_ACCOUNT", "DU123")

    class _FakePG:
        def __init__(self):
            self.registry = {}
            self.executed = []

        async def fetchone(self, sql, *args):
            if "core.core_protection_order_registry" in sql:
                key = args[0]
                return self.registry.get(key)
            return None

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            if "core.core_protection_order_registry" in sql:
                key = args[0]
                self.registry[key] = {
                    "protection_key": key,
                    "broker_order_id": args[11],
                    "order_ref": args[12],
                    "state": "SUBMITTED",
                }
            return "1"

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, order_id=9100 + self.calls, reason="ok")

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc)
    plan_kwargs = {"side": "SELL", "qty": 100.0, plan_price_attr: price}
    result = SimpleNamespace(
        decision_id="decision-protection-stress",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(**plan_kwargs),
        metadata={
            "envelope_extras": {
                "uom_protection": True,
                "protective_stop": protection_kind == "protective_stop",
                "protection_kind": protection_kind,
                "position_id": "lot-stress-1",
                "ref_px": price,
                "skip_uom_ledger_gate": True,
            }
        },
    )

    outcomes = [await manager.ensure_protection(result) for _ in range(100)]

    assert svc.calls == 1
    assert outcomes[0].success is True
    assert outcomes[0].reason == "ok"
    assert all(item.success is True for item in outcomes[1:])
    assert all(item.reason == "uom_duplicate_protection_noop" for item in outcomes[1:])
    registry_writes = [call for call in svc.pg.executed if "core.core_protection_order_registry" in call[0]]
    assert len(registry_writes) == 1
    registry_args = registry_writes[0][1]
    assert registry_args[5] == protection_kind
    assert registry_args[10] == price


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_entry_when_system_paused() -> None:
    class _FakeExecutionService:
        pg = None

        async def execute(self, result):
            raise AssertionError("should not delegate")

    controller = SimpleNamespace(state="paused")
    manager = UnifiedOrderManager(execution_service=_FakeExecutionService(), system_health_controller=controller)
    result = SimpleNamespace(
        decision_id="decision-2",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="BUY", qty=100.0),
        metadata={"envelope_extras": {}},
    )

    outcome = await manager.submit_entry(result)

    assert outcome.success is False
    assert outcome.stage == "uom_system_health_gate"
    assert outcome.reason == "system_health_gate_blocked:paused"


@pytest.mark.asyncio
async def test_unified_order_manager_allows_safety_lane_when_system_recovering() -> None:
    class _FakeExecutionService:
        pg = None
        trading_gate_provider = staticmethod(
            lambda _symbol: {
                "exec_ready": True,
                "market_ready": False,
                "master_truth_complete": True,
                "trading_enabled": False,
            }
        )

        def __init__(self):
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    controller = SimpleNamespace(state="recovering")
    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc, system_health_controller=controller)
    result = SimpleNamespace(
        decision_id="decision-safety-recovering",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"flatten_action": True, "close_only": True, "skip_uom_ledger_gate": True}},
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1
    assert result.metadata["envelope_extras"]["uom_safety_lane"] is True


@pytest.mark.asyncio
async def test_unified_order_manager_allows_safety_lane_when_system_halted() -> None:
    class _FakeExecutionService:
        pg = None
        trading_gate_provider = staticmethod(
            lambda _symbol: {
                "exec_ready": True,
                "market_ready": False,
                "master_truth_complete": True,
                "trading_enabled": False,
            }
        )

        def __init__(self):
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    controller = SimpleNamespace(state="halted")
    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc, system_health_controller=controller)
    result = SimpleNamespace(
        decision_id="decision-safety-halted",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"flatten_action": True, "close_only": True, "skip_uom_ledger_gate": True}},
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1
    assert result.metadata["envelope_extras"]["uom_safety_lane"] is True


@pytest.mark.asyncio
async def test_unified_order_manager_does_not_block_broker_safety_flatten_on_flat_ledger() -> None:
    class _FakeLedger:
        async def current_position(self, **_kwargs):
            return {"qty": 0.0}

    class _FakeExecutionService:
        pg = None
        trading_gate_provider = staticmethod(
            lambda _symbol: {
                "exec_ready": True,
                "market_ready": True,
                "master_truth_complete": True,
                "trading_enabled": False,
            }
        )

        def __init__(self):
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc, position_ledger=_FakeLedger())
    result = SimpleNamespace(
        decision_id="decision-safety-ledger-flat",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"flatten_action": True, "close_only": True}},
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1
    assert result.metadata["envelope_extras"]["uom_ledger_gate_bypassed_for_broker_truth_safety"] is True


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_safety_lane_when_master_truth_incomplete() -> None:
    class _FakeExecutionService:
        pg = None
        trading_gate_provider = staticmethod(
            lambda _symbol: {
                "exec_ready": True,
                "market_ready": True,
                "master_truth_complete": False,
                "trading_enabled": False,
            }
        )

        async def execute(self, result):
            raise AssertionError("should not delegate")

    controller = SimpleNamespace(state="recovering")
    manager = UnifiedOrderManager(execution_service=_FakeExecutionService(), system_health_controller=controller)
    result = SimpleNamespace(
        decision_id="decision-safety-master-incomplete",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"flatten_action": True, "close_only": True, "skip_uom_ledger_gate": True}},
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is False
    assert outcome.stage == "uom_broker_truth_gate"
    assert outcome.reason == "broker_truth_not_ready"
    assert outcome.metadata["safety_lane"] is True
    assert outcome.metadata["master_truth_complete"] is False


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_safety_lane_when_exec_not_ready() -> None:
    class _FakeExecutionService:
        pg = None
        trading_gate_provider = staticmethod(
            lambda _symbol: {
                "exec_ready": False,
                "market_ready": True,
                "master_truth_complete": True,
                "trading_enabled": False,
            }
        )

        async def execute(self, result):
            raise AssertionError("should not delegate")

    controller = SimpleNamespace(state="recovering")
    manager = UnifiedOrderManager(execution_service=_FakeExecutionService(), system_health_controller=controller)
    result = SimpleNamespace(
        decision_id="decision-safety-exec-down",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"flatten_action": True, "close_only": True, "skip_uom_ledger_gate": True}},
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is False
    assert outcome.stage == "uom_broker_truth_gate"
    assert outcome.reason == "broker_truth_not_ready"
    assert outcome.metadata["safety_lane"] is True


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_entry_without_strategy_identity() -> None:
    class _FakeExecutionService:
        pg = None

        async def execute(self, result):
            raise AssertionError("should not delegate")

    manager = UnifiedOrderManager(execution_service=_FakeExecutionService())
    result = SimpleNamespace(
        decision_id="decision-missing-identity",
        strategy_id="",
        symbol="TQQQ",
        plan=SimpleNamespace(side="BUY", qty=100.0),
        metadata={"envelope_extras": {}},
    )

    outcome = await manager.submit_entry(result)

    assert outcome.success is False
    assert outcome.stage == "uom_strategy_admission_gate"
    assert outcome.reason == "strategy_admission_missing_identity"


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_entry_when_strategy_not_admitted() -> None:
    class _FakeExecutionService:
        pg = None

        async def execute(self, result):
            raise AssertionError("should not delegate")

    async def _admission_provider(**_kwargs):
        return {"allowed": False, "reason": "strategy_frozen_by_phase0"}

    manager = UnifiedOrderManager(
        execution_service=_FakeExecutionService(),
        strategy_admission_provider=_admission_provider,
    )
    result = SimpleNamespace(
        decision_id="decision-strategy-frozen",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="BUY", qty=100.0),
        metadata={"envelope_extras": {}},
    )

    outcome = await manager.submit_entry(result)

    assert outcome.success is False
    assert outcome.stage == "uom_strategy_admission_gate"
    assert outcome.reason == "strategy_frozen_by_phase0"


@pytest.mark.asyncio
async def test_unified_order_manager_allows_safety_repair_when_strategy_not_admitted_but_broker_hint_nonzero() -> None:
    class _FakeExecutionService:
        pg = None

        def __init__(self):
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    async def _admission_provider(**_kwargs):
        return {"allowed": False, "reason": "phase0_strategy_not_in_live_baseline"}

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(
        execution_service=svc,
        strategy_admission_provider=_admission_provider,
    )
    result = SimpleNamespace(
        decision_id="decision-safety-not-admitted",
        strategy_id="retired_strategy_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={
            "envelope_extras": {
                "flatten_action": True,
                "close_only": True,
                "broker_position_qty_hint": 100.0,
                "skip_uom_ledger_gate": True,
            }
        },
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1
    assert result.metadata["envelope_extras"]["uom_strategy_admission_bypassed_for_safety_repair"] is True


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_entry_when_broker_truth_not_ready() -> None:
    class _FakeExecutionService:
        pg = None
        trading_gate_provider = staticmethod(
            lambda _symbol: {
                "exec_ready": True,
                "market_ready": False,
                "master_truth_complete": True,
                "trading_enabled": False,
            }
        )

        async def execute(self, result):
            raise AssertionError("should not delegate")

    manager = UnifiedOrderManager(execution_service=_FakeExecutionService())
    result = SimpleNamespace(
        decision_id="decision-broker-not-ready",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="BUY", qty=100.0),
        metadata={"envelope_extras": {}},
    )

    outcome = await manager.submit_entry(result)

    assert outcome.success is False
    assert outcome.stage == "uom_broker_truth_gate"
    assert outcome.reason == "broker_truth_not_ready"
    assert outcome.metadata["market_ready"] is False


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_entry_when_broker_quarantine_active() -> None:
    class _FakePG:
        async def fetchone(self, sql, *args):
            assert "core.core_order_quarantine" in sql
            assert args[0] == "TQQQ"
            return {"reason": "broker_only_position_requires_manual_intervention", "active_count": 1}

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()

        async def execute(self, result):
            raise AssertionError("should not delegate")

    manager = UnifiedOrderManager(execution_service=_FakeExecutionService())
    result = SimpleNamespace(
        decision_id="decision-quarantine-entry",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="BUY", qty=100.0),
        metadata={"envelope_extras": {}},
    )

    outcome = await manager.submit_entry(result)

    assert outcome.success is False
    assert outcome.stage == "uom_broker_quarantine_gate"
    assert outcome.reason == "broker_truth_quarantine_active"
    assert outcome.metadata["quarantine_reason"] == "broker_only_position_requires_manual_intervention"


@pytest.mark.asyncio
async def test_unified_order_manager_allows_flatten_when_broker_quarantine_active() -> None:
    class _FakePG:
        async def fetchone(self, sql, *args):
            raise AssertionError("flatten should skip broker quarantine gate")

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc)
    result = SimpleNamespace(
        decision_id="decision-quarantine-flatten",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"flatten_action": True, "skip_uom_ledger_gate": True}},
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_same_bar_side_flip_before_delegate() -> None:
    class _FakeExecutionService:
        pg = None
        enable_execution = True
        flip_guard_enabled = True

        async def execute(self, result):
            raise AssertionError("should not delegate")

    guard = FlipGuardRegistry(cooldown_sec=0.0, same_bar_reversal_block=True, state_path=None)
    guard.record_submission(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        extras={"bar_id": "m1:123"},
        now_epoch_s=7380.0,
    )
    manager = UnifiedOrderManager(execution_service=_FakeExecutionService(), flip_guard=guard)
    result = SimpleNamespace(
        decision_id="decision-side-flip",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"bar_id": "m1:123"}},
    )

    outcome = await manager.submit_entry(result)

    assert outcome.success is False
    assert outcome.stage == "uom_flip_guard_gate"
    assert outcome.reason == "same_bar_side_flip"
    assert outcome.metadata["last_open_side"] == "BUY"


@pytest.mark.asyncio
async def test_unified_order_manager_noops_duplicate_recovery_flatten() -> None:
    class _FakePG:
        async def fetchone(self, sql, *args):
            if "idempotency_key = $3" in sql and args[2] == "runtime_recovery:qqq_intraday_001:QQQ:long:flatten":
                return {
                    "order_id": "uuid-recovery-flatten",
                    "broker_order_id": "9201",
                    "state": "SUBMITTED",
                    "idempotency_key": "runtime_recovery:qqq_intraday_001:QQQ:long:flatten",
                    "order_ref": "runtime-recovery",
                }
            return None

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc)
    result = SimpleNamespace(
        decision_id="runtime_recovery:qqq_intraday_001:QQQ:long:flatten",
        strategy_id="qqq_intraday_001",
        symbol="QQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={
            "envelope_extras": {
                "runtime_recovery_action": True,
                "flatten_action": True,
                "close_only": True,
                "broker_position_qty_hint": 100.0,
                "intent_id": "runtime_recovery:qqq_intraday_001:QQQ:long:flatten",
                "recovery_intent_id": "runtime_recovery:qqq_intraday_001:QQQ:long:flatten",
            }
        },
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is True
    assert outcome.reason == "uom_duplicate_recovery_safety_noop"
    assert svc.calls == 0


@pytest.mark.asyncio
async def test_unified_order_manager_does_not_noop_different_recovery_intent() -> None:
    class _FakePG:
        async def fetchone(self, sql, *args):
            assert "idempotency_key = $3" in sql
            assert "runtime_recovery_action" not in sql
            assert args[2] == "runtime_recovery:qqq_intraday_001:QQQ:short:flatten"
            return None

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc)
    result = SimpleNamespace(
        decision_id="runtime_recovery:qqq_intraday_001:QQQ:short:flatten",
        strategy_id="qqq_intraday_001",
        symbol="QQQ",
        plan=SimpleNamespace(side="BUY", qty=100.0),
        metadata={
            "envelope_extras": {
                "runtime_recovery_action": True,
                "flatten_action": True,
                "close_only": True,
                "broker_position_qty_hint": -100.0,
                "intent_id": "runtime_recovery:qqq_intraday_001:QQQ:short:flatten",
                "recovery_intent_id": "runtime_recovery:qqq_intraday_001:QQQ:short:flatten",
                "skip_uom_ledger_gate": True,
            }
        },
    )

    outcome = await manager.submit_flatten(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_entry_when_pending_order_exists() -> None:
    class _FakePG:
        async def fetchone(self, sql, *args):
            if "core.core_order_quarantine" in sql:
                return None
            assert "core.core_order" in sql
            assert args == ("qqq_intraday_001", "TQQQ")
            return {
                "order_id": "existing-order",
                "broker_order_id": 1001,
                "state": "SUBMITTED",
                "order_ref": "existing-ref",
            }

    class _FakeExecutionService:
        def __init__(self):
            self.pg = _FakePG()

        async def execute(self, result):
            raise AssertionError("should not delegate")

    manager = UnifiedOrderManager(execution_service=_FakeExecutionService())
    result = SimpleNamespace(
        decision_id="decision-pending",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="BUY", qty=100.0),
        metadata={"envelope_extras": {}},
    )

    outcome = await manager.submit_entry(result)

    assert outcome.success is False
    assert outcome.stage == "uom_pending_order_gate"
    assert outcome.reason == "pending_order_exists"
    assert outcome.metadata["existing_order_id"] == "existing-order"


@pytest.mark.asyncio
async def test_unified_order_manager_blocks_close_when_ledger_flat() -> None:
    class _FakeExecutionService:
        pg = None
        calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    class _FakeLedger:
        async def current_position(self, *, strategy_id, symbol, lot_id=None):
            return {"qty": 0.0}

    manager = UnifiedOrderManager(execution_service=_FakeExecutionService(), position_ledger=_FakeLedger())
    result = SimpleNamespace(
        decision_id="decision-3",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"close_only": True}},
    )

    outcome = await manager.submit_exit(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert result.metadata["envelope_extras"]["uom_ledger_gate_bypassed_for_broker_truth_safety"] is True


@pytest.mark.asyncio
async def test_unified_order_manager_treats_sell_entry_as_entry_not_close() -> None:
    class _FakeExecutionService:
        pg = None

        def __init__(self):
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    class _FakeLedger:
        async def current_position(self, *, strategy_id, symbol, lot_id=None):
            raise AssertionError("entry should not query close-only ledger gate")

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc, position_ledger=_FakeLedger())
    result = SimpleNamespace(
        decision_id="decision-sell-entry",
        strategy_id="stocks_kernel_smh_001",
        symbol="SOXS",
        plan=SimpleNamespace(side="SELL", qty=25.0),
        metadata={"envelope_extras": {"intent_action": "entry"}},
    )

    outcome = await manager.execute(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1
    assert result.metadata["envelope_extras"]["uom_intent_kind"] == "entry"


@pytest.mark.asyncio
async def test_unified_order_manager_allows_safety_repair_when_ledger_flat_broker_hint_nonzero() -> None:
    class _FakeExecutionService:
        pg = None

        def __init__(self):
            self.calls = 0

        async def execute(self, result):
            self.calls += 1
            return ExecutionResult(success=True, decision_id=result.decision_id, reason="delegated")

    class _FakeLedger:
        async def current_position(self, *, strategy_id, symbol, lot_id=None):
            return {"qty": 0.0}

    svc = _FakeExecutionService()
    manager = UnifiedOrderManager(execution_service=svc, position_ledger=_FakeLedger())
    result = SimpleNamespace(
        decision_id="decision-ledger-repair",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=SimpleNamespace(side="SELL", qty=100.0),
        metadata={"envelope_extras": {"close_only": True, "broker_position_qty_hint": 100.0}},
    )

    outcome = await manager.submit_exit(result)

    assert outcome.success is True
    assert outcome.reason == "delegated"
    assert svc.calls == 1
    assert result.metadata["envelope_extras"]["uom_ledger_repair_from_position_hint"] is True
    assert result.metadata["envelope_extras"]["uom_ledger_qty"] == 100.0
