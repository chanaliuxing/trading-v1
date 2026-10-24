import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.exec.models import Fill
from services.exec.safety_kernel import SafetyKernel


class _FakePG:
    _supports_position_ledger = True

    def __init__(self) -> None:
        self.fetchone = AsyncMock(return_value=None)
        self.fetchall = AsyncMock(return_value=[])
        self.execute = AsyncMock(return_value="OK")


def _kernel(*, ledger=None, pg=None) -> SafetyKernel:
    broker = MagicMock()
    broker.cancel = AsyncMock(return_value=True)
    return SafetyKernel(
        broker_adapter=broker,
        pg=pg if pg is not None else _FakePG(),
        reservation_service=None,
        position_ledger=ledger,
    )


def _ledger() -> MagicMock:
    ledger = MagicMock()
    ledger.account_id = "UNKNOWN_ACCOUNT"
    ledger.append_fill_event = AsyncMock(return_value=True)
    return ledger


def _fill(*, order_id=501, qty=100.0) -> Fill:
    return Fill(
        fill_id=f"fill-{order_id}",
        order_id=order_id,
        order_ref=f"sl-ref-{order_id}",
        symbol="SPY",
        side="SELL",
        qty=qty,
        price=395.0,
        ts_us=1_700_000_000_000_000,
        commission=0.0,
        exchange="SMART",
        exec_id=f"exec-{order_id}",
        perm_id=9000 + order_id,
    )


def _stop_order_note(*, account_id="DU123", strategy_instance_id="strat_a_1", lot_id="lot-a") -> str:
    return json.dumps(
        {
            "type": "stop_loss",
            "order_role": "STOP_LOSS",
            "account_id": account_id,
            "strategy_instance_id": strategy_instance_id,
            "position_lot_id": lot_id,
            "position_owner": f"mean_revert:{strategy_instance_id}:SPY:{lot_id}" if strategy_instance_id and lot_id else None,
            "bracket_id": "bracket-a",
            "oca_group": "oca-a",
            "protection_key": f"{account_id}|mean_revert|SPY|{lot_id}|stop_loss|SELL|100|band:395" if lot_id else "",
        }
    )


def _resolved_stop_order(*, strategy_instance_id="strat_a_1", lot_id="lot-a", account_id="DU123") -> dict:
    return {
        "order_id": "uuid-stop-a",
        "idempotency_key": "idem-stop-a",
        "strategy_id": "mean_revert",
        "strategy_instance_id": strategy_instance_id,
        "symbol": "SPY",
        "side": "SELL",
        "qty": 100.0,
        "order_type": "STP",
        "broker_order_id": "501",
        "ib_perm_id": 9501,
        "order_ref": "sl-ref-501",
        "note": _stop_order_note(
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            lot_id=lot_id,
        ),
    }


@pytest.mark.asyncio
async def test_stop_loss_fill_reduces_only_matching_strategy_instance_lot():
    ledger = _ledger()
    kernel = _kernel(ledger=ledger)

    await kernel._apply_fill_to_core_position(
        resolved_order=_resolved_stop_order(),
        fill=_fill(qty=100.0),
    )

    ledger.append_fill_event.assert_awaited_once()
    kwargs = ledger.append_fill_event.await_args.kwargs
    assert kwargs["strategy_id"] == "mean_revert"
    assert kwargs["strategy_instance_id"] == "strat_a_1"
    assert kwargs["symbol"] == "SPY"
    assert kwargs["lot_id"] == "lot-a"
    assert kwargs["order_role"] == "STOP_LOSS"
    assert kwargs["metadata"]["protective_order_owner"]["strategy_instance_id"] == "strat_a_1"


@pytest.mark.asyncio
async def test_stop_loss_fill_does_not_touch_other_strategy_same_symbol():
    ledger = _ledger()
    kernel = _kernel(ledger=ledger)

    await kernel._apply_fill_to_core_position(
        resolved_order=_resolved_stop_order(strategy_instance_id="strat_a_1", lot_id="lot-a"),
        fill=_fill(qty=100.0),
    )

    kwargs = ledger.append_fill_event.await_args.kwargs
    assert kwargs["strategy_instance_id"] == "strat_a_1"
    assert kwargs["lot_id"] == "lot-a"
    assert kwargs["strategy_instance_id"] != "strat_b_1"
    assert kwargs["lot_id"] != "lot-b"


@pytest.mark.asyncio
async def test_stop_loss_fill_requires_strategy_instance_id():
    ledger = _ledger()
    pg = _FakePG()
    kernel = _kernel(ledger=ledger, pg=pg)

    await kernel._apply_fill_to_core_position(
        resolved_order=_resolved_stop_order(strategy_instance_id="", lot_id="lot-a"),
        fill=_fill(),
    )

    ledger.append_fill_event.assert_not_awaited()
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_loss_fill_requires_lot_id():
    ledger = _ledger()
    pg = _FakePG()
    kernel = _kernel(ledger=ledger, pg=pg)

    await kernel._apply_fill_to_core_position(
        resolved_order=_resolved_stop_order(strategy_instance_id="strat_a_1", lot_id=""),
        fill=_fill(),
    )

    ledger.append_fill_event.assert_not_awaited()
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_loss_fill_unknown_owner_goes_quarantine_or_manual_required(monkeypatch):
    monkeypatch.delenv("IB_ACCOUNT", raising=False)
    ledger = _ledger()
    pg = _FakePG()
    kernel = _kernel(ledger=ledger, pg=pg)
    resolved = _resolved_stop_order(strategy_instance_id="", lot_id="", account_id="")
    resolved["strategy_instance_id"] = ""
    resolved["note"] = json.dumps({"type": "stop_loss", "order_role": "STOP_LOSS"})

    await kernel._apply_fill_to_core_position(resolved_order=resolved, fill=_fill())

    ledger.append_fill_event.assert_not_awaited()
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_loss_fill_does_not_use_broker_aggregate_as_strategy_position():
    ledger = _ledger()
    pg = _FakePG()
    pg.fetchone = AsyncMock(side_effect=AssertionError("broker aggregate/core_position must not be read for stop owner"))
    kernel = _kernel(ledger=ledger, pg=pg)

    await kernel._apply_fill_to_core_position(
        resolved_order=_resolved_stop_order(),
        fill=_fill(qty=100.0),
    )

    ledger.append_fill_event.assert_awaited_once()
    pg.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_loss_fill_does_not_cancel_other_strategy_protection():
    ledger = _ledger()
    kernel = _kernel(ledger=ledger)

    await kernel._apply_fill_to_core_position(
        resolved_order=_resolved_stop_order(strategy_instance_id="strat_a_1", lot_id="lot-a"),
        fill=_fill(qty=100.0),
    )

    kernel.broker.cancel.assert_not_awaited()
