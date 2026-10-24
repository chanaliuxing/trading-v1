import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from zoneinfo import ZoneInfo
import sys
from pathlib import Path

# Ensure project root is on sys.path so that "strategies.*" imports work
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from strategies.enhanced_model_signal_strategy import EnhancedModelSignalStrategy
from libs.strategy.base import DecisionEnvelope, Freshness, OrderStyle

# Mock the log_event function from obs.audit
sys.modules['obs.audit'] = MagicMock()
from obs.audit import log_event

class MockPrice:
    def __init__(self, last: float, spread_pct: float = 0.0001):
        self.last = last
        self.spread_pct = spread_pct

class MockContext:
    def __init__(self):
        self.strategy_id = "test_strategy"
        self.log = MagicMock()
        self.metrics = MagicMock()
        self.engine = MagicMock()
        self.market_open_dt = datetime(2025, 12, 5, 15, 0, 0, tzinfo=ZoneInfo("UTC")) # 10:00 AM ET
        self.get_price = MagicMock(return_value=MockPrice(100.0))
        self.position = AsyncMock(return_value=MagicMock(pos_qty=0))
        self.submit_intent = AsyncMock()
        self.now_us = MagicMock(return_value=int(self.market_open_dt.timestamp() * 1_000_000))
        self.subscribe_features = AsyncMock()
        self.enable_realtime_feature_engine = AsyncMock()
        self.get_realtime_features = MagicMock(return_value=(None, None, False)) # For rfe_cfg
        # Mock options_provider init
        self.options_provider = None

# Mock the get_model_service and AiBrainMLGateway to prevent real ML calls
sys.modules['services.ml'] = MagicMock()
sys.modules['services.ml.ai_brain_ml_gateway'] = MagicMock()

import copy # Added for deepcopy

# ... (rest of imports)

class TestEVFilterAndHedgeBlock(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        pass  # Empty setUp for full isolation within test methods.

    async def test_ev_filter_blocks_trade(self):
        """Test that trade is blocked when EV is below threshold."""
        mock_context = MockContext()
        log_event.reset_mock() # Reset global mock
        mock_context.log.reset_mock()
        mock_context.submit_intent.reset_mock()

        default_extras = {
            "symbols": ["TEST"],
            "ensemble": {"models": [{"name": "ml", "weight": 1.0}], "mode": "weighted_avg"},
            "sizing": {"mode": "fixed", "base_qty": 100, "lot": 1},
            "market_filter": {"max_spread_bps": 100.0, "min_volume": 0, "max_vix": 0, "conf_floor": 0.0},
            "execution_routing": {
                "allow_options": False,
                "require_hedge_for_high_risk": False,
                "ev_filter": {
                    "min_rr": 1.0,
                    "min_p_win": 0.0,
                    "min_ev": 0.0,
                }
            },
            "lot": 1,
            "bar_sec": 60,
        }

        params_mock = MagicMock()
        params_mock.extras = copy.deepcopy(default_extras)
        params_mock.capital_limit_usd = 100_000

        params_mock.extras["execution_routing"]["ev_filter"]["min_ev"] = 0.1
        params_mock.extras["execution_routing"]["ev_filter"]["min_p_win"] = 0.55
        params_mock.extras["execution_routing"]["require_hedge_for_high_risk"] = False
        
        # Define expected ML Gateway output
        mock_ml_gateway_return_value = {
            "ml_signal": 1.0,
            "ml_conf": 0.1,
            "raw_pred": (1.0 + 1) / 2,
            "decision": "trade",
            "status": "ok",
        }
        mock_aibrain_ml_gateway_instance = MagicMock()
        mock_aibrain_ml_gateway_instance.get_decision.return_value = mock_ml_gateway_return_value

        with patch('services.market.options_data.create_options_provider') as mock_create_options_provider, \
             patch('strategies.enhanced_model_signal_strategy.AiBrainMLGateway') as mock_aibrain_ml_gateway_cls:
            
            mock_create_options_provider.return_value = None
            mock_aibrain_ml_gateway_cls.return_value = mock_aibrain_ml_gateway_instance # Gateway constructor returns our mock instance

            strategy = EnhancedModelSignalStrategy(mock_context, params_mock)
            await strategy.initialize()
        
            features = {
                "ts": mock_context.market_open_dt,
                "close": 100.0,
                "atr_14": 1.0,
                "atr": 1.0,
                "volume": 1_000_000,
                "spread_bps": 5.0,
                "vix": 15.0,
                "gap_pct": 0.0,
                "ret_1m": 0.001,
                "spy_ret_1m": 0.0005,
            }

            await strategy.on_features("TEST", features)

        # Assert that submit_intent was NOT called
        mock_context.submit_intent.assert_not_called()

        # Assert that 'ev_filter_blocked' was logged by strategy's logger
        found = any("ev_filter_blocked" in str(call) for call in mock_context.log.info.mock_calls)
        self.assertTrue(found, "ev_filter_blocked log not found")

    async def test_hedging_gate_blocks_naked_trade(self):
        """Test that naked trade is blocked when hedging is required and options are not used."""
        mock_context = MockContext()
        log_event.reset_mock()
        mock_context.log.reset_mock()
        mock_context.submit_intent.reset_mock()

        default_extras = {
            "symbols": ["TEST"],
            "ensemble": {"models": [{"name": "ml", "weight": 1.0}], "mode": "weighted_avg"},
            "sizing": {"mode": "fixed", "base_qty": 100, "lot": 1},
            "market_filter": {"max_spread_bps": 100.0, "min_volume": 0, "max_vix": 0, "conf_floor": 0.0},
            "execution_routing": {
                "allow_options": False,
                "require_hedge_for_high_risk": False,
                "ev_filter": {
                    "min_rr": 1.0,
                    "min_p_win": 0.0,
                    "min_ev": 0.0,
                }
            },
            "lot": 1,
            "bar_sec": 60,
        }

        params_mock = MagicMock()
        params_mock.extras = copy.deepcopy(default_extras)
        params_mock.capital_limit_usd = 100_000

        params_mock.extras["execution_routing"]["ev_filter"]["min_ev"] = -100.0
        params_mock.extras["execution_routing"]["ev_filter"]["min_p_win"] = 0.0
        params_mock.extras["execution_routing"]["require_hedge_for_high_risk"] = True

        # Define expected ML Gateway output
        mock_ml_gateway_return_value = {
            "ml_signal": 1.0,
            "ml_conf": 0.6,
            "raw_pred": (1.0 + 1) / 2,
            "decision": "trade",
            "status": "ok",
        }
        mock_aibrain_ml_gateway_instance = MagicMock()
        mock_aibrain_ml_gateway_instance.get_decision.return_value = mock_ml_gateway_return_value

        with patch('services.market.options_data.create_options_provider') as mock_create_options_provider, \
             patch('strategies.enhanced_model_signal_strategy.AiBrainMLGateway') as mock_aibrain_ml_gateway_cls:
            
            mock_create_options_provider.return_value = None
            mock_aibrain_ml_gateway_cls.return_value = mock_aibrain_ml_gateway_instance

            strategy = EnhancedModelSignalStrategy(mock_context, params_mock)
            await strategy.initialize()
        
            features = {
                "ts": mock_context.market_open_dt,
                "close": 100.0,
                "atr_14": 1.0,
                "atr": 1.0,
                "volume": 1_000_000,
                "spread_bps": 5.0,
                "vix": 15.0,
                "gap_pct": 0.0,
                "ret_1m": 0.001,
                "spy_ret_1m": 0.0005,
            }

            await strategy.on_features("TEST", features)

        # Assert that submit_intent was NOT called
        mock_context.submit_intent.assert_not_called()
        
        # Assert that 'hedge_required_blocking_naked_trade' was logged by strategy's logger
        found = any("hedge_required_blocking_naked_trade" in str(call) for call in mock_context.log.info.mock_calls)
        self.assertTrue(found, "hedge_required_blocking_naked_trade log not found")




    async def test_trade_submitted_when_filters_bypassed(self):
        """Test that trade is submitted when both EV and hedging filters are bypassed."""
        mock_context = MockContext()
        log_event.reset_mock()
        mock_context.log.reset_mock()
        mock_context.submit_intent.reset_mock()

        default_extras = {
            "symbols": ["TEST"],
            "ensemble": {"models": [{"name": "ml", "weight": 1.0}], "mode": "weighted_avg"},
            "sizing": {"mode": "fixed", "base_qty": 100, "lot": 1},
            "market_filter": {"max_spread_bps": 100.0, "min_volume": 0, "max_vix": 0, "conf_floor": 0.0},
            "execution_routing": {
                "allow_options": False,
                "require_hedge_for_high_risk": False,
                "ev_filter": {
                    "min_rr": 1.0,
                    "min_p_win": 0.0,
                    "min_ev": 0.0,
                }
            },
            "lot": 1,
            "bar_sec": 60,
        }

        params_mock = MagicMock()
        params_mock.extras = copy.deepcopy(default_extras)
        params_mock.capital_limit_usd = 100_000

        params_mock.extras["execution_routing"]["ev_filter"]["min_ev"] = -100.0
        params_mock.extras["execution_routing"]["ev_filter"]["min_p_win"] = 0.0
        params_mock.extras["execution_routing"]["require_hedge_for_high_risk"] = False

        # Define expected ML Gateway output
        mock_ml_gateway_return_value = {
            "ml_signal": 1.0,
            "ml_conf": 0.8,
            "raw_pred": (1.0 + 1) / 2,
            "decision": "trade",
            "status": "ok",
        }
        mock_aibrain_ml_gateway_instance = MagicMock()
        mock_aibrain_ml_gateway_instance.get_decision.return_value = mock_ml_gateway_return_value

        with patch('services.market.options_data.create_options_provider') as mock_create_options_provider, \
             patch('strategies.enhanced_model_signal_strategy.AiBrainMLGateway') as mock_aibrain_ml_gateway_cls:
            
            mock_create_options_provider.return_value = None
            mock_aibrain_ml_gateway_cls.return_value = mock_aibrain_ml_gateway_instance

            strategy = EnhancedModelSignalStrategy(mock_context, params_mock)
            await strategy.initialize()
        
            features = {
                "ts": mock_context.market_open_dt,
                "close": 100.0,
                "atr_14": 1.0,
                "atr": 1.0,
                "volume": 1_000_000,
                "spread_bps": 5.0,
                "vix": 15.0,
                "gap_pct": 0.0,
                "ret_1m": 0.001,
                "spy_ret_1m": 0.0005,
            }

            await strategy.on_features("TEST", features)

        # Assert that submit_intent was called
        mock_context.submit_intent.assert_called_once()
        
        # Verify the envelope content
        envelope = mock_context.submit_intent.call_args[0][0]
        self.assertIsInstance(envelope, DecisionEnvelope)
        self.assertEqual(envelope.symbol, "TEST")
        self.assertEqual(envelope.target_qty, 100.0)

    async def test_hedge_allows_option_route(self):
        """When hedge required and options allowed, we route to option contract instead of blocking."""
        mock_context = MockContext()
        log_event.reset_mock()
        mock_context.log.reset_mock()
        mock_context.submit_intent.reset_mock()

        default_extras = {
            "symbols": ["TEST"],
            "ensemble": {"models": [{"name": "ml", "weight": 1.0}], "mode": "weighted_avg"},
            "sizing": {"mode": "fixed", "base_qty": 100, "lot": 1},
            "market_filter": {"max_spread_bps": 100.0, "min_volume": 0, "max_vix": 0, "conf_floor": 0.0},
            "execution_routing": {
                "allow_options": True,
                "require_hedge_for_high_risk": True,
                "ev_filter": {
                    "min_rr": 1.0,
                    "min_p_win": 0.0,
                    "min_ev": 0.0,
                },
                "option_preferences": {"target_delta": 0.5},
            },
            "lot": 1,
            "bar_sec": 60,
        }

        params_mock = MagicMock()
        params_mock.extras = copy.deepcopy(default_extras)
        params_mock.capital_limit_usd = 100_000

        mock_ml_gateway_return_value = {
            "ml_signal": 1.0,
            "ml_conf": 0.9,
            "raw_pred": (1.0 + 1) / 2,
            "decision": "trade",
            "status": "ok",
        }
        mock_aibrain_ml_gateway_instance = MagicMock()
        mock_aibrain_ml_gateway_instance.get_decision.return_value = mock_ml_gateway_return_value

        async def _fake_chain(*_args, **_kwargs):
            return []

        fake_options_provider = MagicMock()
        fake_options_provider.get_chain = AsyncMock(side_effect=_fake_chain)

        with patch('services.market.options_data.create_options_provider') as mock_create_options_provider, \
             patch('strategies.enhanced_model_signal_strategy.AiBrainMLGateway') as mock_aibrain_ml_gateway_cls:

            mock_create_options_provider.return_value = fake_options_provider
            mock_aibrain_ml_gateway_cls.return_value = mock_aibrain_ml_gateway_instance

            strategy = EnhancedModelSignalStrategy(mock_context, params_mock)
            await strategy.initialize()
            strategy._select_alpha_contract = MagicMock(return_value=type("C", (), {
                "symbol": "TEST_OPTION_C",
                "delta": 0.55,
                "mid": 2.5,
                "right": "C",
                "expiry_date": "2025-12-31"
            })())

            features = {
                "ts": mock_context.market_open_dt,
                "close": 100.0,
                "atr_14": 1.0,
                "atr": 1.0,
                "volume": 1_000_000,
                "spread_bps": 5.0,
                "vix": 15.0,
                "gap_pct": 0.0,
                "ret_1m": 0.001,
                "spy_ret_1m": 0.0005,
            }

            await strategy.on_features("TEST", features)

        mock_context.submit_intent.assert_called_once()
        envelope = mock_context.submit_intent.call_args[0][0]
        self.assertEqual(envelope.extras.get("instrument_type"), "OPTION")
        self.assertEqual(envelope.symbol, "TEST_OPTION_C")
        self.assertEqual(envelope.target_qty, 1)



if __name__ == '__main__':
    unittest.main()
