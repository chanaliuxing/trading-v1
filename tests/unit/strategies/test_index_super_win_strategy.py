from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
import json

import pytest

from libs.strategy import OrderUpdate, StrategyParams
from services.strategy_runtime_v2 import (
    RuntimeLifecycle,
    RuntimeState,
    get_shared_ownership_service,
    get_shared_runtime_store,
)
from strategies.index_super_win_strategy import IndexSuperWinStrategy


@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = MagicMock()
    ctx.strategy_id = "index_super_win_001"
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    ib.positions.return_value = []
    ib.openTrades.return_value = []
    ib_adapter = MagicMock()
    ib_adapter.ib = ib
    ib_adapter.account = "DU1"
    market_data_service = MagicMock()
    market_data_service.ib_adapter = ib_adapter
    ctx.engine = MagicMock()
    ctx.engine.market_data_service = market_data_service
    ctx.subscribe_features = AsyncMock()
    ctx.set_cadence = AsyncMock()
    ctx.submit_intent = AsyncMock()
    ctx.strategy_run_id = "idx-run-1"
    ctx.engine.audit_writer = MagicMock()
    return ctx


@pytest.fixture(autouse=True)
async def reset_shared_runtime_and_ownership():
    for scope in ["index_super_win"]:
        store = get_shared_runtime_store(scope)
        for state in await store.list_states():
            await store.delete_state(state.strategy_id, state.symbol)
        ownership = get_shared_ownership_service(scope)
        ownership._records.clear()
    yield


@pytest.mark.asyncio
async def test_initialize_subscribes_symbols_and_stays_dry_run(mock_ctx):
    params = StrategyParams(
        cadence_s=3,
        ttl_ms=2500,
        extras={
            "symbols": ["AAPL", "MSFT"],
            "feature_bar_sec": 10,
            "dry_run": True,
        },
    )

    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    mock_ctx.subscribe_features.assert_awaited_once_with(["AAPL", "MSFT"], bar_sec=10)
    mock_ctx.set_cadence.assert_awaited_once_with(3)
    assert strategy.state["dry_run"] is True
    assert strategy.state["symbols"] == ["AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_on_features_records_preview_without_submitting_live_intent(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "is_entry_signal": True,
            "intent_target_qty": 50.0,
            "intent_side": "LONG",
            "signal_reason": "unit_test_signal",
            "signal_price": 188.25,
        },
    )

    preview = strategy._last_preview_by_symbol["AAPL"]
    assert preview["status"] == "DRY_RUN_PREVIEW"
    assert preview["runtime_lifecycle"] == "preview"
    assert preview["exec_symbol"] == "AAPL"
    mock_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_features_uses_binding_exec_symbol_for_preview(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "instrument_bindings": [
                {"key": "qqq_levered", "signal_symbol": "QQQ", "exec_symbol": "TQQQ"},
            ],
            "dry_run": True,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 12.0,
            "intent_side": "LONG",
            "signal_reason": "mapped_signal",
            "signal_price": 602.12,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["exec_symbol"] == "TQQQ"
    assert ":TQQQ:ENTRY:" in str(preview["order_ref"])


@pytest.mark.asyncio
async def test_on_features_uses_manifest_side_specific_exec_symbol(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "replica_manifests": [
                {
                    "key": "levered_qqq",
                    "signal_symbol": "QQQ",
                    "confirm_symbol": "QQEW",
                    "long_exec_symbol": "TQQQ",
                    "short_exec_symbol": "SQQQ",
                    "volatility_symbol": "QQQ",
                }
            ],
            "dry_run": True,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 10.0,
            "intent_side": "SHORT",
            "signal_reason": "manifest_short_signal",
            "signal_price": 500.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["exec_symbol"] == "SQQQ"
    assert ":SQQQ:ENTRY:" in str(preview["order_ref"])
    assert strategy.state["replica_manifest"]["symbols"][0]["confirm_symbol"] == "QQEW"


@pytest.mark.asyncio
async def test_on_features_submits_live_intent_and_records_submitted_runtime(mock_ctx):
    mock_ctx.submit_intent.return_value = {"status": "APPROVED", "reasons": []}
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 7.0,
            "intent_side": "LONG",
            "signal_reason": "live_signal",
            "signal_price": 188.25,
        },
    )

    preview = strategy._last_preview_by_symbol["AAPL"]
    assert preview["status"] == "INTENT_SUBMITTED"
    assert preview["runtime_lifecycle"] == "intent_submitted"
    mock_ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_submit_injects_trace_fields_and_emits_local_audit(mock_ctx):
    mock_ctx.submit_intent.return_value = {"status": "APPROVED", "reasons": []}
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 7.0,
            "intent_side": "LONG",
            "signal_reason": "audit_bridge_live_signal",
            "signal_price": 188.25,
        },
    )

    envelope = mock_ctx.submit_intent.await_args.args[0]
    extras = dict(envelope.extras or {})
    assert extras.get("strategy_run_id") == "idx-run-1"
    assert extras.get("intent_source") == "index_super_win"
    assert extras.get("trigger_id")
    assert extras.get("trace_id") == envelope.decision_id
    stages = [call.kwargs.get("stage") for call in mock_ctx.engine.audit_writer.emit.call_args_list]
    assert "strategy_started" in stages
    assert "strategy_ready" in stages
    assert "startup_consistency_checked" in stages
    assert "entry_intent_requested" in stages
    assert "entry_intent_submitted" in stages


@pytest.mark.asyncio
async def test_on_features_rejected_submit_does_not_persist_submitted_runtime(mock_ctx):
    mock_ctx.submit_intent.return_value = {"status": "REJECTED", "reasons": ["capacity_block"]}
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 7.0,
            "intent_side": "LONG",
            "signal_reason": "live_signal",
            "signal_price": 188.25,
        },
    )

    preview = strategy._last_preview_by_symbol["AAPL"]
    assert preview["status"] == "SUBMIT_REJECTED"
    assert preview["runtime_lifecycle"] == "preview"
    assert preview["submit_status"] == "REJECTED"


@pytest.mark.asyncio
async def test_rejected_submit_emits_warning_local_audit(mock_ctx):
    mock_ctx.submit_intent.return_value = {"status": "REJECTED", "reasons": ["capacity_block"]}
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 7.0,
            "intent_side": "LONG",
            "signal_reason": "audit_bridge_rejected_signal",
        },
    )

    rejected = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "entry_intent_rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0]["severity"] == "warning"
    assert rejected[0]["status"] == "rejected"
    assert rejected[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_initialize_quarantines_unknown_broker_position(mock_ctx):
    mock_ctx.engine.market_data_service.ib_adapter.ib.positions.return_value = [
        SimpleNamespace(
            account="DU1",
            contract=SimpleNamespace(symbol="AAPL"),
            position=9.0,
            avgCost=187.5,
        )
    ]
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "broker_reconcile_on_init": True,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)

    await strategy.initialize()

    state = await strategy._runtime_store.get_state(strategy.strategy_id, "AAPL")
    assert state is not None
    assert state.lifecycle.value == "quarantine"
    ownership = await strategy._ownership_service.get("AAPL")
    assert ownership.state.value == "quarantined"


@pytest.mark.asyncio
async def test_on_features_blocks_entry_when_broker_position_is_unknown(mock_ctx):
    mock_ctx.engine.market_data_service.ib_adapter.ib.positions.return_value = [
        SimpleNamespace(
            account="DU1",
            contract=SimpleNamespace(symbol="AAPL"),
            position=9.0,
            avgCost=187.5,
        )
    ]
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "broker_reconcile_on_init": False,
            "pre_entry_broker_reconcile": True,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 7.0,
            "intent_side": "LONG",
            "signal_reason": "live_signal",
            "signal_price": 188.25,
        },
    )

    preview = strategy._last_preview_by_symbol["AAPL"]
    assert preview["status"] == "QUARANTINED"
    mock_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_features_blocks_entry_when_unknown_open_orders_exist(mock_ctx):
    mock_ctx.engine.market_data_service.ib_adapter.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            order=SimpleNamespace(
                orderId=17,
                action="BUY",
                totalQuantity=10.0,
                orderType="LMT",
                orderRef="qqqv1:qqq_intraday_001:AAPL:ENTRY:TRG:abc",
                account="DU1",
            ),
            orderStatus=SimpleNamespace(status="Submitted"),
        )
    ]
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "broker_reconcile_on_init": False,
            "pre_entry_open_orders_reconcile": True,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 7.0,
            "intent_side": "LONG",
            "signal_reason": "live_signal",
            "signal_price": 188.25,
        },
    )

    preview = strategy._last_preview_by_symbol["AAPL"]
    assert preview["status"] == "QUARANTINED"
    mock_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_diag_snapshot_tracks_entry_block_reasons(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": False,
            "intent_target_qty": 7.0,
            "intent_side": "LONG",
            "signal_reason": "no_signal",
        },
    )

    diag = strategy.state.get("diag_snapshot") or {}
    counters = diag.get("counters") or {}
    reasons = diag.get("reason_counters_top") or {}
    trader_view = (diag.get("trader_view_by_symbol") or {}).get("AAPL") or {}
    assert int(counters.get("entry_gate.blocked", 0)) >= 1
    assert int(reasons.get("signal_inactive", 0)) >= 1
    assert trader_view.get("final_state") == "blocked"
    assert int((trader_view.get("top_reasons") or {}).get("signal_inactive", 0)) >= 1


@pytest.mark.asyncio
async def test_signal_inactive_emits_gate_blocked_local_audit(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": False,
            "intent_target_qty": 7.0,
            "intent_side": "LONG",
            "signal_reason": "no_signal",
        },
    )

    blocked = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "entry_gate_blocked"
    ]
    assert len(blocked) >= 1
    assert blocked[-1]["reason"] == "signal_inactive"
    assert blocked[-1]["status"] == "blocked"
    assert blocked[-1]["trace_id"]


@pytest.mark.asyncio
async def test_diag_files_written_on_shutdown(tmp_path, mock_ctx):
    diag_log = tmp_path / "diag.jsonl"
    diag_snapshot = tmp_path / "diag_snapshot.json"
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "enable_observability_file": True,
            "diag_log_path": str(diag_log),
            "diag_snapshot_path": str(diag_snapshot),
            "diag_flush_interval_sec": 0.01,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 1.0,
            "intent_side": "LONG",
            "signal_reason": "unit_signal",
            "signal_price": 188.25,
        },
    )
    await strategy.on_shutdown("unit_test_done")

    assert diag_log.exists()
    assert diag_snapshot.exists()
    payload = json.loads(diag_snapshot.read_text(encoding="utf-8"))
    assert payload.get("strategy_id") == "index_super_win_001"
    assert "entry.attempts" in (payload.get("counters") or {})
    assert "trader_view_by_symbol" in payload


@pytest.mark.asyncio
async def test_shutdown_emits_local_audit(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()
    await strategy.on_shutdown("unit_test_shutdown")

    shutdown = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "strategy_shutdown_started"
    ]
    assert len(shutdown) == 1
    assert shutdown[0]["domain"] == "system"
    assert shutdown[0]["status"] == "stopping"
    assert shutdown[0]["reason"] == "unit_test_shutdown"


@pytest.mark.asyncio
async def test_market_feature_minute_summary_emits_on_minute_rollover(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "enable_observability_file": False,
            "enable_market_audit_bridge": True,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 1.0,
            "intent_side": "LONG",
            "signal_reason": "minute_one",
            "signal_price": 100.0,
            "ts_bar_end_us": 60_000_000,
        },
    )
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 1.0,
            "intent_side": "LONG",
            "signal_reason": "minute_two",
            "signal_price": 101.0,
            "ts_bar_end_us": 120_000_000,
        },
    )

    summaries = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "feature_minute_summary"
    ]
    assert len(summaries) >= 1
    assert summaries[0]["domain"] == "market"
    assert summaries[0]["symbol"] == "AAPL"
    assert summaries[0]["payload"]["feature_count"] == 1
    assert summaries[0]["payload"]["last_signal_reason"] == "minute_one"
    assert summaries[0]["payload"]["last_signal_side"] == "LONG"
    assert summaries[0]["payload"]["last_target_qty"] == 1.0


@pytest.mark.asyncio
async def test_market_feature_minute_summary_includes_trader_numeric_fields(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["QQQ"],
            "dry_run": True,
            "enable_observability_file": False,
            "enable_market_audit_bridge": True,
            "instrument_bindings": [
                {
                    "key": "qqq_pair",
                    "signal_symbol": "QQQ",
                    "exec_symbol": "TQQQ",
                    "confirm_symbol": "QQQ",
                }
            ],
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 2.0,
            "intent_side": "LONG",
            "signal_reason": "param_snapshot",
            "signal_price": 100.25,
            "exec_price": 50.5,
            "qqq_close_now": 100.25,
            "vwap_now": 99.8,
            "atr_live": 1.2,
            "atr_eff": 1.1,
            "atr_seed": 0.9,
            "atr_cap": 1.5,
            "distance_from_vwap_atr": 0.41,
            "vol_surge_ratio": 1.8,
            "vol_ratio": 1.3,
            "v3m_price": 0.55,
            "v3m_signed": 0.44,
            "ts_bar_end_us": 60_000_000,
        },
    )
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "intent_target_qty": 0.0,
            "intent_side": "LONG",
            "signal_reason": "flush_next_minute",
            "signal_price": 100.5,
            "ts_bar_end_us": 120_000_000,
        },
    )

    summaries = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "feature_minute_summary"
    ]
    assert len(summaries) >= 1
    payload = summaries[0]["payload"]
    assert payload["last_signal_price"] == 100.25
    assert payload["last_exec_price"] == 50.5
    assert payload["last_vwap_now"] == 99.8
    assert payload["last_atr_live"] == 1.2
    assert payload["last_atr_eff"] == 1.1
    assert payload["last_atr_seed"] == 0.9
    assert payload["last_atr_cap"] == 1.5
    assert payload["last_distance_from_vwap_atr"] == 0.41
    assert payload["last_vol_surge_ratio"] == 1.8
    assert payload["last_vol_ratio"] == 1.3
    assert payload["last_v3m_price"] == 0.55
    assert payload["last_v3m_signed"] == 0.44


@pytest.mark.asyncio
async def test_market_feature_gap_and_missing_price_emit_warning_audit(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "enable_observability_file": False,
            "enable_market_audit_bridge": True,
            "market_audit_gap_sec": 10.0,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 1.0,
            "intent_side": "LONG",
            "signal_reason": "first_tick",
            "signal_price": 100.0,
            "ts_bar_end_us": 60_000_000,
        },
    )
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 1.0,
            "intent_side": "LONG",
            "signal_reason": "gap_and_missing_price",
            "ts_bar_end_us": 75_000_000,
        },
    )

    gap_events = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "feature_stream_gap"
    ]
    missing_events = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "feature_price_missing"
    ]
    assert len(gap_events) == 1
    assert gap_events[0]["domain"] == "market"
    assert gap_events[0]["status"] == "stale"
    assert len(missing_events) == 1
    assert missing_events[0]["severity"] == "warning"


@pytest.mark.asyncio
async def test_independent_runtime_state_and_diag_paths_do_not_reference_qqq(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 1.0,
            "intent_side": "LONG",
            "signal_reason": "independent_runtime_contract",
        },
    )

    assert strategy.state["runtime_scope"] == "index_super_win"
    assert "index_super_win_diag_" in str(strategy.state["diag_log_path"])
    assert "index_super_win_diag_snapshot_" in str(strategy.state["diag_snapshot_path"])
    snapshot_text = json.dumps(strategy.state.get("diag_snapshot") or {}, ensure_ascii=True, sort_keys=True)
    assert "qqqv1" not in snapshot_text
    assert "qqq_intraday_diag" not in snapshot_text


@pytest.mark.asyncio
async def test_initialize_clears_stale_submitted_runtime_when_broker_is_clean(mock_ctx):
    scope = "index_super_win_test_stale_clear"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=1,
        )
    )
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "runtime_scope": scope,
            "enable_observability_file": False,
            "startup_stale_submitted_age_sec": 0.01,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)

    await strategy.initialize()

    state = await store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert state.lifecycle == RuntimeLifecycle.FLAT
    recovery = (strategy.state.get("startup_recovery_by_symbol") or {}).get("AAPL") or {}
    assert recovery.get("action") == "cleared_stale_intent_submitted"
    runtime_health = (strategy.state.get("runtime_health_by_symbol") or {}).get("AAPL") or {}
    assert runtime_health.get("runtime_lifecycle") == "flat"


@pytest.mark.asyncio
async def test_initialize_records_runtime_health_when_foreign_open_order_exists(mock_ctx):
    scope = "index_super_win_test_foreign_runtime_health"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=1,
        )
    )
    mock_ctx.engine.market_data_service.ib_adapter.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            order=SimpleNamespace(
                orderId=77,
                action="BUY",
                totalQuantity=10.0,
                orderType="LMT",
                orderRef="qqqv1:qqq_intraday_001:AAPL:ENTRY:TRG:foreign",
                account="DU1",
            ),
            orderStatus=SimpleNamespace(status="Submitted"),
        )
    ]
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "runtime_scope": scope,
            "broker_reconcile_on_init": False,
            "enable_observability_file": False,
            "startup_stale_submitted_age_sec": 0.01,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)

    await strategy.initialize()

    state = await store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert state.lifecycle == RuntimeLifecycle.QUARANTINE
    recovery = (strategy.state.get("startup_recovery_by_symbol") or {}).get("AAPL") or {}
    assert recovery.get("action") == "quarantined_stale_intent_foreign_orders"
    assert int(recovery.get("foreign_open_orders", 0)) == 1
    assert int(recovery.get("foreign_order_conflicts_detected", 0)) == 1
    diag = strategy.state.get("diag_snapshot") or {}
    counters = diag.get("counters") or {}
    conflict_report = (diag.get("conflict_report_by_symbol") or {}).get("AAPL") or {}
    assert int(counters.get("conflict.foreign_open_orders", 0)) >= 1
    assert int(conflict_report.get("foreign_open_order_conflicts", 0)) == 1
    assert conflict_report.get("startup_recovery_action") == "quarantined_stale_intent_foreign_orders"


@pytest.mark.asyncio
async def test_initialize_clears_stale_pending_entry_runtime_when_broker_is_clean(mock_ctx):
    scope = "index_super_win_test_stale_pending_entry_clear"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="MSFT",
            lifecycle=RuntimeLifecycle.PENDING_ENTRY,
            target_qty=3.0,
            side="LONG",
            active_trigger_id="trg999",
            entry_order_ref="iswv2:index_super_win_001:MSFT:ENTRY:TRG:trg999",
            last_synced_at_us=1,
        )
    )
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["MSFT"],
            "dry_run": True,
            "runtime_scope": scope,
            "enable_observability_file": False,
            "startup_stale_submitted_age_sec": 0.01,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)

    await strategy.initialize()

    state = await store.get_state("index_super_win_001", "MSFT")
    assert state is not None
    assert state.lifecycle == RuntimeLifecycle.FLAT
    recovery = (strategy.state.get("startup_recovery_by_symbol") or {}).get("MSFT") or {}
    assert recovery.get("action") == "cleared_stale_pending_entry"
    runtime_health = (strategy.state.get("runtime_health_by_symbol") or {}).get("MSFT") or {}
    assert runtime_health.get("runtime_lifecycle") == "flat"


@pytest.mark.asyncio
async def test_on_features_blocks_when_startup_consistency_is_not_ready(mock_ctx):
    mock_ctx.engine.market_data_service.ib_adapter.ib.isConnected.return_value = False
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)

    await strategy.initialize()
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "LONG",
            "signal_reason": "should_block_startup_not_ready",
        },
    )

    assert strategy.state["entry_enabled_by_symbol"]["AAPL"] is False
    preview = strategy._last_preview_by_symbol["AAPL"]
    assert preview["status"] == "STARTUP_NOT_READY"
    assert "startup_consistency" in str(preview.get("block_reason"))
    mock_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_blocks_entry_when_open_orders_probe_fails(mock_ctx):
    mock_ctx.engine.market_data_service.ib_adapter.ib.openTrades.side_effect = RuntimeError("ib_open_orders_down")
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "enable_observability_file": False,
            "broker_reconcile_on_init": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)

    await strategy.initialize()

    startup = (strategy.state.get("startup_consistency_by_symbol") or {}).get("AAPL") or {}
    assert startup.get("entry_enabled") is False
    assert "open_orders_probe_error" in str(startup.get("block_reason"))


@pytest.mark.asyncio
async def test_on_features_blocks_duplicate_entry_when_runtime_pending_with_owned_open_order(mock_ctx):
    scope = "index_super_win_test_duplicate_entry_guard"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=int(1e6),
        )
    )
    mock_ctx.engine.market_data_service.ib_adapter.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            order=SimpleNamespace(
                orderId=77,
                action="BUY",
                totalQuantity=5.0,
                orderType="LMT",
                orderRef="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
                account="DU1",
            ),
            orderStatus=SimpleNamespace(status="Submitted"),
        )
    ]
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "runtime_scope": scope,
            "broker_reconcile_on_init": False,
            "startup_runtime_recovery": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)

    await strategy.initialize()
    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "LONG",
            "signal_reason": "duplicate_entry_guard",
        },
    )

    preview = strategy._last_preview_by_symbol["AAPL"]
    assert preview["status"] == "RUNTIME_GUARD_BLOCKED"
    assert preview["runtime_lifecycle"] == "intent_submitted"
    assert preview["block_reason"] == "runtime_guard:intent_already_submitted"
    mock_ctx.submit_intent.assert_not_awaited()

    runtime_blocked = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "runtime_guard_blocked"
    ]
    assert len(runtime_blocked) == 1
    assert runtime_blocked[0]["reason"] == "runtime_guard:intent_already_submitted"
    assert runtime_blocked[0]["trigger_id"] == "abc123"


@pytest.mark.asyncio
async def test_restart_with_foreign_open_orders_quarantines_without_cleanup_attempt(mock_ctx):
    scope = "index_super_win_test_restart_foreign_no_cleanup"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=1,
        )
    )
    mock_ctx.engine.market_data_service.ib_adapter.ib.cancelOrder = MagicMock()
    mock_ctx.engine.market_data_service.ib_adapter.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            order=SimpleNamespace(
                orderId=91,
                action="BUY",
                totalQuantity=10.0,
                orderType="LMT",
                orderRef="qqqv1:qqq_intraday_001:AAPL:ENTRY:TRG:foreign",
                account="DU1",
            ),
            orderStatus=SimpleNamespace(status="Submitted"),
        )
    ]
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "runtime_scope": scope,
            "broker_reconcile_on_init": False,
            "enable_observability_file": False,
            "startup_stale_submitted_age_sec": 0.01,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)

    await strategy.initialize()

    state = await store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert state.lifecycle == RuntimeLifecycle.QUARANTINE
    assert mock_ctx.engine.market_data_service.ib_adapter.ib.cancelOrder.call_count == 0
    mock_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_symbol_foreign_ownership_conflict_is_visible_in_diag_snapshot(mock_ctx):
    scope = "index_super_win_test_foreign_owner_conflict"
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "runtime_scope": scope,
            "enable_observability_file": False,
            "broker_reconcile_on_init": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()
    await strategy._ownership_service.claim(
        strategy_id="qqq_intraday_001",
        symbol="AAPL",
        qty=10.0,
        trigger_id="foreigntrg",
        order_namespace="qqqv1",
    )

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "LONG",
            "signal_reason": "foreign_owner_conflict",
        },
    )

    diag = strategy.state.get("diag_snapshot") or {}
    conflict = (diag.get("conflict_report_by_symbol") or {}).get("AAPL") or {}
    trader_view = (diag.get("trader_view_by_symbol") or {}).get("AAPL") or {}
    assert conflict.get("foreign_owner_strategy_id") == "qqq_intraday_001"
    assert int(conflict.get("foreign_symbol_ownership_conflicts", 0)) >= 1
    assert trader_view.get("which_gate_blocked") == "preview:OWNERSHIP_CONFLICT:foreign_symbol_ownership_already_claimed"
    assert trader_view.get("foreign_owner_strategy_id") == "qqq_intraday_001"

    preview_events = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") in {"preview_evaluated", "preview_blocked"}
    ]
    assert [event["stage"] for event in preview_events[-2:]] == ["preview_evaluated", "preview_blocked"]
    assert preview_events[-2]["trigger_id"] == preview_events[-1]["trigger_id"]
    assert preview_events[-2]["trace_id"] == preview_events[-1]["trace_id"]


@pytest.mark.asyncio
async def test_multi_symbol_quarantine_does_not_block_unrelated_symbol(mock_ctx):
    mock_ctx.engine.market_data_service.ib_adapter.ib.openTrades.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            order=SimpleNamespace(
                orderId=88,
                action="BUY",
                totalQuantity=5.0,
                orderType="LMT",
                orderRef="qqqv1:qqq_intraday_001:AAPL:ENTRY:TRG:foreign",
                account="DU1",
            ),
            orderStatus=SimpleNamespace(status="Submitted"),
        )
    ]
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL", "MSFT"],
            "dry_run": True,
            "broker_reconcile_on_init": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "LONG",
            "signal_reason": "aapl_conflict",
        },
    )
    await strategy.on_features(
        "MSFT",
        {
            "signal_active": True,
            "intent_target_qty": 4.0,
            "intent_side": "LONG",
            "signal_reason": "msft_clean",
        },
    )

    assert strategy._last_preview_by_symbol["AAPL"]["status"] == "QUARANTINED"
    assert strategy._last_preview_by_symbol["MSFT"]["status"] == "DRY_RUN_PREVIEW"
    diag = strategy.state.get("diag_snapshot") or {}
    trader_view = diag.get("trader_view_by_symbol") or {}
    assert trader_view["AAPL"]["final_state"] == "blocked"
    assert trader_view["MSFT"]["final_state"] in {"attempted_not_entered", "no_attempt"}

    recon_events = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "broker_open_orders_quarantined"
    ]
    assert len(recon_events) >= 1
    assert recon_events[-1]["domain"] == "recon"
    assert recon_events[-1]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_three_symbols_can_run_with_separate_snapshots(mock_ctx):
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL", "MSFT", "NVDA"],
            "dry_run": True,
            "broker_reconcile_on_init": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    for symbol, qty in [("AAPL", 5.0), ("MSFT", 4.0), ("NVDA", 3.0)]:
        await strategy.on_features(
            symbol,
            {
                "signal_active": True,
                "intent_target_qty": qty,
                "intent_side": "LONG",
                "signal_reason": f"{symbol.lower()}_signal",
            },
        )

    diag = strategy.state.get("diag_snapshot") or {}
    last_preview = diag.get("last_preview_by_symbol") or {}
    trader_view = diag.get("trader_view_by_symbol") or {}
    assert sorted(last_preview.keys()) == ["AAPL", "MSFT", "NVDA"]
    assert last_preview["AAPL"]["exec_symbol"] == "AAPL"
    assert last_preview["MSFT"]["exec_symbol"] == "MSFT"
    assert last_preview["NVDA"]["exec_symbol"] == "NVDA"
    assert trader_view["AAPL"]["entry_attempts"] == 1
    assert trader_view["MSFT"]["entry_attempts"] == 1
    assert trader_view["NVDA"]["entry_attempts"] == 1


@pytest.mark.asyncio
async def test_submit_failure_on_one_symbol_does_not_corrupt_other_symbol(mock_ctx):
    responses = {
        "AAPL": {"status": "REJECTED", "reasons": ["capacity_block"]},
        "MSFT": {"status": "APPROVED", "reasons": []},
    }

    async def _submit(envelope):
        return responses[str(envelope.symbol)]

    mock_ctx.submit_intent.side_effect = _submit
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL", "MSFT"],
            "dry_run": False,
            "broker_reconcile_on_init": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "LONG",
            "signal_reason": "aapl_rejected",
        },
    )
    await strategy.on_features(
        "MSFT",
        {
            "signal_active": True,
            "intent_target_qty": 4.0,
            "intent_side": "LONG",
            "signal_reason": "msft_approved",
        },
    )

    assert strategy._last_preview_by_symbol["AAPL"]["status"] == "SUBMIT_REJECTED"
    assert strategy._last_preview_by_symbol["MSFT"]["status"] == "INTENT_SUBMITTED"
    diag = strategy.state.get("diag_snapshot") or {}
    trader_view = diag.get("trader_view_by_symbol") or {}
    assert trader_view["AAPL"]["final_state"] == "blocked"
    assert trader_view["MSFT"]["final_state"] == "entered"


@pytest.mark.asyncio
async def test_live_runtime_state_blocks_reverse_or_repeat_signal(mock_ctx):
    scope = "index_super_win_test_live_runtime_guard"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.LIVE,
            target_qty=5.0,
            live_qty=5.0,
            broker_qty=5.0,
            side="LONG",
            active_trigger_id="live123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:live123",
            last_synced_at_us=int(1e6),
        )
    )
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "runtime_scope": scope,
            "broker_reconcile_on_init": False,
            "startup_runtime_recovery": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_features(
        "AAPL",
        {
            "signal_active": True,
            "intent_target_qty": 5.0,
            "intent_side": "SHORT",
            "signal_reason": "reverse_signal_should_block",
        },
    )

    preview = strategy._last_preview_by_symbol["AAPL"]
    assert preview["status"] == "RUNTIME_GUARD_BLOCKED"
    assert preview["block_reason"] == "runtime_guard:live_position_exists"
    mock_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_protective_order_refs_updates_runtime_and_diag(mock_ctx):
    scope = "index_super_win_test_protective_orders"
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": True,
            "runtime_scope": scope,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    refs = await strategy.record_protective_order_refs(
        "AAPL",
        ["iswv2:index_super_win_001:AAPL:STOP:TRG:1", "iswv2:index_super_win_001:AAPL:TP:TRG:2"],
        source="unit_test",
    )

    assert len(refs) == 2
    state = await strategy._runtime_store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert tuple(refs) == tuple(state.protective_order_refs)
    diag = strategy.state.get("diag_snapshot") or {}
    assert (diag.get("protective_orders_by_symbol") or {}).get("AAPL") == refs
    chain = (diag.get("chain_report_by_symbol") or {}).get("AAPL") or {}
    assert (chain.get("protective_orders") or {}).get("status") == "recorded"


@pytest.mark.asyncio
async def test_order_update_submitted_advances_runtime_to_pending_entry(mock_ctx):
    scope = "index_super_win_test_order_update_submitted"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=int(1e6),
        )
    )
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "runtime_scope": scope,
            "broker_reconcile_on_init": False,
            "startup_runtime_recovery": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_order_update(
        OrderUpdate(
            order_id="101",
            strategy_id="index_super_win_001",
            symbol="AAPL",
            state="Submitted",
            order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            broker_order_id="9001",
        )
    )

    state = await store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert state.lifecycle == RuntimeLifecycle.PENDING_ENTRY
    events = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "order_state_updated"
    ]
    assert len(events) >= 1
    assert events[-1]["domain"] == "exec"
    assert events[-1]["status"] == "submitted"
    assert events[-1]["payload"]["action"] == "mark_pending_entry"


@pytest.mark.asyncio
async def test_order_update_cancelled_clears_runtime_when_broker_is_flat(mock_ctx):
    scope = "index_super_win_test_order_update_cancelled"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=int(1e6),
        )
    )
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "runtime_scope": scope,
            "broker_reconcile_on_init": False,
            "startup_runtime_recovery": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_order_update(
        OrderUpdate(
            order_id="101",
            strategy_id="index_super_win_001",
            symbol="AAPL",
            state="Cancelled",
            order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            broker_order_id="9001",
        )
    )

    state = await store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert state.lifecycle == RuntimeLifecycle.FLAT
    assert state.last_error == "order_update:cancelled:broker_flat"
    events = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "order_state_updated"
    ]
    assert len(events) >= 1
    assert events[-1]["payload"]["runtime_lifecycle_after"] == "flat"


@pytest.mark.asyncio
async def test_order_update_filled_marks_runtime_live_from_broker(mock_ctx):
    scope = "index_super_win_test_order_update_filled"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=int(1e6),
        )
    )
    mock_ctx.engine.market_data_service.ib_adapter.ib.positions.return_value = [
        SimpleNamespace(
            account="DU1",
            contract=SimpleNamespace(symbol="AAPL"),
            position=5.0,
            avgCost=188.5,
        )
    ]
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "runtime_scope": scope,
            "broker_reconcile_on_init": False,
            "startup_runtime_recovery": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()

    await strategy.on_order_update(
        OrderUpdate(
            order_id="101",
            strategy_id="index_super_win_001",
            symbol="AAPL",
            state="Filled",
            order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            broker_order_id="9001",
        )
    )

    state = await store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert state.lifecycle == RuntimeLifecycle.LIVE
    assert state.live_qty == pytest.approx(5.0)
    assert state.broker_qty == pytest.approx(5.0)
    events = [
        call.kwargs
        for call in mock_ctx.engine.audit_writer.emit.call_args_list
        if call.kwargs.get("stage") == "order_state_updated"
    ]
    assert len(events) >= 1
    assert events[-1]["payload"]["action"] == "live_from_broker"
    assert events[-1]["payload"]["runtime_lifecycle_after"] == "live"


@pytest.mark.asyncio
async def test_order_update_ignores_untrusted_foreign_ref(mock_ctx):
    scope = "index_super_win_test_order_update_untrusted"
    store = get_shared_runtime_store(scope)
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
            target_qty=5.0,
            side="LONG",
            active_trigger_id="abc123",
            entry_order_ref="iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc123",
            last_synced_at_us=int(1e6),
        )
    )
    params = StrategyParams(
        cadence_s=5,
        ttl_ms=2000,
        extras={
            "symbols": ["AAPL"],
            "dry_run": False,
            "runtime_scope": scope,
            "broker_reconcile_on_init": False,
            "startup_runtime_recovery": False,
            "enable_observability_file": False,
        },
    )
    strategy = IndexSuperWinStrategy(mock_ctx, params)
    await strategy.initialize()
    baseline_calls = len(mock_ctx.engine.audit_writer.emit.call_args_list)

    await strategy.on_order_update(
        OrderUpdate(
            order_id="101",
            strategy_id="index_super_win_001",
            symbol="AAPL",
            state="Filled",
            order_ref="qqqv1:qqq_intraday_001:AAPL:ENTRY:TRG:foreign",
            broker_order_id="9001",
        )
    )

    state = await store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert state.lifecycle == RuntimeLifecycle.INTENT_SUBMITTED
    assert len(mock_ctx.engine.audit_writer.emit.call_args_list) == baseline_calls
