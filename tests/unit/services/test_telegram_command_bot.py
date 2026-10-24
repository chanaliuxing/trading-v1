import os
import socket
import ssl
import sys
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.telegram.command_bot import TelegramCommandBot, build_telegram_command_bot


def test_parse_command_supports_cn_and_en():
    assert TelegramCommandBot._parse_command("清仓") == "flatten"
    assert TelegramCommandBot._parse_command("sell all") == "flatten"
    assert TelegramCommandBot._parse_command("sell all shares") == "flatten"
    assert TelegramCommandBot._parse_command("please sell all shares now") == "flatten"
    assert TelegramCommandBot._parse_command("liquidate all shares") == "flatten"
    assert TelegramCommandBot._parse_command("账号") == "account"
    assert TelegramCommandBot._parse_command("账户") == "account"
    assert TelegramCommandBot._parse_command("account") == "account"
    assert TelegramCommandBot._parse_command("帮助") == "help"
    assert TelegramCommandBot._parse_command("help") == "help"
    assert TelegramCommandBot._parse_command("unknown") is None


def test_parse_flatten_target_supports_strategy_and_trigger():
    assert TelegramCommandBot._parse_flatten_target("清仓") == (None, None)
    assert TelegramCommandBot._parse_flatten_target("清仓 策略 qqq_intraday_001") == ("qqq_intraday_001", None)
    assert TelegramCommandBot._parse_flatten_target("清仓 strategy index_super_win_001") == ("index_super_win_001", None)
    assert TelegramCommandBot._parse_flatten_target("清仓 触发 abc123") == (None, "abc123")
    assert TelegramCommandBot._parse_flatten_target("清仓 trigger trig-001") == (None, "trig-001")


def test_build_bot_requires_token_and_chat_id(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "8403758006")
    b = build_telegram_command_bot(
        repo_root=ROOT,
        trading_mode="paper",
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=31,
    )
    assert b is None

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    b = build_telegram_command_bot(
        repo_root=ROOT,
        trading_mode="paper",
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=31,
    )
    assert b is None

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "8403758006")
    b = build_telegram_command_bot(
        repo_root=ROOT,
        trading_mode="paper",
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=31,
    )
    assert b is not None
    assert isinstance(b, TelegramCommandBot)


def test_is_retriable_poll_error_for_timeout_like_errors():
    assert TelegramCommandBot._is_retriable_poll_error(TimeoutError("timed out"))
    assert TelegramCommandBot._is_retriable_poll_error(socket.timeout("read timed out"))
    assert TelegramCommandBot._is_retriable_poll_error(ssl.SSLError("handshake operation timed out"))
    assert TelegramCommandBot._is_retriable_poll_error(Exception("The read operation timed out"))
    assert not TelegramCommandBot._is_retriable_poll_error(Exception("400 bad request"))


@pytest.mark.asyncio
async def test_flatten_command_uses_injected_handler():
    bot = TelegramCommandBot(
        bot_token="token",
        allowed_chat_ids=["1"],
        repo_root=ROOT,
        trading_mode="paper",
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=31,
    )
    send_mock = AsyncMock()
    bot._send_text = send_mock

    async def _flatten_handler(trigger_id: str, strategy_id: str | None, source_trigger_id: str | None) -> str:
        return f"flatten_ok trigger={trigger_id} sid={strategy_id or ''} src_trig={source_trigger_id or ''}"

    bot.set_flatten_handler(_flatten_handler)
    await bot._handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 200,
                "chat": {"id": "1"},
                "text": "清仓",
            },
        }
    )

    assert send_mock.await_count >= 2
    final_msg = send_mock.await_args_list[-1].args[1]
    assert "manual_flatten(trigger_id=telegram:1:200)" in final_msg
    assert "flatten_ok trigger=telegram:1:200 sid= src_trig=" in final_msg


@pytest.mark.asyncio
async def test_flatten_command_passes_strategy_scope_to_handler():
    bot = TelegramCommandBot(
        bot_token="token",
        allowed_chat_ids=["1"],
        repo_root=ROOT,
        trading_mode="paper",
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=31,
    )
    send_mock = AsyncMock()
    bot._send_text = send_mock
    captured = {}

    async def _flatten_handler(trigger_id: str, strategy_id: str | None, source_trigger_id: str | None) -> str:
        captured["trigger_id"] = trigger_id
        captured["strategy_id"] = strategy_id
        captured["source_trigger_id"] = source_trigger_id
        return "ok"

    bot.set_flatten_handler(_flatten_handler)
    await bot._handle_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 201,
                "chat": {"id": "1"},
                "text": "清仓 策略 qqq_intraday_001",
            },
        }
    )

    assert captured["trigger_id"] == "telegram:1:201"
    assert captured["strategy_id"] == "qqq_intraday_001"
    assert captured["source_trigger_id"] is None


@pytest.mark.asyncio
async def test_help_command_returns_examples():
    bot = TelegramCommandBot(
        bot_token="token",
        allowed_chat_ids=["1"],
        repo_root=ROOT,
        trading_mode="paper",
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=31,
    )
    send_mock = AsyncMock()
    bot._send_text = send_mock

    await bot._handle_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 202,
                "chat": {"id": "1"},
                "text": "帮助",
            },
        }
    )

    assert send_mock.await_count == 1
    msg = send_mock.await_args_list[0].args[1]
    assert "清仓 策略 <strategy_id>" in msg
    assert "清仓 触发 <trigger_id>" in msg
    assert "账号" in msg
