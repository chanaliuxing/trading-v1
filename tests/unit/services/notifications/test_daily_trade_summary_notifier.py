import sys
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[5]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.notifications import NotificationEvent, TelegramNotifier


def test_daily_trade_summary_message_contains_required_fields():
    notifier = TelegramNotifier(bot_token="t", chat_id="c")
    event = NotificationEvent(
        event_type="daily_trade_summary",
        status="SUMMARY",
        strategy_id="qqq_intraday_001",
        symbol="ALL",
        side="N/A",
        qty=0.0,
        order_type="N/A",
        decision_id="daily_trade_summary:2026-03-03",
        ts=datetime(2026, 3, 3, 16, 1, tzinfo=ZoneInfo("America/New_York")),
        metadata={
            "report_date": date(2026, 3, 3).isoformat(),
            "window_start_et": "2026-03-03T00:00:00-05:00",
            "window_end_et": "2026-03-04T00:00:00-05:00",
            "fills_count": 2,
            "strategy_count": 1,
            "gross_buy_usd": 1000.0,
            "gross_sell_usd": 1020.0,
            "commission_usd": 1.0,
            "net_cashflow_usd": 19.0,
            "strategy_sections": [
                {
                    "strategy_id": "qqq_intraday_001",
                    "fills_count": 2,
                    "trigger_count": 1,
                    "open_trigger_count": 0,
                    "gross_buy_usd": 1000.0,
                    "gross_sell_usd": 1020.0,
                    "commission_usd": 1.0,
                    "net_cashflow_usd": 19.0,
                    "realized_pnl_usd": 19.0,
                    "unrealized_pnl_usd": 0.0,
                    "total_pnl_usd": 19.0,
                    "chains": [
                        {
                            "trigger_id": "trg-1",
                            "first_trigger_ts_et": "10:00:59",
                            "closed": True,
                            "open_qty": 0.0,
                            "realized_pnl_usd": 19.0,
                            "net_cashflow_usd": 19.0,
                            "events": [
                                {
                                    "fill_ts_et": "10:01:00",
                                    "symbol": "TQQQ",
                                    "side": "SELL",
                                    "qty": 10,
                                    "price": 51.0,
                                    "action": "TP",
                                }
                            ],
                        }
                    ],
                }
            ],
            "realized_pnl_usd": 19.0,
            "unrealized_pnl_usd": 0.0,
            "total_pnl_usd": 19.0,
            "position_market_value_usd": 0.0,
            "position_notional_abs_usd": 0.0,
            "events": [
                {
                    "fill_ts_et": "10:01:00",
                    "trigger_ts_et": "10:00:59",
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "side": "SELL",
                    "qty": 10,
                    "price": 51.0,
                    "action": "TP",
                    "trigger_id": "trg-1",
                }
            ],
            "truncated_details": 0,
        },
    )

    msg = notifier._format_text(event)
    assert "交易财务日报" in msg
    assert "交易日(ET): 2026-03-03" in msg
    assert "当日总损益" in msg
    assert "+$19.00" in msg
    assert "qqq_intraday_001" in msg
    assert "trigger_id=trg-1" in msg


def test_daily_trade_summary_message_marks_carry_close_only_as_closed():
    notifier = TelegramNotifier(bot_token="t", chat_id="c")
    event = NotificationEvent(
        event_type="daily_trade_summary",
        status="SUMMARY",
        strategy_id="qqq_1dte_ma10_reject_0324_v2_001",
        symbol="ALL",
        side="N/A",
        qty=0.0,
        order_type="N/A",
        decision_id="daily_trade_summary:2026-04-10",
        ts=datetime(2026, 4, 10, 16, 1, tzinfo=ZoneInfo("America/New_York")),
        metadata={
            "report_date": date(2026, 4, 10).isoformat(),
            "window_start_et": "2026-04-10T00:00:00-04:00",
            "window_end_et": "2026-04-11T00:00:00-04:00",
            "fills_count": 1,
            "strategy_count": 1,
            "strategy_sections": [
                {
                    "strategy_id": "qqq_1dte_ma10_reject_0324_v2_001",
                    "fills_count": 1,
                    "trigger_count": 1,
                    "open_trigger_count": 0,
                    "realized_pnl_usd": 0.0,
                    "unrealized_pnl_usd": 0.0,
                    "total_pnl_usd": 0.0,
                    "open_positions": [],
                    "chains": [
                        {
                            "trigger_id": "prior-day-trigger",
                            "first_trigger_ts_et": "13:07:03",
                            "closed": False,
                            "carry_close_only": True,
                            "report_closed": True,
                            "realized_pnl_usd": 0.0,
                        }
                    ],
                }
            ],
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "total_pnl_usd": 0.0,
            "position_market_value_usd": 0.0,
            "position_notional_abs_usd": 0.0,
        },
    )

    msg = notifier._format_text(event)
    assert "未闭0" in msg
    assert "闭合(跨日平仓)" in msg
