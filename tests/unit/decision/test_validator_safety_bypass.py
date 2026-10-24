import time
import pytest
from unittest.mock import AsyncMock

from libs.strategy.base import (
    DecisionEnvelope,
    Freshness,
    PriceIntent,
)

from services.decision.validator import PreDecisionValidator, ValidateConfig, SimpleCostModel


class _MS:
    def __init__(self, market):
        self._market = dict(market)

    async def get(self, symbol):
        return dict(self._market)


class _Bars:
    def __init__(self, bars):
        self._bars = list(bars)

    async def window(self, symbol, end_us, count):
        return list(self._bars)


@pytest.mark.asyncio
async def test_validator_allows_safety_intent_even_when_history_nbbo_edge_stale_fail():
    cfg = ValidateConfig(min_hist_bars=36, require_nbbo=True)
    ms = _MS({"halted": False, "bid_px": None, "ask_px": None, "mid_px": None, "last_px": None})
    bars = _Bars([])  # insufficient history by definition
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="d1",
        strategy_id="s1",
        symbol="TQQQ",
        # Make decision extremely stale relative to DLB to prove bypass.
        ts_bar_end_us=now_us - int(60 * 1e6),
        target_qty=-10.0,
        freshness=Freshness(dlb_ms=1, ttl_ms=1000),
        # Force mid-reference NBBO check if it were applied.
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        # Force edge check to fail if applied.
        signal_score=0.0,
        extras={"flatten_action": True, "close_only": True},
    )

    res = await v.validate(env)
    assert res.ok is True
    assert res.skip_reasons == []


@pytest.mark.asyncio
async def test_validator_still_rejects_entry_when_history_insufficient():
    cfg = ValidateConfig(min_hist_bars=36, require_nbbo=True)
    ms = _MS({"halted": False, "bid_px": None, "ask_px": None, "mid_px": None, "last_px": None})
    bars = _Bars([])
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="d2",
        strategy_id="s1",
        symbol="TQQQ",
        ts_bar_end_us=now_us,
        target_qty=10.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        signal_score=0.5,
        extras={},
    )

    res = await v.validate(env)
    assert res.ok is False
    assert "insufficient_history" in res.skip_reasons


@pytest.mark.asyncio
async def test_validator_allows_entry_when_strategy_history_ready_hint_present():
    cfg = ValidateConfig(min_hist_bars=36, require_nbbo=True)
    ms = _MS({"halted": False, "bid_px": 50.0, "ask_px": 50.02, "mid_px": 50.01, "last_px": 50.01})
    bars = _Bars([])  # DB history empty, strategy still declares ready.
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="d3",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        ts_bar_end_us=now_us,
        target_qty=10.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        signal_score=1.0,
        extras={"history_ready": True},
    )

    res = await v.validate(env)
    assert res.ok is True
    assert "insufficient_history" not in res.skip_reasons


@pytest.mark.asyncio
async def test_validator_allows_spy_option_entry_when_strategy_history_ready_hint_present():
    cfg = ValidateConfig(min_hist_bars=36, require_nbbo=True)
    ms = _MS({"halted": False, "bid_px": 657.8, "ask_px": 657.82, "mid_px": 657.81, "last_px": 657.81})
    bars = _Bars([])
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="d3_spy",
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
        ts_bar_end_us=now_us,
        target_qty=4.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        signal_score=1.0,
        extras={
            "history_ready": True,
            "instrument_type": "OPTION",
            "underlying": "SPY",
            "bars_symbol": "SPY",
            "opt_bid_px": 3.59,
            "opt_ask_px": 3.60,
        },
    )

    res = await v.validate(env)
    assert res.ok is True
    assert "insufficient_history" not in res.skip_reasons


@pytest.mark.asyncio
async def test_validator_history_ready_hint_does_not_bypass_nbbo_checks():
    cfg = ValidateConfig(min_hist_bars=36, require_nbbo=True)
    ms = _MS({"halted": False, "bid_px": None, "ask_px": None, "mid_px": None, "last_px": None})
    bars = _Bars([])  # DB history empty
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="d4",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        ts_bar_end_us=now_us,
        target_qty=10.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        signal_score=1.0,
        extras={"history_ready": True},
    )

    res = await v.validate(env)
    assert res.ok is False
    assert "insufficient_history" not in res.skip_reasons
    assert "no_nbbo_for_mid" in res.skip_reasons
    assert "no_nbbo" in res.skip_reasons


@pytest.mark.asyncio
async def test_validator_history_ready_hint_blocked_for_non_whitelisted_strategy():
    cfg = ValidateConfig(
        min_hist_bars=36,
        require_nbbo=True,
        allow_strategy_history_ready_bypass=True,
        history_ready_allowed_strategy_ids=("qqq_intraday_001",),
    )
    ms = _MS({"halted": False, "bid_px": 50.0, "ask_px": 50.02, "mid_px": 50.01, "last_px": 50.01})
    bars = _Bars([])
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="d5",
        strategy_id="other_strategy",
        symbol="TQQQ",
        ts_bar_end_us=now_us,
        target_qty=10.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        signal_score=1.0,
        extras={"history_ready": True},
    )

    res = await v.validate(env)
    assert res.ok is False
    assert "insufficient_history" in res.skip_reasons


@pytest.mark.asyncio
async def test_validator_history_ready_hint_disabled_by_switch():
    cfg = ValidateConfig(
        min_hist_bars=36,
        require_nbbo=True,
        allow_strategy_history_ready_bypass=False,
        history_ready_allowed_strategy_ids=("qqq_intraday_001",),
    )
    ms = _MS({"halted": False, "bid_px": 50.0, "ask_px": 50.02, "mid_px": 50.01, "last_px": 50.01})
    bars = _Bars([])
    feats = AsyncMock()
    v = PreDecisionValidator(cfg, ms, bars, feats, SimpleCostModel())

    now_us = int(time.time() * 1e6)
    env = DecisionEnvelope(
        decision_id="d6",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        ts_bar_end_us=now_us,
        target_qty=10.0,
        freshness=Freshness(dlb_ms=30000, ttl_ms=1000),
        price_intent=PriceIntent(ref="mid", offset_ticks=0),
        signal_score=1.0,
        extras={"history_ready": True},
    )

    res = await v.validate(env)
    assert res.ok is False
    assert "insufficient_history" in res.skip_reasons
