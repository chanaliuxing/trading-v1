from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest

from services.reconciliation.strategy_recovery_policy import (
    DefaultCloseOnlyRecoveryPolicy,
    QQQIntradayRecoveryPolicy,
    RecoveryPolicyContext,
    StocksKernelRecoveryPolicy,
    StrategyRecoveryPolicyRegistry,
    strategy_recovery_policy_registry_from_manifests,
)
from services.reconciliation.runtime_reconciler import RuntimeReconciler
from services.reconciliation.contracts import ReconcilerComponentResult


def test_default_recovery_policy_builds_close_only_flatten_action() -> None:
    policy = DefaultCloseOnlyRecoveryPolicy()

    actions = policy.build_actions(
        RecoveryPolicyContext(
            strategy_id="retired_strategy_001",
            symbol="QQQ",
            broker_qty=12.0,
            ledger_qty=0.0,
            reason="ib_resume_repair",
            account_id="DU123",
            strategy_instance_id="retired_strategy_001",
            lot_id="lot-1",
            position_owner_key="retired_strategy_001:retired_strategy_001:QQQ:lot-1",
            owner_qty=12.0,
        )
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.action_type == "flatten"
    assert action.target_qty == -12.0
    assert action.extras["close_only"] is True
    assert action.extras["flatten_action"] is True
    assert action.extras["broker_position_qty_hint"] == 12.0
    assert action.extras["strategy_instance_id"] == "retired_strategy_001"
    assert action.extras["position_lot_id"] == "lot-1"
    assert action.extras["position_owner_key"] == "retired_strategy_001:retired_strategy_001:QQQ:lot-1"
    envelope = action.to_decision_envelope(decision_id="recovery-test")
    assert envelope.decision_id == "recovery-test"
    assert envelope.extras["runtime_recovery_action"] is True
    assert envelope.extras["intent_id"] == "recovery-test"
    assert envelope.extras["broker_position_qty_hint"] == 12.0


def test_recovery_action_uses_stable_decision_id_without_override() -> None:
    action = DefaultCloseOnlyRecoveryPolicy().build_actions(
        RecoveryPolicyContext(
            strategy_id="qqq_intraday_001",
            symbol="QQQ",
            broker_qty=12.0,
            ledger_qty=0.0,
            account_id="DU123",
            strategy_instance_id="qqq_intraday_001",
            lot_id="lot-1",
            position_owner_key="qqq_intraday_001:qqq_intraday_001:QQQ:lot-1",
            owner_qty=12.0,
        )
    )[0]

    left = action.to_decision_envelope()
    right = action.to_decision_envelope()

    assert left.decision_id == "runtime_recovery:qqq_intraday_001:QQQ:long:flatten"
    assert right.decision_id == left.decision_id
    assert left.extras["intent_id"] == left.decision_id
    assert left.extras["recovery_intent_id"] == left.decision_id


def test_recovery_registry_uses_strategy_specific_policy() -> None:
    registry = StrategyRecoveryPolicyRegistry()
    registry.register("qqq_intraday_001", QQQIntradayRecoveryPolicy())

    actions = registry.build_actions(
        RecoveryPolicyContext(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            broker_qty=5.0,
            ledger_qty=0.0,
            account_id="DU123",
            strategy_instance_id="qqq_intraday_001",
            lot_id="lot-1",
            position_owner_key="qqq_intraday_001:qqq_intraday_001:TQQQ:lot-1",
            owner_qty=5.0,
        )
    )

    assert actions
    assert actions[0].extras["recovery_policy_id"] == "qqq_intraday_close_only"
    assert actions[0].extras["strategy_family"] == "qqq_intraday"


def test_recovery_registry_builds_from_production_manifests() -> None:
    manifests = [
        SimpleNamespace(strategy_id="stocks_kernel_smh_001", recovery_policy_id="stocks_kernel_close_only"),
        SimpleNamespace(strategy_id="qqq_intraday_001", recovery_policy_id="qqq_intraday_close_only"),
    ]
    registry = strategy_recovery_policy_registry_from_manifests(manifests)

    assert registry.registered_strategy_ids() == ["qqq_intraday_001", "stocks_kernel_smh_001"]
    actions = registry.build_actions(
        RecoveryPolicyContext(
            strategy_id="stocks_kernel_smh_001",
            symbol="SOXL",
            broker_qty=3.0,
            ledger_qty=0.0,
            account_id="DU123",
            strategy_instance_id="stocks_kernel_smh_001",
            lot_id="lot-1",
            position_owner_key="stocks_kernel_smh_001:stocks_kernel_smh_001:SOXL:lot-1",
            owner_qty=3.0,
        )
    )

    assert actions[0].extras["recovery_policy_id"] == StocksKernelRecoveryPolicy.policy_id
    assert actions[0].extras["strategy_family"] == "stocks_kernel"


def test_runtime_reconciler_evaluates_strategy_recovery_policy_for_position_mismatch() -> None:
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    rec._emit_audit = MagicMock()
    result = ReconcilerComponentResult(
        component="position",
        ok=False,
        reason="position_ledger_diverged",
        metadata={
            "mismatch_count": 1,
            "mismatches": [
                {
                    "strategy_id": "qqq_intraday_001",
                    "account_id": "DU123",
                    "strategy_instance_id": "qqq_intraday_001",
                    "symbol": "QQQ",
                    "lot_id": "lot-1",
                    "position_owner_key": "qqq_intraday_001:qqq_intraday_001:QQQ:lot-1",
                    "owner_strategy_source": "ledger_owner_lane",
                    "owner_lane_qty": 10.0,
                    "ib_qty": 10.0,
                    "ledger_qty": 0.0,
                    "diff": 10.0,
                }
            ],
        },
    )

    actions = rec._evaluate_strategy_recovery_policy(position_result=result)

    assert len(actions) == 1
    assert actions[0].action_type == "flatten"
    assert actions[0].target_qty == -10.0
    assert actions[0].extras["strategy_instance_id"] == "qqq_intraday_001"
    assert actions[0].extras["position_lot_id"] == "lot-1"
    rec._emit_audit.assert_called_once()
    assert rec._emit_audit.call_args.kwargs["stage"] == "strategy_recovery_policy_evaluated"


def test_runtime_reconciler_blocks_policy_when_owner_identity_incomplete() -> None:
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    rec._emit_audit = MagicMock()
    result = ReconcilerComponentResult(
        component="position",
        ok=False,
        reason="position_ledger_diverged",
        metadata={
            "mismatch_count": 1,
            "mismatches": [
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "QQQ",
                    "ib_qty": 10.0,
                    "ledger_qty": 0.0,
                    "diff": 10.0,
                }
            ],
        },
    )

    actions = rec._evaluate_strategy_recovery_policy(position_result=result)

    assert len(actions) == 1
    assert actions[0].action_type == "quarantine"
    assert actions[0].reason == "position_mismatch_without_complete_owner_identity"


def test_runtime_reconciler_blocks_aggregate_recovery_when_owner_qty_differs_from_broker_qty() -> None:
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    rec._emit_audit = MagicMock()
    result = ReconcilerComponentResult(
        component="position",
        ok=False,
        reason="position_ledger_diverged",
        metadata={
            "mismatch_count": 1,
            "mismatches": [
                {
                    "strategy_id": "nvda_minute_flip_paper_002",
                    "account_id": "DU123",
                    "strategy_instance_id": "nvda_minute_flip_paper_002",
                    "symbol": "NVDA",
                    "lot_id": "lot-002",
                    "position_owner_key": "nvda_minute_flip_paper_002:nvda_minute_flip_paper_002:NVDA:lot-002",
                    "owner_strategy_source": "ledger_owner_lane",
                    "owner_lane_qty": 1.0,
                    "ib_qty": 2.0,
                    "ledger_qty": 1.0,
                    "diff": 1.0,
                }
            ],
        },
    )

    actions = rec._evaluate_strategy_recovery_policy(position_result=result)

    assert len(actions) == 1
    assert actions[0].action_type == "quarantine"
    assert actions[0].reason == "position_mismatch_without_exact_owner_quantity"


def test_runtime_reconciler_blocks_recovery_when_owner_qty_direction_conflicts() -> None:
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    rec._emit_audit = MagicMock()
    result = ReconcilerComponentResult(
        component="position",
        ok=False,
        reason="position_ledger_diverged",
        metadata={
            "mismatch_count": 1,
            "mismatches": [
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "account_id": "DU123",
                    "strategy_instance_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "lot_id": "lot-001",
                    "position_owner_key": "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
                    "owner_strategy_source": "ledger_owner_lane",
                    "owner_lane_qty": -1.0,
                    "ib_qty": 1.0,
                    "ledger_qty": -1.0,
                    "diff": 2.0,
                }
            ],
        },
    )

    actions = rec._evaluate_strategy_recovery_policy(position_result=result)

    assert len(actions) == 1
    assert actions[0].action_type == "quarantine"
    assert actions[0].reason == "position_mismatch_owner_direction_conflict"


def test_runtime_reconciler_blocks_complete_owner_identity_from_non_ledger_source() -> None:
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    rec._emit_audit = MagicMock()
    result = ReconcilerComponentResult(
        component="position",
        ok=False,
        reason="position_ledger_diverged",
        metadata={
            "mismatch_count": 1,
            "mismatches": [
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "account_id": "DU123",
                    "strategy_instance_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "lot_id": "lot-001",
                    "position_owner_key": "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
                    "owner_strategy_source": "configured_owner",
                    "owner_lane_qty": 1.0,
                    "ib_qty": 1.0,
                    "ledger_qty": 0.0,
                    "diff": 1.0,
                }
            ],
        },
    )

    actions = rec._evaluate_strategy_recovery_policy(position_result=result)

    assert len(actions) == 1
    assert actions[0].action_type == "quarantine"
    assert actions[0].reason == "position_mismatch_without_ledger_owner_source"


def test_runtime_reconciler_blocks_policy_when_strategy_owner_missing() -> None:
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock())
    rec._emit_audit = MagicMock()
    result = ReconcilerComponentResult(
        component="position",
        ok=False,
        reason="position_ledger_diverged",
        metadata={
            "mismatch_count": 1,
            "mismatches": [{"symbol": "QQQ", "ib_qty": 10.0, "ledger_qty": 0.0}],
        },
    )

    actions = rec._evaluate_strategy_recovery_policy(position_result=result)

    assert len(actions) == 1
    assert actions[0].action_type == "quarantine"
    assert actions[0].reason == "position_mismatch_without_strategy_owner"
    rec._emit_audit.assert_called_once()
    assert rec._emit_audit.call_args.kwargs["stage"] == "strategy_recovery_policy_unresolved"


@pytest.mark.asyncio
async def test_runtime_reconciler_submits_recovery_flatten_action_through_uom() -> None:
    class _OrderManager:
        def __init__(self) -> None:
            self.result = None

        async def submit_flatten(self, result):
            self.result = result
            return SimpleNamespace(success=True, reason="submitted", stage="uom")

    order_manager = _OrderManager()
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock(), recovery_order_manager=order_manager)
    rec._emit_audit = MagicMock()
    action = DefaultCloseOnlyRecoveryPolicy().build_actions(
        RecoveryPolicyContext(
            strategy_id="qqq_intraday_001",
            symbol="QQQ",
            broker_qty=7.0,
            ledger_qty=0.0,
            account_id="DU123",
            strategy_instance_id="qqq_intraday_001",
            lot_id="lot-1",
            position_owner_key="qqq_intraday_001:qqq_intraday_001:QQQ:lot-1",
            owner_qty=7.0,
        )
    )[0]

    await rec._execute_strategy_recovery_actions([action])

    assert order_manager.result is not None
    assert order_manager.result.status == "APPROVED"
    assert order_manager.result.plan.side == "SELL"
    assert order_manager.result.plan.qty == 7.0
    extras = order_manager.result.metadata["envelope_extras"]
    assert extras["flatten_action"] is True
    assert extras["broker_position_qty_hint"] == 7.0


@pytest.mark.asyncio
async def test_runtime_reconciler_suppresses_duplicate_recovery_action() -> None:
    class _OrderManager:
        def __init__(self) -> None:
            self.calls = 0

        async def submit_flatten(self, result):
            self.calls += 1
            return SimpleNamespace(success=True, reason="submitted", stage="uom")

    order_manager = _OrderManager()
    rec = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=MagicMock(),
        recovery_order_manager=order_manager,
        recovery_action_cooldown_seconds=60.0,
    )
    rec._emit_audit = MagicMock()
    action = DefaultCloseOnlyRecoveryPolicy().build_actions(
        RecoveryPolicyContext(
            strategy_id="qqq_intraday_001",
            symbol="QQQ",
            broker_qty=7.0,
            ledger_qty=0.0,
            account_id="DU123",
            strategy_instance_id="qqq_intraday_001",
            lot_id="lot-1",
            position_owner_key="qqq_intraday_001:qqq_intraday_001:QQQ:lot-1",
            owner_qty=7.0,
        )
    )[0]

    await rec._execute_strategy_recovery_actions([action])
    await rec._execute_strategy_recovery_actions([action])

    assert order_manager.calls == 1
    assert any(
        call.kwargs.get("stage") == "strategy_recovery_action_suppressed"
        for call in rec._emit_audit.call_args_list
    )


@pytest.mark.asyncio
async def test_runtime_reconciler_quarantines_recovery_action_when_no_order_manager() -> None:
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock(), recovery_order_manager=None)
    rec._emit_audit = MagicMock()
    rec._upsert_order_quarantine = AsyncMock(return_value=None)
    action = DefaultCloseOnlyRecoveryPolicy().build_actions(
        RecoveryPolicyContext(
            strategy_id="qqq_intraday_001",
            symbol="QQQ",
            broker_qty=7.0,
            ledger_qty=0.0,
            account_id="DU123",
            strategy_instance_id="qqq_intraday_001",
            lot_id="lot-1",
            position_owner_key="qqq_intraday_001:qqq_intraday_001:QQQ:lot-1",
            owner_qty=7.0,
        )
    )[0]

    await rec._execute_strategy_recovery_actions([action])

    rec._upsert_order_quarantine.assert_awaited_once()
    assert rec._upsert_order_quarantine.await_args.kwargs["reason"] == "recovery_action_not_executable"


@pytest.mark.asyncio
async def test_runtime_reconciler_run_once_executes_position_recovery_action() -> None:
    class _OrderManager:
        def __init__(self) -> None:
            self.result = None

        async def submit_flatten(self, result):
            self.result = result
            return SimpleNamespace(success=True, reason="submitted", stage="uom")

    order_manager = _OrderManager()
    rec = RuntimeReconciler(pg=MagicMock(), ib_adapter=MagicMock(), recovery_order_manager=order_manager)
    rec.order_truth_reconciler.collect_snapshot = AsyncMock(
        return_value=SimpleNamespace(
            db_orders=[],
            ib_orders=[],
            ghosts=[],
            orphans=[],
            mismatches=[],
            db_by_broker_id={},
            db_by_perm_id={},
            ib_by_id={},
            ib_by_perm_id={},
        )
    )
    rec.order_quarantine_handler.active_quarantine_summary = AsyncMock(return_value={"active_count": 0})
    rec.order_quarantine_handler.resolve_cleared_order_quarantines = AsyncMock(return_value=None)
    rec._audit_positions = AsyncMock(return_value=None)
    rec._audit_flatten_lifecycle = AsyncMock(return_value=None)
    rec._maybe_push_alert = AsyncMock(return_value=None)
    rec._publish_runtime_health_if_needed = AsyncMock(return_value=None)
    rec._converge_broker_flat_position_mismatches = AsyncMock(return_value=None)
    rec.position_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="position",
            ok=False,
            reason="position_ledger_diverged",
            metadata={
                "mismatch_count": 1,
                "mismatches": [
                    {
                        "strategy_id": "qqq_intraday_001",
                        "account_id": "DU123",
                        "strategy_instance_id": "qqq_intraday_001",
                        "symbol": "QQQ",
                        "lot_id": "lot-1",
                        "position_owner_key": "qqq_intraday_001:qqq_intraday_001:QQQ:lot-1",
                        "owner_strategy_source": "ledger_owner_lane",
                        "owner_lane_qty": 7.0,
                        "ib_qty": 7.0,
                        "ledger_qty": 0.0,
                        "diff": 7.0,
                    }
                ],
            },
        )
    )

    report = await rec.run_once()

    assert report.position_mismatches == 1
    assert order_manager.result is not None
    assert order_manager.result.plan.side == "SELL"
    assert order_manager.result.plan.qty == 7.0
