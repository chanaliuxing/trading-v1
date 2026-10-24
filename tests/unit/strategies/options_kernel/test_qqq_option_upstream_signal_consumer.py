from datetime import datetime

import pytest

from strategies.options_kernel.types import KernelBarContext, KernelPositionState, PluginRuntimeState, Selection
from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin
from strategies.qqq_stock_signal_artifact import (
    clear_stock_entry_signal_artifacts,
    get_pending_stock_entry_signal,
    get_stock_option_signal_handoff_records,
    publish_stock_entry_signal,
)


@pytest.fixture(autouse=True)
def _isolated_handoff_path(tmp_path, monkeypatch):
    monkeypatch.setenv("QQQ_STOCK_OPTION_HANDOFF_PATH", str(tmp_path / "handoff.jsonl"))
    clear_stock_entry_signal_artifacts()
    yield
    clear_stock_entry_signal_artifacts()


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def info(self, event: str, **kwargs):
        self.records.append((event, kwargs))


def _runtime(now: datetime) -> PluginRuntimeState:
    features = {
        "v3m": 0.7,
        "vwap": 500.0,
        "atr_live": 1.0,
        "atr_seed": 1.0,
        "atr_eff": 1.0,
        "vol_surge_ratio": 1.2,
        "vol_t": 1500.0,
        "vol_ma10": 1200.0,
        "distance_from_vwap_atr": 0.1,
        "qqew_now": 99.0,
        "qqew_vwap_now": 98.9,
        "qqew_decoupled": True,
        "data_age_qqq_sec": 0.0,
        "data_age_qqew_sec": 0.0,
        "continuity_180s_ok": True,
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


def test_upstream_signal_allows_entry_plan_and_shadow_replay_blocks_entry_plan() -> None:
    clear_stock_entry_signal_artifacts(forget_loaded=True)
    now = datetime(2026, 4, 9, 10, 5, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    upstream = QQQIntradayOptionPlugin()
    upstream.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )
    rt = _runtime(now)
    sig = upstream.build_signal_pure(rt)
    assert sig is not None
    assert upstream.build_entry_plan_pure(rt, sig) is not None

    replay = QQQIntradayOptionPlugin()
    replay.apply_default_extras({"signal_source_mode": "independent_rule_replay"})
    replay_sig = replay.build_signal_pure(rt)
    if replay_sig is not None:
        assert replay.build_entry_plan_pure(rt, replay_sig) is None


def test_upstream_signal_defaults_use_fast_polling_and_short_signal_window() -> None:
    plugin = QQQIntradayOptionPlugin()
    extras = plugin.apply_default_extras({})
    assert extras["source_bar_sec"] == 5
    assert extras["signal_source_max_signal_age_sec"] == 10.0


def test_upstream_signal_is_consumed_only_after_submit_success() -> None:
    clear_stock_entry_signal_artifacts(forget_loaded=True)
    now = datetime(2026, 4, 9, 10, 7, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
        trigger_id="qqq-trigger-1",
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )
    first = plugin.build_signal_pure(_runtime(now))
    second = plugin.build_signal_pure(_runtime(now))
    assert first is not None
    assert second is not None

    plugin.on_entry_submitted_pure(
        runtime=_runtime(now),
        signal=first,
        entry_plan=type("P", (), {"side": "CALL", "signal_kind": "x"})(),
        selection=Selection(
            opt_symbol="OPT:QQQ:20260409:500:C",
            expiry="2026-04-09",
            strike=500.0,
            side="CALL",
            bid=1.0,
            ask=1.1,
            mid=1.05,
            quote_age_sec=0.5,
        ),
        position_lot_id="lot-after-submit",
    )
    # Submission approved alone should not consume trigger yet.
    third = plugin.build_signal_pure(_runtime(now))
    assert third is not None

    live_rt = _runtime(now)
    live_rt = PluginRuntimeState(
        strategy_id=live_rt.strategy_id,
        kernel_mode=live_rt.kernel_mode,
        symbol=live_rt.symbol,
        now=live_rt.now,
        wall_now=live_rt.wall_now,
        spot=live_rt.spot,
        bar=live_rt.bar,
        features=live_rt.features,
        active_position=KernelPositionState(
            strategy_id=live_rt.strategy_id,
            position_lot_id="lot-after-submit",
            opt_symbol="OPT:QQQ:20260409:500:C",
            side="CALL",
            qty=2.0,
            remaining_qty=2.0,
            opened_at=now,
            payload={
                "plugin_payload": {
                    "signal": {"payload": {"source_trigger_key": str(first.payload.get("source_trigger_key") or "")}}
                }
            },
        ),
        active_positions=[],
        payload=live_rt.payload,
    )
    assert plugin.build_signal_pure(live_rt) is None

    fourth = plugin.build_signal_pure(_runtime(now))
    assert fourth is None


def test_downstream_result_logs_include_submit_and_block_codes() -> None:
    now = datetime(2026, 4, 9, 10, 6, 0)
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )
    log = _Log()
    plugin._pure_log = log
    plugin._pure_strategy_id = "qqq_intraday_option_v1_001"
    plugin._pure_underlying = "QQQ"
    rt = _runtime(now)

    plugin.on_entry_submit_blocked_pure(
        runtime=rt,
        signal=None,
        entry_plan=type("P", (), {"side": "CALL", "signal_kind": "x"})(),
        selection=None,
        reason="no_contract_selection",
        payload={"reject_reason": "NO_QUOTES"},
    )
    plugin.on_entry_submitted_pure(
        runtime=rt,
        signal=type("S", (), {"side": "CALL"})(),
        entry_plan=type("P", (), {"side": "CALL", "signal_kind": "x"})(),
        selection=Selection(
            opt_symbol="OPT:QQQ:20260409:500:C",
            expiry="2026-04-09",
            strike=500.0,
            side="CALL",
            bid=1.0,
            ask=1.1,
            mid=1.05,
            quote_age_sec=0.5,
        ),
        position_lot_id="lot1",
    )

    rows = [payload for event, payload in log.records if event == "qqq_intraday_option_downstream_result"]
    assert {row.get("result_code") for row in rows} >= {"no_contract_selection", "entry_intent_submitted"}
    blocked = next(row for row in rows if row.get("result_code") == "no_contract_selection")
    assert blocked.get("reject_reason") == "NO_QUOTES"


def test_upstream_signal_handoff_records_terminal_states() -> None:
    clear_stock_entry_signal_artifacts(forget_loaded=True)
    now = datetime(2026, 4, 9, 10, 8, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
        trigger_id="qqq-trigger-handoff",
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )
    plugin._pure_log = _Log()
    plugin._pure_strategy_id = "qqq_intraday_option_v1_001"
    plugin._pure_underlying = "QQQ"
    signal = plugin.build_signal_pure(_runtime(now))
    assert signal is not None

    plugin.on_entry_submit_blocked_pure(
        runtime=_runtime(now),
        signal=signal,
        entry_plan=type("P", (), {"side": "CALL", "signal_kind": "x"})(),
        selection=None,
        reason="no_contract_selection",
        payload={"reject_reason": "NO_QUOTES", "reject_diagnostics": {"code": "NO_QUOTES"}},
    )
    records = get_stock_option_signal_handoff_records()
    latest = next(row for row in records if row.status == "contract_rejected")
    assert latest.trigger_id == "qqq-trigger-handoff"
    assert latest.consumer_strategy_id == "qqq_intraday_option_v1_001"
    assert latest.status == "contract_rejected"
    assert latest.terminal is True
    assert latest.payload["reject_diagnostics"]["code"] == "NO_QUOTES"


def test_pending_signal_queue_consumes_oldest_nonterminal_trigger() -> None:
    clear_stock_entry_signal_artifacts(forget_loaded=True)
    now = datetime(2026, 4, 9, 10, 9, 0)
    first = publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
        trigger_id="trigger-old",
    )
    second = publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now.replace(second=5),
        direction="SHORT",
        instrument="SQQQ",
        entry_allowed=True,
        trigger_id="trigger-new",
    )

    pending = get_pending_stock_entry_signal(
        underlying="QQQ",
        source_strategy_id="qqq_intraday_001",
    )
    assert pending == first

    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )
    signal = plugin.build_signal_pure(_runtime(now))
    assert signal is not None
    assert signal.payload["source_trigger_id"] == "trigger-old"
    plugin.on_entry_submit_blocked_pure(
        runtime=_runtime(now),
        signal=signal,
        entry_plan=type("P", (), {"side": "CALL", "signal_kind": "x"})(),
        selection=None,
        reason="no_contract_selection",
        payload={"reject_reason": "NO_QUOTES"},
    )

    pending = get_pending_stock_entry_signal(
        underlying="QQQ",
        source_strategy_id="qqq_intraday_001",
    )
    assert pending == second


def test_pending_signal_queue_recovers_nonterminal_trigger_from_jsonl(tmp_path, monkeypatch) -> None:
    handoff_path = tmp_path / "handoff.jsonl"
    monkeypatch.setenv("QQQ_STOCK_OPTION_HANDOFF_PATH", str(handoff_path))
    clear_stock_entry_signal_artifacts(forget_loaded=True)
    now = datetime(2026, 4, 9, 10, 10, 0)
    published = publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
        trigger_id="trigger-recover",
    )
    assert get_pending_stock_entry_signal(underlying="QQQ", source_strategy_id="qqq_intraday_001") == published

    clear_stock_entry_signal_artifacts(forget_loaded=True)
    recovered = get_pending_stock_entry_signal(underlying="QQQ", source_strategy_id="qqq_intraday_001")
    assert recovered == published

    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )
    signal = plugin.build_signal_pure(_runtime(now))
    assert signal is not None
    plugin.on_entry_submit_blocked_pure(
        runtime=_runtime(now),
        signal=signal,
        entry_plan=type("P", (), {"side": "CALL", "signal_kind": "x"})(),
        selection=None,
        reason="no_contract_selection",
        payload={"reject_reason": "NO_QUOTES"},
    )

    clear_stock_entry_signal_artifacts(forget_loaded=True)
    assert get_pending_stock_entry_signal(underlying="QQQ", source_strategy_id="qqq_intraday_001") is None
