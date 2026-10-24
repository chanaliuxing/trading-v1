from datetime import datetime, timedelta

import pytest

from strategies.options_kernel.types import KernelBarContext, KernelPositionState, PluginRuntimeState
from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin
from strategies.qqq_stock_signal_artifact import clear_stock_entry_signal_artifacts, publish_stock_entry_signal


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def info(self, event: str, **kwargs):
        self.records.append((event, kwargs))


@pytest.fixture(autouse=True)
def _signal_backend_memory(monkeypatch):
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "memory")


def _runtime(now: datetime) -> PluginRuntimeState:
    features = {
        "v3m": 0.6,
        "vwap": 500.0,
        "atr_live": 1.0,
        "atr_seed": 1.0,
        "atr_eff": 1.0,
        "vol_surge_ratio": 1.2,
        "distance_from_vwap_atr": 0.1,
        "qqew_now": 99.0,
        "qqew_vwap": 98.9,
        "qqew_decoupled": True,
        "close_by_minute": {
            now.replace(second=0, microsecond=0): 500.1,
        },
    }
    return PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=500.1,
        bar=KernelBarContext(
            ts=now,
            symbol="QQQ",
            open=500.0,
            high=500.3,
            low=499.8,
            close=500.1,
            volume=1500.0,
            vwap=500.0,
            atr=1.0,
            payload=features,
        ),
        features=features,
        payload={},
    )


def test_upstream_stock_signal_source_builds_option_signal() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 10, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
        decision_id="d1",
        trigger_id="t1",
        payload={"publish_stage": "shared_entry_candidate"},
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
            "signal_source_max_signal_age_sec": 5.0,
        }
    )
    runtime = _runtime(now)
    signal = plugin.build_signal_pure(runtime)
    assert signal is not None
    assert signal.side == "CALL"
    assert signal.payload["signal_source"] == "upstream_stock_signal"
    assert signal.payload["source_decision_id"] == "d1"
    assert signal.payload["source_publish_stage"] == "shared_entry_candidate"
    assert float(signal.payload["source_latency_ms"]) >= 0.0


def test_upstream_source_does_not_depend_on_replay_engine(monkeypatch) -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 10, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
            "signal_source_max_signal_age_sec": 5.0,
        }
    )
    monkeypatch.setattr("strategies.options_plugins.qqq_intraday_option.QQQIntradayRuleEngine.evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should_not_call")))
    signal = plugin.build_signal_pure(_runtime(now))
    assert signal is not None
    assert signal.side == "CALL"


def test_upstream_live_signal_does_not_emit_entry_block() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 10, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
            "minute_heartbeat_log_enabled": True,
        }
    )
    log = _Log()
    plugin._pure_log = log
    plugin._pure_strategy_id = "qqq_intraday_option_v1_001"
    plugin._pure_underlying = "QQQ"
    signal = plugin.build_signal_pure(_runtime(now))
    assert signal is not None
    block_rows = [payload for event, payload in log.records if event == "qqq_intraday_option_entry_block"]
    assert not block_rows


def test_upstream_stock_signal_source_blocks_stale_signal() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 10, 20)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now - timedelta(seconds=30),
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
            "signal_source_max_signal_age_sec": 5.0,
        }
    )
    signal = plugin.build_signal_pure(_runtime(now))
    assert signal is None


def test_heartbeat_emits_signal_source_audit_fields(monkeypatch) -> None:
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "memory")
    now = datetime(2026, 4, 8, 10, 11, 0)
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
            "minute_heartbeat_log_enabled": True,
        }
    )
    log = _Log()
    plugin._pure_log = log
    plugin._pure_strategy_id = "qqq_intraday_option_v1_001"
    plugin._pure_underlying = "QQQ"

    _ = plugin.build_signal_pure(_runtime(now))

    heartbeat_rows = [payload for event, payload in log.records if event == "qqq_intraday_option_minute_heartbeat"]
    assert heartbeat_rows
    row = heartbeat_rows[-1]
    assert "signal_source_mode" in row
    assert "signal_source_status" in row
    assert "signal_source_age_sec" in row
    assert "signal_source_latency_ms" in row
    assert "signal_source_publish_stage" in row
    assert "signal_source_strategy_id" in row
    assert "signal_plane_backend_config" in row
    assert "signal_plane_state" in row
    assert "signal_plane_using_redis" in row
    assert "signal_plane_redis_ok" in row
    assert "signal_plane_error_streak" in row
    assert "signal_plane_retry_in_sec" in row
    assert row["signal_source_status"] == "idle"
    block_rows = [payload for event, payload in log.records if event == "qqq_intraday_option_entry_block"]
    assert not block_rows


def test_upstream_stock_signal_is_suppressed_when_active_position_exists() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 12, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )
    pos = KernelPositionState(
        strategy_id="qqq_intraday_option_v1_001",
        position_lot_id="lot1",
        opt_symbol="OPT:QQQ:20260408:500:C",
        side="CALL",
        qty=1.0,
        remaining_qty=1.0,
        opened_at=now,
    )
    rt = _runtime(now)
    rt = PluginRuntimeState(
        strategy_id=rt.strategy_id,
        kernel_mode=rt.kernel_mode,
        symbol=rt.symbol,
        now=rt.now,
        wall_now=rt.wall_now,
        spot=rt.spot,
        bar=rt.bar,
        features=rt.features,
        active_position=pos,
        active_positions=[pos],
        payload=rt.payload,
    )
    signal = plugin.build_signal_pure(rt)
    assert signal is None
