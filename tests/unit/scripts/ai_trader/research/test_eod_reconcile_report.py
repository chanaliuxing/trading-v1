from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts.ai_trader.research.eod_reconcile_report import (
    _adhoc_ib_connect_allowed,
    _load_system_payload_from_db_async,
    _load_rows,
    build_reconciliation_report,
    infer_report_mode,
    load_enabled_strategy_ids_from_configs,
    load_ib_flex_snapshot,
    load_ib_live_snapshot,
    render_markdown,
)


def test_eod_reconcile_detects_presubmitted_and_non_flat_positions() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {"strategy_id": "strat_a", "broker_order_id": "1001", "symbol": "QQQ", "side": "BUY", "qty": 1, "status": "FILLED"},
            {"strategy_id": "strat_a", "broker_order_id": "1002", "symbol": "QQQ", "side": "SELL", "qty": 1, "status": "CANCELED"},
        ],
        ib_orders=[
            {"broker_order_id": "1001", "symbol": "QQQ", "side": "BUY", "qty": 1, "status": "Filled"},
            {"broker_order_id": "1002", "symbol": "QQQ", "side": "SELL", "qty": 1, "status": "PreSubmitted"},
            {"broker_order_id": "9999", "symbol": "SPY", "side": "BUY", "qty": 1, "status": "Submitted"},
        ],
        telegram_audit=[
            {
                "outcome": "sent",
                "push_ts": "2026-04-10T20:01:00+00:00",
                "broker_order_id": "1001",
                "strategy_id": "strat_a",
                "symbol": "QQQ",
                "side": "BUY",
                "qty": 1,
            }
        ],
        system_positions=[],
        ib_positions=[{"symbol": "QQQ", "qty": 1}],
        system_account={"day_pnl": 100.0, "realized_pnl": 90.0, "unrealized_pnl": 10.0, "equity": 100_100.0},
        ib_account={"day_pnl": 90.0, "realized_pnl": 80.0, "unrealized_pnl": 10.0, "equity": 100_090.0},
        pnl_tolerance=1.0,
    )

    summary = report["summary"]
    assert summary["ibkr_presubmitted_count"] == 1
    assert summary["ibkr_non_flat_positions"] == 1
    assert summary["critical_issues"] >= 1
    assert summary["high_issues"] >= 1

    categories = {x["category"] for x in report["issues"]}
    assert "ibkr_presubmitted_open" in categories
    assert "ibkr_not_flattened" in categories
    assert "missing_telegram_push" in categories

    md = render_markdown(report)
    assert "高光问题" in md
    assert "PreSubmitted" in md


def test_eod_reconcile_no_major_issue_when_three_sides_match() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {"strategy_id": "strat_ok", "broker_order_id": "2001", "symbol": "SPY", "side": "BUY", "qty": 2, "status": "FILLED"},
        ],
        ib_orders=[
            {"broker_order_id": "2001", "symbol": "SPY", "side": "BUY", "qty": 2, "status": "filled"},
        ],
        telegram_audit=[
            {
                "outcome": "sent",
                "push_ts": "2026-04-10T20:01:00+00:00",
                "broker_order_id": "2001",
                "strategy_id": "strat_ok",
                "symbol": "SPY",
                "side": "BUY",
                "qty": 2,
            }
        ],
        system_positions=[],
        ib_positions=[],
        system_account={"day_pnl": 10.0, "realized_pnl": 10.0, "unrealized_pnl": 0.0, "equity": 100_010.0},
        ib_account={"day_pnl": 10.0, "realized_pnl": 10.0, "unrealized_pnl": 0.0, "equity": 100_010.0},
        pnl_tolerance=1.0,
    )

    summary = report["summary"]
    assert summary["critical_issues"] == 0
    assert summary["high_issues"] == 0
    assert summary["ibkr_presubmitted_count"] == 0
    assert summary["ibkr_non_flat_positions"] == 0


def test_enabled_strategies_from_config_affect_scope(tmp_path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "\n".join(
            [
                "strategies:",
                "  - id: strat_on",
                "    enabled: true",
                "  - id: strat_off",
                "    enabled: false",
            ]
        ),
        encoding="utf-8",
    )
    enabled = load_enabled_strategy_ids_from_configs([str(cfg)])
    assert enabled == ["strat_on"]

    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {"strategy_id": "strat_off", "broker_order_id": "3001", "symbol": "SPY", "side": "BUY", "qty": 1, "status": "FILLED"},
        ],
        ib_orders=[
            {"broker_order_id": "3001", "symbol": "SPY", "side": "BUY", "qty": 1, "status": "filled"},
        ],
        telegram_audit=[
            {
                "outcome": "sent",
                "push_ts": "2026-04-10T20:01:00+00:00",
                "broker_order_id": "3001",
                "strategy_id": "strat_off",
                "symbol": "SPY",
                "side": "BUY",
                "qty": 1,
            }
        ],
        system_positions=[],
        ib_positions=[],
        system_account={"day_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "equity": 100_000.0},
        ib_account={"day_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "equity": 100_000.0},
        enabled_strategies=enabled,
    )

    categories = {x["category"] for x in report["issues"]}
    assert "order_from_non_enabled_strategy" in categories
    assert "enabled_strategy_no_orders" in categories
    assert report["summary"]["enabled_strategy_count"] == 1
    assert report["summary"]["enabled_strategy_no_orders_count"] == 1
    assert "strat_on" in report["enabled_strategy_no_orders"]


def test_rejected_without_broker_id_should_not_raise_missing_in_ibkr() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {
                "strategy_id": "strat_x",
                "client_order_id": "strat_x-cli-001",
                "symbol": "SPY",
                "side": "BUY",
                "qty": 1,
                "status": "REJECTED",
            }
        ],
        ib_orders=[],
        telegram_audit=[],
        system_positions=[],
        ib_positions=[],
        system_account={"equity": 100000},
        ib_account={"equity": 100000},
    )
    categories = {x["category"] for x in report["issues"]}
    assert "missing_in_ibkr" not in categories


def test_load_rows_reads_nested_account_object(tmp_path) -> None:
    payload_path = tmp_path / "broker_session_state.json"
    payload_path.write_text(
        """{
  "session_id": "s1",
  "account": {"equity": 100000.0, "day_pnl": 10.0},
  "orders": []
}""",
        encoding="utf-8",
    )
    rows = _load_rows(str(payload_path), fallback_keys=("account", "items", "rows", "data"))
    assert len(rows) == 1
    assert float(rows[0].get("equity", 0.0)) == 100000.0


def test_research_mode_suppresses_strategy_enablement_warnings() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {"strategy_id": "exp-a", "client_order_id": "exp-a-001", "symbol": "SPY", "side": "BUY", "qty": 1, "status": "REJECTED"},
        ],
        ib_orders=[],
        telegram_audit=[],
        system_positions=[],
        ib_positions=[],
        system_account={"equity": 100000},
        ib_account={"equity": 100000},
        enabled_strategies=["prod-1", "prod-2"],
        report_mode="research",
    )
    categories = {x["category"] for x in report["issues"]}
    assert "order_from_non_enabled_strategy" not in categories
    assert "enabled_strategy_no_orders" not in categories
    assert report["summary"]["enabled_strategy_no_orders_count"] == 0
    assert report["report_mode"] == "research"


def test_infer_report_mode_detects_research_paths() -> None:
    mode = infer_report_mode(
        "./runs/ai_trader/research/cycles/abc/orders_state.json",
        "",
    )
    assert mode == "research"


def test_runtime_reconciliation_alerts_are_reflected_in_report() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[],
        ib_orders=[],
        telegram_audit=[
            {
                "ts": "2026-04-10T13:30:37.577460+00:00",
                "event_type": "reconciliation_alert",
                "strategy_id": "system_reconciler",
                "metadata": {
                    "db_open_orders": 1,
                    "ib_open_orders": 0,
                    "ib_pre_submitted_orders": 0,
                    "ghosts_found": 1,
                    "mismatches": 0,
                    "evidence": {
                        "ghost_samples": [
                            {
                                "order_id": "oid-1",
                                "strategy_id": "qqq_intraday_001",
                                "symbol": "SQQQ",
                            }
                        ]
                    },
                },
            }
        ],
        system_positions=[],
        ib_positions=[],
        system_account={"equity": 100000},
        ib_account={"equity": 100000},
        report_mode="live",
    )
    categories = {x["category"] for x in report["issues"]}
    assert "runtime_ghost_orders" in categories
    assert report["summary"]["runtime_recon_alert_count"] == 1
    assert report["summary"]["runtime_max_ghosts_found"] == 1


def test_no_enabled_strategy_no_orders_when_no_system_orders() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[],
        ib_orders=[],
        telegram_audit=[],
        system_positions=[],
        ib_positions=[],
        system_account={},
        ib_account={},
        enabled_strategies=["s1", "s2"],
        report_mode="live",
    )
    categories = {x["category"] for x in report["issues"]}
    assert "enabled_strategy_no_orders" not in categories
    assert report["summary"]["enabled_strategy_no_orders_count"] == 0


def test_adhoc_ib_connect_allowed_default_and_disable_flag(monkeypatch) -> None:
    monkeypatch.delenv("IBKR_DISABLE_ADHOC_CONNECT", raising=False)
    assert _adhoc_ib_connect_allowed() is True
    monkeypatch.setenv("IBKR_DISABLE_ADHOC_CONNECT", "true")
    assert _adhoc_ib_connect_allowed() is False


@pytest.mark.asyncio
async def test_load_system_payload_account_table_is_scoped_by_trade_date(monkeypatch) -> None:
    class _FakeConn:
        async def fetch(self, _query, *_args):
            return []

        async def fetchval(self, query, *args):
            q = str(query)
            if "information_schema.tables" in q:
                return True
            if "information_schema.columns" in q:
                return str(args[2]) == "ts"
            return None

        async def fetchrow(self, query, *args):
            q = str(query)
            if "FROM core.core_account_snapshot" in q:
                assert "WHERE ts >= $1 AND ts < $2" in q
                return {"equity": 100001.0, "day_pnl": 12.0, "ts": args[0]}
            if "core_position_aggregate_window" in q:
                return {
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "ts": None,
                    "row_count": 0,
                    "account_source": "core_position_aggregate_window",
                }
            return None

        async def close(self):
            return None

    async def _fake_connect(_dsn):
        return _FakeConn()

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=_fake_connect))
    payload = await _load_system_payload_from_db_async(
        trade_date="2026-04-10",
        dsn="postgresql://fake",
        account_table="core.core_account_snapshot",
    )
    assert len(payload["account"]) == 1
    assert float(payload["account"][0].get("equity", 0.0)) == 100001.0


@pytest.mark.asyncio
async def test_load_system_payload_no_historical_fallback_when_no_day_rows(monkeypatch) -> None:
    class _FakeConn:
        async def fetch(self, _query, *_args):
            return []

        async def fetchval(self, query, *args):
            q = str(query)
            if "information_schema.tables" in q:
                return True
            if "information_schema.columns" in q:
                return str(args[2]) == "ts"
            return None

        async def fetchrow(self, query, *_args):
            q = str(query)
            if "FROM core.core_account_snapshot" in q:
                return None
            if "core_position_aggregate_window" in q:
                return {
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "ts": None,
                    "row_count": 0,
                    "account_source": "core_position_aggregate_window",
                }
            return None

        async def close(self):
            return None

    async def _fake_connect(_dsn):
        return _FakeConn()

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=_fake_connect))
    payload = await _load_system_payload_from_db_async(
        trade_date="2026-04-10",
        dsn="postgresql://fake",
        account_table="core.core_account_snapshot",
    )
    assert payload["account"] == []


def test_skip_broker_consistency_when_ib_orders_not_provided() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {
                "strategy_id": "s1",
                "broker_order_id": "101",
                "symbol": "SPY",
                "side": "BUY",
                "qty": 1,
                "status": "FILLED",
            }
        ],
        ib_orders=[],
        telegram_audit=[],
        system_positions=[],
        ib_positions=[],
        system_account={},
        ib_account={},
        report_mode="live",
        enable_broker_order_consistency=False,
    )
    categories = {x["category"] for x in report["issues"]}
    assert "missing_in_ibkr" not in categories
    assert report["summary"]["broker_order_consistency_enabled"] is False
    assert "missing_telegram_push" in categories
    assert "ibkr_input_missing" in categories


def test_no_ibkr_input_missing_when_no_system_orders() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[],
        ib_orders=[],
        telegram_audit=[],
        system_positions=[],
        ib_positions=[],
        system_account={},
        ib_account={},
        report_mode="live",
        enable_broker_order_consistency=False,
    )
    categories = {x["category"] for x in report["issues"]}
    assert "ibkr_input_missing" not in categories


def test_telegram_partial_fills_should_not_trigger_qty_mismatch() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {
                "strategy_id": "s1",
                "broker_order_id": "5001",
                "symbol": "SOXS",
                "side": "BUY",
                "qty": 424,
                "status": "FILLED",
            }
        ],
        ib_orders=[
            {
                "broker_order_id": "5001",
                "symbol": "SOXS",
                "side": "BUY",
                "qty": 424,
                "status": "Filled",
            }
        ],
        telegram_audit=[
            {
                "outcome": "sent",
                "event_type": "order_fill",
                "push_ts": "2026-04-10T20:01:00+00:00",
                "broker_order_id": "5001",
                "strategy_id": "s1",
                "symbol": "SOXS",
                "side": "BUY",
                "qty": 100,
            },
            {
                "outcome": "sent",
                "event_type": "order_fill",
                "push_ts": "2026-04-10T20:01:01+00:00",
                "broker_order_id": "5001",
                "strategy_id": "s1",
                "symbol": "SOXS",
                "side": "BUY",
                "qty": 324,
            },
        ],
        system_positions=[],
        ib_positions=[],
        system_account={},
        ib_account={},
    )
    categories = {x["category"] for x in report["issues"]}
    assert "telegram_qty_mismatch" not in categories


def test_runtime_ghost_order_resolved_is_downgraded_to_high() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {
                "strategy_id": "s1",
                "client_order_id": "oid-1",
                "symbol": "SQQQ",
                "side": "BUY",
                "qty": 100,
                "status": "CANCELED",
            }
        ],
        ib_orders=[],
        telegram_audit=[
            {
                "ts": "2026-04-10T13:30:37.577460+00:00",
                "event_type": "reconciliation_alert",
                "strategy_id": "system_reconciler",
                "metadata": {
                    "db_open_orders": 1,
                    "ib_open_orders": 0,
                    "ghosts_found": 1,
                    "evidence": {
                        "ghost_samples": [
                            {
                                "order_id": "oid-1",
                                "strategy_id": "s1",
                                "symbol": "SQQQ",
                            }
                        ]
                    },
                },
            }
        ],
        system_positions=[],
        ib_positions=[],
        system_account={},
        ib_account={},
        report_mode="live",
        enable_broker_order_consistency=False,
    )
    issues = report["issues"]
    cats = {x["category"] for x in issues}
    assert "runtime_ghost_orders_resolved" in cats
    resolved = next(x for x in issues if x["category"] == "runtime_ghost_orders_resolved")
    assert resolved["severity"] == "HIGH"


def test_telegram_order_ref_fallback_avoids_false_missing_push() -> None:
    report = build_reconciliation_report(
        trade_date="2026-04-10",
        system_orders=[
            {
                "strategy_id": "s1",
                "broker_order_id": "6001",
                "perm_id": "7001",
                "order_ref": "qqqv1:s1:TQQQ:STOP:TRG:abc12345",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
                "status": "CANCELED",
            }
        ],
        ib_orders=[],
        telegram_audit=[
            {
                "outcome": "sent",
                "event_type": "order_submitted",
                "push_ts": "2026-04-10T20:01:00+00:00",
                "broker_order_id": None,
                "perm_id": None,
                "order_ref": "qqqv1:s1:TQQQ:STOP:TRG:abc12345",
                "strategy_id": "s1",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
            }
        ],
        system_positions=[],
        ib_positions=[],
        system_account={},
        ib_account={},
        report_mode="live",
        enable_broker_order_consistency=False,
    )
    categories = {x["category"] for x in report["issues"]}
    assert "missing_telegram_push" not in categories


def test_load_ib_live_snapshot_merges_open_execution_and_completed_orders(monkeypatch) -> None:
    class FakeIB:
        def __init__(self):
            self.connected = False

        async def connectAsync(self, host, port, clientId, timeout=10.0, readonly=True):
            self.connected = True

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

        async def reqAllOpenOrdersAsync(self):
            return None

        def openTrades(self):
            return [
                SimpleNamespace(
                    order=SimpleNamespace(
                        orderId=9101,
                        permId=19101,
                        orderRef="entry:s1:QQQ:abc",
                        clientId=7,
                        action="BUY",
                        totalQuantity=3,
                        orderType="LMT",
                    ),
                    contract=SimpleNamespace(symbol="QQQ"),
                    orderStatus=SimpleNamespace(status="Submitted"),
                )
            ]

        def positions(self):
            return []

        async def accountSummaryAsync(self, _account):
            return []

        async def reqExecutionsAsync(self, _filter):
            return [
                SimpleNamespace(
                    execution=SimpleNamespace(
                        orderId=9102,
                        permId=19102,
                        side="BUY",
                        shares=3,
                        time="20260413 10:15:00",
                    ),
                    contract=SimpleNamespace(symbol="QQQ"),
                )
            ]

        async def reqCompletedOrdersAsync(self, apiOnly=False):
            return [
                SimpleNamespace(
                    order=SimpleNamespace(
                        orderId=9103,
                        permId=19103,
                        orderRef="tp:s1:QQQ:abc",
                        clientId=7,
                        action="SELL",
                        totalQuantity=3,
                        orderType="LMT",
                    ),
                    contract=SimpleNamespace(symbol="QQQ"),
                    orderStatus=SimpleNamespace(status="Cancelled", completedTime="20260413 15:59:00"),
                )
            ]

    fake_module = ModuleType("ib_insync")
    fake_module.IB = FakeIB
    fake_module.ExecutionFilter = lambda: SimpleNamespace(time="")
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)

    payload = load_ib_live_snapshot(
        host="127.0.0.1",
        port=4002,
        client_id=2991,
        trade_date="2026-04-13",
        include_completed_orders=True,
    )

    rows = payload["orders"]
    broker_ids = {str(row.get("broker_order_id") or row.get("order_id") or "") for row in rows}
    assert {"9101", "9102", "9103"} <= broker_ids
    completed = next(row for row in rows if str(row.get("broker_order_id") or "") == "9103")
    assert completed["status"] == "Cancelled"


def test_load_ib_live_snapshot_default_excludes_completed_orders(monkeypatch) -> None:
    class FakeIB:
        def __init__(self):
            self.connected = False

        async def connectAsync(self, host, port, clientId, timeout=10.0, readonly=True):
            self.connected = True

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

        async def reqAllOpenOrdersAsync(self):
            return None

        def openTrades(self):
            return []

        def positions(self):
            return []

        async def accountSummaryAsync(self, _account):
            return []

        async def reqExecutionsAsync(self, _filter):
            return []

        async def reqCompletedOrdersAsync(self, apiOnly=False):
            raise AssertionError("reqCompletedOrdersAsync should not be called by default")

    fake_module = ModuleType("ib_insync")
    fake_module.IB = FakeIB
    fake_module.ExecutionFilter = lambda: SimpleNamespace(time="")
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)

    payload = load_ib_live_snapshot(
        host="127.0.0.1",
        port=4002,
        client_id=2991,
        trade_date="2026-04-13",
    )
    assert payload["orders"] == []


def test_load_ib_flex_snapshot_extracts_trade_rows(monkeypatch, tmp_path) -> None:
    class FakeFlexReport:
        def __init__(self, token=None, queryId=None, path=None):
            self.token = token
            self.queryId = queryId
            self.path = path

        def save(self, path):
            Path(path).write_text("<xml />", encoding="utf-8")

        def topics(self):
            return {"Trade", "OpenPosition", "EquitySummaryByReportDateInBase"}

        def extract(self, topic):
            if topic == "Trade":
                return [
                    SimpleNamespace(
                        tradeDate="2026-04-13",
                        ibOrderID="3001",
                        orderReference="entry:s1:SPY:abc",
                        symbol="SPY",
                        buySell="BUY",
                        quantity=2,
                        orderType="MKT",
                        tradePrice=501.25,
                    ),
                    SimpleNamespace(
                        tradeDate="2026-04-12",
                        ibOrderID="skip-me",
                        orderReference="entry:s1:QQQ:old",
                        symbol="QQQ",
                        buySell="BUY",
                        quantity=1,
                        orderType="MKT",
                        tradePrice=400.0,
                    ),
                ]
            if topic == "OpenPosition":
                return [SimpleNamespace(symbol="SPY", position=2, costBasisPrice=501.25)]
            if topic == "EquitySummaryByReportDateInBase":
                return [SimpleNamespace(netLiquidationValue=100500.0, endingSettledValue=5000.0, changeInNAV=120.5)]
            return []

    fake_module = ModuleType("ib_insync")
    fake_module.FlexReport = FakeFlexReport
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)

    payload = load_ib_flex_snapshot(
        trade_date="2026-04-13",
        token="tok",
        query_id="qid",
        save_xml_path=str(tmp_path / "statement.xml"),
    )

    assert len(payload["orders"]) == 1
    assert payload["orders"][0]["broker_order_id"] == "3001"
    assert payload["orders"][0]["status"] == "Filled"
    assert payload["positions"][0]["symbol"] == "SPY"
    assert payload["account"][0]["equity"] == 100500.0
