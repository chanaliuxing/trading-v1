import sys
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.safety_kernel import SafetyKernel
from services.exec.models import Fill


def _mk_fill(*, qty: float, price: float) -> Fill:
    return Fill(
        fill_id="f1",
        order_id=1,
        order_ref="ref",
        symbol="TQQQ",
        qty=qty,
        price=price,
        ts_us=1_700_000_000_000_000,
        side="BUY",
        commission=0.0,
        exchange="SMART",
        exec_id="x1",
        perm_id=111,
    )


@pytest.mark.asyncio
async def test_apply_fill_updates_position_reduce_long_realized_pnl():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        return_value={
            "pos_qty": 10.0,
            "avg_px": 100.0,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
        }
    )
    pg.execute = AsyncMock(return_value="OK")
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel.position_ledger = None

    resolved = {"strategy_id": "s1", "symbol": "TQQQ", "side": "SELL"}
    fill = _mk_fill(qty=4.0, price=105.0)

    await kernel._apply_fill_to_core_position(resolved_order=resolved, fill=fill)

    # new_qty=6, avg stays 100, realized + 4*(105-100)=20
    assert pg.execute.await_count == 1
    args = pg.execute.await_args.args
    assert "UPDATE core.core_position" in args[0]
    assert args[1] == "s1"
    assert args[2] == "TQQQ"
    assert float(args[3]) == 6.0
    assert float(args[4]) == 100.0
    assert float(args[5]) == 20.0


@pytest.mark.asyncio
async def test_apply_fill_updates_position_reversal_sets_new_avg_to_fill_price():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        return_value={
            "pos_qty": 3.0,
            "avg_px": 100.0,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
        }
    )
    pg.execute = AsyncMock(return_value="OK")
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel.position_ledger = None

    resolved = {"strategy_id": "s1", "symbol": "TQQQ", "side": "SELL"}
    fill = _mk_fill(qty=5.0, price=90.0)

    await kernel._apply_fill_to_core_position(resolved_order=resolved, fill=fill)

    # new_qty=-2, avg becomes 90, realized closes 3*(90-100)=-30
    args = pg.execute.await_args.args
    assert float(args[3]) == -2.0
    assert float(args[4]) == 90.0
    assert float(args[5]) == -30.0


@pytest.mark.asyncio
async def test_apply_fill_syncs_core_position_qty_from_ledger_but_avg_from_fill_accounting():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        side_effect=[
            {
                "ledger_qty": -3168,
                "ledger_notional_sum": -4_400_000.0,
            },
            None,
        ]
    )
    pg.execute = AsyncMock(return_value="INSERT 0 1")
    ledger = MagicMock()
    ledger.account_id = "UNKNOWN_ACCOUNT"
    ledger.append_fill_event = AsyncMock(return_value=True)
    kernel = SafetyKernel(
        broker_adapter=MagicMock(),
        pg=pg,
        reservation_service=None,
        position_ledger=ledger,
    )

    resolved = {
        "order_id": "order-1",
        "strategy_id": "stocks_kernel_shadow_smh_001",
        "symbol": "SOXS",
        "side": "SELL",
        "ib_perm_id": 1324960648,
        "idempotency_key": "idem-1",
        "order_ref": "tp:UNKNOWN_ACCOUNT|stocks_kernel_shadow_smh_0",
    }
    fill = _mk_fill(qty=1056.0, price=18.23)

    await kernel._apply_fill_to_core_position(resolved_order=resolved, fill=fill)

    ledger.append_fill_event.assert_awaited_once()
    assert pg.execute.await_count == 1
    args = pg.execute.await_args.args
    assert "position_ledger_current" not in args[0]
    assert "ON CONFLICT (strategy_id, symbol) DO UPDATE" in args[0]
    assert args[1] == "stocks_kernel_shadow_smh_001"
    assert args[2] == "SOXS"
    assert float(args[3]) == -3168.0
    assert float(args[4]) == pytest.approx(18.23)


@pytest.mark.asyncio
async def test_on_fill_does_not_update_position_when_fill_is_duplicate():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value="OK")

    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel._resolve_order_row_for_fill = AsyncMock(
        return_value={"order_id": "uuid1", "idempotency_key": "k1", "qty": 1.0, "strategy_id": "s1", "symbol": "TQQQ", "side": "BUY"}
    )
    kernel._persist_fill = AsyncMock(return_value=False)
    kernel._get_filled_qty = AsyncMock(return_value=1.0)
    kernel._update_order_state = AsyncMock(return_value=None)
    kernel._release_reservation = AsyncMock(return_value=None)

    await kernel.on_fill(_mk_fill(qty=1.0, price=50.0))

    # No INSERT/UPDATE to core_position should happen
    exec_sqls = [str(c.args[0]) for c in pg.execute.call_args_list]
    assert not any("core.core_position" in s for s in exec_sqls)


@pytest.mark.asyncio
async def test_on_fill_blocks_production_mutation_when_account_unknown(monkeypatch):
    monkeypatch.delenv("IB_ACCOUNT", raising=False)
    pg = MagicMock()
    pg._requires_known_account_for_fill_mutation = True
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value="OK")
    broker = MagicMock()
    broker.account = ""

    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    kernel._resolve_order_row_for_fill = AsyncMock(
        return_value={
            "order_id": "uuid1",
            "idempotency_key": "k1",
            "qty": 1.0,
            "strategy_id": "s1",
            "symbol": "TQQQ",
            "side": "BUY",
            "order_ref": "ref",
            "note": '{"decision_id":"d1"}',
        }
    )
    kernel._persist_fill = AsyncMock(return_value=True)
    kernel._apply_fill_to_core_position = AsyncMock(return_value=None)
    kernel._get_filled_qty = AsyncMock(return_value=1.0)
    kernel._update_order_state = AsyncMock(return_value=None)

    await kernel.on_fill(_mk_fill(qty=1.0, price=50.0))

    kernel._persist_fill.assert_not_awaited()
    kernel._apply_fill_to_core_position.assert_not_awaited()
    kernel._update_order_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_fill_retries_order_resolution_until_db_row_visible():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        side_effect=[
            None,  # resolve by perm_id first pass
            None,  # resolve by order_ref first pass
            None,  # resolve by broker_order_id first pass
            {
                "order_id": "uuid1",
                "idempotency_key": "k1",
                "qty": 1.0,
                "strategy_id": "s1",
                "symbol": "TQQQ",
                "side": "BUY",
                "note": '{"decision_id":"d1"}',
            },  # resolve by perm_id retry
        ]
    )
    pg.execute = AsyncMock(return_value="OK")

    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel._fill_order_resolve_retry_attempts = 1
    kernel._fill_order_resolve_retry_sleep_sec = 0.000001
    kernel._persist_fill = AsyncMock(return_value=True)
    kernel._apply_fill_to_core_position = AsyncMock(return_value=None)
    kernel._get_filled_qty = AsyncMock(return_value=1.0)
    kernel._update_order_state = AsyncMock(return_value=None)
    kernel._finalize_decision_terminal = AsyncMock(return_value=None)
    kernel._release_reservation = AsyncMock(return_value=None)
    kernel._resolve_decision_id_from_events = AsyncMock(return_value="d1")

    await kernel.on_fill(_mk_fill(qty=1.0, price=50.0))

    kernel._persist_fill.assert_awaited_once()
    kernel._apply_fill_to_core_position.assert_awaited_once()
    kernel._update_order_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_order_row_for_fill_ignores_stale_broker_order_id_candidate():
    stale_row = {
        "order_id": "uuid-stale",
        "idempotency_key": "idem-stale",
        "qty": 1.0,
        "strategy_id": "legacy_strategy",
        "symbol": "TQQQ",
        "side": "BUY",
        "state": "FILLED",
        "order_ref": "legacy-ref",
        "note": "{}",
        "created_at": datetime.now(timezone.utc) - timedelta(days=7),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=7),
    }
    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=[None, None, stale_row])
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel._fill_broker_order_id_fallback_recent_sec = 300.0

    fill = Fill(
        fill_id="fill-stale-fallback",
        order_id=241,
        order_ref="",
        symbol="SQQQ",
        qty=1.0,
        price=50.0,
        ts_us=1_700_000_000_000_000,
        commission=0.0,
        exchange="SMART",
        exec_id="exec-stale-fallback",
        perm_id=None,
    )

    resolved = await kernel._resolve_order_row_for_fill_once(fill)

    assert resolved is None


@pytest.mark.asyncio
async def test_on_fill_retries_past_stale_broker_order_id_candidate_until_recent_row_visible():
    stale_row = {
        "order_id": "uuid-stale",
        "idempotency_key": "idem-stale",
        "qty": 126.0,
        "strategy_id": "legacy_strategy",
        "symbol": "TQQQ",
        "side": "BUY",
        "state": "FILLED",
        "order_ref": "legacy-ref",
        "note": '{"decision_id":"legacy-decision"}',
        "created_at": datetime.now(timezone.utc) - timedelta(days=7),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=7),
    }
    fresh_row = {
        "order_id": "uuid-fresh",
        "idempotency_key": "idem-fresh",
        "qty": 126.0,
        "strategy_id": "qqq_intraday_001",
        "symbol": "SQQQ",
        "side": "SELL",
        "state": "SUBMITTED",
        "order_ref": "qqqv1:qqq_intraday_001:SQQQ:FLATTEN:TRG:test",
        "note": '{"decision_id":"fresh-decision","extras":{"flatten_action":true,"intent_action":"flatten"}}',
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        side_effect=[
            stale_row,
            fresh_row,
        ]
    )
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel._fill_order_resolve_retry_attempts = 1
    kernel._fill_order_resolve_retry_sleep_sec = 0.000001
    kernel._fill_broker_order_id_fallback_recent_sec = 300.0
    kernel._allow_broker_order_id_fill_fallback = True
    kernel._persist_fill = AsyncMock(return_value=True)
    kernel._apply_fill_to_core_position = AsyncMock(return_value=None)
    kernel._get_filled_qty = AsyncMock(return_value=126.0)
    kernel._update_order_state = AsyncMock(return_value=None)
    kernel._finalize_decision_terminal = AsyncMock(return_value=None)
    kernel._release_reservation = AsyncMock(return_value=None)
    kernel.flatten_controller = MagicMock()
    kernel.flatten_controller.resolve_from_position = AsyncMock(return_value="confirmed")

    fill_ts_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    fill = Fill(
        fill_id="fill-retry-fresh",
        order_id=241,
        order_ref="",
        symbol="SQQQ",
        qty=126.0,
        price=55.0,
        ts_us=fill_ts_us,
        side="SELL",
        commission=0.0,
        exchange="SMART",
        exec_id="exec-retry-fresh",
        perm_id=None,
    )

    await kernel.on_fill(fill)

    kernel._persist_fill.assert_awaited_once()
    kernel._apply_fill_to_core_position.assert_awaited_once()
    resolved_call = kernel._apply_fill_to_core_position.await_args.kwargs["resolved_order"]
    assert resolved_call["order_id"] == "uuid-fresh"
    assert resolved_call["strategy_id"] == "qqq_intraday_001"
    assert resolved_call["symbol"] == "SQQQ"


@pytest.mark.asyncio
async def test_fill_resolution_rejects_side_mismatch_before_position_mutation():
    row = {
        "order_id": "uuid-side-mismatch",
        "idempotency_key": "idem-side-mismatch",
        "qty": 10.0,
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "side": "BUY",
        "state": "SUBMITTED",
        "broker_order_id": "241",
        "ib_perm_id": 999,
        "order_ref": "qqqv1:qqq_intraday_001:TQQQ:ENTRY",
        "note": '{"decision_id":"decision-side-mismatch"}',
        "created_at": datetime.fromtimestamp(1_700_000_000_000_000 / 1e6, tz=timezone.utc),
        "updated_at": datetime.fromtimestamp(1_700_000_000_000_000 / 1e6, tz=timezone.utc),
    }
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=[row])
    pg.fetchone = AsyncMock(return_value=None)
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)
    kernel._persist_fill = AsyncMock(return_value=True)
    kernel._apply_fill_to_core_position = AsyncMock(return_value=None)

    fill = Fill(
        fill_id="fill-side-mismatch",
        order_id=241,
        order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY",
        symbol="TQQQ",
        qty=10.0,
        price=50.0,
        ts_us=1_700_000_000_000_000,
        side="SELL",
        exec_id="exec-side-mismatch",
        perm_id=999,
    )

    await kernel.on_fill(fill)

    kernel._persist_fill.assert_not_awaited()
    kernel._apply_fill_to_core_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_resolution_requires_unique_verified_candidate():
    ts = datetime.fromtimestamp(1_700_000_000_000_000 / 1e6, tz=timezone.utc)
    rows = [
        {
            "order_id": f"uuid-{idx}",
            "idempotency_key": f"idem-{idx}",
            "qty": 10.0,
            "strategy_id": "qqq_intraday_001",
            "symbol": "TQQQ",
            "side": "BUY",
            "state": "SUBMITTED",
            "broker_order_id": str(500 + idx),
            "ib_perm_id": 999,
            "order_ref": f"duplicate-ref-{idx}",
            "note": '{"decision_id":"decision-duplicate"}',
            "created_at": ts,
            "updated_at": ts,
        }
        for idx in range(2)
    ]
    pg = MagicMock()
    pg.fetchall = AsyncMock(return_value=rows)
    pg.fetchone = AsyncMock(return_value=None)
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=pg, reservation_service=None)

    fill = Fill(
        fill_id="fill-ambiguous",
        order_id=501,
        order_ref="",
        symbol="TQQQ",
        qty=5.0,
        price=50.0,
        ts_us=1_700_000_000_000_000,
        side="BUY",
        exec_id="exec-ambiguous",
        perm_id=999,
    )

    resolved = await kernel._resolve_order_row_for_fill_once(fill)

    assert resolved is None


@pytest.mark.asyncio
async def test_backfill_fill_after_order_visible_is_idempotent_for_terminal_side_effects():
    pg = MagicMock()
    broker = MagicMock()
    reservation = MagicMock()
    reservation.release_on_terminal = AsyncMock(return_value=(True, 100.0))
    flatten_controller = MagicMock()
    flatten_controller.resolve_from_position = AsyncMock(return_value=SimpleNamespace(state="confirmed"))

    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=pg,
        reservation_service=reservation,
        flatten_controller=flatten_controller,
    )

    resolved_row = {
        "order_id": "uuid-fill-backfill",
        "idempotency_key": "idem-fill-backfill",
        "qty": 5.0,
        "strategy_id": "qqq_intraday_001",
        "symbol": "SQQQ",
        "side": "SELL",
        "state": "SUBMITTED",
        "order_ref": "qqqv1:qqq_intraday_001:SQQQ:FLATTEN:TRG:test",
        "note": '{"decision_id":"dec-fill-backfill","extras":{"flatten_action":true,"intent_action":"flatten"}}',
    }
    filled_row = dict(resolved_row)
    filled_row["state"] = "FILLED"

    kernel._resolve_order_row_for_fill = AsyncMock(side_effect=[resolved_row, filled_row])
    kernel._fill_exists_by_exec_id = AsyncMock(side_effect=[False, True])
    kernel._persist_fill = AsyncMock(return_value=True)
    kernel._apply_fill_to_core_position = AsyncMock(return_value=None)
    kernel._get_filled_qty = AsyncMock(return_value=5.0)
    kernel._update_order_state = AsyncMock(return_value=None)
    kernel._finalize_decision_terminal = AsyncMock(return_value=None)
    kernel._resolve_decision_id_from_events = AsyncMock(return_value="dec-fill-backfill")

    fill = Fill(
        fill_id="fill-backfill-1",
        order_id=419,
        order_ref="qqqv1:qqq_intraday_001:SQQQ:FLATTEN:TRG:test",
        symbol="SQQQ",
        qty=5.0,
        price=67.58,
        ts_us=1_700_000_000_000_000,
        commission=0.0,
        exchange="SMART",
        exec_id="exec-fill-backfill-1",
        perm_id=974609308,
    )

    first = await kernel.backfill_fill_after_order_visible(fill)
    second = await kernel.backfill_fill_after_order_visible(fill)

    assert first["resolved"] is True
    assert first["persisted"] is True
    assert first["fill_inserted"] is True
    assert first["terminal_applied"] is True
    assert second["resolved"] is True
    assert second["persisted"] is True
    assert second["fill_inserted"] is False
    assert second["terminal_applied"] is False

    kernel._persist_fill.assert_awaited_once()
    kernel._apply_fill_to_core_position.assert_awaited_once()
    assert kernel._update_order_state.await_count == 2
    kernel._finalize_decision_terminal.assert_awaited_once_with(
        decision_id="dec-fill-backfill",
        terminal_state="FILLED",
    )
    assert reservation.release_on_terminal.await_count == 1
    flatten_controller.resolve_from_position.assert_awaited_once()


class _SharedPositionState:
    def __init__(self):
        self.row = None
        self.advisory_lock = asyncio.Lock()


class _FakeConn:
    def __init__(self, shared: _SharedPositionState):
        self.shared = shared
        self._lock_held = False

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
        finally:
            if self._lock_held:
                self.shared.advisory_lock.release()
                self._lock_held = False

    async def execute(self, sql: str, *args):
        if "pg_advisory_xact_lock" in sql:
            await self.shared.advisory_lock.acquire()
            self._lock_held = True
            return "OK"
        if "UPDATE core.core_position" in sql:
            self.shared.row = {
                "pos_qty": float(args[2]),
                "avg_px": float(args[3]) if args[3] is not None else None,
                "realized_pnl_usd": float(args[4]),
                "unrealized_pnl_usd": float((self.shared.row or {}).get("unrealized_pnl_usd") or 0.0),
            }
            return "UPDATE 1"
        if "INSERT INTO core.core_position" in sql:
            self.shared.row = {
                "pos_qty": float(args[2]),
                "avg_px": float(args[3]) if args[3] is not None else None,
                "realized_pnl_usd": float(args[4]),
                "unrealized_pnl_usd": float(args[5]),
            }
            return "INSERT 1"
        return "OK"

    async def fetchrow(self, sql: str, *args):
        if "FROM core.core_position" not in sql:
            return None
        snapshot = dict(self.shared.row) if self.shared.row is not None else None
        await asyncio.sleep(0.01)
        return snapshot


class _FakePg:
    def __init__(self, shared: _SharedPositionState):
        self.shared = shared
        self._supports_position_locking = True

    @asynccontextmanager
    async def acquire_conn(self):
        yield _FakeConn(self.shared)


@pytest.mark.asyncio
async def test_apply_fill_position_update_is_atomic_under_concurrent_partial_fills():
    shared = _SharedPositionState()
    kernel = SafetyKernel(broker_adapter=MagicMock(), pg=_FakePg(shared), reservation_service=None)
    kernel.position_ledger = None
    resolved = {"strategy_id": "s1", "symbol": "SOXL", "side": "BUY"}

    fill_a = Fill(
        fill_id="fa",
        order_id=96,
        order_ref="ref-a",
        symbol="SOXL",
        qty=100.0,
        price=47.95,
        ts_us=1_700_000_000_000_001,
        commission=0.0,
        exchange="OVERNIGHT",
        exec_id="exec-a",
        perm_id=111,
    )
    fill_b = Fill(
        fill_id="fb",
        order_id=96,
        order_ref="ref-b",
        symbol="SOXL",
        qty=108.0,
        price=47.95,
        ts_us=1_700_000_000_000_002,
        commission=0.0,
        exchange="OVERNIGHT",
        exec_id="exec-b",
        perm_id=111,
    )

    await asyncio.gather(
        kernel._apply_fill_to_core_position(resolved_order=resolved, fill=fill_a),
        kernel._apply_fill_to_core_position(resolved_order=resolved, fill=fill_b),
    )

    assert shared.row is not None
    assert shared.row["pos_qty"] == pytest.approx(208.0)
    assert shared.row["avg_px"] == pytest.approx(47.95)
