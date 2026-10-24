import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.models import Fill, OCOPair, PlacedOrder
from services.exec.safety_kernel import Plan, Position, SafetyKernel


def _placed(
    *,
    order_id: int,
    order_ref: str,
    symbol: str,
    side: str,
    order_type: str,
    qty: float,
    limit_px: float | None = None,
    stop_px: float | None = None,
) -> PlacedOrder:
    return PlacedOrder(
        order_id=order_id,
        broker_order_id=str(order_id),
        perm_id=order_id,
        order_ref=order_ref,
        symbol=symbol,
        side=side,
        order_type=order_type,
        qty=qty,
        limit_px=limit_px,
        stop_px=stop_px,
        tif="DAY",
        oca_group="OCO_TEST",
        status="SUBMITTED",
    )


def _placed_pending_local(
    *,
    order_id: int,
    order_ref: str,
    symbol: str,
    side: str,
    order_type: str,
    qty: float,
    age_sec: float,
) -> PlacedOrder:
    po = _placed(
        order_id=order_id,
        order_ref=order_ref,
        symbol=symbol,
        side=side,
        order_type=order_type,
        qty=qty,
    )
    po.perm_id = 0
    po.status = "PendingSubmit"
    po.ts_us = int((time.time() - float(age_sec)) * 1e6)
    return po


def _single_owner_scoped_plan(kernel: SafetyKernel) -> Plan:
    assert len(kernel.plans) == 1
    key, plan = next(iter(kernel.plans.items()))
    assert key == kernel._plan_key_for_plan(plan)
    assert key != f"{plan.strategy_id}:{plan.symbol}"
    return plan


@pytest.mark.asyncio
async def test_persist_oco_orders_updates_existing_child_rows():
    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=[
        {"order_id": "parent-uuid"},
        {"order_id": "sl-uuid"},
        {"order_id": "tp-uuid"},
    ])
    pg.execute = AsyncMock(return_value="OK")
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)

    sl_order = _placed(
        order_id=6,
        order_ref="sl:spy_1dte_ma10_reject_0324_001:OPT:SPY:20260402:655:C:deadbeef",
        symbol="OPT:SPY:20260402:655:C",
        side="SELL",
        order_type="STP",
        qty=4.0,
        stop_px=3.43,
    )
    tp_order = _placed(
        order_id=7,
        order_ref="tp:spy_1dte_ma10_reject_0324_001:OPT:SPY:20260402:655:C:feedbeef",
        symbol="OPT:SPY:20260402:655:C",
        side="SELL",
        order_type="LMT",
        qty=4.0,
        limit_px=3.8,
    )

    await kernel._persist_oco_orders(
        parent_order_id=4,
        sl_order=sl_order,
        tp_order=tp_order,
        oca_group="OCO_TEST",
        strategy_id="spy_1dte_ma10_reject_0324_001",
    )

    assert pg.execute.await_count == 2
    sl_sql = pg.execute.await_args_list[0].args[0]
    tp_sql = pg.execute.await_args_list[1].args[0]

    assert "UPDATE core.core_order" in sl_sql
    assert "WHERE order_id = $1" in sl_sql
    assert pg.execute.await_args_list[0].args[1] == "sl-uuid"
    assert pg.execute.await_args_list[0].args[5] == sl_order.perm_id
    assert pg.execute.await_args_list[0].args[13] == "parent-uuid"

    assert "UPDATE core.core_order" in tp_sql
    assert "WHERE order_id = $1" in tp_sql
    assert pg.execute.await_args_list[1].args[1] == "tp-uuid"
    assert pg.execute.await_args_list[1].args[5] == tp_order.perm_id
    assert pg.execute.await_args_list[1].args[13] == "parent-uuid"


@pytest.mark.asyncio
async def test_persist_oco_orders_inserts_new_child_rows_when_missing():
    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=[
        {"order_id": "parent-uuid"},
        None,
        None,
        None,
        None,
        None,
        None,
    ])
    pg.execute = AsyncMock(return_value="OK")
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)

    sl_order = _placed(
        order_id=16,
        order_ref="sl:stocks_kernel_shadow_smh_001:SOXS:abcd1234",
        symbol="SOXS",
        side="BUY",
        order_type="STP",
        qty=254.0,
        stop_px=39.95,
    )
    tp_order = _placed(
        order_id=18,
        order_ref="tp:stocks_kernel_shadow_smh_001:SOXS:dcba4321",
        symbol="SOXS",
        side="BUY",
        order_type="LMT",
        qty=254.0,
        limit_px=38.12,
    )

    await kernel._persist_oco_orders(
        parent_order_id=15,
        sl_order=sl_order,
        tp_order=tp_order,
        oca_group="OCO_TEST",
        strategy_id="stocks_kernel_shadow_smh_001",
    )

    assert pg.execute.await_count == 2
    sl_sql = pg.execute.await_args_list[0].args[0]
    tp_sql = pg.execute.await_args_list[1].args[0]

    assert "INSERT INTO core.core_order" in sl_sql
    assert pg.execute.await_args_list[0].args[1] == "stocks_kernel_shadow_smh_001"
    assert pg.execute.await_args_list[0].args[4] == str(sl_order.order_id)
    assert pg.execute.await_args_list[0].args[5] == sl_order.perm_id
    assert pg.execute.await_args_list[0].args[13] == "parent-uuid"

    assert "INSERT INTO core.core_order" in tp_sql
    assert pg.execute.await_args_list[1].args[1] == "stocks_kernel_shadow_smh_001"
    assert pg.execute.await_args_list[1].args[4] == str(tp_order.order_id)
    assert pg.execute.await_args_list[1].args[5] == tp_order.perm_id
    assert pg.execute.await_args_list[1].args[13] == "parent-uuid"
    assert pg.execute.await_args_list[0].args[3].startswith("oco:sl:parent-uuid:")
    assert pg.execute.await_args_list[1].args[3].startswith("oco:tp:parent-uuid:")


@pytest.mark.asyncio
async def test_persist_oco_orders_registers_protection_keys_when_present():
    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=[
        {"order_id": "parent-uuid"},
        None,
        None,
    ])
    pg.execute = AsyncMock(return_value="OK")
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    sl_order = _placed(
        order_id=26,
        order_ref="sl:key",
        symbol="SOXS",
        side="BUY",
        order_type="STP",
        qty=254.0,
        stop_px=39.95,
    )

    await kernel._persist_oco_orders(
        parent_order_id=25,
        sl_order=sl_order,
        tp_order=None,
        oca_group="OCO_TEST",
        strategy_id="stocks_kernel_shadow_smh_001",
        sl_protection_key="DU123|stocks_kernel_shadow_smh_001|SOXS|lot-1|stop_loss|BUY|254|band:3995",
    )

    sql_texts = [call.args[0] for call in pg.execute.await_args_list]
    assert any("core_protection_order_registry" in sql for sql in sql_texts)
    registry_call = next(call for call in pg.execute.await_args_list if "core_protection_order_registry" in call.args[0])
    assert registry_call.args[1] == "DU123|stocks_kernel_shadow_smh_001|SOXS|lot-1|stop_loss|BUY|254|band:3995"
    assert registry_call.args[11] == "stop_loss"
    assert registry_call.args[12] == "BUY"
    assert registry_call.args[17] == 26


@pytest.mark.asyncio
async def test_persist_oco_orders_does_not_reuse_old_rows_by_broker_order_id():
    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=[
        {"order_id": "parent-uuid"},
        None,
        None,
        None,
        None,
        None,
        None,
    ])
    pg.execute = AsyncMock(return_value="OK")
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)

    sl_order = _placed(
        order_id=44,
        order_ref="sl:stocks_kernel_shadow_smh_001:SOXS:newstop",
        symbol="SOXS",
        side="BUY",
        order_type="STP",
        qty=200.0,
        stop_px=39.9,
    )
    tp_order = _placed(
        order_id=46,
        order_ref="tp:stocks_kernel_shadow_smh_001:SOXS:newtp",
        symbol="SOXS",
        side="BUY",
        order_type="LMT",
        qty=200.0,
        limit_px=39.1,
    )

    await kernel._persist_oco_orders(
        parent_order_id=42,
        sl_order=sl_order,
        tp_order=tp_order,
        oca_group="OCO_REUSED",
        strategy_id="stocks_kernel_shadow_smh_001",
    )

    sql_texts = [call.args[0] for call in pg.execute.await_args_list]
    assert all("INSERT INTO core.core_order" in sql for sql in sql_texts)


@pytest.mark.asyncio
async def test_attach_oco_protection_uses_strategy_offsets_when_present():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.place_stop = AsyncMock(return_value=_placed(
        order_id=11,
        order_ref="sl:test",
        symbol="SOXL",
        side="SELL",
        order_type="STP",
        qty=10.0,
        stop_px=48.5,
    ))
    broker.place_limit = AsyncMock(return_value=_placed(
        order_id=12,
        order_ref="tp:test",
        symbol="SOXL",
        side="SELL",
        order_type="LMT",
        qty=10.0,
        limit_px=51.75,
    ))
    kernel = SafetyKernel(broker_adapter=broker, pg=None, reservation_service=None)
    kernel.orders[7] = _placed(
        order_id=7,
        order_ref="entry:test",
        symbol="SOXL",
        side="BUY",
        order_type="LMT",
        qty=10.0,
        limit_px=50.0,
    )
    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXL",
        entry_order_id=7,
        entry_qty=10.0,
        attach_oco=True,
        oco_config={"sl_offset_abs": 1.5, "tp_offset_abs": 1.75, "tif": "DAY"},
        filled_qty=10.0,
        avg_fill_px=50.0,
        first_fill_ts_us=1,
        position=Position(qty=10.0),
    )
    kernel.plans["stocks_kernel_shadow_smh_001:SOXL"] = plan

    await kernel._attach_oco_protection(plan)

    broker.place_stop.assert_awaited_once()
    broker.place_limit.assert_awaited_once()
    assert broker.place_stop.await_args.args[3] == pytest.approx(48.5)
    assert broker.place_limit.await_args.args[3] == pytest.approx(51.75)


@pytest.mark.asyncio
async def test_attach_oco_protection_passes_offhours_execution_params():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.place_stop = AsyncMock()
    broker.place_stop_limit = AsyncMock(return_value=_placed(
        order_id=21,
        order_ref="sl:test-offhours",
        symbol="SOXL",
        side="SELL",
        order_type="STP_LMT",
        qty=10.0,
        stop_px=48.5,
        limit_px=48.0,
    ))
    broker.place_limit = AsyncMock(return_value=_placed(
        order_id=22,
        order_ref="tp:test-offhours",
        symbol="SOXL",
        side="SELL",
        order_type="LMT",
        qty=10.0,
        limit_px=51.75,
    ))
    kernel = SafetyKernel(broker_adapter=broker, pg=None, reservation_service=None)
    kernel.orders[17] = _placed(
        order_id=17,
        order_ref="entry:test-offhours",
        symbol="SOXL",
        side="BUY",
        order_type="LMT",
        qty=10.0,
        limit_px=50.0,
    )
    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXL",
        entry_order_id=17,
        entry_qty=10.0,
        attach_oco=True,
        oco_config={
            "sl_offset_abs": 1.5,
            "tp_offset_abs": 1.75,
            "tif": "DAY",
            "outside_rth": True,
            "exchange_override": "OVERNIGHT",
            "sl_order_type": "STP_LMT",
            "sl_limit_offset_abs": 0.5,
        },
        filled_qty=10.0,
        avg_fill_px=50.0,
        first_fill_ts_us=1,
        position=Position(qty=10.0),
    )
    kernel.plans["stocks_kernel_shadow_smh_001:SOXL"] = plan

    await kernel._attach_oco_protection(plan)

    broker.place_stop.assert_not_awaited()
    assert broker.place_stop_limit.await_args.kwargs["outside_rth"] is True
    assert broker.place_stop_limit.await_args.kwargs["exchange_override"] == "OVERNIGHT"
    assert broker.place_stop_limit.await_args.args[3] == pytest.approx(48.5)
    assert broker.place_stop_limit.await_args.args[4] == pytest.approx(48.0)
    assert broker.place_limit.await_args.kwargs["outside_rth"] is True
    assert broker.place_limit.await_args.kwargs["exchange_override"] == "OVERNIGHT"


@pytest.mark.asyncio
async def test_sync_oco_child_quantities_updates_broker_and_db_after_additional_fill():
    broker = MagicMock()
    broker.modify = AsyncMock(return_value=True)
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXL",
        entry_order_id=34,
        entry_qty=209.0,
        attach_oco=True,
        filled_qty=209.0,
        avg_fill_px=47.8,
        first_fill_ts_us=1,
        position=Position(qty=209.0),
        oco_pair=OCOPair(
            parent_order_id=34,
            stop_loss_order_id=36,
            take_profit_order_id=38,
            oca_group="OCO_TEST",
        ),
    )
    kernel.orders[36] = _placed(
        order_id=36,
        order_ref="sl:stocks_kernel_shadow_smh_001:SOXL:test",
        symbol="SOXL",
        side="SELL",
        order_type="STP",
        qty=200.0,
        stop_px=47.0,
    )
    kernel.orders[38] = _placed(
        order_id=38,
        order_ref="tp:stocks_kernel_shadow_smh_001:SOXL:test",
        symbol="SOXL",
        side="SELL",
        order_type="LMT",
        qty=200.0,
        limit_px=48.2,
    )

    await kernel._sync_oco_child_quantities(plan)

    assert broker.modify.await_count == 2
    broker.modify.assert_any_await(36, qty=209.0)
    broker.modify.assert_any_await(38, qty=209.0)
    assert kernel.orders[36].qty == pytest.approx(209.0)
    assert kernel.orders[38].qty == pytest.approx(209.0)
    assert pg.execute.await_count == 2
    assert pg.execute.await_args_list[0].args[1] == 209.0
    assert pg.execute.await_args_list[1].args[1] == 209.0


@pytest.mark.asyncio
async def test_on_fill_syncs_existing_oco_children_after_follow_on_fill():
    broker = MagicMock()
    broker.modify = AsyncMock(return_value=True)
    kernel = SafetyKernel(broker_adapter=broker, pg=None, reservation_service=None)

    kernel.orders[34] = _placed(
        order_id=34,
        order_ref="entry:stocks_kernel_shadow_smh_001:SOXL:test",
        symbol="SOXL",
        side="BUY",
        order_type="LMT",
        qty=209.0,
        limit_px=47.8,
    )
    kernel.orders[36] = _placed(
        order_id=36,
        order_ref="sl:stocks_kernel_shadow_smh_001:SOXL:test",
        symbol="SOXL",
        side="SELL",
        order_type="STP",
        qty=200.0,
        stop_px=47.0,
    )
    kernel.orders[38] = _placed(
        order_id=38,
        order_ref="tp:stocks_kernel_shadow_smh_001:SOXL:test",
        symbol="SOXL",
        side="SELL",
        order_type="LMT",
        qty=200.0,
        limit_px=48.2,
    )

    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXL",
        entry_order_id=34,
        entry_qty=209.0,
        attach_oco=True,
        filled_qty=200.0,
        avg_fill_px=47.8,
        first_fill_ts_us=1,
        position=Position(qty=200.0, avg_px=47.8),
        oco_pair=OCOPair(
            parent_order_id=34,
            stop_loss_order_id=36,
            take_profit_order_id=38,
            oca_group="OCO_TEST",
        ),
        status="PROTECTED",
    )
    kernel.plans["stocks_kernel_shadow_smh_001:SOXL"] = plan
    kernel._resolve_order_row_for_fill = AsyncMock(
        return_value={
            "order_id": "parent-uuid",
            "idempotency_key": "idem:test",
            "qty": 209.0,
            "strategy_id": "stocks_kernel_shadow_smh_001",
            "symbol": "SOXL",
            "side": "BUY",
            "note": "{}",
        }
    )
    kernel._persist_fill = AsyncMock(return_value=True)
    kernel._apply_fill_to_core_position = AsyncMock(return_value=None)
    kernel._get_filled_qty = AsyncMock(return_value=209.0)
    kernel._update_order_state = AsyncMock(return_value=None)
    kernel._finalize_decision_terminal = AsyncMock(return_value=None)
    kernel._release_reservation = AsyncMock(return_value=None)

    fill = Fill(
        fill_id="fill-2",
        order_id=34,
        order_ref="entry:stocks_kernel_shadow_smh_001:SOXL:test",
        symbol="SOXL",
        qty=9.0,
        price=47.8,
        ts_us=2,
        perm_id=311275884,
    )

    await kernel.on_fill(fill)

    assert broker.modify.await_count == 2
    broker.modify.assert_any_await(36, qty=209.0)
    broker.modify.assert_any_await(38, qty=209.0)
    assert plan.position.qty == pytest.approx(209.0)


@pytest.mark.asyncio
async def test_attach_oco_protection_marks_plan_failed_on_attach_error():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.place_stop = AsyncMock(side_effect=RuntimeError("shadow_pending_submit"))
    broker.place_limit = AsyncMock()
    kernel = SafetyKernel(broker_adapter=broker, pg=None, reservation_service=None)
    kernel.orders[51] = _placed(
        order_id=51,
        order_ref="entry:test-fail",
        symbol="SOXS",
        side="SELL",
        order_type="LMT",
        qty=10.0,
        limit_px=39.5,
    )
    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXS",
        entry_order_id=51,
        entry_qty=10.0,
        attach_oco=True,
        oco_config={"sl_offset_abs": 0.5, "tp_offset_abs": 1.0, "outside_rth": True, "exchange_override": "SMART"},
        filled_qty=10.0,
        avg_fill_px=39.5,
        first_fill_ts_us=1,
        position=Position(qty=-10.0),
    )

    await kernel._attach_oco_protection(plan)

    assert plan.status == "PROTECTION_FAILED"
    assert plan.protection_failure_ts_us is not None
    assert plan.protection_failure_reason == "shadow_pending_submit"


def test_is_stale_local_protective_pending_uses_age_and_perm_id():
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=None, reservation_service=None)

    stale = _placed_pending_local(
        order_id=91,
        order_ref="sl:stocks_kernel_shadow_smh_001:SOXS:test",
        symbol="SOXS",
        side="BUY",
        order_type="STP",
        qty=10.0,
        age_sec=20.0,
    )
    fresh = _placed_pending_local(
        order_id=92,
        order_ref="sl:stocks_kernel_shadow_smh_001:SOXS:test",
        symbol="SOXS",
        side="BUY",
        order_type="STP",
        qty=10.0,
        age_sec=1.0,
    )

    assert kernel._is_stale_local_protective_pending(stale) is True
    assert kernel._is_stale_local_protective_pending(fresh) is False


@pytest.mark.asyncio
async def test_attach_oco_protection_coalesces_follow_on_fills_before_sizing_children():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.place_stop = AsyncMock(return_value=_placed(
        order_id=61,
        order_ref="sl:test-coalesce",
        symbol="SOXS",
        side="BUY",
        order_type="STP",
        qty=253.0,
        stop_px=39.97,
    ))
    broker.place_limit = AsyncMock(return_value=_placed(
        order_id=62,
        order_ref="tp:test-coalesce",
        symbol="SOXS",
        side="BUY",
        order_type="LMT",
        qty=253.0,
        limit_px=38.12,
    ))
    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=None,
        reservation_service=None,
        oco_attach_timeout_sec=1.0,
        oco_fill_coalesce_sec=0.05,
    )
    kernel.orders[59] = _placed(
        order_id=59,
        order_ref="entry:test-coalesce",
        symbol="SOXS",
        side="SELL",
        order_type="LMT",
        qty=253.0,
        limit_px=39.47,
    )
    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXS",
        entry_order_id=59,
        entry_qty=253.0,
        attach_oco=True,
        oco_config={"sl_offset_abs": 0.5, "tp_offset_abs": 1.0, "tif": "DAY"},
        filled_qty=200.0,
        avg_fill_px=39.47,
        first_fill_ts_us=1,
        position=Position(qty=-200.0, avg_px=39.47),
    )
    kernel.plans["stocks_kernel_shadow_smh_001:SOXS"] = plan

    async def _late_fill() -> None:
        await asyncio.sleep(0.01)
        plan.filled_qty = 253.0
        plan.position.qty = -253.0

    waiter = asyncio.create_task(_late_fill())
    await kernel._attach_oco_protection(plan)
    await waiter

    assert broker.place_stop.await_args.args[2] == pytest.approx(253.0)
    assert broker.place_limit.await_args.args[2] == pytest.approx(253.0)


@pytest.mark.asyncio
async def test_oco_child_quantity_sync_clamps_to_owner_lane_qty():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.modify = AsyncMock(return_value=True)
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"owner_lane_qty": 1.0})
    pg.execute = AsyncMock(return_value="OK")
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    kernel.orders[61] = _placed(
        order_id=61,
        order_ref="sl:owner-lane",
        symbol="NVDA",
        side="SELL",
        order_type="STP",
        qty=1.0,
        stop_px=221.0,
    )
    kernel.orders[62] = _placed(
        order_id=62,
        order_ref="tp:owner-lane",
        symbol="NVDA",
        side="SELL",
        order_type="LMT",
        qty=1.0,
        limit_px=232.0,
    )
    plan = Plan(
        account_id="DU_TEST",
        strategy_id="nvda_minute_flip_paper_001",
        strategy_instance_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        lot_id="lot-nvda-1",
        entry_order_id=59,
        entry_qty=1.0,
        attach_oco=True,
        oco_config={"position_lot_id": "lot-nvda-1"},
        filled_qty=2.0,
        avg_fill_px=226.0,
        position=Position(qty=2.0, avg_px=226.0),
        oco_pair=OCOPair(
            parent_order_id=59,
            stop_loss_order_id=61,
            take_profit_order_id=62,
            oca_group="OCO_OWNER_LANE",
        ),
    )

    await kernel._sync_oco_child_quantities(plan)

    broker.modify.assert_not_awaited()
    assert kernel.orders[61].qty == pytest.approx(1.0)
    assert kernel.orders[62].qty == pytest.approx(1.0)
    sql = str(pg.fetchone.await_args.args[0])
    assert "position_ledger_current" in sql
    assert pg.fetchone.await_args.args[1:4] == ("nvda_minute_flip_paper_001", "NVDA", "lot-nvda-1")


@pytest.mark.asyncio
async def test_attach_oco_protection_sets_manual_lock_on_protection_failure():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.place_stop = AsyncMock(side_effect=RuntimeError("stop placement failed"))
    broker.place_limit = AsyncMock()
    lock_store = MagicMock()
    lock_store.lock = AsyncMock(return_value={})
    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=None,
        reservation_service=None,
        manual_intervention_lock_store=lock_store,
        protection_failure_lock_ttl_sec=900,
    )
    kernel.orders[59] = _placed(
        order_id=59,
        order_ref="entry:test-protection-lock",
        symbol="SOXS",
        side="SELL",
        order_type="LMT",
        qty=253.0,
        limit_px=39.47,
    )
    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXS",
        entry_order_id=59,
        entry_qty=253.0,
        attach_oco=True,
        oco_config={"sl_offset_abs": 0.5, "tp_offset_abs": 1.0, "tif": "DAY"},
        filled_qty=253.0,
        avg_fill_px=39.47,
        first_fill_ts_us=1,
        position=Position(qty=-253.0, avg_px=39.47),
    )

    await kernel._attach_oco_protection(plan)

    assert plan.status == "PROTECTION_FAILED"
    assert plan.protection_lock_set_ts_us is not None
    lock_store.lock.assert_awaited_once()
    kwargs = lock_store.lock.await_args.kwargs
    assert kwargs["strategy_id"] == "stocks_kernel_shadow_smh_001"
    assert kwargs["symbol"] == "SOXS"
    assert kwargs["ttl_sec"] == 900
    assert kwargs["reason"] == "protection_failed"


def test_oco_price_sanity_rejects_market_ref_outlier():
    plan = Plan(
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        entry_order_id=60,
        entry_qty=1.0,
        attach_oco=True,
        oco_config={},
        filled_qty=1.0,
        avg_fill_px=1414.0,
        first_fill_ts_us=1,
        position=Position(qty=1.0, avg_px=1414.0),
    )

    ok, reason = SafetyKernel._validate_oco_price_sanity(
        plan=plan,
        entry_px=1414.0,
        sl_px=1361.11,
        tp_px=1430.56,
        oco_cfg={"market_ref_px": 198.6},
    )

    assert ok is False
    assert reason.startswith("oco_price_out_of_market_band")


@pytest.mark.asyncio
async def test_attach_oco_protection_sets_lock_only_once_for_repeated_failure():
    broker = MagicMock()
    broker.place_stop = AsyncMock(side_effect=RuntimeError("stop placement failed"))
    broker.place_limit = AsyncMock()
    lock_store = MagicMock()
    lock_store.lock = AsyncMock(return_value={})
    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=None,
        reservation_service=None,
        manual_intervention_lock_store=lock_store,
    )
    kernel.orders[60] = _placed(
        order_id=60,
        order_ref="entry:test-protection-lock-repeat",
        symbol="SOXS",
        side="SELL",
        order_type="LMT",
        qty=100.0,
        limit_px=39.47,
    )
    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXS",
        entry_order_id=60,
        entry_qty=100.0,
        attach_oco=True,
        oco_config={"sl_offset_abs": 0.5, "tp_offset_abs": 1.0, "tif": "DAY"},
        filled_qty=100.0,
        avg_fill_px=39.47,
        first_fill_ts_us=1,
        position=Position(qty=-100.0, avg_px=39.47),
    )

    await kernel._attach_oco_protection(plan)
    await kernel._attach_oco_protection(plan)

    lock_store.lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_oco_child_quantities_defers_pending_submit_children(monkeypatch):
    broker = MagicMock()
    broker.modify = AsyncMock(return_value=False)
    kernel = SafetyKernel(broker_adapter=broker, pg=None, reservation_service=None)
    info_mock = MagicMock()
    warn_mock = MagicMock()
    monkeypatch.setattr("services.exec.safety_kernel.logger.info", info_mock)
    monkeypatch.setattr("services.exec.safety_kernel.logger.warning", warn_mock)

    plan = Plan(
        strategy_id="stocks_kernel_shadow_smh_001",
        symbol="SOXS",
        entry_order_id=70,
        entry_qty=253.0,
        attach_oco=True,
        filled_qty=253.0,
        avg_fill_px=39.47,
        first_fill_ts_us=1,
        position=Position(qty=-253.0, avg_px=39.47),
        oco_pair=OCOPair(
            parent_order_id=70,
            stop_loss_order_id=71,
            take_profit_order_id=None,
            oca_group="OCO_TEST",
        ),
    )
    kernel.orders[71] = PlacedOrder(
        order_id=71,
        broker_order_id="71",
        perm_id=0,
        order_ref="sl:stocks_kernel_shadow_smh_001:SOXS:test",
        symbol="SOXS",
        side="BUY",
        order_type="STP_LMT",
        qty=200.0,
        tif="DAY",
        status="PendingSubmit",
    )

    await kernel._sync_oco_child_quantities(plan)

    broker.modify.assert_awaited_once_with(71, qty=253.0)
    info_mock.assert_called()
    assert info_mock.call_args.args[0] == "oco_child_qty_sync_deferred"
    warn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_bootstrap_open_positions_reattaches_missing_protection():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.open_orders = AsyncMock(return_value=[])
    broker.ib = MagicMock()
    broker.ib.positions.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="SQQQ"),
            position=-254.0,
        )
    ]
    broker.place_stop = AsyncMock(return_value=_placed(
        order_id=201,
        order_ref="sl:qqq_intraday_001:SQQQ:bootstop",
        symbol="SQQQ",
        side="BUY",
        order_type="STP",
        qty=254.0,
        stop_px=39.95,
    ))
    broker.place_limit = AsyncMock(return_value=_placed(
        order_id=202,
        order_ref="tp:qqq_intraday_001:SQQQ:boottp",
        symbol="SQQQ",
        side="BUY",
        order_type="LMT",
        qty=254.0,
        limit_px=38.10,
    ))
    pg = MagicMock()
    pg.fetchall = AsyncMock(side_effect=[
        [
            {
                "strategy_id": "qqq_intraday_001",
                "symbol": "SQQQ",
                "pos_qty": -254.0,
                "avg_px": 39.32,
                "updated_at": datetime(2026, 4, 2, 5, 0, 0),
            }
        ],
        [],
    ])
    pg.fetchrow = AsyncMock(return_value={
        "order_id": "parent-uuid",
        "broker_order_id": "128",
        "ib_perm_id": 311282571,
        "order_ref": "stkv1:qqq_intraday_001:SQQQ:ENTRY",
        "strategy_id": "qqq_intraday_001",
        "symbol": "SQQQ",
        "side": "SELL",
        "order_type": "LMT",
        "qty": 254.0,
        "limit_px": 39.18,
        "stop_px": None,
        "tif": "DAY",
        "state": "FILLED",
        "note": json.dumps({"extras": {"protective_oco": {"sl_offset_abs": 0.63, "tp_offset_abs": 1.22}}}),
        "updated_at": datetime(2026, 4, 2, 5, 0, 0),
    })
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    kernel._persist_oco_orders = AsyncMock(return_value=None)
    kernel._sync_oco_child_quantities = AsyncMock(return_value=None)

    await kernel._bootstrap_open_positions()

    broker.open_orders.assert_awaited_once()
    broker.place_stop.assert_awaited_once()
    broker.place_limit.assert_awaited_once()
    plan = _single_owner_scoped_plan(kernel)
    assert plan.status == "PROTECTED"
    assert plan.position.qty == pytest.approx(-254.0)
    assert plan.oco_pair is not None
    assert plan.oco_pair.stop_loss_order_id == 201
    assert plan.oco_pair.take_profit_order_id == 202


@pytest.mark.asyncio
async def test_bootstrap_open_positions_attaches_protection_for_enabled_paper_strategy():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.open_orders = AsyncMock(return_value=[])
    broker.ib = MagicMock()
    broker.ib.positions.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="SOXS"),
            position=-254.0,
        )
    ]
    broker.place_stop = AsyncMock()
    broker.place_limit = AsyncMock()
    pg = MagicMock()
    pg.fetchall = AsyncMock(side_effect=[
        [
            {
                "strategy_id": "stocks_kernel_smh_001",
                "symbol": "SOXS",
                "pos_qty": -254.0,
                "avg_px": 39.32,
                "updated_at": datetime(2026, 4, 2, 5, 0, 0),
            }
        ],
        [],
    ])
    pg.fetchrow = AsyncMock(return_value={
        "order_id": "parent-uuid",
        "broker_order_id": "128",
        "ib_perm_id": 311282571,
        "order_ref": "stkv1:stocks_kernel_smh_001:SOXS:ENTRY",
        "strategy_id": "stocks_kernel_smh_001",
        "symbol": "SOXS",
        "side": "SELL",
        "order_type": "LMT",
        "qty": 254.0,
        "limit_px": 39.18,
        "stop_px": None,
        "tif": "DAY",
        "state": "FILLED",
        "note": json.dumps({"extras": {"protective_oco": {"sl_offset_abs": 0.63, "tp_offset_abs": 1.22}}}),
        "updated_at": datetime(2026, 4, 2, 5, 0, 0),
    })
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    await kernel._bootstrap_open_positions()

    broker.place_stop.assert_awaited_once()
    broker.place_limit.assert_awaited_once()
    plan = _single_owner_scoped_plan(kernel)
    assert plan.attach_oco is True
    assert plan.status == "PROTECTED"
    assert plan.position.qty == pytest.approx(-254.0)
    assert plan.oco_pair is not None


@pytest.mark.asyncio
async def test_bootstrap_open_positions_does_not_attach_oco_to_close_only_parent():
    broker = MagicMock()
    broker.account = "DU_TEST"
    broker.open_orders = AsyncMock(return_value=[])
    broker.ib = MagicMock()
    broker.ib.positions.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="NVDA"),
            position=1.0,
        )
    ]
    broker.place_stop = AsyncMock()
    broker.place_limit = AsyncMock()
    pg = MagicMock()
    pg.fetchall = AsyncMock(side_effect=[
        [
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "pos_qty": 1.0,
                "avg_px": 224.44,
                "updated_at": datetime(2026, 5, 13, 7, 5, 0),
            }
        ],
        [],
    ])
    pg.fetchrow = AsyncMock(return_value={
        "order_id": "flatten-parent-uuid",
        "broker_order_id": "1002",
        "ib_perm_id": 178522278,
        "order_ref": "nvda_flip:nvda_minute_flip_paper_001:NVDA:FLA",
        "strategy_id": "nvda_minute_flip_paper_001",
        "symbol": "NVDA",
        "side": "SELL",
        "order_type": "LMT",
        "qty": 1.0,
        "limit_px": 224.43,
        "stop_px": None,
        "tif": "DAY",
        "state": "SUBMITTED",
        "note": json.dumps({
            "extras": {
                "close_only": True,
                "flatten_action": True,
                "protective_oco": {"sl_offset_abs": 5.5, "tp_offset_abs": 5.5},
            }
        }),
        "updated_at": datetime(2026, 5, 13, 7, 5, 0),
    })
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    await kernel._bootstrap_open_positions()

    broker.place_stop.assert_not_awaited()
    broker.place_limit.assert_not_awaited()
    plan = _single_owner_scoped_plan(kernel)
    assert plan.attach_oco is False
    assert plan.oco_pair is None


@pytest.mark.asyncio
async def test_bootstrap_open_positions_reuses_broker_visible_children():
    broker = MagicMock()
    broker.open_orders = AsyncMock(return_value=[
        _placed(
            order_id=301,
            order_ref="sl:qqq_intraday_001:SQQQ:live-stop",
            symbol="SQQQ",
            side="BUY",
            order_type="STP",
            qty=254.0,
            stop_px=39.95,
        ),
        _placed(
            order_id=302,
            order_ref="tp:qqq_intraday_001:SQQQ:live-tp",
            symbol="SQQQ",
            side="BUY",
            order_type="LMT",
            qty=254.0,
            limit_px=38.10,
        ),
    ])
    broker.ib = MagicMock()
    broker.ib.positions.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="SQQQ"),
            position=-254.0,
        )
    ]
    broker.place_stop = AsyncMock()
    broker.place_limit = AsyncMock()
    pg = MagicMock()
    pg.fetchall = AsyncMock(side_effect=[
        [
            {
                "strategy_id": "qqq_intraday_001",
                "symbol": "SQQQ",
                "pos_qty": -254.0,
                "avg_px": 39.32,
                "updated_at": datetime(2026, 4, 2, 5, 0, 0),
            }
        ],
        [
            {
                "order_id": "sl-uuid",
                "broker_order_id": "301",
                "ib_perm_id": 311300001,
                "order_ref": "sl:qqq_intraday_001:SQQQ:live-stop",
                "symbol": "SQQQ",
                "side": "BUY",
                "order_type": "STP",
                "qty": 254.0,
                "limit_px": None,
                "stop_px": 39.95,
                "tif": "DAY",
                "state": "SUBMITTED",
                "note": json.dumps({"type": "stop_loss"}),
                "updated_at": datetime(2026, 4, 2, 5, 0, 0),
            },
            {
                "order_id": "tp-uuid",
                "broker_order_id": "302",
                "ib_perm_id": 311300002,
                "order_ref": "tp:qqq_intraday_001:SQQQ:live-tp",
                "symbol": "SQQQ",
                "side": "BUY",
                "order_type": "LMT",
                "qty": 254.0,
                "limit_px": 38.10,
                "stop_px": None,
                "tif": "DAY",
                "state": "SUBMITTED",
                "note": json.dumps({"type": "take_profit"}),
                "updated_at": datetime(2026, 4, 2, 5, 0, 0),
            },
        ],
    ])
    pg.fetchrow = AsyncMock(return_value={
        "order_id": "parent-uuid",
        "broker_order_id": "128",
        "ib_perm_id": 311282571,
        "order_ref": "stkv1:qqq_intraday_001:SQQQ:ENTRY",
        "strategy_id": "qqq_intraday_001",
        "symbol": "SQQQ",
        "side": "SELL",
        "order_type": "LMT",
        "qty": 254.0,
        "limit_px": 39.18,
        "stop_px": None,
        "tif": "DAY",
        "state": "FILLED",
        "note": json.dumps({"extras": {"protective_oco": {"sl_offset_abs": 0.63, "tp_offset_abs": 1.22}}}),
        "updated_at": datetime(2026, 4, 2, 5, 0, 0),
    })
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    await kernel._bootstrap_open_positions()

    broker.place_stop.assert_not_awaited()
    broker.place_limit.assert_not_awaited()
    plan = _single_owner_scoped_plan(kernel)
    assert plan.status == "PROTECTED"
    assert plan.oco_pair is not None
    assert plan.oco_pair.stop_loss_order_id == 301
    assert plan.oco_pair.take_profit_order_id == 302


@pytest.mark.asyncio
async def test_bootstrap_open_positions_keeps_owner_lane_qty_when_same_symbol_has_multiple_owners():
    broker = MagicMock()
    broker.open_orders = AsyncMock(
        return_value=[
            _placed(
                order_id=1184,
                order_ref="sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                symbol="NVDA",
                side="SELL",
                order_type="STP",
                qty=1.0,
                stop_px=220.0,
            ),
            _placed(
                order_id=1186,
                order_ref="tp:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                symbol="NVDA",
                side="SELL",
                order_type="LMT",
                qty=1.0,
                limit_px=232.0,
            ),
            _placed(
                order_id=1188,
                order_ref="sl:DUO112752|nvda_minute_flip_paper_002|NVDA|",
                symbol="NVDA",
                side="SELL",
                order_type="STP",
                qty=1.0,
                stop_px=220.0,
            ),
            _placed(
                order_id=1190,
                order_ref="tp:DUO112752|nvda_minute_flip_paper_002|NVDA|",
                symbol="NVDA",
                side="SELL",
                order_type="LMT",
                qty=1.0,
                limit_px=232.0,
            ),
        ]
    )
    broker.ib = MagicMock()
    broker.ib.positions.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="NVDA"),
            position=2.0,
        )
    ]
    broker.place_stop = AsyncMock()
    broker.place_limit = AsyncMock()
    pg = MagicMock()
    pg.fetchall = AsyncMock(
        side_effect=[
            [
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "pos_qty": 1.0,
                    "avg_px": 226.0,
                    "updated_at": datetime(2026, 5, 13, 5, 0, 0),
                },
                {
                    "strategy_id": "nvda_minute_flip_paper_002",
                    "symbol": "NVDA",
                    "pos_qty": 1.0,
                    "avg_px": 226.0,
                    "updated_at": datetime(2026, 5, 13, 5, 0, 0),
                },
            ],
            [
                {
                    "order_id": "sl-001",
                    "broker_order_id": "1184",
                    "ib_perm_id": 178523226,
                    "order_ref": "sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                    "symbol": "NVDA",
                    "side": "SELL",
                    "order_type": "STP",
                    "qty": 1.0,
                    "limit_px": None,
                    "stop_px": 220.0,
                    "tif": "DAY",
                    "state": "SUBMITTED",
                    "note": json.dumps({"type": "stop_loss"}),
                    "updated_at": datetime(2026, 5, 13, 5, 0, 0),
                },
                {
                    "order_id": "tp-001",
                    "broker_order_id": "1186",
                    "ib_perm_id": 178523227,
                    "order_ref": "tp:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                    "symbol": "NVDA",
                    "side": "SELL",
                    "order_type": "LMT",
                    "qty": 1.0,
                    "limit_px": 232.0,
                    "stop_px": None,
                    "tif": "DAY",
                    "state": "SUBMITTED",
                    "note": json.dumps({"type": "take_profit"}),
                    "updated_at": datetime(2026, 5, 13, 5, 0, 0),
                },
            ],
            [
                {
                    "order_id": "sl-002",
                    "broker_order_id": "1188",
                    "ib_perm_id": 178523228,
                    "order_ref": "sl:DUO112752|nvda_minute_flip_paper_002|NVDA|",
                    "symbol": "NVDA",
                    "side": "SELL",
                    "order_type": "STP",
                    "qty": 1.0,
                    "limit_px": None,
                    "stop_px": 220.0,
                    "tif": "DAY",
                    "state": "SUBMITTED",
                    "note": json.dumps({"type": "stop_loss"}),
                    "updated_at": datetime(2026, 5, 13, 5, 0, 0),
                },
                {
                    "order_id": "tp-002",
                    "broker_order_id": "1190",
                    "ib_perm_id": 178523229,
                    "order_ref": "tp:DUO112752|nvda_minute_flip_paper_002|NVDA|",
                    "symbol": "NVDA",
                    "side": "SELL",
                    "order_type": "LMT",
                    "qty": 1.0,
                    "limit_px": 232.0,
                    "stop_px": None,
                    "tif": "DAY",
                    "state": "SUBMITTED",
                    "note": json.dumps({"type": "take_profit"}),
                    "updated_at": datetime(2026, 5, 13, 5, 0, 0),
                },
            ],
        ]
    )

    async def _fetch_parent(sql, strategy_id, symbol):
        return {
            "order_id": f"parent-{strategy_id}",
            "broker_order_id": "1170" if strategy_id.endswith("_001") else "1180",
            "ib_perm_id": 178522620 if strategy_id.endswith("_001") else 178522621,
            "order_ref": f"stkv1:{strategy_id}:{symbol}:ENTRY",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": "BUY",
            "order_type": "LMT",
            "qty": 1.0,
            "limit_px": 226.0,
            "stop_px": None,
            "tif": "DAY",
            "state": "FILLED",
            "note": json.dumps(
                {
                    "extras": {
                        "account_id": "DUO112752",
                        "strategy_instance_id": strategy_id,
                        "position_lot_id": f"lot-{strategy_id}",
                    }
                }
            ),
            "updated_at": datetime(2026, 5, 13, 5, 0, 0),
        }

    pg.fetchrow = AsyncMock(side_effect=_fetch_parent)
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    await kernel._bootstrap_open_positions()

    broker.place_stop.assert_not_awaited()
    broker.place_limit.assert_not_awaited()
    assert len(kernel.plans) == 2
    plans = {plan.strategy_id: plan for plan in kernel.plans.values()}
    assert plans["nvda_minute_flip_paper_001"].position.qty == pytest.approx(1.0)
    assert plans["nvda_minute_flip_paper_001"].filled_qty == pytest.approx(1.0)
    assert plans["nvda_minute_flip_paper_001"].oco_pair is not None
    assert plans["nvda_minute_flip_paper_002"].position.qty == pytest.approx(1.0)
    assert plans["nvda_minute_flip_paper_002"].filled_qty == pytest.approx(1.0)
    assert plans["nvda_minute_flip_paper_002"].oco_pair is not None


@pytest.mark.asyncio
async def test_bootstrap_open_positions_skips_db_stale_position_when_broker_flat():
    broker = MagicMock()
    broker.open_orders = AsyncMock(return_value=[])
    broker.ib = MagicMock()
    broker.ib.positions.return_value = []
    broker.place_stop = AsyncMock()
    broker.place_limit = AsyncMock()
    pg = MagicMock()
    pg.fetchall = AsyncMock(side_effect=[
        [
            {
                "strategy_id": "spy_1dte_ma10_reject_0324_001",
                "symbol": "OPT:SPY:20260402:655:C",
                "pos_qty": -12.0,
                "avg_px": 0.23,
                "updated_at": datetime(2026, 4, 2, 5, 0, 0),
            }
        ],
    ])
    pg.fetchrow = AsyncMock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    await kernel._bootstrap_open_positions()

    broker.place_stop.assert_not_awaited()
    broker.place_limit.assert_not_awaited()
    pg.fetchrow.assert_not_awaited()
    assert "spy_1dte_ma10_reject_0324_001:OPT:SPY:20260402:655:C" not in kernel.plans


@pytest.mark.asyncio
async def test_clear_entry_lock_if_flat_unlocks_after_position_and_orders_zero():
    broker = MagicMock()
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    pg.fetchval = AsyncMock(return_value=0)
    lock_store = MagicMock()
    lock_store.unlock = AsyncMock(return_value=True)
    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=pg,
        reservation_service=None,
        manual_intervention_lock_store=lock_store,
    )

    await kernel._clear_entry_lock_if_flat(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
        reason="flatten_filled",
    )

    lock_store.unlock.assert_awaited_once_with(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
    )


@pytest.mark.asyncio
async def test_clear_entry_lock_if_flat_keeps_lock_when_open_orders_remain():
    broker = MagicMock()
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    pg.fetchval = AsyncMock(return_value=1)
    lock_store = MagicMock()
    lock_store.unlock = AsyncMock(return_value=True)
    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=pg,
        reservation_service=None,
        manual_intervention_lock_store=lock_store,
    )

    await kernel._clear_entry_lock_if_flat(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
        reason="flatten_filled",
    )

    lock_store.unlock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_close_only_lifecycle_note_marks_confirmed():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value=None)
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)

    await kernel._update_close_only_lifecycle_note(
        order_uuid="11111111-1111-1111-1111-111111111111",
        resolved_order={"note": '{"decision_id":"d1","extras":{"flatten_action":true,"close_only":true}}'},
        state="confirmed",
        payload={"completed_at": "2026-04-02T16:00:00", "flatten_action": True},
    )

    note_data = pg.execute.await_args.args[0]
    assert "UPDATE core.core_order" in note_data
    payload = pg.execute.await_args.args[1]
    assert '"flatten_lifecycle"' in payload
    assert '"state": "confirmed"' in payload or '"state":"confirmed"' in payload


@pytest.mark.asyncio
async def test_on_cancel_marks_flatten_lifecycle_cancelled():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={
        "order_id": "11111111-1111-1111-1111-111111111111",
        "note": '{"decision_id":"d1","extras":{"flatten_action":true,"close_only":true}}',
    })
    pg.execute = AsyncMock(return_value=None)
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel._finalize_decision_terminal = AsyncMock(return_value=None)
    kernel._release_reservation = AsyncMock(return_value=None)

    await kernel.on_cancel(101, reason="ib_cancelled")

    note_payload = pg.execute.await_args.args[0]
    assert "UPDATE core.core_order" in note_payload
    merged_note = pg.execute.await_args.args[1]
    assert '"state": "cancelled"' in merged_note or '"state":"cancelled"' in merged_note
    assert '"cancel_reason": "ib_cancelled"' in merged_note or '"cancel_reason":"ib_cancelled"' in merged_note


@pytest.mark.asyncio
async def test_on_reject_marks_flatten_lifecycle_rejected():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={
        "order_id": "11111111-1111-1111-1111-111111111111",
        "note": '{"decision_id":"d1","extras":{"flatten_action":true,"close_only":true}}',
    })
    pg.execute = AsyncMock(return_value=None)
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel._finalize_decision_terminal = AsyncMock(return_value=None)
    kernel._release_reservation = AsyncMock(return_value=None)

    await kernel.on_reject(101, reason="broker_rejected")

    merged_note = pg.execute.await_args.args[1]
    assert '"state": "rejected"' in merged_note or '"state":"rejected"' in merged_note
    assert '"reject_reason": "broker_rejected"' in merged_note or '"reject_reason":"broker_rejected"' in merged_note
