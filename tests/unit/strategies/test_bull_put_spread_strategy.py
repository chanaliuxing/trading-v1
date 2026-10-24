import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from libs.strategy import StrategyContext, StrategyParams
from services.market.options_data import OptionContract
import strategies.options.bull_put_spread_strategy as bull_put_module
from strategies.options.bull_put_spread_strategy import BullPutSpreadStrategy


class DummyLogger:
    def __init__(self):
        self.logs = []

    def info(self, *args, **kwargs):
        self.logs.append(("info", args, kwargs))

    def debug(self, *args, **kwargs):
        self.logs.append(("debug", args, kwargs))

    def warning(self, *args, **kwargs):
        self.logs.append(("warn", args, kwargs))

    def error(self, *args, **kwargs):
        self.logs.append(("error", args, kwargs))


class DummyEngine:
    async def subscribe_features(self, strategy_id, symbols, bar_sec):
        return


@pytest.fixture
def base_extras():
    return {
        "symbol": "QQQ",
        "vix_halt": 50.0,
        "max_feature_age_sec": 3600,
        "width_candidates": [5.0],
        "min_open_interest": 100,
        "max_spread_bps": 1000,
        "min_credit": 0.1,
        "scan_interval_sec": 0,
        "take_profit_pct": 0.5,
        "stop_loss_pct": 2.0,
        "provider": {"backend": "stub"},
        "clickhouse_table": "bull_put_spread_events",
        "trade_window_start_et": "00:00",
        "trade_window_end_et": "23:59",
        "target_delta": -0.25,
        "min_dte": 14,
        "max_dte": 35,
    }


def make_strategy(extras):
    logger = DummyLogger()
    ctx = StrategyContext(engine=DummyEngine(), strategy_id="bull_put_test", logger=logger, metrics=lambda: {})
    params = StrategyParams()
    params.extras["bull_put"] = extras
    strat = BullPutSpreadStrategy(ctx, params)
    strat.ch = None  # avoid real ClickHouse writes
    class DummyRepo:
        async def insert_trade(self, **kwargs):
            return
    strat.trade_repo = DummyRepo()
    return strat


@pytest.mark.asyncio
async def test_skip_vix_halt(base_extras, monkeypatch):
    extras = dict(base_extras)
    extras["vix_halt"] = 10.0
    events = []

    strat = make_strategy(extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()
    features = {"close": 400.0, "ma50": 390.0, "ma100": 380.0, "ma200": 350.0, "pct_change_1d": -0.5, "vix": 15.0}
    await strat.on_features("QQQ", features)
    assert any(ev[0] == "skip_vix_halt" for ev in events)


@pytest.mark.asyncio
async def test_take_profit_audit(base_extras):
    events = []

    strat = make_strategy(base_extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()
    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")


    short_leg = SimpleNamespace(right="P", expiry_date=expiry_date, strike=360.0)
    long_leg = SimpleNamespace(right="P", expiry_date=expiry_date, strike=355.0)
    strat.position = {"entry_credit": 1.0, "short": short_leg, "long": long_leg}

    async def fake_mid(*args, **kwargs):
        return 0.4  # below TP threshold (entry * (1 - 0.5) = 0.5)

    async def dummy_close(*args, **kwargs):
        return

    strat._current_mid = fake_mid  # type: ignore
    strat._close = dummy_close  # type: ignore

    await strat._manage_position()
    assert any(ev[0] == "take_profit_triggered" for ev in events)


@pytest.mark.parametrize(
    "features, expected_trend",
    [
        # Scenario 1: UP trend (close > ma50, normal drop)
        ({"close": 101, "ma50": 100, "ma200": 90, "pct_change_1d": -0.01, "vix": 20}, "UP"),
        # Scenario 2: DOWN_OK trend (ma50 > close > ma200, normal drop)
        ({"close": 99, "ma50": 100, "ma200": 90, "pct_change_1d": -0.01, "vix": 20}, "DOWN_OK"),
        # Scenario 3: DOWN_FAST due to pct_change_1d
        ({"close": 101, "ma50": 100, "ma200": 90, "pct_change_1d": -2.1, "vix": 20}, "DOWN_FAST"),
        # Scenario 4: DOWN_FAST due to close <= ma200
        ({"close": 89, "ma50": 100, "ma200": 90, "pct_change_1d": -0.01, "vix": 20}, "DOWN_FAST"),
        # Scenario 5: DOWN_FAST due to VIX halt
        ({"close": 101, "ma50": 100, "ma200": 90, "pct_change_1d": -0.01, "vix": 40.0}, "DOWN_FAST"),
    ],
)
@pytest.mark.asyncio
async def test_trend_classification(base_extras, features, expected_trend):
    extras = dict(base_extras)
    extras["vix_halt"] = 30.0  # from strategy default
    strat = make_strategy(extras)
    await strat.initialize()

    price = features.get("close")
    ma50 = features.get("ma50")
    ma100 = features.get("ma100")
    ma200 = features.get("ma200")

    trend = strat._classify_trend(price, ma50, ma100, ma200, features)
    assert trend == expected_trend


@pytest.mark.asyncio
async def test_spread_selector_case_A_up_trend(base_extras, monkeypatch):
    # 1. Setup strategy
    extras = dict(base_extras)
    extras["otm_pct_up"] = 0.10  # 10% OTM for easy math
    extras["min_credit"] = 0.2
    extras["width_candidates"] = [5.0]
    extras["min_open_interest"] = 50
    extras["max_spread_bps"] = 800  # 8%
    extras["target_delta"] = -0.25
    strat = make_strategy(extras)
    await strat.initialize()

    price = 400.0
    trend = "UP"
    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")

    # 2. Prepare stub chain
    chain = [
        # Valid short leg candidate, best delta (-0.25)
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0, right="P", bid=1.49, ask=1.51, delta=-0.25, open_interest=100), # mid=1.5, spread_bps=133
        # Valid short leg candidate, worse delta
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=359.0, right="P", bid=1.39, ask=1.41, delta=-0.22, open_interest=100), # mid=1.4, spread_bps=142
        # Invalid short leg candidate (strike too high)
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=361.0, right="P", bid=1.59, ask=1.61, delta=-0.28, open_interest=100),

        # Valid long leg, matches width=5.0 for 360 strike
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0, right="P", bid=0.99, ask=1.01, delta=-0.15, open_interest=100), # mid=1.0

        # Another potential long leg for a different short candidate (359 strike)
        # but let's make it have high spread_bps to test filtering
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=354.0, right="P", bid=0.85, ask=0.95, delta=-0.14, open_interest=100), # mid=0.9, spread_bps=1111 > max_spread_bps

        # Another potential long leg, but with low OI
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=353.0, right="P", bid=0.79, ask=0.81, delta=-0.13, open_interest=40), # OI < min_open_interest
    ]

    # 3. Call _select_legs and assert
    legs = strat._select_legs(chain, price, ma100=None, trend=trend)
    assert legs is not None
    short_leg, long_leg, spread_used = legs

    assert short_leg.strike == 360.0
    assert long_leg.strike == 355.0
    assert spread_used == 5.0

    # check credit > min_credit
    credit = strat._spread_mid(short_leg, long_leg) # 1.5 - 1.0 = 0.5
    assert credit > strat.min_credit # 0.5 > 0.2


@pytest.mark.asyncio
async def test_spread_selector_case_B_down_ok_trend(base_extras, monkeypatch):
    # 1. Setup strategy
    extras = dict(base_extras)
    extras["otm_pct_down"] = 0.05
    extras["width_candidates"] = [5.0, 10.0]
    extras["target_delta"] = -0.15
    strat = make_strategy(extras)
    await strat.initialize()

    price = 390.0
    ma100 = 380.0
    trend = "DOWN_OK"
    # max_short_strike = min(390 * (1 - 0.05), 380) = 370.5

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")

    # 2. Prepare stub chain
    chain = [
        # Short leg candidate, delta is -0.15
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=370.0, right="P", bid=2.0, ask=2.1, delta=-0.15, open_interest=100),

        # Long leg for width 5.0 (strike 365) -> credit ~0.5
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=365.0, right="P", bid=1.5, ask=1.6, delta=-0.10, open_interest=100),

        # Long leg for width 10.0 (strike 360) -> credit ~0.9 (BEST)
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0, right="P", bid=1.1, ask=1.2, delta=-0.08, open_interest=100),
    ]

    # 3. Call _select_legs and assert
    legs = strat._select_legs(chain, price, ma100=ma100, trend=trend)
    assert legs is not None
    short_leg, long_leg, spread_used = legs

    assert short_leg.strike == 370.0
    assert long_leg.strike == 360.0
    assert spread_used == 10.0

    credit = strat._spread_mid(short_leg, long_leg)
    assert credit == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_spread_selector_case_C_fallback(base_extras, monkeypatch):
    # 1. Setup strategy
    extras = dict(base_extras)
    extras["width_candidates"] = [5.0, 10.0] # 5.0 will fail
    extras["target_delta"] = -0.15
    extras["otm_pct_up"] = 0.10
    strat = make_strategy(extras)
    await strat.initialize()

    price = 390.0
    trend = "UP" # max_short_strike = 390 * 0.9 = 351
    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")

    # 2. Prepare stub chain
    chain = [
        # Short leg candidate
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=350.0, right="P", bid=2.0, ask=2.1, delta=-0.15, open_interest=100),

        # Long leg for width 5.0 (strike 345) - BUT low OI
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=345.0, right="P", bid=1.5, ask=1.6, delta=-0.10, open_interest=1), # min OI is 100 in strategy

        # Long leg for width 10.0 (strike 340) - This one is valid
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=340.0, right="P", bid=1.1, ask=1.2, delta=-0.08, open_interest=100),
    ]

    # 3. Call _select_legs and assert
    legs = strat._select_legs(chain, price, ma100=None, trend=trend)
    assert legs is not None
    short_leg, long_leg, spread_used = legs

    assert short_leg.strike == 350.0
    assert long_leg.strike == 340.0
    assert spread_used == 10.0

@pytest.mark.asyncio
async def test_spread_selector_case_C_no_legs(base_extras, monkeypatch):
    strat = make_strategy(base_extras)
    await strat.initialize()
    price = 390.0
    trend = "UP"
    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    chain = [
        OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=370.0, right="P", bid=2.0, ask=2.1, delta=-0.15, open_interest=1),
    ]
    legs = strat._select_legs(chain, price, ma100=None, trend=trend)
    assert legs is None


@pytest.mark.asyncio
async def test_margin_guard(base_extras, monkeypatch):
    extras = dict(base_extras)
    extras["max_margin_usd"] = 10
    strat = make_strategy(extras)
    await strat.initialize()

    events = []
    strat._audit_event = lambda event, **payload: events.append((event, payload))

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    short_leg = OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0, right="P", bid=1.49, ask=1.51, delta=-0.25, open_interest=100)
    long_leg = OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0, right="P", bid=0.99, ask=1.01, delta=-0.15, open_interest=100)
    
    async def fake_fetch_chain(*args, **kwargs):
        return [short_leg, long_leg]
    
    def fake_select_legs(*args, **kwargs):
        return short_leg, long_leg, 5.0

    monkeypatch.setattr(strat, "_fetch_chain", fake_fetch_chain)
    monkeypatch.setattr(strat, "_select_legs", fake_select_legs)
    
    features = {"close": 400.0, "ma50": 390.0}
    await strat._try_enter(features)

    assert any(e[0] == "skip_margin_guard" for e in events)


@pytest.mark.asyncio
async def test_available_funds_guard(base_extras, monkeypatch):
    extras = dict(base_extras)
    extras["provider"] = {"backend": "ibkr"}
    strat = make_strategy(extras)
    await strat.initialize()

    events = []
    strat._audit_event = lambda event, **payload: events.append((event, payload))

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    short_leg = OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0, right="P", bid=1.49, ask=1.51, delta=-0.25, open_interest=100)
    long_leg = OptionContract(symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0, right="P", bid=0.99, ask=1.01, delta=-0.15, open_interest=100)
    
    async def fake_fetch_chain(*args, **kwargs):
        return [short_leg, long_leg]
    
    def fake_select_legs(*args, **kwargs):
        return short_leg, long_leg, 5.0

    monkeypatch.setattr(strat, "_fetch_chain", fake_fetch_chain)
    monkeypatch.setattr(strat, "_select_legs", fake_select_legs)

    class MockAccountService:
        async def available_funds(self):
            return 20.0

    monkeypatch.setattr(bull_put_module, "get_account_service", MockAccountService)

    features = {"close": 400.0, "ma50": 390.0}
    await strat._try_enter(features)

    assert any(e[0] == "skip_available_funds_ib" for e in events)


@pytest.mark.asyncio
async def test_stop_loss_audit(base_extras, monkeypatch):
    events = []
    close_called = False

    strat = make_strategy(base_extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    short_leg = SimpleNamespace(right="P", expiry_date=expiry_date, strike=360.0)
    long_leg = SimpleNamespace(right="P", expiry_date=expiry_date, strike=355.0)
    strat.position = {"entry_credit": 1.0, "short": short_leg, "long": long_leg}

    # SL trigger is entry * (1 + stop_loss_pct) = 1.0 * (1 + 2.0) = 3.0
    async def fake_mid(*args, **kwargs):
        return 3.1

    async def dummy_close(limit_debit: float):
        nonlocal close_called
        close_called = True
        # In the real _close, this event would be audited.
        strat._audit_event("close_submitted", result={}, limit_debit=limit_debit)

    monkeypatch.setattr(strat, "_current_mid", fake_mid)
    monkeypatch.setattr(strat, "_close", dummy_close)

    await strat._manage_position()

    assert any(ev[0] == "stop_loss_triggered" for ev in events)
    assert close_called
    assert any(ev[0] == "close_submitted" for ev in events)


@pytest.mark.asyncio
async def test_skip_outside_trade_window(base_extras):
    """Test that strategy skips execution outside trade window."""
    extras = dict(base_extras)
    extras["trade_window_start_et"] = "10:00"
    extras["trade_window_end_et"] = "15:00"
    events = []

    strat = make_strategy(extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()

    # Mock time to be outside window (e.g., 9:00 AM)
    from datetime import time as time_et
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

    # Override _within_trade_window to return False
    strat._within_trade_window = lambda: False

    features = {"close": 400.0, "ma50": 390.0, "vix": 20.0}
    await strat.on_features("QQQ", features)

    # Should not have tried to enter (no events should be logged)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_skip_stale_features(base_extras):
    """Test that strategy skips when features are too old."""
    extras = dict(base_extras)
    extras["max_feature_age_sec"] = 60  # 1 minute max
    events = []

    strat = make_strategy(extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()

    import time
    stale_ts_us = int((time.time() - 120) * 1e6)  # 2 minutes old
    features = {
        "close": 400.0,
        "ma50": 390.0,
        "vix": 20.0,
        "ts_bar_end_us": stale_ts_us,
    }
    await strat.on_features("QQQ", features)

    assert any(ev[0] == "skip_stale_features" for ev in events)


@pytest.mark.asyncio
async def test_skip_down_fast_trend(base_extras):
    """Test that strategy skips entry when trend is DOWN_FAST."""
    events = []
    strat = make_strategy(base_extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()

    features = {
        "close": 380.0,
        "ma50": 390.0,
        "ma100": 395.0,
        "ma200": 400.0,  # price below ma200 -> DOWN_FAST
        "pct_change_1d": -0.5,
        "vix": 20.0,
    }
    await strat._try_enter(features)

    assert any(ev[0] == "skip_down_fast" for ev in events)


@pytest.mark.asyncio
async def test_skip_low_credit(base_extras, monkeypatch):
    """Test that strategy rejects spread when credit is below minimum."""
    extras = dict(base_extras)
    extras["min_credit"] = 0.5
    events = []

    strat = make_strategy(extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")

    # Create a spread with low credit (0.1)
    short_leg = OptionContract(
        symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0,
        right="P", bid=1.09, ask=1.11, delta=-0.25, open_interest=100
    )
    long_leg = OptionContract(
        symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0,
        right="P", bid=0.99, ask=1.01, delta=-0.15, open_interest=100
    )
    chain = [short_leg, long_leg]

    async def fake_fetch_chain(*args, **kwargs):
        return chain

    monkeypatch.setattr(strat, "_fetch_chain", fake_fetch_chain)

    features = {"close": 400.0, "ma50": 390.0}
    await strat._try_enter(features)

    assert any(ev[0] == "skip_low_credit" for ev in events)


@pytest.mark.asyncio
async def test_feature_price_extraction(base_extras):
    """Test that _feature_price extracts price from various field names."""
    strat = make_strategy(base_extras)
    await strat.initialize()

    # Test different price field names
    assert strat._feature_price({"close": 100.0}) == 100.0
    assert strat._feature_price({"last": 101.0}) == 101.0
    assert strat._feature_price({"price": 102.0}) == 102.0
    assert strat._feature_price({"mid": 103.0}) == 103.0
    assert strat._feature_price({"other": 99.0}) is None
    assert strat._feature_price({}) is None


@pytest.mark.asyncio
async def test_unknown_trend_when_missing_ma(base_extras):
    """Test that _classify_trend returns UNKNOWN when price or ma50 is None."""
    strat = make_strategy(base_extras)
    await strat.initialize()

    features = {"vix": 20.0, "pct_change_1d": -0.5}

    trend = strat._classify_trend(None, 100.0, 90.0, 80.0, features)
    assert trend == "UNKNOWN"

    trend = strat._classify_trend(100.0, None, 90.0, 80.0, features)
    assert trend == "UNKNOWN"


@pytest.mark.asyncio
async def test_scan_interval_throttling(base_extras):
    """Test that strategy skips scans when called too frequently."""
    extras = dict(base_extras)
    extras["scan_interval_sec"] = 300  # 5 minutes
    events = []

    strat = make_strategy(extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()

    import time
    strat.last_scan_ts = time.time()  # Set to now

    features = {"close": 400.0, "ma50": 390.0, "vix": 20.0}

    # First call should be skipped (too soon)
    await strat.on_features("QQQ", features)
    assert len(events) == 0

    # Move time forward
    strat.last_scan_ts = time.time() - 301  # 301 seconds ago
    await strat.on_features("QQQ", features)
    # Now it should process (will fail at fetch_chain but that's ok)


@pytest.mark.asyncio
async def test_spread_mid_with_invalid_prices(base_extras):
    """Test that _spread_mid handles zero/negative prices."""
    strat = make_strategy(base_extras)
    await strat.initialize()

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")

    short_leg = OptionContract(
        symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0,
        right="P", bid=0.0, ask=0.0, delta=-0.25, open_interest=100
    )
    long_leg = OptionContract(
        symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0,
        right="P", bid=0.99, ask=1.01, delta=-0.15, open_interest=100
    )

    credit = strat._spread_mid(short_leg, long_leg)
    assert credit is None


@pytest.mark.asyncio
async def test_manage_position_no_mid_price(base_extras, monkeypatch):
    """Test that _manage_position handles missing mid price gracefully."""
    strat = make_strategy(base_extras)
    await strat.initialize()

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    short_leg = SimpleNamespace(right="P", expiry_date=expiry_date, strike=360.0)
    long_leg = SimpleNamespace(right="P", expiry_date=expiry_date, strike=355.0)
    strat.position = {"entry_credit": 1.0, "short": short_leg, "long": long_leg}

    async def fake_mid(*args, **kwargs):
        return None

    monkeypatch.setattr(strat, "_current_mid", fake_mid)

    # Should not crash, just log warning and return
    await strat._manage_position()

    # Position should still exist (not closed)
    assert strat.position is not None


@pytest.mark.asyncio
async def test_skip_no_chain(base_extras, monkeypatch):
    """Test that strategy skips when option chain is empty."""
    events = []
    strat = make_strategy(base_extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()

    async def fake_fetch_chain(*args, **kwargs):
        return []

    monkeypatch.setattr(strat, "_fetch_chain", fake_fetch_chain)

    features = {"close": 400.0, "ma50": 390.0}
    await strat._try_enter(features)

    assert any(ev[0] == "skip_no_chain" for ev in events)


@pytest.mark.asyncio
async def test_skip_no_candidate_legs(base_extras, monkeypatch):
    """Test that strategy skips when no valid candidate legs found."""
    events = []
    strat = make_strategy(base_extras)
    strat._audit_event = lambda event, **payload: events.append((event, payload))
    await strat.initialize()

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    # Create a chain with only high OI requirement violations
    chain = [
        OptionContract(
            symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0,
            right="P", bid=1.49, ask=1.51, delta=-0.25, open_interest=10  # Below min_oi
        )
    ]

    async def fake_fetch_chain(*args, **kwargs):
        return chain

    monkeypatch.setattr(strat, "_fetch_chain", fake_fetch_chain)

    features = {"close": 400.0, "ma50": 390.0}
    await strat._try_enter(features)

    assert any(ev[0] == "skip_no_candidate" for ev in events)


@pytest.mark.asyncio
async def test_select_legs_filters_high_spread_bps(base_extras):
    """Test that _select_legs filters out options with high spread_bps."""
    strat = make_strategy(base_extras)
    await strat.initialize()

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    chain = [
        # Valid short leg
        OptionContract(
            symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0,
            right="P", bid=1.49, ask=1.51, delta=-0.25, open_interest=100
        ),
        # Long leg with excessive spread_bps (>1000)
        OptionContract(
            symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0,
            right="P", bid=0.50, ask=1.50, delta=-0.15, open_interest=100  # spread_bps = 1000
        ),
    ]

    legs = strat._select_legs(chain, 400.0, None, "UP")
    # Should not find valid legs due to high spread_bps
    assert legs is None


@pytest.mark.asyncio
async def test_select_legs_filters_low_oi(base_extras):
    """Test that _select_legs filters out options with low open interest."""
    extras = dict(base_extras)
    extras["min_open_interest"] = 500
    strat = make_strategy(extras)
    await strat.initialize()

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    chain = [
        OptionContract(
            symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0,
            right="P", bid=1.49, ask=1.51, delta=-0.25, open_interest=100  # Below min
        ),
        OptionContract(
            symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0,
            right="P", bid=0.99, ask=1.01, delta=-0.15, open_interest=100  # Below min
        ),
    ]

    legs = strat._select_legs(chain, 400.0, None, "UP")
    assert legs is None


@pytest.mark.asyncio
async def test_select_legs_filters_out_of_dte_range(base_extras):
    """Test that _select_legs filters out options outside DTE range."""
    extras = dict(base_extras)
    extras["min_dte"] = 30
    extras["max_dte"] = 45
    strat = make_strategy(extras)
    await strat.initialize()

    # Create option with DTE = 7 (too short)
    expiry_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    chain = [
        OptionContract(
            symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0,
            right="P", bid=1.49, ask=1.51, delta=-0.25, open_interest=100
        ),
        OptionContract(
            symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0,
            right="P", bid=0.99, ask=1.01, delta=-0.15, open_interest=100
        ),
    ]

    legs = strat._select_legs(chain, 400.0, None, "UP")
    assert legs is None


@pytest.mark.asyncio
async def test_manage_position_no_position(base_extras):
    """Test that _manage_position returns early when no position exists."""
    strat = make_strategy(base_extras)
    await strat.initialize()

    # No position set
    strat.position = None

    # Should not crash
    await strat._manage_position()


@pytest.mark.asyncio
async def test_current_mid_no_matching_contracts(base_extras, monkeypatch):
    """Test that _current_mid returns None when contracts don't match."""
    strat = make_strategy(base_extras)
    await strat.initialize()

    expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
    short_leg = OptionContract(
        symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=360.0,
        right="P", bid=1.49, ask=1.51, delta=-0.25, open_interest=100
    )
    long_leg = OptionContract(
        symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=355.0,
        right="P", bid=0.99, ask=1.01, delta=-0.15, open_interest=100
    )

    # Return a chain with different strikes
    different_chain = [
        OptionContract(
            symbol="...", underlying="QQQ", expiry_date=expiry_date, strike=370.0,
            right="P", bid=2.0, ask=2.1, delta=-0.30, open_interest=100
        )
    ]

    async def fake_fetch_chain(*args, **kwargs):
        return different_chain

    monkeypatch.setattr(strat, "_fetch_chain", fake_fetch_chain)

    mid = await strat._current_mid(short_leg, long_leg)
    assert mid is None


@pytest.mark.asyncio
async def test_parse_et_invalid_time_format(base_extras):
    """Test that _parse_et returns default when given invalid format."""
    strat = make_strategy(base_extras)
    await strat.initialize()

    from datetime import time as time_et
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

    # Invalid format should return default (10:00)
    result = strat._parse_et("invalid")
    assert result == time_et(hour=10, minute=0, tzinfo=ET)

    # Valid format should work
    result = strat._parse_et("14:30")
    assert result == time_et(hour=14, minute=30, tzinfo=ET)
