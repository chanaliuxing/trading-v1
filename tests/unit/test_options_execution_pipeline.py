
import pytest
import datetime
import json
from unittest.mock import MagicMock, call
from pathlib import Path
import os
import sys

# Add project root to path to allow importing libs and scripts
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.ai_trader.controllers.llm_trader_controller import LLMTraderController, NY_TZ
from libs.skills.runtime import SkillRuntime

# --- Fixed date for consistent expiry in tests ---
FIXED_TEST_DATE = datetime.datetime(2025, 12, 19, 10, 0, 0, tzinfo=NY_TZ).date()

# --- Mocks for common dependencies ---

@pytest.fixture
def mock_skill_runtime():
    """Mocks SkillRuntime for LLMTraderController instantiation."""
    runtime = MagicMock(spec=SkillRuntime)
    runtime.apply_plan_hooks.side_effect = lambda plan: plan
    return runtime

@pytest.fixture(autouse=True)
def mock_hedge_execution(monkeypatch):
    """Disable hedge execution for these unit tests to reduce noise."""
    mock_hedge = MagicMock()
    monkeypatch.setattr(LLMTraderController, "_execute_hedge_intent", mock_hedge)
    return mock_hedge

@pytest.fixture
def mock_options_provider_factory(monkeypatch):
    """Mocks create_options_provider to return a fake options provider."""
    mock_provider_instance = MagicMock()
    mock_provider_instance.place_vertical_spread.return_value = {
        "status": "prepared", "transmit": False, "order_id": "mock_order_123"
    }
    mock_create_provider = MagicMock(return_value=mock_provider_instance)
    monkeypatch.setattr("scripts.ai_trader.controllers.llm_trader_controller.create_options_provider", mock_create_provider)
    return mock_create_provider, mock_provider_instance

@pytest.fixture
def mock_qqq_put_spread_suggest(monkeypatch):
    """Mocks qqq_put_spread_suggest to return a predefined suggestion."""
    mock_suggestion = MagicMock()
    mock_suggestion.status = "ready"
    mock_suggestion.short_strike = 427.5
    mock_suggestion.long_strike = 422.5
    monkeypatch.setattr("scripts.ai_trader.controllers.llm_trader_controller.qqq_put_spread_suggest", MagicMock(return_value=mock_suggestion))
    return mock_suggestion

@pytest.fixture
def mock_fetch_quotes(monkeypatch):
    """Mocks _fetch_option_chain_quotes to return predefined quotes."""
    expiry_date = (FIXED_TEST_DATE + datetime.timedelta(days=7)).isoformat()
    mock_quotes = {
        "expiry_date": expiry_date, "short_strike": 427.5, "long_strike": 422.5,
        "short_bid": 0.50, "short_ask": 0.55, "long_bid": 0.30, "long_ask": 0.35,
    }
    mock_fetch = MagicMock(return_value=mock_quotes)
    monkeypatch.setattr(LLMTraderController, "_fetch_option_chain_quotes", mock_fetch)
    return mock_fetch, mock_quotes

@pytest.fixture
def mock_trace_write(monkeypatch):
    """Mocks _trace_write to capture records instead of writing to disk."""
    mock_writer = MagicMock()
    monkeypatch.setattr(LLMTraderController, "_trace_write", mock_writer)
    return mock_writer

@pytest.fixture
def controller(mock_skill_runtime, tmp_path):
    """Returns an LLMTraderController instance configured for testing."""
    os.environ["OPTIONS_MIN_CREDIT"] = "0.1"
    os.environ["OPTIONS_TP_PCT"] = "0.5"
    os.environ["OPTIONS_SL_PCT"] = "1.0"
    os.environ["OPTIONS_EXEC_MODE"] = "paper"
    os.environ["OPTIONS_BACKEND"] = "stub"
    return LLMTraderController(
        cmds={}, paper=True, runs_dir=str(tmp_path), skill_runtime=mock_skill_runtime
    )

@pytest.fixture(autouse=True)
def patch_datetime_now(monkeypatch):
    """Fixture to mock datetime to return fixed values."""
    class MockDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2025, 12, 19, 10, 0, 0, tzinfo=NY_TZ)
    monkeypatch.setattr(datetime, 'datetime', MockDateTime)
    class MockDate(datetime.date):
        @classmethod
        def today(cls):
            return datetime.date(2025, 12, 19)
    monkeypatch.setattr(datetime, 'date', MockDate)

class TestOptionsExecutionPipeline:

    def test_1_strategy_generates_order(self, controller, mock_qqq_put_spread_suggest, mock_fetch_quotes, mock_trace_write, mock_options_provider_factory):
        """用例 1: 策略生成订单"""
        plan = {
            "session": "intraday", "providers": [{"symbols": ["QQQ"]}],
            "qqq_price": 450.0, "qqq_ma50": 445.0, "qqq_ma100": 440.0, "qqq_ma200": 430.0, "qqq_trend": "up"
        }
        controller.run_with_plan(plan)
        
        injected_trace = next((c.args[1] for c in mock_trace_write.call_args_list if c.args[0] == 'options_order_injected'), None)
        assert injected_trace is not None, "'options_order_injected' was not traced"
        assert injected_trace['order']['short_strike'] == 427.5
        
        routing_trace = next((c.args[1] for c in mock_trace_write.call_args_list if c.args[0] == 'options_order_routing'), None)
        assert routing_trace is not None, "'options_order_routing' was not traced"
        assert routing_trace['order']['credit_mid'] == pytest.approx(0.20)
        assert routing_trace['status'] == 'prepared'

    def test_2_credit_below_threshold_rejects_order(self, controller, mock_qqq_put_spread_suggest, mock_fetch_quotes, mock_trace_write, monkeypatch):
        """用例 2: credit 低于阈值拒单"""
        monkeypatch.setenv("OPTIONS_MIN_CREDIT", "0.25")
        plan = {
            "session": "intraday", "providers": [{"symbols": ["QQQ"]}],
            "qqq_price": 450.0, "qqq_ma50": 445.0, "qqq_ma100": 440.0, "qqq_ma200": 430.0, "qqq_trend": "up",
        }
        controller.run_with_plan(plan)
        
        rejection_trace = next((c.args[1] for c in mock_trace_write.call_args_list if c.args[0] == 'options_order_rejected'), None)
        assert rejection_trace is not None, "'options_order_rejected' was not traced"
        assert "credit_mid 0.2 is below min_credit 0.25" in rejection_trace["reason"]

    def test_3_order_routes_to_provider(self, controller, mock_options_provider_factory, mock_fetch_quotes, mock_trace_write):
        """用例 3: 路由调用 provider"""
        mock_create_provider, mock_provider = mock_options_provider_factory
        order = {
            "strategy_id": "test_strat", "symbol": "QQQ", "side": "SELL_PUT_SPREAD",
            "short_strike": 427.5, "long_strike": 422.5, "qty": 1, "expiry": "2025-12-26"
        }
        plan = {"options_orders": [order]}
        controller._execute_options_orders(plan)

        mock_provider.place_vertical_spread.assert_called_once()
        call_kwargs = mock_provider.place_vertical_spread.call_args.kwargs
        assert call_kwargs['credit_mid'] == pytest.approx(0.20)
        assert call_kwargs['transmit'] is False, "Transmit should be False in paper mode"
