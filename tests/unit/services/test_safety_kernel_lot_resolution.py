import pytest

from services.exec.safety_kernel import SafetyKernel


class _FakePG:
    def __init__(self):
        self.parent_note = None
        self.registry_rows = []

    async def fetchone(self, sql, *args):
        if "FROM core.core_order" in str(sql):
            return {"note": self.parent_note}
        return None

    async def fetchall(self, sql, *args):
        if "FROM core.core_protection_order_registry" in str(sql):
            return list(self.registry_rows)
        return []


@pytest.mark.asyncio
async def test_fill_lot_resolves_from_protection_key_before_ledger_write():
    kernel = SafetyKernel(broker_adapter=object(), pg=None, position_ledger=None)

    lot_id = await kernel._resolve_fill_lot_id(
        resolved_order={"strategy_id": "stocks_kernel_smh_001", "symbol": "SOXL"},
        note_payload={
            "protection_key": "DUO112752|stocks_kernel_smh_001|SOXL|lot-475f3|stop_loss|SELL|58|band:16957"
        },
        extras={},
    )

    assert lot_id == "lot-475f3"


@pytest.mark.asyncio
async def test_fill_lot_resolves_from_parent_entry_note_for_oco_child():
    pg = _FakePG()
    pg.parent_note = '{"extras":{"position_lot_id":"lot-parent"}}'
    kernel = SafetyKernel(broker_adapter=object(), pg=pg, position_ledger=None)

    lot_id = await kernel._resolve_fill_lot_id(
        resolved_order={"strategy_id": "stocks_kernel_smh_001", "symbol": "SOXL"},
        note_payload={"parent_order_id": "5684d8dd-1ec8-4fb7-954b-1aa765c73690"},
        extras={},
    )

    assert lot_id == "lot-parent"


@pytest.mark.asyncio
async def test_fill_lot_resolves_unique_protection_registry_match():
    pg = _FakePG()
    pg.registry_rows = [{"lot_id": "lot-registry"}]
    kernel = SafetyKernel(broker_adapter=object(), pg=pg, position_ledger=None)

    lot_id = await kernel._resolve_fill_lot_id(
        resolved_order={
            "strategy_id": "stocks_kernel_smh_001",
            "symbol": "SOXL",
            "broker_order_id": "762",
            "ib_perm_id": 1238546101,
            "order_ref": "sl:DUO112752|stocks_kernel_smh_001|SOXL|475f3",
        },
        note_payload={},
        extras={},
    )

    assert lot_id == "lot-registry"


@pytest.mark.asyncio
async def test_fill_lot_rejects_ambiguous_protection_registry_match():
    pg = _FakePG()
    pg.registry_rows = [{"lot_id": "lot-a"}, {"lot_id": "lot-b"}]
    kernel = SafetyKernel(broker_adapter=object(), pg=pg, position_ledger=None)

    lot_id = await kernel._resolve_fill_lot_id(
        resolved_order={
            "strategy_id": "stocks_kernel_smh_001",
            "symbol": "SOXL",
            "broker_order_id": "762",
        },
        note_payload={},
        extras={},
    )

    assert lot_id is None


@pytest.mark.asyncio
async def test_option_fill_lot_uses_deterministic_owner_scoped_fallback():
    kernel = SafetyKernel(broker_adapter=object(), pg=None, position_ledger=None)

    lot_id = await kernel._resolve_fill_lot_id(
        resolved_order={
            "strategy_id": "nvda_option_churn_smoke_001",
            "symbol": "OPT:NVDA:20260508:215:P",
        },
        note_payload={},
        extras={},
    )

    assert lot_id == "lot:UNKNOWN_ACCOUNT:nvda_option_churn_smoke_001:OPT:NVDA:20260508:215:P"


@pytest.mark.asyncio
async def test_stock_fill_lot_uses_deterministic_owner_scoped_fallback():
    kernel = SafetyKernel(broker_adapter=object(), pg=None, position_ledger=None)

    lot_id = await kernel._resolve_fill_lot_id(
        resolved_order={
            "strategy_id": "qqq_intraday_001",
            "symbol": "TQQQ",
        },
        note_payload={},
        extras={},
    )

    assert lot_id == "lot:UNKNOWN_ACCOUNT:qqq_intraday_001:TQQQ"
