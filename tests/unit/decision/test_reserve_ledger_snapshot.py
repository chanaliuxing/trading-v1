import pytest

from services.decision.providers import ReserveLedger


class _PGReservationOK:
    async def fetchall(self, sql: str):
        if "core.core_order_reservation" in sql:
            return [
                {"strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "reserved_usd": 10050.25},
                {"strategy_id": "index_super_win_001", "symbol": "TQQQ", "reserved_usd": 5050.00},
            ]
        raise AssertionError("unexpected SQL")


class _PGReservationMissingFallback:
    async def fetchall(self, sql: str):
        if "core.core_order_reservation" in sql:
            raise RuntimeError("relation core.core_order_reservation does not exist")
        if "FROM core.core_order" in sql:
            return [
                {"strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "open_qty": 100.0},
            ]
        raise AssertionError("unexpected SQL")


@pytest.mark.asyncio
async def test_reserve_ledger_uses_reservation_table_as_primary_source():
    ledger = ReserveLedger(pg=_PGReservationOK())
    snap = await ledger.snapshot()
    items = snap["items"]
    assert items[("qqq_intraday_001", "TQQQ")]["amount_remaining"] == pytest.approx(10050.25)
    assert items[("qqq_intraday_001", "TQQQ")]["notional"] == pytest.approx(10050.25)
    assert items[("index_super_win_001", "TQQQ")]["amount_remaining"] == pytest.approx(5050.00)


@pytest.mark.asyncio
async def test_reserve_ledger_falls_back_to_open_order_approximation():
    ledger = ReserveLedger(pg=_PGReservationMissingFallback())
    snap = await ledger.snapshot()
    items = snap["items"]
    assert items[("qqq_intraday_001", "TQQQ")]["amount_remaining"] == pytest.approx(10000.0)
