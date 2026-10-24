from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from libs.strategy import StrategyContext, StrategyParams
from strategies.qqq_intraday_strategy import QQQIntradayStrategy


TEST_TZ = ZoneInfo("America/New_York")


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


def _make_strategy() -> QQQIntradayStrategy:
    ctx = MagicMock(spec=StrategyContext)
    ctx.log = _DummyLogger()
    ctx.metrics = lambda *args, **kwargs: None
    ctx.strategy_id = "qqq_intraday_001"
    ctx.position = AsyncMock(return_value=None)
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    ctx.engine = MagicMock()
    ctx.engine.decision_pipeline = object()
    ctx.engine.market_data_service = MagicMock()
    ctx.engine.market_data_service.ib_adapter = MagicMock()
    ctx.engine.market_data_service.ib_adapter.is_connected.return_value = True

    params = StrategyParams()
    params.extras = {
        "all_day_test_mode": False,
        "stable_entry_guard_enabled": True,
        "first_entry_require_seed_ready": True,
        "first_entry_seed_min_samples": 5,
        "startup_anchor_entry_block_enabled": True,
        "startup_anchor_entry_block_start": "09:30:00",
        "startup_anchor_entry_block_end": "10:00:00",
        "restart_degrade_enabled": True,
        "restart_degrade_default_until": "10:00:00",
        "restart_degrade_allow_early_unlock": True,
        "fail_closed_on_data_integrity": True,
        "entry_require_continuity_180s": False,
        "entry_max_data_age_sec_qqq": 15.0,
    }
    strat = QQQIntradayStrategy(ctx, params)
    strat._day = "2026-03-05"
    strat._running = True
    strat._is_traded = False
    return strat


def test_stable_guard_blocks_first_entry_when_seed_not_ready():
    strat = _make_strategy()
    now = datetime(2026, 3, 5, 9, 45, tzinfo=TEST_TZ)
    strat.params.extras["fail_closed_on_data_integrity"] = False
    strat._stream_invalid = False
    strat._last_seen_ts_ms["QQQ"] = int((now - timedelta(seconds=1)).timestamp() * 1000)
    strat._seed_ready = False
    strat._seed_velocity_samples = []

    reason = strat._stable_entry_block_reason(now, strat.params.extras)
    assert reason == "first_entry_seed_not_ready"


def test_stable_guard_blocks_startup_anchor_in_opening_window():
    strat = _make_strategy()
    now = datetime(2026, 3, 5, 9, 40, tzinfo=TEST_TZ)
    strat.params.extras["first_entry_require_seed_ready"] = False
    strat.params.extras["fail_closed_on_data_integrity"] = False
    strat.params.extras["restart_degrade_enabled"] = False
    strat._price_base_source = "startup_anchor_10s"

    reason = strat._stable_entry_block_reason(now, strat.params.extras)
    assert reason == "startup_anchor_embargo"


def test_stable_guard_allows_startup_anchor_after_window():
    strat = _make_strategy()
    now = datetime(2026, 3, 5, 10, 1, tzinfo=TEST_TZ)
    strat.params.extras["first_entry_require_seed_ready"] = False
    strat.params.extras["fail_closed_on_data_integrity"] = False
    strat.params.extras["restart_degrade_enabled"] = False
    strat._price_base_source = "startup_anchor_10s"

    reason = strat._stable_entry_block_reason(now, strat.params.extras)
    assert reason is None


def test_stable_guard_fail_closed_on_data_integrity():
    strat = _make_strategy()
    now = datetime(2026, 3, 5, 9, 50, tzinfo=TEST_TZ)
    strat.params.extras["first_entry_require_seed_ready"] = False
    strat._stream_invalid = True

    reason = strat._stable_entry_block_reason(now, strat.params.extras)
    assert reason == "data_integrity_fail_closed"


def test_restart_degrade_blocks_then_early_unlocks():
    strat = _make_strategy()
    now = datetime(2026, 3, 5, 9, 40, tzinfo=TEST_TZ)
    strat.params.extras["first_entry_require_seed_ready"] = False
    strat.params.extras["fail_closed_on_data_integrity"] = False
    strat._no_new_entries_until_et = datetime(2026, 3, 5, 10, 0, tzinfo=TEST_TZ)
    strat._seed_ready = False
    strat._seed_velocity_samples = []

    reason1 = strat._stable_entry_block_reason(now, strat.params.extras)
    assert reason1 == "restart_degrade_active"

    strat._seed_ready = True
    strat._seed_velocity_samples = [0.1] * 6
    strat._price_base_source = "sfs_0930_window"
    reason2 = strat._stable_entry_block_reason(now, strat.params.extras)
    assert reason2 is None
    assert strat._no_new_entries_until_et is None
    assert strat._restart_degrade_unlock_reason == "early_unlock_seed_ready"


def test_stable_guard_blocks_entry_when_decision_pipeline_unavailable():
    strat = _make_strategy()
    now = datetime(2026, 3, 5, 10, 5, tzinfo=TEST_TZ)
    strat.params.extras["first_entry_require_seed_ready"] = False
    strat.params.extras["fail_closed_on_data_integrity"] = False
    strat.params.extras["restart_degrade_enabled"] = False
    strat.ctx.engine.decision_pipeline = None

    reason = strat._stable_entry_block_reason(now, strat.params.extras)

    assert reason == "decision_pipeline_unavailable"


def test_stable_guard_blocks_entry_when_market_data_ib_disconnected():
    strat = _make_strategy()
    now = datetime(2026, 3, 5, 10, 5, tzinfo=TEST_TZ)
    strat.params.extras["first_entry_require_seed_ready"] = False
    strat.params.extras["fail_closed_on_data_integrity"] = False
    strat.params.extras["restart_degrade_enabled"] = False
    strat.ctx.engine.decision_pipeline = object()
    strat.ctx.engine.market_data_service = MagicMock()
    strat.ctx.engine.market_data_service.ib_adapter = MagicMock()
    strat.ctx.engine.market_data_service.ib_adapter.is_connected.return_value = False

    reason = strat._stable_entry_block_reason(now, strat.params.extras)

    assert reason == "market_data_unavailable"


def test_stable_guard_blocks_entry_when_market_data_service_missing():
    strat = _make_strategy()
    now = datetime(2026, 3, 5, 10, 5, tzinfo=TEST_TZ)
    strat.params.extras["first_entry_require_seed_ready"] = False
    strat.params.extras["fail_closed_on_data_integrity"] = False
    strat.params.extras["restart_degrade_enabled"] = False
    strat.ctx.engine.decision_pipeline = object()
    strat.ctx.engine.market_data_service = None

    reason = strat._stable_entry_block_reason(now, strat.params.extras)

    assert reason == "market_data_unavailable"
