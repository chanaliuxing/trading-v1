from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone
import types

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("clickhouse_connect", types.ModuleType("clickhouse_connect"))

from services.reconciliation.runtime_reconciler import RuntimeReconciler
import services.reconciliation.runtime_reconciler as runtime_reconciler_mod


def _mk_position(symbol: str, qty: float, avg_cost: float = 0.0, account: str = "DUO112752"):
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(symbol=symbol),
        position=qty,
        avgCost=avg_cost,
    )


def _mk_opt_position(
    underlying: str,
    expiry: str,
    strike: float,
    right: str,
    qty: float,
    avg_cost: float = 0.0,
    account: str = "DUO112752",
):
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(
            secType="OPT",
            symbol=underlying,
            lastTradeDateOrContractMonth=expiry,
            strike=strike,
            right=right,
        ),
        position=qty,
        avgCost=avg_cost,
    )


@pytest.mark.asyncio
async def test_sync_positions_from_ib_skips_core_position_upsert():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")

    async def _fetchall(sql: str, *params):
        if "ROW_NUMBER() OVER" in sql:
            return [{"symbol": "SQQQ", "strategy_id": "qqq_intraday_001"}]
        if "FROM core.core_position" in sql:
            return []
        return []

    pg.fetchall = AsyncMock(side_effect=_fetchall)

    adapter = SimpleNamespace(
        account="DUO112752",
        ib=SimpleNamespace(positions=lambda: [_mk_position("SQQQ", 133.0, 75.18)]),
    )

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)
    reconciler._emit_audit = MagicMock()
    await reconciler._sync_positions_from_ib()

    upserts = [c for c in pg.execute.call_args_list if "INSERT INTO core.core_position" in str(c.args[0])]
    assert not upserts
    pg.execute.assert_not_awaited()
    reconciler._emit_audit.assert_called_once()
    assert reconciler._emit_audit.call_args.kwargs["stage"] == "position_sync_skipped"
    assert reconciler._emit_audit.call_args.kwargs["payload"]["must_not_mutate_virtual_positions"] is True


@pytest.mark.asyncio
async def test_sync_positions_from_ib_skips_stale_db_zero_update():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")

    async def _fetchall(sql: str, *params):
        if "ROW_NUMBER() OVER" in sql:
            return [{"symbol": "SQQQ", "strategy_id": "qqq_intraday_001"}]
        if "FROM core.core_position" in sql:
            return [{"strategy_id": "qqq_intraday_001", "symbol": "SQQQ", "pos_qty": 133.0}]
        return []

    pg.fetchall = AsyncMock(side_effect=_fetchall)

    adapter = SimpleNamespace(
        account="DUO112752",
        ib=SimpleNamespace(positions=lambda: []),
    )

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)
    reconciler._emit_audit = MagicMock()
    await reconciler._sync_positions_from_ib()

    zero_updates = [c for c in pg.execute.call_args_list if "SET pos_qty = 0" in str(c.args[0])]
    assert not zero_updates
    pg.execute.assert_not_awaited()
    assert reconciler._emit_audit.call_args.kwargs["reason"] == (
        "broker_aggregate_position_must_not_overwrite_strategy_virtual_positions"
    )


def test_runtime_reconciler_forces_position_sync_disabled_even_if_enabled():
    pg = MagicMock()
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, position_sync_enabled=True)

    assert reconciler.position_sync_enabled is False


@pytest.mark.asyncio
async def test_audit_positions_maps_option_contract_to_internal_symbol_and_skips_auto_heal():
    pg = MagicMock()

    async def _fetchall(sql: str, *params):
        if "FROM core.core_position" in sql:
            return [{"strategy_id": "qqq_0dte_opt_live_001", "symbol": "OPT:QQQ:20260310:610:P", "pos_qty": 3.0}]
        return []

    pg.fetchall = AsyncMock(side_effect=_fetchall)
    pg.execute = AsyncMock(return_value="OK")

    adapter = SimpleNamespace(
        account="DUO112752",
        ib=SimpleNamespace(positions=lambda: [_mk_opt_position("QQQ", "20260310", 610.0, "P", 3.0)]),
    )

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)
    reconciler._auto_heal_stale_db_position = AsyncMock(return_value=False)

    await reconciler._audit_positions(db_open_orders=[], ib_open_orders=[])

    reconciler._auto_heal_stale_db_position.assert_not_awaited()
    assert pg.execute.await_count == 0


@pytest.mark.asyncio
async def test_auto_heal_stale_db_position_is_disabled_in_runtime_mode():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    healed = await reconciler._auto_heal_stale_db_position(
        symbol="OPT:SPY:20260402:655:C",
        ib_qty=0.0,
        db_qty=-12.0,
        db_rows=[{"strategy_id": "spy_1dte_ma10_reject_0324_001", "symbol": "OPT:SPY:20260402:655:C", "pos_qty": -12.0}],
        db_open_symbols=set(),
        ib_open_symbols=set(),
    )

    assert healed is False
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_heal_stale_db_position_skips_when_broker_truth_disconnected():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    adapter = SimpleNamespace(account="DUO112752", is_connected=lambda: False)

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)

    healed = await reconciler._auto_heal_stale_db_position(
        symbol="OPT:SPY:20260402:655:C",
        ib_qty=0.0,
        db_qty=-12.0,
        db_rows=[{"strategy_id": "spy_1dte_ma10_reject_0324_001", "symbol": "OPT:SPY:20260402:655:C", "pos_qty": -12.0}],
        db_open_symbols=set(),
        ib_open_symbols=set(),
        broker_truth_connected=False,
    )

    assert healed is False
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_flatten_lifecycle_emits_incomplete_detected_for_stale_pending():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": -4.0})
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)
    reconciler._emit_audit = MagicMock()

    await reconciler._audit_flatten_lifecycle(
        db_orders=[
            {
                "order_id": "11111111-1111-1111-1111-111111111111",
                "strategy_id": "spy_1dte_ma10_reject_0324_001",
                "symbol": "OPT:SPY:20260402:655:C",
                "broker_order_id": "101",
                "ib_perm_id": 999,
                "state": "SUBMITTED",
                "order_ref": "optk:spy:flatten",
                "updated_at": datetime.now(timezone.utc) - timedelta(seconds=90),
                "note": '{"flatten_lifecycle":{"state":"pending"}}',
            }
        ]
    )

    reconciler._emit_audit.assert_called_once()
    kwargs = reconciler._emit_audit.call_args.kwargs
    assert kwargs["stage"] == "flatten_incomplete_detected"
    assert kwargs["reason"] == "flatten_lifecycle_still_open"


@pytest.mark.asyncio
async def test_reconcile_flatten_lifecycle_auto_confirms_and_clears_flatten_action_lock():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    pg.execute = AsyncMock(return_value="OK")
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value={"reason": "flatten_action", "context": {"flatten_action": True}})
    lock_store.unlock = AsyncMock(return_value=True)

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, manual_intervention_lock_store=lock_store)
    reconciler._emit_audit = MagicMock()

    await reconciler._reconcile_flatten_lifecycle(
        db_orders=[
            {
                "order_id": "22222222-2222-2222-2222-222222222222",
                "strategy_id": "spy_1dte_ma10_reject_0324_001",
                "symbol": "OPT:SPY:20260402:655:C",
                "broker_order_id": "102",
                "ib_perm_id": 1000,
                "state": "SUBMITTED",
                "order_ref": "optk:spy:flatten",
                "note": '{"flatten_lifecycle":{"state":"submitted"}}',
            }
        ],
        ib_open_orders=[],
    )

    update_sql = pg.execute.await_args_list[0].args[0]
    note_payload = pg.execute.await_args_list[0].args[1]
    assert "UPDATE core.core_order" in update_sql
    assert '"flatten_lifecycle"' in note_payload
    assert '"state": "confirmed"' in note_payload or '"state":"confirmed"' in note_payload
    lock_store.unlock.assert_awaited_once_with(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
    )
    stages = [call.kwargs.get("stage") for call in reconciler._emit_audit.call_args_list]
    assert "flatten_auto_confirmed" in stages
    assert "flatten_action_lock_cleared" in stages


@pytest.mark.asyncio
async def test_load_broker_symbol_position_qty_returns_none_when_option_mapping_unknown(monkeypatch):
    pg = MagicMock()
    adapter = SimpleNamespace(
        account="DUO112752",
        ib=SimpleNamespace(positions=lambda: [_mk_opt_position("QQQ", "20260413", 610.0, "C", 2.0)]),
    )
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)
    monkeypatch.setattr(runtime_reconciler_mod, "internal_opt_symbol_from_ib_contract", lambda _c: "")

    qty = await reconciler._load_broker_symbol_position_qty(symbol="OPT:QQQ:20260413:610:C")

    assert qty is None


def test_ib_position_symbol_returns_empty_for_option_when_mapping_unknown(monkeypatch):
    pg = MagicMock()
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter)
    position = _mk_opt_position("QQQ", "20260413", 610.0, "C", 2.0)
    monkeypatch.setattr(runtime_reconciler_mod, "internal_opt_symbol_from_ib_contract", lambda _c: "")

    symbol = reconciler._ib_position_symbol(position)

    assert symbol == ""


@pytest.mark.asyncio
async def test_reconcile_flatten_lifecycle_does_not_clear_manual_flatten_lock():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    pg.execute = AsyncMock(return_value="OK")
    adapter = SimpleNamespace(account="DUO112752", ib=SimpleNamespace(positions=lambda: []))
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value={"reason": "manual_flatten", "context": {"flatten_action": True}})
    lock_store.unlock = AsyncMock(return_value=True)

    reconciler = RuntimeReconciler(pg=pg, ib_adapter=adapter, manual_intervention_lock_store=lock_store)
    reconciler._emit_audit = MagicMock()

    await reconciler._reconcile_flatten_lifecycle(
        db_orders=[
            {
                "order_id": "33333333-3333-3333-3333-333333333333",
                "strategy_id": "spy_1dte_ma10_reject_0324_001",
                "symbol": "OPT:SPY:20260402:655:C",
                "broker_order_id": "103",
                "ib_perm_id": 1001,
                "state": "SUBMITTED",
                "order_ref": "optk:spy:flatten",
                "note": '{"flatten_lifecycle":{"state":"pending"}}',
            }
        ],
        ib_open_orders=[],
    )

    lock_store.unlock.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_positions_quarantines_broker_only_option_position_for_recent_strategy_owner():
    pg = MagicMock()

    async def _fetchall(sql: str, *params):
        if "FROM core.core_position" in sql and "WHERE abs(pos_qty) > 0.000001" in sql:
            return []
        if "FROM core.core_order" in sql and "WHERE symbol = $1" in sql:
            return [{"strategy_id": "spy_1dte_ma10_reject_0324_001"}]
        return []

    pg.fetchall = AsyncMock(side_effect=_fetchall)
    adapter = SimpleNamespace(
        account="DUO112752",
        is_connected=lambda: True,
        ib=SimpleNamespace(
            positions=lambda: [_mk_opt_position("SPY", "20260402", 655.0, "C", -28.0)]
        ),
    )
    lock_store = MagicMock()
    lock_store.lock = AsyncMock(return_value={"reason": "position_mismatch_quarantine"})

    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
    )
    reconciler._emit_audit = MagicMock()

    await reconciler._audit_positions(db_open_orders=[], ib_open_orders=[])

    lock_store.lock.assert_awaited_once_with(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
        ttl_sec=21600,
        reason="position_mismatch_quarantine",
        context={
            "ib_qty": -28.0,
            "db_qty": 0.0,
            "symbol": "OPT:SPY:20260402:655:C",
        },
    )
    stages = [call.kwargs.get("stage") for call in reconciler._emit_audit.call_args_list]
    assert "position_mismatch_detected" in stages
    assert "position_mismatch_quarantined" in stages


@pytest.mark.asyncio
async def test_audit_positions_skips_quarantine_when_no_strategy_owner_found():
    pg = MagicMock()

    async def _fetchall(sql: str, *params):
        if "FROM core.core_position" in sql and "WHERE abs(pos_qty) > 0.000001" in sql:
            return []
        if "FROM core.core_order" in sql and "WHERE symbol = $1" in sql:
            return []
        return []

    pg.fetchall = AsyncMock(side_effect=_fetchall)
    adapter = SimpleNamespace(
        account="DUO112752",
        is_connected=lambda: True,
        ib=SimpleNamespace(
            positions=lambda: [_mk_opt_position("SPY", "20260402", 655.0, "C", -28.0)]
        ),
    )
    lock_store = MagicMock()
    lock_store.lock = AsyncMock()

    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
    )
    reconciler._emit_audit = MagicMock()

    await reconciler._audit_positions(db_open_orders=[], ib_open_orders=[])

    lock_store.lock.assert_not_awaited()
    stages = [call.kwargs.get("stage") for call in reconciler._emit_audit.call_args_list]
    assert "position_mismatch_detected" in stages
    assert "position_mismatch_quarantine_skipped" in stages
