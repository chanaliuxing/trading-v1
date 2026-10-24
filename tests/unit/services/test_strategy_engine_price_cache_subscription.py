from unittest.mock import MagicMock

from services.strategy.engine import StrategyEngine


def test_strategy_engine_subscribe_price_cache_symbols_normalizes_symbols():
    engine = object.__new__(StrategyEngine)
    engine.price_cache = MagicMock()

    engine._subscribe_price_cache_symbols([" qqq ", "TQQQ", "", None, "sqqq"])

    engine.price_cache.subscribe.assert_called_once_with(["QQQ", "TQQQ", "SQQQ"])


def test_strategy_engine_subscribe_price_cache_symbols_noop_without_subscribe():
    engine = object.__new__(StrategyEngine)
    engine.price_cache = object()

    engine._subscribe_price_cache_symbols(["QQQ"])
