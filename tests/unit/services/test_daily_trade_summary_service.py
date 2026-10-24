import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.reports.daily_trade_summary_service import DailySummaryConfig, DailyTradeSummaryService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_daily_trade_summary_service_publishes_expected_metadata():
    pg = SimpleNamespace()
    pg.fetchall = AsyncMock(
        side_effect=[
            [
                {
                    "db_order_id": "o1",
                    "fill_ts": datetime(2026, 3, 3, 14, 30, tzinfo=timezone.utc),
                    "fill_qty": 10.0,
                    "fill_price": 50.0,
                    "commission_usd": 0.5,
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "side": "BUY",
                    "order_type": "LMT",
                    "decision_id": "d1",
                    "order_ref": "qqqv1:qqq_intraday_001:TQQQ:ENTRY",
                    "note": {"extras": {"trigger_id": "trg-a"}},
                    "trigger_id": "trg-a",
                    "trigger_ts": datetime(2026, 3, 3, 14, 29, 59, tzinfo=timezone.utc),
                },
                {
                    "db_order_id": "o2",
                    "fill_ts": datetime(2026, 3, 3, 15, 30, tzinfo=timezone.utc),
                    "fill_qty": 10.0,
                    "fill_price": 51.0,
                    "commission_usd": 0.5,
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "side": "SELL",
                    "order_type": "LMT",
                    "decision_id": "d2",
                    "order_ref": "qqqv1:qqq_intraday_001:TQQQ:TP",
                    "note": {"extras": {"trigger_id": "trg-b"}},
                    "trigger_id": "trg-b",
                    "trigger_ts": datetime(2026, 3, 3, 15, 29, 59, tzinfo=timezone.utc),
                },
            ],
            [
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "pos_qty": 5.0,
                    "avg_px": 52.0,
                }
            ],
        ]
    )

    events = []

    class FakeNotificationManager:
        def publish(self, event):
            events.append(event)
            return True

    svc = DailyTradeSummaryService(
        pg=pg,
        notification_manager=FakeNotificationManager(),
        cfg=DailySummaryConfig(report_time_et="16:01", include_weekends=True, max_detail_rows=10),
    )
    ok = await svc.publish_for_date(date(2026, 3, 3))

    assert ok is True
    assert len(events) == 1

    event = events[0]
    assert event.event_type == "daily_trade_summary"
    assert event.metadata["fills_count"] == 2
    assert event.metadata["strategy_count"] == 1
    assert event.metadata["net_cashflow_usd"] == 9.0
    assert event.metadata["position_market_value_usd"] == 255.0
    assert event.metadata["equity_like_delta_usd"] == 264.0
    assert event.metadata["realized_pnl_usd"] == 0.0
    assert event.metadata["unrealized_pnl_usd"] == -5.0
    assert event.metadata["total_pnl_usd"] == -5.0
    assert event.metadata["strategies"][0]["strategy_id"] == "qqq_intraday_001"
    assert event.metadata["events"][1]["action"] == "TP"


@pytest.mark.anyio
async def test_daily_trade_summary_groups_child_tp_sl_into_parent_trigger_chain():
    pg = SimpleNamespace()
    pg.fetchall = AsyncMock(
        side_effect=[
            [
                {
                    "db_order_id": "entry-1",
                    "fill_ts": datetime(2026, 4, 10, 14, 1, tzinfo=timezone.utc),
                    "fill_qty": 100.0,
                    "fill_price": 23.60,
                    "commission_usd": 0.2,
                    "strategy_id": "stocks_kernel_shadow_smh_001",
                    "symbol": "SOXS",
                    "side": "SELL",
                    "order_type": "MKT",
                    "decision_id": "entry-trg-1",
                    "order_ref": "stkv1:stocks_kernel_shadow_smh_001:SOXS:ENTRY",
                    "note": {"extras": {"trigger_id": "entry-trg-1"}},
                    "trigger_id": "entry-trg-1",
                    "parent_trigger_id": "",
                    "trigger_ts": datetime(2026, 4, 10, 14, 1, tzinfo=timezone.utc),
                    "parent_order_id": "",
                },
                {
                    "db_order_id": "tp-1",
                    "fill_ts": datetime(2026, 4, 10, 14, 11, tzinfo=timezone.utc),
                    "fill_qty": 100.0,
                    "fill_price": 23.17,
                    "commission_usd": 0.2,
                    "strategy_id": "stocks_kernel_shadow_smh_001",
                    "symbol": "SOXS",
                    "side": "BUY",
                    "order_type": "LMT",
                    "decision_id": "",
                    "order_ref": "tp:stocks_kernel_shadow_smh_001:SOXS:abc",
                    "note": {"parent_order_id": "entry-1", "type": "take_profit"},
                    "trigger_id": "",
                    "parent_trigger_id": "entry-trg-1",
                    "trigger_ts": None,
                    "parent_order_id": "entry-1",
                },
            ],
            [],
        ]
    )

    events = []

    class FakeNotificationManager:
        def publish(self, event):
            events.append(event)
            return True

    svc = DailyTradeSummaryService(
        pg=pg,
        notification_manager=FakeNotificationManager(),
        cfg=DailySummaryConfig(report_time_et="16:01", include_weekends=True, max_detail_rows=10),
    )
    ok = await svc.publish_for_date(date(2026, 4, 10))

    assert ok is True
    assert len(events) == 1
    section = events[0].metadata["strategy_sections"][0]
    assert section["trigger_count"] == 1
    assert section["open_trigger_count"] == 0
    assert section["chains"][0]["trigger_id"] == "entry-trg-1"
    assert section["chains"][0]["closed"] is True


@pytest.mark.anyio
async def test_daily_trade_summary_includes_native_trailing_protection_details():
    pg = SimpleNamespace()
    pg.fetchall = AsyncMock(
        side_effect=[
            [
                {
                    "db_order_id": "trail-1",
                    "fill_ts": datetime(2026, 4, 10, 15, 1, tzinfo=timezone.utc),
                    "fill_qty": 2.0,
                    "fill_price": 51.25,
                    "commission_usd": 0.1,
                    "strategy_id": "index_super_win_replica_001",
                    "symbol": "TQQQ",
                    "side": "SELL",
                    "order_type": "TRAIL",
                    "decision_id": "trail-d1",
                    "order_ref": "iswv2:index_super_win_replica_001:TQQQ:TRAIL:TRG:trigger-1:1",
                    "note": {
                        "order_style": {
                            "kind": "TRAIL",
                            "trail_percent": 1.5,
                            "trail_stop_px": 50.25,
                        },
                        "extras": {
                            "protection_spec_mode": "NATIVE_TRAIL",
                            "protection_position_id": "trigger-1",
                        },
                    },
                    "trigger_id": "trigger-1",
                    "parent_trigger_id": "",
                    "trigger_ts": datetime(2026, 4, 10, 15, 0, tzinfo=timezone.utc),
                    "parent_order_id": "",
                },
            ],
            [],
        ]
    )
    events = []

    class FakeNotificationManager:
        def publish(self, event):
            events.append(event)
            return True

    svc = DailyTradeSummaryService(
        pg=pg,
        notification_manager=FakeNotificationManager(),
        cfg=DailySummaryConfig(report_time_et="16:01", include_weekends=True, max_detail_rows=10),
    )

    assert await svc.publish_for_date(date(2026, 4, 10)) is True
    detail = events[0].metadata["events"][0]
    assert detail["action"] == "SL"
    assert detail["protection"]["kind"] == "TRAIL"
    assert detail["protection"]["trail_percent"] == 1.5
    assert detail["protection"]["trail_stop_px"] == 50.25
    assert detail["protection"]["protection_spec_mode"] == "NATIVE_TRAIL"
    assert detail["protection"]["protection_position_id"] == "trigger-1"


@pytest.mark.anyio
async def test_daily_trade_summary_marks_carry_close_only_chain_as_report_closed():
    pg = SimpleNamespace()
    pg.fetchall = AsyncMock(
        side_effect=[
            [
                {
                    "db_order_id": "tp-carry-1",
                    "fill_ts": datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc),
                    "fill_qty": 4.0,
                    "fill_price": 1.85,
                    "commission_usd": 0.1,
                    "strategy_id": "qqq_1dte_ma10_reject_0324_v2_001",
                    "symbol": "OPT:QQQ:20260410:609:C",
                    "side": "SELL",
                    "order_type": "LMT",
                    "decision_id": "prior-day-d1",
                    "order_ref": "tp:qqq_1dte_ma10_reject_0324_v2_001:OPT:QQQ:2",
                    "note": {"type": "take_profit", "parent_order_id": "parent-1"},
                    "trigger_id": "",
                    "parent_trigger_id": "prior-day-trigger",
                    "trigger_ts": None,
                    "parent_order_id": "parent-1",
                },
            ],
            [],
        ]
    )

    events = []

    class FakeNotificationManager:
        def publish(self, event):
            events.append(event)
            return True

    svc = DailyTradeSummaryService(
        pg=pg,
        notification_manager=FakeNotificationManager(),
        cfg=DailySummaryConfig(report_time_et="16:01", include_weekends=True, max_detail_rows=10),
    )
    ok = await svc.publish_for_date(date(2026, 4, 10))

    assert ok is True
    assert len(events) == 1
    section = events[0].metadata["strategy_sections"][0]
    assert section["trigger_count"] == 1
    assert section["open_trigger_count"] == 0
    chain = section["chains"][0]
    assert chain["closed"] is False
    assert chain["carry_close_only"] is True
    assert chain["report_closed"] is True
