import sys
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.notifications import NotificationEvent, TelegramNotifier
from services.notifications.telegram_notifier import SendAttemptResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_telegram_notifier_retries_then_succeeds(monkeypatch):
    notifier = TelegramNotifier(
        bot_token="t",
        chat_id="c",
        max_retries=2,
        retry_base_delay_sec=0.01,
        retry_max_delay_sec=0.02,
        retry_jitter_sec=0.0,
    )

    calls = {"n": 0}

    async def fake_send_once(_text: str) -> SendAttemptResult:
        calls["n"] += 1
        if calls["n"] < 3:
            return SendAttemptResult(ok=False, retriable=True, error="tmp")
        return SendAttemptResult(ok=True, retriable=False)

    monkeypatch.setattr(notifier, "_send_once", fake_send_once)

    event = NotificationEvent(
        event_type="order_submitted",
        status="SUBMITTED",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        qty=10,
        order_type="MKT",
        decision_id="d1",
    )

    assert await notifier.send(event) is True
    assert calls["n"] == 3


@pytest.mark.anyio
async def test_telegram_notifier_circuit_breaker_opens_after_repeated_failures(monkeypatch):
    notifier = TelegramNotifier(
        bot_token="t",
        chat_id="c",
        max_retries=0,
        failure_threshold=2,
        breaker_open_timeout_sec=60.0,
    )

    calls = {"n": 0}

    async def always_fail(_text: str) -> SendAttemptResult:
        calls["n"] += 1
        return SendAttemptResult(ok=False, retriable=False, error="bad_req", status_code=400)

    monkeypatch.setattr(notifier, "_send_once", always_fail)

    event = NotificationEvent(
        event_type="order_submitted",
        status="SUBMITTED",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        qty=10,
        order_type="MKT",
        decision_id="d1",
    )

    assert await notifier.send(event) is False
    assert await notifier.send(event) is False

    # Third call should be skipped by open circuit.
    assert await notifier.send(event) is False
    assert calls["n"] == 2


def test_format_text_includes_option_call_put_action():
    notifier = TelegramNotifier(bot_token="t", chat_id="c")

    put_event = NotificationEvent(
        event_type="order_fill",
        status="FILLED",
        strategy_id="qqq_0dte_opt_live_001",
        symbol="QQQ 20260311 610P",
        side="BUY",
        qty=3,
        order_type="MKT",
        decision_id="d-put",
        price=2.13,
        fill_qty=3,
        amount_usd=639.0,
    )
    put_msg = notifier._format_text(put_event)
    assert "🟢 订单成交" in put_msg
    assert "📌 基本信息" in put_msg
    assert "📦 订单明细" in put_msg
    assert "🧭 跟踪" in put_msg
    assert "Action: BUY PUT" in put_msg
    assert "Option Type: PUT" in put_msg

    call_event = NotificationEvent(
        event_type="order_fill",
        status="FILLED",
        strategy_id="qqq_0dte_opt_live_001",
        symbol="QQQ 20260311 610 C",
        side="SELL",
        qty=3,
        order_type="MKT",
        decision_id="d-call",
        price=3.02,
        fill_qty=3,
        amount_usd=906.0,
    )
    call_msg = notifier._format_text(call_event)
    assert "🟢 订单成交" in call_msg
    assert "Action: SELL CALL" in call_msg
    assert "Option Type: CALL" in call_msg


def test_format_text_for_system_alert():
    notifier = TelegramNotifier(bot_token="t", chat_id="c")
    event = NotificationEvent(
        event_type="system_alert",
        status="CRITICAL",
        strategy_id="SYSTEM",
        symbol="market_data",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="service_crashed",
        metadata={
            "host": "trading-mac",
            "service": "market_data",
            "error_type": "MarketDataServiceFatalError",
            "error": "tick publisher degraded too long",
            "detail": "Background service task market_data exited with an exception.",
            "pid": 12345,
            "cwd": ".",
        },
    )

    msg = notifier._format_text(event)
    assert "🚨 系统故障告警" in msg
    assert "Service: market_data" in msg
    assert "Error Type: MarketDataServiceFatalError" in msg
    assert "Error: tick publisher degraded too long" in msg


def test_format_text_for_system_alert_info_uses_recovery_title():
    notifier = TelegramNotifier(bot_token="t", chat_id="c")
    event = NotificationEvent(
        event_type="system_alert",
        status="INFO",
        strategy_id="SYSTEM",
        symbol="IB_CONNECTION",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="system_resumed",
        metadata={
            "host": "trading-mac",
            "service": "IB_CONNECTION",
            "error_type": "system_resumed",
            "error": "ib connectivity restored",
            "detail": "system_health_controller",
        },
    )

    msg = notifier._format_text(event)
    assert "ℹ️ 系统恢复通知" in msg
    assert "🚨 系统故障告警" not in msg
    assert "Service: IB_CONNECTION" in msg
    assert "Error: ib connectivity restored" in msg


@pytest.mark.anyio
async def test_telegram_notifier_persists_push_audit(tmp_path, monkeypatch):
    audit_path = tmp_path / "telegram_push_audit.jsonl"
    monkeypatch.setenv("TELEGRAM_PUSH_AUDIT_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_PUSH_AUDIT_PATH", str(audit_path))

    notifier = TelegramNotifier(
        bot_token="t",
        chat_id="c",
        max_retries=0,
    )

    async def fake_send_once(_text: str) -> SendAttemptResult:
        return SendAttemptResult(ok=True, retriable=False, status_code=200)

    monkeypatch.setattr(notifier, "_send_once", fake_send_once)

    event = NotificationEvent(
        event_type="order_submitted",
        status="SUBMITTED",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        qty=10,
        order_type="MKT",
        decision_id="d1",
        metadata={"source": "unit-test"},
    )

    assert await notifier.send(event) is True
    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    last = json.loads(lines[-1])
    assert last["outcome"] == "sent"
    assert last["strategy_id"] == "qqq_intraday_001"
    assert last["event_type"] == "order_submitted"
    assert isinstance(last.get("push_ts"), str) and last["push_ts"]


@pytest.mark.anyio
async def test_telegram_notifier_persists_push_audit_when_missing_credentials(tmp_path, monkeypatch):
    audit_path = tmp_path / "telegram_push_audit.jsonl"
    monkeypatch.setenv("TELEGRAM_PUSH_AUDIT_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_PUSH_AUDIT_PATH", str(audit_path))

    notifier = TelegramNotifier(
        bot_token="",
        chat_id="",
        max_retries=0,
    )

    event = NotificationEvent(
        event_type="order_submitted",
        status="SUBMITTED",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        qty=10,
        order_type="MKT",
        decision_id="d1",
    )

    assert await notifier.send(event) is False
    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["outcome"] == "skipped"
    assert row["reason"] == "missing_credentials"


@pytest.mark.anyio
async def test_telegram_notifier_persists_push_audit_when_terminal_failure(tmp_path, monkeypatch):
    audit_path = tmp_path / "telegram_push_audit.jsonl"
    monkeypatch.setenv("TELEGRAM_PUSH_AUDIT_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_PUSH_AUDIT_PATH", str(audit_path))

    notifier = TelegramNotifier(
        bot_token="t",
        chat_id="c",
        max_retries=0,
    )

    async def fake_send_once(_text: str) -> SendAttemptResult:
        return SendAttemptResult(ok=False, retriable=False, status_code=400, error="bad_req")

    monkeypatch.setattr(notifier, "_send_once", fake_send_once)

    event = NotificationEvent(
        event_type="order_submitted",
        status="SUBMITTED",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        qty=10,
        order_type="MKT",
        decision_id="d1",
    )

    assert await notifier.send(event) is False
    assert audit_path.exists()
    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").strip().splitlines() if line]
    outcomes = [line["outcome"] for line in lines]
    assert outcomes == ["attempt_failed", "failed"]
    assert lines[-1]["status_code"] == 400
