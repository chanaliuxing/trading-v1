from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from services.production.broker_truth_snapshot_service import (
    build_live_broker_truth_snapshot,
    build_live_broker_truth_snapshot_from_manager,
    query_broker_truth_snapshot,
    query_broker_truth_snapshot_from_broker,
)


class _FakePg:
    def __init__(self, rows_by_marker: dict[str, list[dict]]):
        self.rows_by_marker = rows_by_marker
        self.calls = []

    async def fetchall(self, sql, *args):
        sql_s = str(sql)
        self.calls.append((sql_s, args))
        for marker, rows in self.rows_by_marker.items():
            if marker in sql_s:
                return list(rows)
        return []


async def test_broker_truth_snapshot_go_when_projection_clean():
    status = await query_broker_truth_snapshot(
        pg=_FakePg({}),
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        strategy_ids=["qqq_intraday_001"],
        symbols=["TQQQ"],
        live_broker_snapshot={
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "stale": False,
            "lookup_failed": False,
            "go_no_go": "go",
            "source_process_pid": 123,
            "account": "DU123",
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "generated_at": "2026-05-05T10:00:00-04:00",
            "positions": [],
            "open_orders": [],
        },
    )

    assert status["go_no_go"] == "go"
    assert status["broker_truth_projection_clean"] is True
    assert status["projection_divergence_count"] == 0
    assert status["order_truth_active_quarantine_count"] == 0


async def test_broker_truth_snapshot_blocks_quarantine_and_projection_divergence():
    status = await query_broker_truth_snapshot(
        pg=_FakePg(
            {
                "FROM core.core_order_quarantine": [
                    {"symbol": "TQQQ", "reason": "ambiguous_broker_fill", "count": 1}
                ],
                "FULL OUTER JOIN core_net": [
                    {
                        "strategy_id": "qqq_intraday_001",
                        "symbol": "TQQQ",
                        "ledger_qty": 10.0,
                        "core_qty": 0.0,
                        "qty_delta": 10.0,
                    }
                ],
            }
        ),
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        strategy_ids=["qqq_intraday_001"],
        symbols=["TQQQ"],
        live_broker_snapshot={
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "stale": False,
            "lookup_failed": False,
            "go_no_go": "go",
            "source_process_pid": 123,
            "account": "DU123",
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "generated_at": "2026-05-05T10:00:00-04:00",
            "positions": [],
            "open_orders": [],
        },
    )

    assert status["go_no_go"] == "no_go"
    assert status["order_truth_active_quarantine_count"] == 1
    assert status["projection_divergence_count"] == 1
    assert status["blockers"] == [
        "broker_truth_active_order_quarantine",
        "broker_truth_position_projection_divergence",
    ]


async def test_broker_truth_snapshot_blocks_nonzero_null_lot_ledger_position():
    status = await query_broker_truth_snapshot(
        pg=_FakePg(
            {
                "SELECT account_id, strategy_id, symbol, lot_id": [
                    {
                        "account_id": "DU123",
                        "strategy_id": "stocks_kernel_smh_001",
                        "symbol": "SOXL",
                        "lot_id": None,
                        "qty": -59.0,
                        "notional_sum": 9942.09,
                    }
                ],
            }
        ),
        generated_at=datetime.fromisoformat("2026-05-08T10:00:00-04:00"),
        strategy_ids=["stocks_kernel_smh_001"],
        symbols=["SOXL"],
        live_broker_snapshot={
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "stale": False,
            "lookup_failed": False,
            "go_no_go": "go",
            "source_process_pid": 123,
            "account": "DU123",
            "broker_position_count": 1,
            "broker_open_order_count": 0,
            "generated_at": "2026-05-08T10:00:00-04:00",
            "positions": [{"account": "DU123", "symbol": "SOXL", "qty": -59.0}],
            "open_orders": [],
        },
    )

    assert status["go_no_go"] == "no_go"
    assert status["broker_truth_projection_clean"] is False
    assert status["dirty_lot_position_count"] == 1
    assert status["dirty_lot_positions"][0]["symbol"] == "SOXL"
    assert "broker_truth_dirty_ledger_lot_identity" in status["blockers"]


async def test_broker_truth_snapshot_blocks_db_projection_only():
    status = await query_broker_truth_snapshot(
        pg=_FakePg({}),
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        strategy_ids=["qqq_intraday_001"],
        symbols=["TQQQ"],
    )

    assert status["go_no_go"] == "no_go"
    assert status["truth_source"] == "db_projection_only"
    assert "broker_truth_live_snapshot_missing" in status["blockers"]


async def test_broker_truth_db_projection_only_is_no_go():
    status = await query_broker_truth_snapshot(
        pg=_FakePg({}),
        generated_at=datetime.fromisoformat("2026-05-11T10:00:00-04:00"),
        strategy_ids=["stocks_kernel_smh_001"],
        symbols=["SMH"],
        require_live_broker_truth=True,
    )

    assert status["go_no_go"] == "no_go"
    assert status["broker_truth_backed"] is False
    assert status["truth_source"] == "db_projection_only"


async def test_broker_truth_live_snapshot_sets_broker_truth_backed_true():
    status = await query_broker_truth_snapshot(
        pg=_FakePg({}),
        generated_at=datetime.fromisoformat("2026-05-11T10:00:00-04:00"),
        strategy_ids=["stocks_kernel_smh_001"],
        symbols=["SMH"],
        live_broker_snapshot={
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "stale": False,
            "lookup_failed": False,
            "go_no_go": "go",
            "source_process_pid": 123,
            "account": "DU123",
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "generated_at": "2026-05-11T10:00:00-04:00",
            "positions": [],
            "open_orders": [],
        },
    )

    assert status["go_no_go"] == "go"
    assert status["broker_truth_backed"] is True
    assert status["truth_source"] == "live_broker_snapshot_plus_db_projection"


async def test_broker_truth_lookup_failed_blocks_go():
    status = await query_broker_truth_snapshot(
        pg=_FakePg({}),
        generated_at=datetime.fromisoformat("2026-05-11T10:00:00-04:00"),
        strategy_ids=["stocks_kernel_smh_001"],
        symbols=["SMH"],
        live_broker_snapshot={
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "stale": False,
            "lookup_failed": True,
            "go_no_go": "no_go",
            "source_process_pid": 123,
            "account": "DU123",
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "generated_at": "2026-05-11T10:00:00-04:00",
            "positions": [],
            "open_orders": [],
        },
    )

    assert status["go_no_go"] == "no_go"
    assert status["broker_truth_backed"] is False
    assert "broker_truth_live_lookup_failed" in status["blockers"]


async def test_broker_truth_stale_snapshot_blocks_go():
    status = await query_broker_truth_snapshot(
        pg=_FakePg({}),
        generated_at=datetime.fromisoformat("2026-05-11T10:00:00-04:00"),
        strategy_ids=["stocks_kernel_smh_001"],
        symbols=["SMH"],
        live_broker_snapshot={
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "stale": True,
            "lookup_failed": False,
            "go_no_go": "go",
            "source_process_pid": 123,
            "account": "DU123",
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "generated_at": "2026-05-11T09:55:00-04:00",
            "positions": [],
            "open_orders": [],
        },
    )

    assert status["go_no_go"] == "no_go"
    assert status["broker_truth_backed"] is False
    assert "stale" in status["live_broker_snapshot_validation_errors"]


async def test_broker_truth_snapshot_rejects_position_safety_wrapper_as_live_truth():
    status = await query_broker_truth_snapshot(
        pg=_FakePg({}),
        generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
        strategy_ids=["qqq_intraday_001"],
        symbols=["TQQQ"],
        live_broker_snapshot={
            "artifact_type": "position_safety",
            "artifact_present": True,
            "stale": False,
            "lookup_failed": False,
            "broker_position_count": 0,
            "generated_at": "2026-05-05T10:00:00-04:00",
        },
    )

    assert status["go_no_go"] == "no_go"
    assert status["broker_truth_backed"] is False
    assert "invalid_artifact_type" in status["live_broker_snapshot_validation_errors"]
    assert "broker_truth_live_snapshot_missing" in status["blockers"]


async def test_broker_truth_snapshot_rejects_empty_or_unknown_account_live_truth():
    for account in ("", "UNKNOWN_ACCOUNT"):
        status = await query_broker_truth_snapshot(
            pg=_FakePg({}),
            generated_at=datetime.fromisoformat("2026-05-05T10:00:00-04:00"),
            strategy_ids=["qqq_intraday_001"],
            symbols=["TQQQ"],
            live_broker_snapshot={
                "artifact_type": "live_broker_truth_snapshot",
                "artifact_present": True,
                "stale": False,
                "lookup_failed": False,
                "go_no_go": "go",
                "source_process_pid": 123,
                "account": account,
                "broker_position_count": 0,
                "broker_open_order_count": 0,
                "generated_at": "2026-05-05T10:00:00-04:00",
                "positions": [],
                "open_orders": [],
            },
        )

        assert status["go_no_go"] == "no_go"
        assert status["broker_truth_backed"] is False
        assert "invalid_account" in status["live_broker_snapshot_validation_errors"]


async def test_live_broker_truth_snapshot_reads_adapter_and_filters_account():
    generated_at = datetime.fromisoformat("2026-05-05T10:00:00-04:00")
    adapter = SimpleNamespace(
        account="DU123",
        positions=lambda: [
            SimpleNamespace(
                account="DU123",
                contract=SimpleNamespace(secType="STK", symbol="TQQQ"),
                position=10,
                avgCost=50.0,
            ),
            SimpleNamespace(
                account="OTHER",
                contract=SimpleNamespace(secType="STK", symbol="NVDA"),
                position=5,
                avgCost=100.0,
            ),
        ],
        open_orders=lambda: [
            {
                "account": "DU123",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
                "broker_order_id": "1001",
                "perm_id": 2001,
                "order_ref": "close:s1:TQQQ",
                "status": "Submitted",
            },
            {
                "account": "DU123",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
                "broker_order_id": "1002",
                "status": "Filled",
            },
        ],
    )

    status = await build_live_broker_truth_snapshot(ib_adapter=adapter, generated_at=generated_at)

    assert status["go_no_go"] == "go"
    assert status["lookup_failed"] is False
    assert status["account"] == "DU123"
    assert status["broker_position_count"] == 1
    assert status["positions"][0]["symbol"] == "TQQQ"
    assert status["broker_open_order_count"] == 1
    assert status["open_orders"][0]["broker_order_id"] == "1001"


async def test_live_broker_truth_snapshot_no_go_when_adapter_unavailable():
    generated_at = datetime.fromisoformat("2026-05-05T10:00:00-04:00")

    status = await build_live_broker_truth_snapshot(ib_adapter=object(), generated_at=generated_at)

    assert status["go_no_go"] == "no_go"
    assert status["lookup_failed"] is True
    assert "positions" in status["error"]


async def test_live_broker_truth_snapshot_no_go_when_account_scope_missing():
    generated_at = datetime.fromisoformat("2026-05-05T10:00:00-04:00")
    adapter = SimpleNamespace(
        account="",
        positions=lambda: [],
        open_orders=lambda: [],
    )

    status = await build_live_broker_truth_snapshot(ib_adapter=adapter, generated_at=generated_at)

    assert status["go_no_go"] == "no_go"
    assert status["account_invalid"] is True
    assert status["error"] == "broker_account_scope_invalid"


async def test_broker_truth_snapshot_from_broker_uses_live_adapter_truth():
    generated_at = datetime.fromisoformat("2026-05-05T10:00:00-04:00")
    adapter = SimpleNamespace(
        account="DU123",
        positions=lambda: [],
        open_orders=lambda: [],
    )

    status = await query_broker_truth_snapshot_from_broker(
        pg=_FakePg({}),
        ib_adapter=adapter,
        generated_at=generated_at,
        strategy_ids=["qqq_intraday_001"],
        symbols=["TQQQ"],
    )

    assert status["go_no_go"] == "go"
    assert status["truth_source"] == "live_broker_snapshot_plus_db_projection"
    assert status["broker_truth_backed"] is True


async def test_broker_truth_uses_manager_owned_handle_not_adapter_connect():
    class _FakeIB:
        def positions(self):
            return [
                SimpleNamespace(
                    account="DU123",
                    contract=SimpleNamespace(secType="STK", symbol="SMH"),
                    position=3,
                    avgCost=100.0,
                )
            ]

        async def reqOpenOrdersAsync(self):
            return None

        def openTrades(self):
            return []

    class _FakeManager:
        def __init__(self):
            self.called = False

        async def get_exec_ib_handle(self, **kwargs):
            self.called = True
            return SimpleNamespace(role="exec", client_id=17, epoch=4, ib=_FakeIB())

    manager = _FakeManager()
    status = await build_live_broker_truth_snapshot_from_manager(
        generated_at=datetime.fromisoformat("2026-05-11T10:00:00-04:00"),
        account="DU123",
        manager=manager,
    )

    assert manager.called is True
    assert status["lookup_failed"] is False
    assert status["source"] == "live_broker"
    assert status["ib_client_id"] == 17
    assert status["ib_connection_epoch"] == 4
    assert status["broker_position_count"] == 1
