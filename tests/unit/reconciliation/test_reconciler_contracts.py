from datetime import datetime, timezone
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from services.reconciliation.contracts import (
    AckReconciler,
    OrderTruthReconciler,
    PositionReconciler,
    ReconcilerComponentResult,
    StartupReconciler,
)
from services.reconciliation.runtime_reconciler import ReconciliationReport


def _report(*, ghosts=0, orphans=0, mismatches=0):
    return ReconciliationReport(
        timestamp=datetime.now(timezone.utc),
        db_open_count=0,
        ib_open_count=0,
        ghosts_found=ghosts,
        orphans_found=orphans,
        mismatches=mismatches,
        duration_ms=1.0,
    )


@pytest.mark.asyncio
async def test_startup_reconciler_delegates_gate_check():
    runtime = type("Runtime", (), {})()
    runtime.order_truth_reconciler = type("OrderTruth", (), {})()
    runtime.order_truth_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="order_truth",
            ok=True,
            reason="order_truth_clean",
            metadata={
                "db_open_count": 0,
                "ib_open_count": 0,
                "ghosts_found": 0,
                "orphans_found": 0,
                "mismatches": 0,
            },
        )
    )
    runtime.position_reconciler = type("Position", (), {})()
    runtime.position_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="position",
            ok=True,
            reason="position_ledger_clean",
            metadata={"mismatch_count": 0},
        )
    )
    runtime.order_quarantine_handler = type("Quarantine", (), {})()
    runtime.order_quarantine_handler.active_quarantine_summary = AsyncMock(return_value={"active_count": 0})
    runtime._raise_startup_gate_failure = AsyncMock()

    result = await StartupReconciler(runtime).ensure_ready()

    assert result.ok is True
    assert result.metadata["active_quarantine_count"] == 0
    assert result.component == "startup"
    runtime.order_truth_reconciler.reconcile_once.assert_awaited_once_with(apply_runtime_filters=True)
    runtime.position_reconciler.reconcile_once.assert_awaited_once()
    runtime._raise_startup_gate_failure.assert_called_once()


@pytest.mark.asyncio
async def test_startup_reconciler_blocks_active_quarantine():
    runtime = type("Runtime", (), {})()
    runtime.order_truth_reconciler = type("OrderTruth", (), {})()
    runtime.order_truth_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="order_truth",
            ok=True,
            reason="order_truth_clean",
            metadata={
                "db_open_count": 0,
                "ib_open_count": 0,
                "ghosts_found": 0,
                "orphans_found": 0,
                "mismatches": 0,
            },
        )
    )
    runtime.position_reconciler = type("Position", (), {})()
    runtime.position_reconciler.reconcile_once = AsyncMock(
        return_value=ReconcilerComponentResult(
            component="position",
            ok=True,
            reason="position_ledger_clean",
            metadata={"mismatch_count": 0},
        )
    )
    runtime.order_quarantine_handler = type("Quarantine", (), {})()
    runtime.order_quarantine_handler.active_quarantine_summary = AsyncMock(return_value={"active_count": 1})
    runtime._raise_startup_gate_failure = AsyncMock()

    with pytest.raises(RuntimeError, match="active_quarantine"):
        await StartupReconciler(runtime).ensure_ready()


@pytest.mark.asyncio
async def test_order_truth_reconciler_reports_divergence():
    class _IBOrder:
        order_id = 22
        perm_id = 123
        symbol = "QQQ"
        status = "Submitted"
        order_ref = "orphan-ref"

    ib_order = _IBOrder()
    runtime = type("Runtime", (), {})()
    runtime._fetch_db_open_orders = AsyncMock(return_value=[])
    runtime._fetch_ib_open_orders = AsyncMock(return_value=[ib_order])
    runtime._normalize_broker_order_id = lambda value: int(value) if value is not None else None
    runtime._normalize_perm_id = lambda value: str(value) if value else None
    runtime._is_unconfirmed_pending_ib_order = lambda order: False
    runtime._state_mismatch = lambda db_state, ib_status: db_state != ib_status

    result = await OrderTruthReconciler(runtime).reconcile_once()

    assert result.ok is False
    assert result.reason == "order_truth_diverged"
    assert result.metadata["orphans_found"] == 1
    assert result.metadata["orphan_samples"][0]["order_id"] == "22"
    runtime._fetch_ib_open_orders.assert_awaited_once_with(force_refresh=True)


@pytest.mark.asyncio
async def test_order_truth_reconciler_detects_ghost_and_state_mismatch():
    class _IBOrder:
        order_id = 100
        perm_id = 900
        symbol = "QQQ"
        status = "Cancelled"
        order_ref = "matched-ref"

    runtime = type("Runtime", (), {})()
    runtime._fetch_db_open_orders = AsyncMock(
        return_value=[
            {
                "order_id": "db-matched",
                "broker_order_id": "100",
                "ib_perm_id": "900",
                "strategy_id": "qqq_intraday_001",
                "symbol": "QQQ",
                "state": "SUBMITTED",
            },
            {
                "order_id": "db-ghost",
                "broker_order_id": "101",
                "ib_perm_id": None,
                "strategy_id": "qqq_intraday_001",
                "symbol": "QQQ",
                "state": "SUBMITTED",
            },
        ]
    )
    runtime._fetch_ib_open_orders = AsyncMock(return_value=[_IBOrder()])
    runtime._normalize_broker_order_id = lambda value: int(value) if value is not None and str(value) else None
    runtime._normalize_perm_id = lambda value: str(value) if value else None
    runtime._is_unconfirmed_pending_ib_order = lambda order: False
    runtime._state_mismatch = lambda db_state, ib_status: db_state == "SUBMITTED" and ib_status == "Cancelled"

    result = await OrderTruthReconciler(runtime).reconcile_once()

    assert result.ok is False
    assert result.metadata["ghosts_found"] == 1
    assert result.metadata["mismatches"] == 1
    assert result.metadata["ghost_samples"][0]["order_id"] == "db-ghost"
    assert result.metadata["mismatch_samples"][0]["order_id"] == "db-matched"


@pytest.mark.asyncio
async def test_order_truth_reconciler_ignores_prior_session_broker_order_id_reuse():
    class _IBOrder:
        order_id = 156
        perm_id = None
        symbol = "OPT:SPY:20260407:655:P"
        status = "Inactive"
        order_ref = "new-session-order"

    ib_order = _IBOrder()
    runtime = type("Runtime", (), {})()
    runtime._fetch_db_open_orders = AsyncMock(
        return_value=[
            {
                "order_id": "old-db-order",
                "broker_order_id": "156",
                "ib_perm_id": None,
                "strategy_id": "stocks_kernel_shadow_smh_001",
                "symbol": "SOXS",
                "state": "CANCELLED",
                "updated_at": datetime.now(timezone.utc) - timedelta(days=1),
            }
        ]
    )
    runtime._fetch_ib_open_orders = AsyncMock(return_value=[ib_order])
    runtime._normalize_broker_order_id = lambda value: int(value) if value is not None and str(value) else None
    runtime._normalize_perm_id = lambda value: str(value) if value else None
    runtime._is_unconfirmed_pending_ib_order = lambda order: False
    runtime._state_mismatch = lambda db_state, ib_status: True

    snapshot = await OrderTruthReconciler(runtime).collect_snapshot()

    assert 156 not in snapshot.db_by_broker_id
    assert snapshot.orphans == [ib_order]
    assert snapshot.mismatches == []


@pytest.mark.asyncio
async def test_ack_reconciler_delegates_quarantine_ack():
    runtime = type("Runtime", (), {})()
    runtime.acknowledge_order_quarantine = AsyncMock(return_value=1)

    result = await AckReconciler(runtime).acknowledge_order_quarantine(symbol="QQQ")

    assert result.ok is True
    assert result.component == "ack"
    runtime.acknowledge_order_quarantine.assert_awaited_once_with(symbol="QQQ")


@pytest.mark.asyncio
async def test_position_reconciler_compares_ib_to_position_ledger_current():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        async def fetchall(self, sql, account_id):
            assert "position_ledger_current" in sql
            assert account_id == "DU123"
            return [{"symbol": "QQQ", "qty": 8}]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    assert result.ok is False
    assert result.reason == "position_ledger_diverged"
    assert result.metadata["account_id"] == "DU123"
    assert result.metadata["mismatch_count"] == 1
    assert result.metadata["mismatches"][0]["diff"] == 2.0
    assert result.metadata["ledger_lot_count_by_symbol"] == {"QQQ": 1}


@pytest.mark.asyncio
async def test_position_reconciler_adds_owner_from_single_ledger_lot():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        async def fetchall(self, sql, account_id):
            if "position_ledger_current" not in sql:
                return []
            return [
                {
                    "symbol": "QQQ",
                    "account_id": "DU123",
                    "strategy_id": "qqq_intraday_001",
                    "strategy_instance_id": "qqq_intraday_001",
                    "lot_id": "lot-1",
                    "position_owner_key": "qqq_intraday_001:qqq_intraday_001:QQQ:lot-1",
                    "qty": 4,
                }
            ]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert mismatch["strategy_id"] == "qqq_intraday_001"
    assert mismatch["strategy_instance_id"] == "qqq_intraday_001"
    assert mismatch["lot_id"] == "lot-1"
    assert mismatch["position_owner_key"] == "qqq_intraday_001:qqq_intraday_001:QQQ:lot-1"
    assert mismatch["owner_strategy_source"] == "ledger_owner_lane"


@pytest.mark.asyncio
async def test_position_reconciler_does_not_synthesize_missing_owner_key():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        async def fetchall(self, sql, account_id):
            if "position_ledger_current" not in sql:
                return []
            return [
                {
                    "symbol": "QQQ",
                    "account_id": "DU123",
                    "strategy_id": "qqq_intraday_001",
                    "strategy_instance_id": "qqq_intraday_001",
                    "lot_id": "lot-1",
                    "position_owner_key": "",
                    "qty": 4,
                }
            ]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert "position_owner_key" not in mismatch
    assert "strategy_instance_id" not in mismatch
    assert "owner_strategy_source" not in mismatch


@pytest.mark.asyncio
async def test_position_reconciler_adds_owner_from_working_safety_order_when_ledger_flat():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        def __init__(self):
            self.calls = 0

        async def fetchall(self, sql, account_id):
            self.calls += 1
            if self.calls == 1:
                assert "position_ledger_current" in sql
                return []
            assert account_id == "QQQ"
            assert "runtime_recovery_action" in sql
            assert "flatten_action" in sql
            assert "protective_stop" in sql
            return [{"strategy_id": "qqq_intraday_001"}]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert mismatch["strategy_id"] == "qqq_intraday_001"
    assert mismatch["owner_strategy_source"] == "working_order"


@pytest.mark.asyncio
async def test_position_reconciler_does_not_owner_from_ambiguous_working_safety_orders():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        def __init__(self):
            self.calls = 0

        async def fetchall(self, sql, account_id):
            self.calls += 1
            if self.calls == 1:
                return []
            return [{"strategy_id": "strategy_a"}, {"strategy_id": "strategy_b"}]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert "strategy_id" not in mismatch


@pytest.mark.asyncio
async def test_position_reconciler_does_not_owner_from_plain_working_entry():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        def __init__(self):
            self.calls = 0

        async def fetchall(self, sql, account_id):
            self.calls += 1
            if self.calls == 1:
                return []
            assert account_id == "QQQ"
            assert "state IN ('NEW', 'SUBMITTED', 'PARTIAL')" in sql
            assert "flatten_action" in sql
            return []

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert "strategy_id" not in mismatch


@pytest.mark.asyncio
async def test_position_reconciler_does_not_owner_from_stale_core_position_only():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        def __init__(self):
            self.calls = 0

        async def fetchall(self, sql, account_id):
            self.calls += 1
            if self.calls == 1:
                return []
            assert "FROM core.core_order" in sql
            assert "FROM core.core_position" not in sql
            return []

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert "strategy_id" not in mismatch


@pytest.mark.asyncio
async def test_position_reconciler_adds_owner_from_unique_exact_option_order():
    class _Contract:
        symbol = "QQQ"
        secType = "OPT"
        lastTradeDateOrContractMonth = "20260505"
        strike = 682
        right = "C"

    class _Position:
        contract = _Contract()
        position = 2

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        def __init__(self):
            self.calls = 0

        async def fetchall(self, sql, *args):
            self.calls += 1
            if self.calls == 1:
                assert "position_ledger_current" in sql
                return []
            if self.calls == 2:
                assert "state IN ('NEW', 'SUBMITTED', 'PARTIAL')" in sql
                return []
            assert "created_at >= now() - interval '14 days'" in sql
            assert args[0] == "OPT:QQQ:20260505:682:C"
            assert args[1] == "BUY"
            return [{"strategy_id": "qqq_intraday_option_v1_001"}]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: "OPT:QQQ:20260505:682:C"

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert mismatch["strategy_id"] == "qqq_intraday_option_v1_001"
    assert mismatch["owner_strategy_source"] == "exact_option_order"


@pytest.mark.asyncio
async def test_position_reconciler_does_not_owner_from_ambiguous_exact_option_orders():
    class _Contract:
        symbol = "QQQ"
        secType = "OPT"
        lastTradeDateOrContractMonth = "20260505"
        strike = 682
        right = "C"

    class _Position:
        contract = _Contract()
        position = 2

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        def __init__(self):
            self.calls = 0

        async def fetchall(self, sql, *args):
            self.calls += 1
            if self.calls <= 2:
                return []
            return [{"strategy_id": "strategy_a"}, {"strategy_id": "strategy_b"}]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: "OPT:QQQ:20260505:682:C"

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert "strategy_id" not in mismatch


@pytest.mark.asyncio
async def test_position_reconciler_adds_owner_from_configured_symbol_map():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        async def fetchall(self, sql, account_id):
            return []

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime.symbol_strategy_owner_map = {"QQQ": "qqq_intraday_001"}
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    mismatch = result.metadata["mismatches"][0]
    assert mismatch["strategy_id"] == "qqq_intraday_001"
    assert mismatch["owner_strategy_source"] == "configured_owner"


@pytest.mark.asyncio
async def test_runtime_reconciler_publishes_reconciled_order_truth_snapshot():
    published = {}

    class _TruthService:
        def publish_reconciled_open_orders(self, *, db_orders, ib_orders, published_at):
            published["db_orders"] = list(db_orders)
            published["ib_orders"] = list(ib_orders)
            published["published_at"] = published_at

    runtime = type("Runtime", (), {})()
    runtime.order_truth_service = _TruthService()

    snapshot = type("Snapshot", (), {"db_orders": [{"order_id": "db1"}], "ib_orders": [type("IBOrder", (), {"order_id": 7})()]})()

    from services.reconciliation.runtime_reconciler import RuntimeReconciler

    RuntimeReconciler._publish_runtime_order_truth(runtime, order_snapshot=snapshot)

    assert published["db_orders"] == [{"order_id": "db1"}]
    assert len(published["ib_orders"]) == 1
    assert getattr(published["ib_orders"][0], "order_id", None) == 7
    assert published["published_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_runtime_reconciler_publishes_reconciled_position_truth_snapshot():
    published = {}

    class _TruthService:
        def publish_reconciled_positions(self, *, ib_positions, ledger_positions, mismatches, published_at):
            published["ib_positions"] = dict(ib_positions)
            published["ledger_positions"] = dict(ledger_positions)
            published["mismatches"] = list(mismatches)
            published["published_at"] = published_at

    runtime = type("Runtime", (), {})()
    runtime.order_truth_service = _TruthService()

    result = ReconcilerComponentResult(
        component="position",
        ok=False,
        reason="position_ledger_diverged",
        metadata={
            "ib_positions": {"QQQ": 10.0},
            "ledger_positions": {"QQQ": 8.0},
            "mismatches": [{"symbol": "QQQ", "diff": 2.0}],
        },
    )

    from services.reconciliation.runtime_reconciler import RuntimeReconciler

    RuntimeReconciler._publish_runtime_position_truth(runtime, position_result=result)

    assert published["ib_positions"] == {"QQQ": 10.0}
    assert published["ledger_positions"] == {"QQQ": 8.0}
    assert published["mismatches"] == [{"symbol": "QQQ", "diff": 2.0}]
    assert published["published_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_position_reconciler_handles_sync_adapter_and_tuple_rows():
    class _Contract:
        symbol = "SPY"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 5

    class _IB:
        def positions(self):
            return [_Position()]

    class _Adapter:
        ib = _IB()

    class _PG:
        def fetchall(self, sql, account_id):
            assert account_id == ""
            return [("SPY", 5)]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime).reconcile_once()

    assert result.ok is True
    assert result.reason == "position_ledger_clean"
    assert result.metadata["mismatch_count"] == 0


@pytest.mark.asyncio
async def test_position_reconciler_preserves_lot_level_ledger_breakdown():
    class _Contract:
        symbol = "QQQ"
        secType = "STK"

    class _Position:
        contract = _Contract()
        position = 10

    class _Adapter:
        async def positions(self):
            return [_Position()]

    class _PG:
        async def fetchall(self, sql, account_id):
            assert "GROUP BY symbol, account_id, strategy_id, strategy_instance_id, lot_id" in sql
            assert account_id == "DU123"
            return [
                {
                    "symbol": "QQQ",
                    "account_id": "DU123",
                    "strategy_id": "qqq_intraday_001",
                    "strategy_instance_id": "qqq_intraday_001",
                    "lot_id": "lot-a",
                    "position_owner_key": "qqq_intraday_001:qqq_intraday_001:QQQ:lot-a",
                    "qty": 4,
                },
                {
                    "symbol": "QQQ",
                    "account_id": "DU123",
                    "strategy_id": "qqq_intraday_001",
                    "strategy_instance_id": "qqq_intraday_001",
                    "lot_id": "lot-b",
                    "position_owner_key": "qqq_intraday_001:qqq_intraday_001:QQQ:lot-b",
                    "qty": 6,
                },
            ]

    runtime = type("Runtime", (), {})()
    runtime.adapter = _Adapter()
    runtime.pg = _PG()
    runtime._ib_position_symbol = lambda position: position.contract.symbol

    result = await PositionReconciler(runtime, account_id="DU123").reconcile_once()

    assert result.ok is True
    assert result.metadata["ledger_positions"] == {"QQQ": 10.0}
    assert result.metadata["ledger_lot_count_by_symbol"] == {"QQQ": 2}
    assert result.metadata["ledger_lots"] == [
        {
            "symbol": "QQQ",
            "account_id": "DU123",
            "strategy_id": "qqq_intraday_001",
            "strategy_instance_id": "qqq_intraday_001",
            "lot_id": "lot-a",
            "position_owner_key": "qqq_intraday_001:qqq_intraday_001:QQQ:lot-a",
            "qty": 4.0,
        },
        {
            "symbol": "QQQ",
            "account_id": "DU123",
            "strategy_id": "qqq_intraday_001",
            "strategy_instance_id": "qqq_intraday_001",
            "lot_id": "lot-b",
            "position_owner_key": "qqq_intraday_001:qqq_intraday_001:QQQ:lot-b",
            "qty": 6.0,
        },
    ]
