import json
import asyncio
import contextlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from libs.strategy import StrategyContext, StrategyParams
import strategies.qqq_intraday_strategy as qmod
from strategies.qqq_intraday_strategy import ET, QQQIntradayStrategy
from strategies.qqq_stock_signal_artifact import clear_stock_entry_signal_artifacts, get_latest_stock_entry_signal
from strategies.shared_data import UnderlyingSharedDataPlane


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class _DummyEngine:
    pass


class _CaptureAuditWriter:
    def __init__(self):
        self.events = []

    def emit(self, **kwargs):
        self.events.append(dict(kwargs))


class _FakeSFS:
    def __init__(self, rows):
        self._rows = rows

    async def last_n(self, symbol: str, n: int = 60):
        return self._rows[:n]


class _FakeSFSRetry:
    def __init__(self, rows, fail_calls: int = 1):
        self._rows = rows
        self._fail_calls = fail_calls
        self.calls = 0

    async def last_n(self, symbol: str, n: int = 60):
        self.calls += 1
        if self.calls <= self._fail_calls:
            return []
        return self._rows[:n]


class _FakeSFSBySymbol:
    def __init__(self, rows_by_symbol):
        self._rows_by_symbol = rows_by_symbol

    async def last_n(self, symbol: str, n: int = 60):
        rows = self._rows_by_symbol.get(symbol, [])
        # Mirror Redis last_n behavior: newest row first.
        return list(reversed(rows))[:n]


def _make_strategy():
    ctx = StrategyContext(
        engine=_DummyEngine(),
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    params.extras["enable_observability_file"] = True
    return QQQIntradayStrategy(ctx, params)


def _prime_entry_allowed_state(strategy: QQQIntradayStrategy, now: datetime) -> None:
    strategy._seed_ready = True
    strategy._v_seed_sigma = 0.4
    strategy._seed_velocity_samples = [0.2, 0.3, 0.25, 0.4, 0.35, 0.32]
    strategy._orh = 610.0
    strategy._orl = 600.0
    strategy._last_stop_exit_at = None
    strategy._last_stop_exit_side = None
    strategy._atr_pre["TQQQ"] = 0.35

    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000

    for i in range(181):
        ts = now_ms - (180 - i) * 1000
        strategy._qqq_buffer.append((ts, 611.0 if i > 10 else 609.0))
        strategy._qqew_buffer.append((ts, 134.5 if i > 10 else 132.8))

    strategy.params.extras["atr_period_1m"] = 3
    strategy.params.extras["anti_chase_chop_enable"] = False
    strategy.params.extras["qty"] = 1
    strategy._minute_bars.extend(
        [
            qmod._OhlcvBar(now - qmod.timedelta(minutes=4), now - qmod.timedelta(minutes=3), 608.0, 609.0, 607.0, 608.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=3), now - qmod.timedelta(minutes=2), 608.5, 610.0, 608.0, 609.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=2), now - qmod.timedelta(minutes=1), 609.5, 610.5, 609.0, 610.2, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=1), now, 610.2, 611.0, 610.0, 610.8, 100.0),
        ]
    )
    strategy._rolling_vol_queue.extend([100.0] * 10)
    strategy._today_vol_series.extend([100.0] * 20)
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=611.0,
        high=612.0,
        low=610.8,
        close=611.0,
        volume=300.0,
    )
    strategy._vwap_pv = 609.5 * 100.0
    strategy._vwap_v = 100.0
    strategy._qqew_last_price = 134.5
    strategy._qqew_vwap_pv = 133.2 * 100.0
    strategy._qqew_vwap_v = 100.0
    strategy._amp_locked = True
    strategy._calc_qty = lambda price: 1
    strategy._exec_minute_close_for_gate = lambda symbol, minute_ts, now_dt: 49.5
    strategy._effective_atr = lambda symbol, period, now=None: 0.35


def test_observability_file_written_once_per_minute(tmp_path):
    strategy = _make_strategy()
    strategy._obs_path = tmp_path / "qqq_obs.json"
    strategy._price_base = 601.23
    strategy._price_base_source = "ibkr_1m_0930_open"

    now = datetime(2026, 2, 9, 10, 6, 1, tzinfo=ET)
    strategy._maybe_write_observability_file(now)
    assert strategy._obs_path.exists()

    first_payload = json.loads(strategy._obs_path.read_text(encoding="utf-8"))
    assert first_payload["strategy_id"] == "qqq_intraday_001"
    assert "required_values" in first_payload
    assert "missing_required" in first_payload
    assert "strategy_state" in first_payload
    assert first_payload["required_values"]["QQQ"]["price_base"] == 601.23
    assert first_payload["required_values"]["QQQ"]["price_base_source"] == "ibkr_1m_0930_open"
    assert first_payload["strategy_state"]["price_base_source"] == "ibkr_1m_0930_open"
    assert first_payload["strategy_state"]["seed_ready"] is False
    assert first_payload["strategy_state"]["seed_n"] == 0
    assert "orh" in first_payload["strategy_state"]
    assert "orl" in first_payload["strategy_state"]

    strategy._last_seen_ts_ms["QQQ"] = int(now.timestamp() * 1000)
    strategy._maybe_write_observability_file(datetime(2026, 2, 9, 10, 6, 59, tzinfo=ET))
    second_payload = json.loads(strategy._obs_path.read_text(encoding="utf-8"))
    assert second_payload == first_payload

    strategy._maybe_write_observability_file(datetime(2026, 2, 9, 10, 7, 0, tzinfo=ET))
    third_payload = json.loads(strategy._obs_path.read_text(encoding="utf-8"))
    assert third_payload["ts_now_et"] != first_payload["ts_now_et"]


def test_effective_atr_observed_uses_seed_and_fallback():
    strategy = _make_strategy()
    strategy._atr_pre["TQQQ"] = 0.42

    before_945 = datetime(2026, 2, 9, 9, 44, 30, tzinfo=ET)
    after_945 = datetime(2026, 2, 9, 9, 45, 30, tzinfo=ET)

    t_val, t_src, _, _ = strategy._effective_atr_observed("TQQQ", 14, before_945)
    s_val, s_src, _, _ = strategy._effective_atr_observed("SQQQ", 14, before_945)
    assert t_val == 0.42 and t_src == "atr_pre"
    assert s_val is None and s_src == "missing"

    s_after, s_after_src, _, _ = strategy._effective_atr_observed("SQQQ", 14, after_945)
    assert s_after == 0.30 and s_after_src == "fallback"


def test_entry_blocked_written_to_diag_with_required_fields(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    now = datetime(2026, 2, 9, 10, 1, 5, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000
    strategy._min_closes[now.replace(second=0, microsecond=0)] = 610.0
    strategy._vwap_minute[now.replace(second=0, microsecond=0)] = 609.0
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=611.0,
        high=611.0,
        low=611.0,
        close=611.0,
        volume=10.0,
    )
    strategy._vwap_pv = 609.5 * 100.0
    strategy._vwap_v = 100.0
    strategy._qqew_last_price = 132.9
    strategy._qqew_vwap_pv = 133.0 * 100.0
    strategy._qqew_vwap_v = 100.0
    strategy._qqq_buffer.append((now_ms - 180_000, 610.0))
    strategy._qqq_buffer.append((now_ms - 1_000, 611.0))
    strategy._qqew_buffer.append((now_ms - 180_000, 133.0))
    strategy._qqew_buffer.append((now_ms - 1_000, 132.9))
    strategy._amp_locked = True

    import asyncio
    asyncio.run(strategy._maybe_enter(now))

    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["event"] == "entry_gate"
    assert rec["decision"] == "BLOCKED"
    assert rec["reason"] == "data_not_ready"
    for key in (
        "ts_et",
        "decision",
        "reason",
        "direction",
        "instrument",
        "mode",
        "qqq_3m_ago",
        "qqew_3m_ago",
        "v3m_price",
        "v3m_signed",
        "qqew_vs_vwap",
        "qqq_close_now",
        "qqew_close_now",
        "vwap_now",
        "qqew_vwap_now",
        "atr_live",
        "atr_seed",
        "atr_eff",
        "atr_cap",
        "k",
        "pulse",
        "vol_ratio",
        "tv",
        "vol_t",
        "vol_ma10",
        "vol_surge_ratio",
        "data_age_qqq_sec",
        "data_age_qqew_sec",
        "stream_invalid",
        "seed_ready",
        "seed_n",
    ):
        assert key in rec


def test_heartbeat_written_once_per_minute(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    now = datetime(2026, 2, 9, 10, 1, 1, tzinfo=ET)
    strategy._maybe_emit_heartbeat(now)
    strategy._maybe_emit_heartbeat(datetime(2026, 2, 9, 10, 1, 59, tzinfo=ET))
    strategy._maybe_emit_heartbeat(datetime(2026, 2, 9, 10, 2, 0, tzinfo=ET))
    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    beats = [json.loads(x) for x in lines if json.loads(x).get("event") == "qqq_loop_heartbeat"]
    assert len(beats) == 2
    assert "v3m_price" in beats[-1]
    assert "qqew_vs_vwap" in beats[-1]
    assert "vol_surge_ratio" in beats[-1]
    assert "loop_iteration_seq" in beats[-1]
    assert "loop_last_stage" in beats[-1]
    assert "loop_last_duration_ms" in beats[-1]


def test_heartbeat_emits_local_audit_runtime_evidence():
    strategy = _make_strategy()
    writer = _CaptureAuditWriter()
    strategy._resolve_local_audit_writer = lambda: writer  # type: ignore[assignment]
    strategy._system_paused = True

    now = datetime(2026, 2, 9, 10, 1, 1, tzinfo=ET)
    strategy._maybe_emit_heartbeat(now)

    assert len(writer.events) == 1
    event = writer.events[0]
    assert event["stage"] == "strategy_loop_heartbeat"
    assert event["status"] == "alive"
    assert event["symbol"] == "QQQ"
    assert event["payload"]["system_paused"] is True


def test_heartbeat_refreshes_stream_invalid_from_live_data(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._stream_invalid = True
    now = datetime(2026, 2, 9, 10, 1, 1, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=611.0,
        high=611.2,
        low=610.9,
        close=611.0,
        volume=100.0,
    )
    strategy._qqew_last_price = 133.0

    strategy._maybe_emit_heartbeat(now)

    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    beats = [json.loads(x) for x in lines if json.loads(x).get("event") == "qqq_loop_heartbeat"]
    assert beats[-1]["stream_invalid"] is False
    assert strategy._stream_invalid is False


def test_observability_snapshot_refreshes_stream_invalid_from_live_data():
    strategy = _make_strategy()
    strategy._stream_invalid = True
    now = datetime(2026, 2, 9, 10, 1, 1, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=611.0,
        high=611.2,
        low=610.9,
        close=611.0,
        volume=100.0,
    )
    strategy._qqew_last_price = 133.0

    payload = strategy._build_observability_snapshot(now)

    assert strategy._stream_invalid is False
    assert payload["ready"] is False or payload["ready"] is True
    assert payload["required_values"]["QQQ"]["age_sec"] is not None


def test_loop_stall_detection_emits_diag_and_local_audit(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._running = True
    strategy._loop_in_iteration = True
    strategy._loop_iteration_seq = 12
    strategy._loop_current_started_mono = 100.0
    strategy._loop_last_stage = "manage_position"
    strategy.params.extras["loop_stall_threshold_sec"] = 20.0
    strategy.params.extras["loop_stall_warn_interval_sec"] = 30.0

    writer = _CaptureAuditWriter()
    strategy._resolve_local_audit_writer = lambda: writer  # type: ignore[assignment]

    now = datetime(2026, 2, 9, 10, 31, 0, tzinfo=ET)
    strategy._check_loop_stall(now=now, now_mono=131.0)
    strategy._check_loop_stall(now=now, now_mono=132.0)

    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    stall_events = [rec for rec in records if rec.get("event") == "qqq_loop_stall_detected"]
    assert len(stall_events) == 1
    assert stall_events[0]["loop_stage"] == "manage_position"
    assert stall_events[0]["iteration_seq"] == 12

    assert len(writer.events) == 1
    assert writer.events[0]["stage"] == "strategy_loop_stall"
    assert writer.events[0]["reason"] == "loop_stall_detected"


def test_emit_local_audit_uses_pending_and_active_ids_as_fallback():
    strategy = _make_strategy()
    strategy._pending_decision_id = "decision-001"
    strategy._active_trigger_id = "TRIGGER-ABC-1234"
    writer = _CaptureAuditWriter()
    strategy._resolve_local_audit_writer = lambda: writer  # type: ignore[assignment]

    strategy._emit_local_audit(stage="unit_test_stage", reason="unit_test")

    assert len(writer.events) == 1
    event = writer.events[0]
    assert event["decision_id"] == "decision-001"
    assert event["trigger_id"] == "triggerabc1234"
    assert event["trace_id"] == "decision-001"


def test_ctx_position_timeout_emits_audit_and_raises():
    strategy = _make_strategy()
    writer = _CaptureAuditWriter()
    strategy._resolve_local_audit_writer = lambda: writer  # type: ignore[assignment]
    strategy.params.extras["ctx_position_timeout_sec"] = 0.01

    async def _hang(_symbol: str):
        await asyncio.sleep(1.0)

    strategy.ctx.position = _hang  # type: ignore[assignment]

    async def _run():
        try:
            await strategy._ctx_position_with_timeout("TQQQ")
        except RuntimeError as exc:
            assert "ctx_position_timeout:TQQQ" in str(exc)
            return
        raise AssertionError("expected RuntimeError")

    asyncio.run(_run())
    assert any(evt["stage"] == "ctx_position_timeout" for evt in writer.events)


def test_sync_position_state_returns_after_ctx_timeout(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.params.extras["ctx_position_timeout_sec"] = 0.01
    strategy._broker_position_snapshot = AsyncMock(return_value=(0.0, None))
    writer = _CaptureAuditWriter()
    strategy._resolve_local_audit_writer = lambda: writer  # type: ignore[assignment]

    async def _hang(_symbol: str):
        await asyncio.sleep(1.0)

    strategy.ctx.position = _hang  # type: ignore[assignment]

    asyncio.run(strategy._sync_position_state(force=False))

    assert any(evt["stage"] == "ctx_position_timeout" for evt in writer.events)
    assert any(evt["stage"] == "position_sync_qty_resolution" for evt in writer.events)


def test_current_exec_qty_uses_broker_fallback_after_ctx_timeout():
    strategy = _make_strategy()
    strategy.params.extras["ctx_position_timeout_sec"] = 0.01
    strategy._broker_position_qty = AsyncMock(return_value=137.0)

    async def _hang(_symbol: str):
        await asyncio.sleep(1.0)

    strategy.ctx.position = _hang  # type: ignore[assignment]

    qty = asyncio.run(strategy._current_exec_qty_for_symbol("TQQQ"))
    assert qty == 137.0


def test_sync_position_state_uses_broker_resolution_after_ctx_timeout():
    strategy = _make_strategy()
    strategy.params.extras["ctx_position_timeout_sec"] = 0.01
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._resolve_local_audit_writer = lambda: _CaptureAuditWriter()  # type: ignore[assignment]

    async def _hang(_symbol: str):
        await asyncio.sleep(1.0)

    strategy.ctx.position = _hang  # type: ignore[assignment]
    strategy._broker_position_snapshot = AsyncMock(side_effect=[(50.0, 100.0), (0.0, None)])

    asyncio.run(strategy._sync_position_state(force=False))

    assert strategy._is_traded is True
    assert strategy._exec_symbol == "TQQQ"


def test_sync_position_state_uses_broker_only_offhours_without_ctx_calls():
    strategy = _make_strategy()
    strategy.params.extras["legacy_trader_parity_mode"] = False
    writer = _CaptureAuditWriter()
    strategy._resolve_local_audit_writer = lambda: writer  # type: ignore[assignment]
    strategy._is_regular_session = lambda _now: False  # type: ignore[assignment]
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._planned_total_qty = 25
    strategy._ensure_live_protection = AsyncMock(return_value=None)
    strategy.ctx.position = AsyncMock(side_effect=AssertionError("ctx.position should not be called"))  # type: ignore[assignment]
    strategy._broker_position_snapshot = AsyncMock(side_effect=[(25.0, 101.5), (0.0, None)])

    asyncio.run(strategy._sync_position_state(force=False))

    strategy.ctx.position.assert_not_called()  # type: ignore[attr-defined]
    assert strategy._is_traded is True
    assert strategy._exec_symbol == "TQQQ"
    resolution_events = [evt for evt in writer.events if evt["stage"] == "position_sync_qty_resolution"]
    assert len(resolution_events) == 1
    payload = resolution_events[0]["payload"]
    assert payload["broker_only_offhours"] is True
    assert payload["qty_source"]["TQQQ"] == "broker_only_offhours"
    assert payload["ctx_position_timed_out"]["TQQQ"] is False


def test_sync_position_state_can_opt_in_to_ctx_queries_offhours():
    strategy = _make_strategy()
    strategy.params.extras["offhours_ctx_position_sync_enabled"] = True
    strategy._is_regular_session = lambda _now: False  # type: ignore[assignment]
    strategy.ctx.position = AsyncMock(side_effect=[{"pos_qty": 0.0}, {"pos_qty": 0.0}])  # type: ignore[assignment]
    strategy._broker_position_snapshot = AsyncMock(side_effect=[(0.0, None), (0.0, None)])

    asyncio.run(strategy._sync_position_state(force=True))

    assert strategy.ctx.position.await_count == 2  # type: ignore[attr-defined]


def test_submit_intent_timeout_emits_audit_and_raises():
    strategy = _make_strategy()
    writer = _CaptureAuditWriter()
    strategy._resolve_local_audit_writer = lambda: writer  # type: ignore[assignment]
    strategy.params.extras["protective_stop_submit_timeout_sec"] = 0.01

    envelope = MagicMock()
    envelope.symbol = "TQQQ"
    envelope.decision_id = "decision-timeout"
    envelope.order_style = MagicMock(kind="STP")
    envelope.extras = {"order_ref": "qqqv1:test", "trigger_id": "trigger-1"}

    async def _run():
        async def _hang(_envelope):
            await asyncio.sleep(1.0)

        strategy.ctx.submit_intent = _hang  # type: ignore[assignment]
        try:
            await strategy._submit_intent_with_timeout(
                envelope,
                timeout_key="protective_stop_submit_timeout_sec",
            )
        except RuntimeError as exc:
            assert "submit_intent_timeout:TQQQ:protective_stop_submit_timeout_sec" in str(exc)
            return
        raise AssertionError("expected RuntimeError")

    asyncio.run(_run())
    assert any(evt["stage"] == "submit_intent_timeout" for evt in writer.events)


def test_run_loop_cancelled_emits_diag_event(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._running = True
    strategy._daily_reset_if_needed = lambda now: None  # type: ignore[assignment]

    async def _slow_ingest(_now):
        await asyncio.sleep(5.0)

    strategy._ingest_required = _slow_ingest  # type: ignore[assignment]
    strategy._maybe_retry_seed_warmup = AsyncMock(return_value=None)
    strategy._lock_amp_params = AsyncMock(return_value=None)
    strategy._startup_positions_gate = AsyncMock(return_value=False)
    strategy._force_flatten_1559 = AsyncMock(return_value=None)
    strategy._sync_position_state = AsyncMock(return_value=None)
    strategy._maybe_persist_startup_reconciled_state = AsyncMock(return_value=None)
    strategy._update_crosscount = lambda now: None  # type: ignore[assignment]
    strategy._maybe_lock_seed = lambda now: None  # type: ignore[assignment]
    strategy._refresh_atr_pre_by_schedule = lambda now: None  # type: ignore[assignment]
    strategy._maybe_persist_daily_atr_seed = lambda now: None  # type: ignore[assignment]
    strategy._maybe_write_observability_file = lambda now: None  # type: ignore[assignment]
    strategy._maybe_emit_heartbeat = lambda now: None  # type: ignore[assignment]

    async def _run_and_cancel():
        task = asyncio.create_task(strategy._run_loop(10.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run_and_cancel())

    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert any(rec.get("event") == "qqq_loop_cancelled" for rec in records)


def test_run_loop_exception_emits_diag_event(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._running = True
    strategy._daily_reset_if_needed = lambda now: None  # type: ignore[assignment]

    async def _raise_once(_now):
        raise RuntimeError("boom")

    strategy._ingest_required = _raise_once  # type: ignore[assignment]
    strategy._maybe_retry_seed_warmup = AsyncMock(return_value=None)
    strategy._lock_amp_params = AsyncMock(return_value=None)
    strategy._startup_positions_gate = AsyncMock(return_value=False)
    strategy._force_flatten_1559 = AsyncMock(return_value=None)
    strategy._sync_position_state = AsyncMock(return_value=None)
    strategy._maybe_persist_startup_reconciled_state = AsyncMock(return_value=None)
    strategy._update_crosscount = lambda now: None  # type: ignore[assignment]
    strategy._maybe_lock_seed = lambda now: None  # type: ignore[assignment]
    strategy._refresh_atr_pre_by_schedule = lambda now: None  # type: ignore[assignment]
    strategy._maybe_persist_daily_atr_seed = lambda now: None  # type: ignore[assignment]
    strategy._maybe_write_observability_file = lambda now: None  # type: ignore[assignment]
    strategy._maybe_emit_heartbeat = lambda now: None  # type: ignore[assignment]

    async def _run_and_cancel():
        task = asyncio.create_task(strategy._run_loop(0.1))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run_and_cancel())

    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert any(rec.get("event") == "qqq_loop_exception" for rec in records)


def test_current_exec_qty_does_not_claim_broker_net_when_ctx_flat():
    strategy = _make_strategy()

    class _Pos:
        pos_qty = 0.0

    async def _ctx_position(_symbol: str):
        return _Pos()

    async def _broker_qty(_symbol: str):
        return 137.0

    strategy.ctx.position = _ctx_position  # type: ignore[assignment]
    strategy._broker_position_qty = _broker_qty  # type: ignore[method-assign]

    qty = asyncio.run(strategy._current_exec_qty_for_symbol("TQQQ"))
    assert qty == 0.0


def test_restore_runtime_state_sets_day_and_survives_same_day_reset(tmp_path):
    strategy = _make_strategy()
    state_path = tmp_path / "qqq_state.json"
    today = datetime.now(ET).strftime("%Y-%m-%d")
    state_path.write_text(
        json.dumps(
            {
                "day": today,
                "is_traded": True,
                "amp_locked": True,
                "price_0945": 610.0,
                "atr_pre": {"TQQQ": 0.4, "SQQQ": 0.3},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    strategy._state_path = state_path
    strategy._restore_runtime_state()
    assert strategy._day == today
    assert strategy._amp_locked is True

    now = datetime.now(ET)
    strategy._daily_reset_if_needed(now)
    assert strategy._amp_locked is True


def test_restore_runtime_state_backfills_trade_target_from_entry_context(tmp_path):
    strategy = _make_strategy()
    state_path = tmp_path / "qqq_state.json"
    today = datetime.now(ET).strftime("%Y-%m-%d")
    state_path.write_text(
        json.dumps(
            {
                "day": today,
                "is_traded": True,
                "entry_px": 48.06,
                "entry_atr_used": 0.2768,
                "locked_target": 1.0,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    strategy._state_path = state_path

    strategy._restore_runtime_state()

    assert strategy._locked_target == 1.0
    assert strategy._trade_target_px == 48.34


def test_price_base_window_missed_diag_event(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._day = "2026-02-09"
    strategy._initialized_at_et = datetime(2026, 2, 9, 10, 0, 0, tzinfo=ET)
    strategy._amp_locked = False
    strategy._price_0945 = 609.5
    strategy._price_base = None

    asyncio.run(strategy._lock_amp_params(datetime(2026, 2, 9, 10, 1, 0, tzinfo=ET)))

    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["event"] == "price_base_window_missed"
    assert rec["reason"] == "init_after_price_base_window"


def test_price_base_lock_persists_runtime_state_immediately(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_state.json"

    t0 = datetime(2026, 2, 9, 9, 30, 0, tzinfo=ET)
    t10 = datetime(2026, 2, 9, 9, 30, 10, tzinfo=ET)
    strategy._update_vwap(int(t0.timestamp() * 1000), 600.0, 100.0)
    strategy._update_vwap(int(t10.timestamp() * 1000), 601.0, 50.0)

    payload = json.loads(strategy._state_path.read_text(encoding="utf-8"))
    assert float(payload["price_base"]) == 600.0
    assert payload["reason"] == "price_base_locked"


def test_shutdown_does_not_override_existing_today_seed_with_stale_pre(tmp_path, monkeypatch):
    strategy = _make_strategy()
    strategy._atr_seed_path = tmp_path / "atr_seed.json"
    fixed_now = datetime(2026, 2, 9, 17, 5, 0, tzinfo=ET)
    today = fixed_now.strftime("%Y-%m-%d")

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(qmod, "datetime", _FixedDateTime)
    strategy._atr_seed_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": f"{today}T15:59:00-05:00",
                "latest": {"day": today, "atr_pre": {"TQQQ": 0.12, "SQQQ": 0.17}},
                "days": {today: {"TQQQ": 0.12, "SQQQ": 0.17}},
                "reason": "eod_snapshot",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    # Simulate stale preheat snapshot while no live ATR is available at shutdown.
    strategy._atr_pre["TQQQ"] = 1.02
    strategy._atr_pre["SQQQ"] = 0.27

    import asyncio
    asyncio.run(strategy.on_shutdown("engine_shutdown"))

    payload = json.loads(strategy._atr_seed_path.read_text(encoding="utf-8"))
    latest = payload["latest"]["atr_pre"]
    assert float(latest["TQQQ"]) == 0.12
    assert float(latest["SQQQ"]) == 0.17
    assert payload["reason"] == "eod_snapshot"


def test_daily_eod_seed_not_written_after_1600(tmp_path):
    strategy = _make_strategy()
    strategy._atr_seed_path = tmp_path / "atr_seed.json"
    strategy._atr_pre["TQQQ"] = 0.20
    strategy._atr_pre["SQQQ"] = 0.15

    now = datetime(2026, 2, 9, 16, 30, 0, tzinfo=ET)
    strategy._maybe_persist_daily_atr_seed(now)

    assert not strategy._atr_seed_path.exists()


def test_all_day_test_mode_locks_amp_without_0945_window():
    strategy = _make_strategy()
    strategy.params.extras["all_day_test_mode"] = True

    now = datetime(2026, 2, 9, 3, 0, 0, tzinfo=ET)
    asyncio.run(strategy._lock_amp_params(now))

    assert strategy._amp_locked is True
    assert strategy._locked_add_threshold == 120.0
    assert strategy._locked_dev_threshold == 0.001
    assert strategy._locked_target == 2.0


def test_all_day_test_mode_enables_atr_fallback_before_0945():
    strategy = _make_strategy()
    strategy.params.extras["all_day_test_mode"] = True

    now = datetime(2026, 2, 9, 3, 0, 0, tzinfo=ET)
    t_val, t_src, _, _ = strategy._effective_atr_observed("TQQQ", 14, now)
    s_val, s_src, _, _ = strategy._effective_atr_observed("SQQQ", 14, now)
    assert t_val == 0.35 and t_src == "fallback"
    assert s_val == 0.30 and s_src == "fallback"


def test_subscription_symbols_autofills_required_symbols():
    strategy = _make_strategy()
    extras = {"symbols": ["QQQ", "TQQQ", "SQQQ", "AD-NASD"]}
    resolved = strategy._resolve_subscription_symbols(extras)
    assert resolved == ["QQQ", "TQQQ", "SQQQ"]


def test_subscription_symbols_ignores_retired_confirm_symbol():
    strategy = _make_strategy()
    strategy.params.extras["use_confirm_symbol"] = True
    extras = {"symbols": ["QQQ", "TQQQ", "SQQQ", "AD-NASD"]}
    resolved = strategy._resolve_subscription_symbols(extras)
    assert resolved == ["QQQ", "TQQQ", "SQQQ"]


def test_price_base_recovered_from_0930_window(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._state_path = tmp_path / "qqq_state.json"
    strategy._day = "2026-02-09"
    strategy._initialized_at_et = datetime(2026, 2, 9, 10, 0, 0, tzinfo=ET)
    strategy._price_0945 = 609.0
    strategy._price_base = None
    rows = [
        {"ts": int(datetime(2026, 2, 9, 9, 30, 1, tzinfo=ET).timestamp() * 1000), "close": 600.0, "volume": 100.0},
        {"ts": int(datetime(2026, 2, 9, 9, 30, 5, tzinfo=ET).timestamp() * 1000), "close": 602.0, "volume": 100.0},
    ]
    strategy._sfs = _FakeSFS(rows)

    asyncio.run(strategy._lock_amp_params(datetime(2026, 2, 9, 10, 1, 0, tzinfo=ET)))

    assert strategy._price_base is not None
    assert round(strategy._price_base, 4) == 601.0
    assert strategy._amp_locked is True


def test_price_base_fallback_to_startup_anchor_10s(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._state_path = tmp_path / "qqq_state.json"
    strategy._day = "2026-02-09"
    strategy._initialized_at_et = datetime(2026, 2, 9, 10, 0, 5, tzinfo=ET)
    strategy._price_0945 = 612.0
    strategy._price_base = None
    rows = [
        {"ts": int(datetime(2026, 2, 9, 10, 0, 1, tzinfo=ET).timestamp() * 1000), "close": 610.0, "volume": 100.0},
        {"ts": int(datetime(2026, 2, 9, 10, 0, 4, tzinfo=ET).timestamp() * 1000), "close": 611.0, "volume": 100.0},
    ]
    strategy._sfs = _FakeSFS(rows)

    asyncio.run(strategy._lock_amp_params(datetime(2026, 2, 9, 10, 1, 0, tzinfo=ET)))

    assert strategy._price_base is not None
    assert round(strategy._price_base, 4) == 610.5


def test_price_base_recovery_retries_after_first_failure(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._state_path = tmp_path / "qqq_state.json"
    strategy._day = "2026-02-11"
    strategy._initialized_at_et = datetime(2026, 2, 11, 10, 0, 0, tzinfo=ET)
    strategy._price_0945 = 610.0
    strategy._price_base = None
    rows = [
        {"ts": int(datetime(2026, 2, 11, 9, 30, 2, tzinfo=ET).timestamp() * 1000), "close": 600.0, "volume": 100.0},
        {"ts": int(datetime(2026, 2, 11, 9, 30, 8, tzinfo=ET).timestamp() * 1000), "close": 602.0, "volume": 100.0},
    ]
    strategy._sfs = _FakeSFSRetry(rows, fail_calls=1)

    asyncio.run(strategy._lock_amp_params(datetime(2026, 2, 11, 10, 1, 0, tzinfo=ET)))
    assert strategy._price_base is None
    assert strategy._price_base_window_missed_day == "2026-02-11"

    asyncio.run(strategy._lock_amp_params(datetime(2026, 2, 11, 10, 2, 0, tzinfo=ET)))
    assert strategy._sfs.calls >= 2
    assert strategy._price_base is not None
    assert round(strategy._price_base, 4) == 601.0


def test_price_base_recovered_from_ibkr_fallback_when_sfs_missing(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy._state_path = tmp_path / "qqq_state.json"
    strategy._day = "2026-02-11"
    strategy._initialized_at_et = datetime(2026, 2, 11, 10, 0, 0, tzinfo=ET)
    strategy._price_0945 = 610.0
    strategy._price_base = None
    strategy._sfs = _FakeSFS([])

    async def _fake_ibkr_recover(now):
        return 603.0, "ibkr_1m_0930_open", 1

    strategy._recover_price_base_from_ibkr = _fake_ibkr_recover  # type: ignore[method-assign]

    asyncio.run(strategy._lock_amp_params(datetime(2026, 2, 11, 10, 1, 0, tzinfo=ET)))

    assert strategy._price_base is not None
    assert round(strategy._price_base, 4) == 603.0
    assert strategy._amp_locked is True


def test_symbol_specific_freshness_thresholds_for_qqq_and_qqew():
    strategy = _make_strategy()
    strategy.params.extras["freshness_max_sec"] = 5
    strategy.params.extras["freshness_max_sec_qqew"] = 8

    now = datetime(2026, 2, 11, 10, 40, 0, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)

    # QQQ data at 6s old should be stale under 5s threshold.
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 6000
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now,
        ts_end=now,
        open=610.0,
        high=610.0,
        low=610.0,
        close=610.0,
        volume=100.0,
    )
    strategy._vwap_pv = 610.0 * 100.0
    strategy._vwap_v = 100.0

    # QQEW at 6s old should still be fresh under 8s threshold.
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 6000
    strategy._qqew_last_price = 133.0
    strategy._qqew_vwap_pv = 133.0 * 100.0
    strategy._qqew_vwap_v = 100.0

    missing = strategy._missing_required(now)
    assert "QQQ:last_stale" in missing
    assert "QQEW:last_stale" not in missing


def test_missing_required_skips_qqew_when_decoupled():
    strategy = _make_strategy()
    strategy.params.extras["elite_decouple_qqew"] = True
    now = datetime(2026, 2, 11, 10, 41, 0, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)

    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now,
        ts_end=now,
        open=610.0,
        high=610.0,
        low=610.0,
        close=610.0,
        volume=100.0,
    )
    strategy._vwap_pv = 610.0 * 100.0
    strategy._vwap_v = 100.0

    # QQEW intentionally stale/missing-like
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 60_000
    strategy._qqew_last_price = None
    strategy._qqew_vwap_pv = 0.0
    strategy._qqew_vwap_v = 0.0

    missing = strategy._missing_required(now)
    assert "QQQ:last_stale" not in missing
    assert "QQEW:last" not in missing
    assert "QQEW:last_stale" not in missing
    assert "QQEW:vwap" not in missing


def test_seed_lock_not_sticky_when_seed_insufficient():
    strategy = _make_strategy()
    now = datetime(2026, 2, 12, 10, 0, 0, tzinfo=ET)

    strategy._maybe_lock_seed(now)
    assert strategy._seed_ready is False
    assert strategy._seed_locked_day is None

    strategy._orh = 611.0
    strategy._orl = 608.0
    strategy._seed_velocity_samples = [60.0, 75.0, 72.0, 68.0, 80.0]
    strategy._maybe_lock_seed(now)
    assert strategy._seed_ready is True
    assert strategy._seed_locked_day == "2026-02-12"


def test_warmup_entry_seed_data_rebuilds_seed_after_mid_session_restart():
    strategy = _make_strategy()
    strategy.params.extras["enable_shared_underlying_data_plane"] = True
    strategy.params.extras["shared_underlying_read_mode"] = "plane_only"
    plane = UnderlyingSharedDataPlane()
    for ts_dt, close_px in [
        (datetime(2026, 2, 12, 9, 35, 0, tzinfo=ET), 609.8),
        (datetime(2026, 2, 12, 9, 40, 0, tzinfo=ET), 610.4),
        (datetime(2026, 2, 12, 9, 44, 0, tzinfo=ET), 611.0),
    ]:
        plane.publish_underlying(
            symbol="QQQ",
            namespace="qqq",
            ts_epoch_s=ts_dt.timestamp(),
            last_price=close_px,
            bar={"ts_epoch_s": ts_dt.timestamp(), "close": close_px, "open": close_px, "high": close_px, "low": close_px, "volume": 100.0, "vwap": close_px},
        )
    plane.publish_underlying(
        symbol="QQEW",
        namespace="qqq",
        ts_epoch_s=datetime(2026, 2, 12, 9, 46, 0, tzinfo=ET).timestamp(),
        last_price=133.1,
        bar={"ts_epoch_s": datetime(2026, 2, 12, 9, 46, 0, tzinfo=ET).timestamp(), "close": 133.1, "open": 133.1, "high": 133.1, "low": 133.1, "volume": 100.0, "vwap": 133.1},
    )
    saved_plane = qmod.shared_underlying_data_plane
    qmod.shared_underlying_data_plane = lambda: plane

    try:
        asyncio.run(strategy._warmup_entry_seed_data(datetime(2026, 2, 12, 10, 0, 0, tzinfo=ET)))
    finally:
        qmod.shared_underlying_data_plane = saved_plane
    assert strategy._orh is not None
    assert strategy._orl is not None
    assert len(strategy._seed_velocity_samples) >= 2


def test_entry_allowed_emits_entry_gate(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    strategy._seed_ready = True
    strategy._v_seed_sigma = 0.4
    strategy._seed_velocity_samples = [0.2, 0.3, 0.25, 0.4, 0.35, 0.32]
    strategy._orh = 610.0
    strategy._orl = 600.0
    strategy._last_stop_exit_at = None
    strategy._last_stop_exit_side = None
    strategy._atr_pre["TQQQ"] = 0.35

    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000

    for i in range(181):
        ts = now_ms - (180 - i) * 1000
        strategy._qqq_buffer.append((ts, 611.0 if i > 10 else 609.0))
        strategy._qqew_buffer.append((ts, 134.5 if i > 10 else 132.8))

    strategy.params.extras["atr_period_1m"] = 3
    strategy._minute_bars.extend(
        [
            qmod._OhlcvBar(now - qmod.timedelta(minutes=4), now - qmod.timedelta(minutes=3), 608.0, 609.0, 607.0, 608.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=3), now - qmod.timedelta(minutes=2), 608.5, 610.0, 608.0, 609.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=2), now - qmod.timedelta(minutes=1), 609.5, 610.5, 609.0, 610.2, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=1), now, 610.2, 611.0, 610.0, 610.8, 100.0),
        ]
    )
    strategy._rolling_vol_queue.extend([100.0] * 10)
    strategy._today_vol_series.extend([100.0] * 20)

    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=611.0,
        high=612.0,
        low=610.8,
        close=611.0,
        volume=300.0,
    )
    strategy._vwap_pv = 609.5 * 100.0
    strategy._vwap_v = 100.0
    strategy._qqew_last_price = 134.5
    strategy._qqew_vwap_pv = 133.2 * 100.0
    strategy._qqew_vwap_v = 100.0
    strategy._amp_locked = True
    strategy.params.extras["anti_chase_chop_enable"] = False
    strategy.params.extras["qty"] = 1
    strategy._calc_qty = lambda price: 1
    strategy._exec_minute_close_for_gate = lambda symbol, minute_ts, now_dt: 49.5
    strategy._effective_atr = lambda symbol, period, now=None: 0.35
    strategy._current_exec_qty_for_symbol = AsyncMock(return_value=0)

    submit_calls = []

    async def _fake_submit(symbol: str, qty: int, **kwargs) -> bool:
        submit_calls.append({"symbol": symbol, "qty": qty, **kwargs})
        return True

    async def _fake_stop(reason: str) -> None:
        return None

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]
    strategy._submit_protective_stop_intent = _fake_stop  # type: ignore[method-assign]

    import asyncio
    asyncio.run(strategy._maybe_enter(now))

    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(x) for x in lines]
    entry_gate = next(e for e in events if e.get("event") == "entry_gate")
    assert entry_gate["decision"] == "ENTRY_ALLOWED"
    assert entry_gate["direction"] == "LONG"
    minute_metrics = next(e for e in events if e.get("event") == "qqq_entry_minute_metrics")
    assert "v3m_price" in minute_metrics
    assert "qqew_vs_vwap" in minute_metrics
    assert "vol_surge_ratio" in minute_metrics
    assert len(submit_calls) == 1

    gate_call = next(
        call
        for call in strategy.ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "gate_evaluated" and call.kwargs.get("status") == "entry_allowed"
    )
    submit_call = submit_calls[0]
    assert gate_call.kwargs["decision_id"] == submit_call["decision_id_override"]
    assert gate_call.kwargs["trigger_id"] == submit_call["trigger_id"]
    assert gate_call.kwargs["trace_id"] == submit_call["trace_id_override"]
    ready_call = next(
        call
        for call in strategy.ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "entry_submit_ready"
    )
    assert ready_call.kwargs["decision_id"] == submit_call["decision_id_override"]
    assert ready_call.kwargs["trigger_id"] == submit_call["trigger_id"]
    assert ready_call.kwargs["trace_id"] == submit_call["trace_id_override"]


def test_entry_allowed_can_be_blocked_by_reentry_cooldown_with_explicit_audit(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    strategy._seed_ready = True
    strategy._v_seed_sigma = 0.4
    strategy._seed_velocity_samples = [0.2, 0.3, 0.25, 0.4, 0.35, 0.32]
    strategy._orh = 610.0
    strategy._orl = 600.0
    strategy._atr_pre["TQQQ"] = 0.35

    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000
    strategy._last_stop_exit_at = now - qmod.timedelta(seconds=20)
    strategy._last_stop_exit_side = "LONG"

    for i in range(181):
        ts = now_ms - (180 - i) * 1000
        strategy._qqq_buffer.append((ts, 611.0 if i > 10 else 609.0))
        strategy._qqew_buffer.append((ts, 134.5 if i > 10 else 132.8))

    strategy.params.extras["atr_period_1m"] = 3
    strategy.params.extras["qty"] = 1
    strategy.params.extras["stop_reentry_cooldown_min"] = 1.0
    strategy._minute_bars.extend(
        [
            qmod._OhlcvBar(now - qmod.timedelta(minutes=4), now - qmod.timedelta(minutes=3), 608.0, 609.0, 607.0, 608.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=3), now - qmod.timedelta(minutes=2), 608.5, 610.0, 608.0, 609.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=2), now - qmod.timedelta(minutes=1), 609.5, 610.5, 609.0, 610.2, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=1), now, 610.2, 611.0, 610.0, 610.8, 100.0),
        ]
    )
    strategy._rolling_vol_queue.extend([100.0] * 10)
    strategy._today_vol_series.extend([100.0] * 20)
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=611.0,
        high=612.0,
        low=610.8,
        close=611.0,
        volume=300.0,
    )
    strategy._vwap_pv = 609.5 * 100.0
    strategy._vwap_v = 100.0
    strategy._qqew_last_price = 134.5
    strategy._qqew_vwap_pv = 133.2 * 100.0
    strategy._qqew_vwap_v = 100.0
    strategy._amp_locked = True
    strategy.params.extras["anti_chase_chop_enable"] = False

    import asyncio
    asyncio.run(strategy._maybe_enter(now))

    stages = [call.kwargs.get("stage") for call in strategy.ctx.engine.audit_writer.emit.call_args_list]
    assert "gate_evaluated" in stages
    assert "entry_blocked_reentry_cooldown" in stages
    assert "entry_submit_ready" not in stages


def test_open_orders_block_stock_submit_but_not_shared_signal_publish(tmp_path, monkeypatch):
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "memory")
    strategy = _make_strategy()
    strategy.params.extras["enforce_stock_execution_blockers"] = True
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    _prime_entry_allowed_state(strategy, now)
    strategy.ctx.open_orders = AsyncMock(
        return_value=[
            MagicMock(
                strategy_id=strategy.strategy_id,
                state="Submitted",
                order_type="LMT",
            )
        ]
    )
    strategy._current_exec_qty_for_symbol = AsyncMock(return_value=0)

    submit_calls = []

    async def _fake_submit(symbol: str, qty: int, **kwargs) -> bool:
        submit_calls.append({"symbol": symbol, "qty": qty, **kwargs})
        return True

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]

    asyncio.run(strategy._maybe_enter(now))

    artifact = get_latest_stock_entry_signal(
        underlying="QQQ",
        signal_family="qqq_intraday_entry",
        source_strategy_id="qqq_intraday_001",
    )
    assert artifact is not None
    assert artifact.direction == "LONG"
    assert artifact.payload["publish_stage"] == "shared_entry_candidate"
    assert submit_calls == []


def test_legacy_open_orders_block_before_shared_signal_publish(tmp_path, monkeypatch):
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "memory")
    strategy = _make_strategy()
    strategy.params.extras["legacy_trader_parity_mode"] = True
    strategy.params.extras["enforce_stock_execution_blockers"] = False
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    _prime_entry_allowed_state(strategy, now)
    strategy.ctx.open_orders = AsyncMock(
        return_value=[
            MagicMock(
                strategy_id=strategy.strategy_id,
                state="Submitted",
                order_type="LMT",
            )
        ]
    )
    strategy._current_exec_qty_for_symbol = AsyncMock(return_value=0)

    submit_calls = []

    async def _fake_submit(symbol: str, qty: int, **kwargs) -> bool:
        submit_calls.append({"symbol": symbol, "qty": qty, **kwargs})
        return True

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]

    asyncio.run(strategy._maybe_enter(now))

    artifact = get_latest_stock_entry_signal(
        underlying="QQQ",
        signal_family="qqq_intraday_entry",
        source_strategy_id="qqq_intraday_001",
    )
    assert artifact is None
    assert submit_calls == []
    stages = [call.kwargs.get("stage") for call in strategy.ctx.engine.audit_writer.emit.call_args_list]
    assert "entry_blocked_legacy_open_orders" in stages


def test_legacy_live_position_blocks_before_shared_signal_publish(tmp_path, monkeypatch):
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "memory")
    strategy = _make_strategy()
    strategy.params.extras["legacy_trader_parity_mode"] = True
    strategy.params.extras["enforce_stock_execution_blockers"] = False
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    _prime_entry_allowed_state(strategy, now)
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._current_exec_qty_for_symbol = AsyncMock(side_effect=lambda symbol: 123.0 if symbol == "SQQQ" else 0.0)

    submit_calls = []

    async def _fake_submit(symbol: str, qty: int, **kwargs) -> bool:
        submit_calls.append({"symbol": symbol, "qty": qty, **kwargs})
        return True

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]

    asyncio.run(strategy._maybe_enter(now))

    artifact = get_latest_stock_entry_signal(
        underlying="QQQ",
        signal_family="qqq_intraday_entry",
        source_strategy_id="qqq_intraday_001",
    )
    assert artifact is None
    assert submit_calls == []
    assert strategy._is_traded is True
    assert strategy._round_live_confirmed is True
    stages = [call.kwargs.get("stage") for call in strategy.ctx.engine.audit_writer.emit.call_args_list]
    assert "entry_blocked_legacy_real_position" in stages


def test_live_position_blocks_stock_submit_but_not_shared_signal_publish(tmp_path, monkeypatch):
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "memory")
    strategy = _make_strategy()
    strategy.params.extras["enforce_stock_execution_blockers"] = True
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    _prime_entry_allowed_state(strategy, now)
    strategy._current_exec_qty_for_symbol = AsyncMock(side_effect=lambda symbol: 3.0 if symbol == "TQQQ" else 0.0)

    submit_calls = []

    async def _fake_submit(symbol: str, qty: int, **kwargs) -> bool:
        submit_calls.append({"symbol": symbol, "qty": qty, **kwargs})
        return True

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]

    asyncio.run(strategy._maybe_enter(now))

    artifact = get_latest_stock_entry_signal(
        underlying="QQQ",
        signal_family="qqq_intraday_entry",
        source_strategy_id="qqq_intraday_001",
    )
    assert artifact is not None
    assert artifact.direction == "LONG"
    assert artifact.payload["publish_stage"] == "shared_entry_candidate"
    assert strategy._is_traded is True
    assert strategy._round_live_confirmed is True
    assert submit_calls == []


def test_non_legacy_live_position_keeps_shared_signal_for_downstream_modules(tmp_path, monkeypatch):
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "memory")
    strategy = _make_strategy()
    strategy.strategy_id = "qqq_intraday_shadow_001"
    strategy.params.extras["legacy_trader_parity_mode"] = False
    strategy.params.extras["enforce_stock_execution_blockers"] = True
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    _prime_entry_allowed_state(strategy, now)
    strategy._current_exec_qty_for_symbol = AsyncMock(side_effect=lambda symbol: 3.0 if symbol == "TQQQ" else 0.0)

    submit_calls = []

    async def _fake_submit(symbol: str, qty: int, **kwargs) -> bool:
        submit_calls.append({"symbol": symbol, "qty": qty, **kwargs})
        return True

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]

    asyncio.run(strategy._maybe_enter(now))

    artifact = get_latest_stock_entry_signal(
        underlying="QQQ",
        signal_family="qqq_intraday_entry",
        source_strategy_id="qqq_intraday_shadow_001",
    )
    assert artifact is not None
    assert artifact.direction == "LONG"
    assert artifact.payload["publish_stage"] == "shared_entry_candidate"
    assert submit_calls == []


def test_recommended_entry_filter_blocks_before_entry_allowed_and_submit(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    strategy._seed_ready = True
    strategy._v_seed_sigma = 0.4
    strategy._seed_velocity_samples = [0.2, 0.3, 0.25, 0.4, 0.35, 0.32]
    strategy._orh = 610.0
    strategy._orl = 600.0
    strategy._last_stop_exit_at = None
    strategy._last_stop_exit_side = None
    strategy._atr_pre["TQQQ"] = 0.35

    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000

    for i in range(181):
        ts = now_ms - (180 - i) * 1000
        strategy._qqq_buffer.append((ts, 611.0 if i > 10 else 609.0))
        strategy._qqew_buffer.append((ts, 134.5 if i > 10 else 132.8))

    strategy.params.extras["atr_period_1m"] = 3
    strategy._minute_bars.extend(
        [
            qmod._OhlcvBar(now - qmod.timedelta(minutes=4), now - qmod.timedelta(minutes=3), 608.0, 609.0, 607.0, 608.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=3), now - qmod.timedelta(minutes=2), 608.5, 610.0, 608.0, 609.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=2), now - qmod.timedelta(minutes=1), 609.5, 610.5, 609.0, 610.2, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=1), now, 610.2, 611.0, 610.0, 610.8, 100.0),
        ]
    )
    strategy._rolling_vol_queue.extend([100.0] * 10)
    strategy._today_vol_series.extend([100.0] * 20)

    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=611.0,
        high=612.0,
        low=610.8,
        close=611.0,
        volume=300.0,
    )
    strategy._vwap_pv = 609.5 * 100.0
    strategy._vwap_v = 100.0
    strategy._qqew_last_price = 134.5
    strategy._qqew_vwap_pv = 133.2 * 100.0
    strategy._qqew_vwap_v = 100.0
    strategy._amp_locked = True
    strategy.params.extras["qty"] = 1
    strategy._calc_qty = lambda price: 1
    strategy._exec_minute_close_for_gate = lambda symbol, minute_ts, now_dt: 49.5
    strategy._effective_atr = lambda symbol, period, now=None: 0.35
    strategy._current_exec_qty_for_symbol = AsyncMock(return_value=0)
    strategy._anti_chase_reason = lambda **kwargs: {"reason": "anti_chase_blocked", "rank15": 0.95, "distance_from_vwap_atr": 0.2, "tv": 0.93}  # type: ignore[method-assign]

    submit_calls = []

    async def _fake_submit(symbol: str, qty: int, **kwargs) -> bool:
        submit_calls.append({"symbol": symbol, "qty": qty, **kwargs})
        return True

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]

    asyncio.run(strategy._maybe_enter(now))

    lines = strategy._diag_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(x) for x in lines]
    entry_gate = next(e for e in events if e.get("event") == "entry_gate")
    assert entry_gate["decision"] == "BLOCKED"
    assert entry_gate["reason"] == "anti_chase_blocked"
    assert submit_calls == []

    stages = [call.kwargs.get("stage") for call in strategy.ctx.engine.audit_writer.emit.call_args_list]
    statuses = [call.kwargs.get("status") for call in strategy.ctx.engine.audit_writer.emit.call_args_list]
    assert "entry_blocked_anti_chase" in stages
    assert "entry_allowed" not in statuses
    assert "entry_submit_ready" not in stages


def test_r1_blocks_before_entry_allowed_and_submit(tmp_path):
    strategy = _make_strategy()
    strategy._diag_path = tmp_path / "qqq_diag.jsonl"
    strategy.ctx.engine.audit_writer = MagicMock()
    strategy._seed_ready = True
    strategy._v_seed_sigma = 0.4
    strategy._seed_velocity_samples = [0.2, 0.3, 0.25, 0.4, 0.35, 0.32]
    strategy._orh = 610.0
    strategy._orl = 600.0
    strategy._last_stop_exit_at = None
    strategy._last_stop_exit_side = None
    strategy._atr_pre["TQQQ"] = 0.35
    strategy._atr_pre["SQQQ"] = 0.35
    strategy._success_count = 3
    strategy._day_realized_pnl = 50.0

    now = datetime(2026, 2, 9, 10, 2, 5, tzinfo=ET)
    now_ms = int(now.timestamp() * 1000)
    strategy._last_seen_ts_ms["QQQ"] = now_ms - 1000
    strategy._last_seen_ts_ms["QQEW"] = now_ms - 1000

    for i in range(181):
        ts = now_ms - (180 - i) * 1000
        strategy._qqq_buffer.append((ts, 609.0 if i > 10 else 611.0))
        strategy._qqew_buffer.append((ts, 132.8 if i > 10 else 134.5))

    strategy.params.extras["atr_period_1m"] = 3
    strategy.params.extras["max_trades_per_day"] = 4
    strategy.params.extras["qty"] = 1
    strategy._minute_bars.extend(
        [
            qmod._OhlcvBar(now - qmod.timedelta(minutes=4), now - qmod.timedelta(minutes=3), 611.0, 611.2, 610.2, 610.8, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=3), now - qmod.timedelta(minutes=2), 610.8, 610.9, 609.8, 610.1, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=2), now - qmod.timedelta(minutes=1), 610.1, 610.2, 609.2, 609.5, 100.0),
            qmod._OhlcvBar(now - qmod.timedelta(minutes=1), now, 609.5, 609.6, 608.9, 609.0, 100.0),
        ]
    )
    strategy._rolling_vol_queue.extend([100.0] * 10)
    strategy._today_vol_series.extend([100.0] * 20)
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=now.replace(second=0, microsecond=0),
        ts_end=now.replace(second=0, microsecond=0) + qmod.timedelta(minutes=1),
        open=609.1,
        high=609.2,
        low=608.7,
        close=609.0,
        volume=300.0,
    )
    strategy._vwap_pv = 609.5 * 100.0
    strategy._vwap_v = 100.0
    strategy._qqew_last_price = 132.8
    strategy._qqew_vwap_pv = 133.2 * 100.0
    strategy._qqew_vwap_v = 100.0
    strategy._amp_locked = True
    strategy.params.extras["anti_chase_chop_enable"] = False
    strategy._calc_qty = lambda price: 1
    strategy._exec_minute_close_for_gate = lambda symbol, minute_ts, now_dt: 49.5
    strategy._effective_atr = lambda symbol, period, now=None: 0.35
    strategy._current_exec_qty_for_symbol = AsyncMock(return_value=0)

    submit_calls = []

    async def _fake_submit(symbol: str, qty: int, **kwargs) -> bool:
        submit_calls.append({"symbol": symbol, "qty": qty, **kwargs})
        return True

    strategy._submit_target = _fake_submit  # type: ignore[method-assign]

    asyncio.run(strategy._maybe_enter(now))

    assert submit_calls == []

    stages = [call.kwargs.get("stage") for call in strategy.ctx.engine.audit_writer.emit.call_args_list]
    statuses = [call.kwargs.get("status") for call in strategy.ctx.engine.audit_writer.emit.call_args_list]
    reasons = [call.kwargs.get("reason") for call in strategy.ctx.engine.audit_writer.emit.call_args_list]
    assert "gate_blocked" in stages
    assert "r1_fourth_short_small_green_blocked" in reasons
    assert "entry_allowed" not in statuses
    assert "entry_submit_ready" not in stages


def test_ctx_nonzero_broker_flat_self_heal_clears_stale_trade_runtime() -> None:
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 50.0
    strategy._planned_total_qty = 9
    strategy._pending_decision_id = "d-1"
    strategy.params.extras["ctx_nonzero_broker_flat_self_heal_probe_need"] = 2
    strategy.params.extras["ctx_nonzero_broker_flat_self_heal_cooldown_sec"] = 0.0

    strategy._register_ctx_nonzero_broker_flat("TQQQ", qty_ctx=9.0, qty_broker=0.0)
    assert strategy._is_traded is True

    strategy._register_ctx_nonzero_broker_flat("TQQQ", qty_ctx=9.0, qty_broker=0.0)
    assert strategy._is_traded is False
    assert strategy._exec_symbol is None
    assert strategy._pending_decision_id is None
    assert strategy._ctx_stale_quarantined_by_symbol["TQQQ"] is True
    assert strategy._last_nonzero_ctx_qty_by_symbol["TQQQ"] == 0.0


def test_ctx_flat_broker_nonzero_quarantine_marks_foreign_residual() -> None:
    strategy = _make_strategy()
    strategy.params.extras["ctx_flat_broker_nonzero_quarantine_probe_need"] = 2
    strategy.params.extras["ctx_flat_broker_nonzero_quarantine_cooldown_sec"] = 0.0

    strategy._register_ctx_flat_broker_nonzero("SQQQ", qty_ctx=0.0, qty_broker=1.0)
    assert strategy._foreign_broker_quarantined_by_symbol["SQQQ"] is False

    strategy._register_ctx_flat_broker_nonzero("SQQQ", qty_ctx=0.0, qty_broker=1.0)
    assert strategy._foreign_broker_quarantined_by_symbol["SQQQ"] is True


def test_seed_warmup_retry_skips_when_qqq_stream_is_fresh() -> None:
    strategy = _make_strategy()
    strategy.params.extras["freshness_max_sec"] = 5
    now = datetime(2026, 2, 9, 10, 1, 0, tzinfo=ET)
    strategy._seed_ready = False
    strategy._last_seen_ts_ms["QQQ"] = int((now.timestamp() - 2.0) * 1000)
    strategy._warmup_entry_seed_data = AsyncMock()

    asyncio.run(strategy._maybe_retry_seed_warmup(now))

    strategy._warmup_entry_seed_data.assert_not_awaited()


def test_seed_warmup_retry_runs_when_qqq_stream_is_stale() -> None:
    strategy = _make_strategy()
    strategy.params.extras["freshness_max_sec"] = 5
    now = datetime(2026, 2, 9, 10, 1, 0, tzinfo=ET)
    strategy._seed_ready = False
    strategy._warmup_entry_seed_data = AsyncMock()

    asyncio.run(strategy._maybe_retry_seed_warmup(now))

    strategy._warmup_entry_seed_data.assert_awaited_once()


def test_seed_warmup_retry_skips_after_live_qqq_seen_even_if_stale() -> None:
    strategy = _make_strategy()
    strategy.params.extras["freshness_max_sec"] = 5
    now = datetime(2026, 2, 9, 10, 1, 0, tzinfo=ET)
    strategy._seed_ready = False
    strategy._last_seen_ts_ms["QQQ"] = int((now.timestamp() - 30.0) * 1000)
    strategy._warmup_entry_seed_data = AsyncMock()

    asyncio.run(strategy._maybe_retry_seed_warmup(now))

    strategy._warmup_entry_seed_data.assert_not_awaited()


def test_entry_data_integrity_failure_reports_specific_reason() -> None:
    strategy = _make_strategy()
    now = datetime(2026, 2, 9, 9, 50, 0, tzinfo=ET)
    strategy._last_seen_ts_ms["QQQ"] = int((now.timestamp() - 1.0) * 1000)
    strategy._last_seen_ts_ms["QQEW"] = int((now.timestamp() - 1.0) * 1000)
    strategy._price_base_source = "unknown"

    assert strategy._entry_data_integrity_failure(now, strategy.params.extras) == "price_base_source"

    strategy._price_base_source = "sfs_0930_window"
    strategy._last_seen_ts_ms["QQQ"] = int((now.timestamp() - 10.0) * 1000)
    assert strategy._entry_data_integrity_failure(now, strategy.params.extras) == "qqq_age"
