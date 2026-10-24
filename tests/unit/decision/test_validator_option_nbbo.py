import time
from unittest.mock import AsyncMock

import pytest

from libs.strategy.base import DecisionEnvelope, Freshness, PriceIntent
from services.decision.validator import PreDecisionValidator, ValidateConfig, SimpleCostModel


class _MS:
    def __init__(self, market_by_symbol):
        self._market_by_symbol = {k: dict(v) for k, v in dict(market_by_symbol).items()}

    async def get(self, symbol):
        return dict(self._market_by_symbol.get(symbol, {}))


class _Bars:
    def __init__(self, bars_by_symbol):
        self._bars_by_symbol = {k: list(v) for k, v in dict(bars_by_symbol).items()}

    async def window(self, symbol, end_us, count):
        return list(self._bars_by_symbol.get(symbol, []))


@pytest.mark.asyncio
async def test_validator_option_uses_bars_symbol_and_opt_nbbo():
    cfg = ValidateConfig(min_hist_bars=3, require_nbbo=True, enforce_quote_age=True, max_quote_age_sec=15.0)
    ms = _MS(
        {
            # Underlying market state, used for halted checks only.
            "QQQ": {"halted": False, "bid_px": 400.0, "ask_px": 400.01, "mid_px": 400.005, "last_px": 400.0, "quote_age_sec": 1.0},
        }
    )
    bars = _Bars(
        {
            "QQQ": [
                {"atr_bp": 10.0, "volume": 1000.0, "quality": "GOOD"},
                {"atr_bp": 10.0, "volume": 1000.0, "quality": "GOOD"},
                {"atr_bp": 10.0, "volume": 1000.0, "quality": "GOOD"},
            ]
        }
    )
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="dopt1",
        strategy_id="sopt",
        symbol="OPT:QQQ:20260305:430:C",
        ts_bar_end_us=now_us,
        target_qty=3.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        signal_score=2.0,
        extras={
            "instrument_type": "OPTION",
            "bars_symbol": "QQQ",
            "opt_bid_px": 2.00,
            "opt_ask_px": 2.05,
            "opt_quote_age_sec": 0.2,
        },
    )

    res = await v.validate(env)
    assert res.ok is True
    assert res.skip_reasons == []


@pytest.mark.asyncio
async def test_validator_option_rejects_missing_opt_nbbo():
    cfg = ValidateConfig(min_hist_bars=1, require_nbbo=True)
    ms = _MS({"QQQ": {"halted": False}})
    bars = _Bars({"QQQ": [{"atr_bp": 10.0, "volume": 1000.0, "quality": "GOOD"}]})
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="dopt2",
        strategy_id="sopt",
        symbol="OPT:QQQ:20260305:430:P",
        ts_bar_end_us=now_us,
        target_qty=3.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        signal_score=1.0,
        extras={"instrument_type": "OPTION", "bars_symbol": "QQQ"},
    )
    res = await v.validate(env)
    assert res.ok is False
    assert "no_nbbo" in res.skip_reasons


@pytest.mark.asyncio
async def test_validator_option_prefers_explicit_edge_bp_over_signal_score_mapping():
    cfg = ValidateConfig(min_hist_bars=1, require_nbbo=True, edge_buffer_bp=0.0)
    ms = _MS({"QQQ": {"halted": False, "bid_px": 400.0, "ask_px": 400.01, "mid_px": 400.005, "last_px": 400.0}})
    bars = _Bars({"QQQ": [{"atr_bp": 10.0, "volume": 1000.0, "quality": "GOOD"}]})
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="dopt3",
        strategy_id="qqq_1dte_ma10_reject_0324_v2_001",
        symbol="OPT:QQQ:20260305:430:C",
        ts_bar_end_us=now_us,
        target_qty=3.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        # Legacy mapping would yield only 30bp and fail this spread/cost profile.
        signal_score=0.3,
        extras={
            "instrument_type": "OPTION",
            "bars_symbol": "QQQ",
            "opt_bid_px": 2.00,
            "opt_ask_px": 2.03,
            "opt_quote_age_sec": 0.2,
            "edge_bp": 90.0,
        },
    )
    res = await v.validate(env)
    assert res.ok is True
    assert not any("edge_insufficient" in reason for reason in res.skip_reasons)
