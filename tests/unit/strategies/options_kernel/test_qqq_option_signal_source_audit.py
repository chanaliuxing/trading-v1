from datetime import datetime

import pytest

from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin
from strategies.qqq_stock_signal_artifact import clear_stock_entry_signal_artifacts, publish_stock_entry_signal


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def info(self, event: str, **kwargs):
        self.records.append((event, kwargs))


class _Kernel:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def _emit_strategy_audit(self, **kwargs) -> None:
        self.records.append(dict(kwargs))


@pytest.fixture(autouse=True)
def _signal_backend_memory(monkeypatch):
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "memory")


def _runtime(now: datetime) -> PluginRuntimeState:
    features = {
        "v3m": 0.8,
        "vwap": 500.0,
        "atr_live": 1.0,
        "atr_seed": 1.0,
        "atr_eff": 1.0,
        "vol_surge_ratio": 1.2,
        "distance_from_vwap_atr": 0.15,
        "qqew_now": 99.0,
        "qqew_vwap": 98.9,
        "qqew_decoupled": True,
        "close_by_minute": {now.replace(second=0, microsecond=0): 500.1},
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


def test_signal_source_audit_fields_present_on_live_signal() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 40, 0)
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

    _ = plugin.build_signal_pure(_runtime(now))

    heartbeat = [p for e, p in log.records if e == "qqq_intraday_option_minute_heartbeat"][-1]
    assert heartbeat["signal_source_mode"] == "upstream_stock_signal"
    assert heartbeat["signal_source_status"] == "live"
    assert heartbeat["signal_source_strategy_id"] == "qqq_intraday_001"
    assert heartbeat["signal_source_age_sec"] == 0.0
    assert heartbeat["signal_source_latency_ms"] == 0.0
    assert heartbeat["signal_source_publish_stage"] == ""


def test_signal_source_status_is_written_to_structured_audit_when_heartbeat_disabled() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 40, 0)
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
            "minute_heartbeat_log_enabled": False,
        }
    )
    kernel = _Kernel()
    plugin._pure_log = _Log()
    plugin._pure_kernel = kernel
    plugin._pure_strategy_id = "qqq_intraday_option_v1_001"
    plugin._pure_underlying = "QQQ"

    signal = plugin.build_signal_pure(_runtime(now))

    assert signal is not None
    rows = [row for row in kernel.records if row.get("stage") == "plugin_signal_source_observability"]
    assert rows
    assert rows[-1]["status"] == "signal"
    assert rows[-1]["reason"] == "signal_ready"
    assert rows[-1]["payload"]["signal_source_status"] == "live"
    assert rows[-1]["payload"]["signal_source_strategy_id"] == "qqq_intraday_001"
