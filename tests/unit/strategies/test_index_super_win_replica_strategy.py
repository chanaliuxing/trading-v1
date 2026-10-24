from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from libs.strategy import OrderUpdate, PositionSnapshot, StrategyParams
from strategies.index_super_win_replica_ownership_types import ReplicaOwnershipIntentRecord
from strategies.index_super_win_replica_strategy import ET, IndexSuperWinReplicaStrategy


@pytest.fixture
def replica_ctx():
    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = MagicMock()
    ctx.strategy_id = "index_super_win_replica_001"
    ctx.subscribe_features = AsyncMock()
    ctx.set_cadence = AsyncMock()
    ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED", "reasons": []})
    ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    return ctx


@pytest.fixture(autouse=True)
async def _cleanup_stop_audit_tasks():
    yield
    current = asyncio.current_task()
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and str(task.get_name() or "").startswith("index_super_win_stop_audit:")
    ]
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass


class _CaptureAuditWriter:
    def __init__(self) -> None:
        self.records = []

    def emit(self, **kwargs):
        self.records.append(dict(kwargs))
        return True


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("outside_ab_window", "OUTSIDE_AB_WINDOW"),
        ("after_no_new_entry", "AFTER_NO_NEW_ENTRY"),
        ("decision_pipeline_unavailable", "SIGNAL_GATE_FAIL_DECISION_PIPELINE_UNAVAILABLE"),
        ("market_data_unavailable", "SIGNAL_GATE_FAIL_MARKET_DATA_UNAVAILABLE"),
        ("data_integrity_fail_closed", "SIGNAL_GATE_FAIL_DATA_INTEGRITY_FAIL_CLOSED"),
        ("restart_degrade_active", "SIGNAL_GATE_FAIL_RESTART_DEGRADE_ACTIVE"),
        ("required_data_missing", "SIGNAL_GATE_FAIL_REQUIRED_DATA_MISSING"),
        ("data_not_ready", "SIGNAL_GATE_FAIL_DATA_NOT_READY"),
        ("direction_mismatch", "SIGNAL_GATE_FAIL_DIRECTION_MISMATCH"),
        ("extreme_blocked", "SIGNAL_GATE_FAIL_EXTREME_BLOCKED"),
        ("first_entry_seed_not_ready", "SIGNAL_GATE_FAIL_FIRST_ENTRY_SEED_NOT_READY"),
        ("startup_anchor_embargo", "SIGNAL_GATE_FAIL_STARTUP_ANCHOR_EMBARGO"),
        ("ownership_conflict", "OWNERSHIP_CONFLICT"),
        ("signal_inactive_or_invalid", "SIGNAL_INACTIVE_OR_INVALID"),
        ("unknown_reason", "UNKNOWN_REASON"),
    ],
)
def test_trader_reason_code_mapping(reason: str, expected: str) -> None:
    assert IndexSuperWinReplicaStrategy._trader_reason_code(reason) == expected


@pytest.mark.asyncio
async def test_replica_strategy_initializes_from_manifest(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "protection_audit_self_heal_after_misses": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    replica_ctx.subscribe_features.assert_awaited_once_with(["QQQ"], bar_sec=5)
    assert "QQQ" in strategy.state["symbols"]
    assert strategy.state["replica_manifest"]["symbols"][0]["short_exec_symbol"] == "SQQQ"


@pytest.mark.asyncio
async def test_replica_strategy_dry_run_uses_side_specific_exec_symbol(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "protection_audit_self_heal_after_misses": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "SHORT",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "DRY_RUN_PREVIEW"
    assert preview["exec_symbol"] == "SQQQ"
    assert ":SQQQ:ENTRY:" in str(preview["order_ref"])
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_blocks_entry_outside_session_window(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "session_name": "premarket",
                "session_start_et": "04:00:00",
                "session_end_et": "09:29:59",
                "enable_entry_during_session_only": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-26T12:00:00-04:00",
        },
    )
    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "outside_session_window"
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_blocks_entry_when_previous_session_not_flat(replica_ctx, tmp_path):
    replica_ctx.open_orders = AsyncMock(return_value=[])
    prev_runtime_path = tmp_path / "prev_runtime.json"
    prev_runtime_path.write_text(
        '{"QQQ":{"is_traded":true,"active_exec_symbol":"TQQQ","expected_entry_qty":8,"extras":{"live_qty":8}}}',
        encoding="utf-8",
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "session_name": "afterhours",
                "session_start_et": "16:00:00",
                "session_end_et": "19:59:59",
                "enable_entry_during_session_only": True,
                "require_prev_session_flat": True,
                "prev_session_strategy_id": "qqq_intraday_001",
                "prev_session_runtime_snapshot_path": str(prev_runtime_path),
                "prev_session_order_namespace": "qqqv1",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-26T16:10:00-04:00",
        },
    )
    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "previous_session_runtime_not_flat"
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_pending_decision_blocks_duplicate_entry(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "strict_recovered_lot_mode": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    strategy._symbol_state["QQQ"].pending_decision_id = "pending-x"

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PENDING_DECISION_BLOCKED"
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_pending_decision_clears_when_flat_and_no_active_orders(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(return_value=[])
    replica_ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "pending_decision_min_hold_sec": 0.0,
                "pending_decision_probe_interval_sec": 0.0,
                "pending_decision_flat_probe_need": 2,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.pending_decision_id = "pending-x"
    state.active_trigger_id = "trigger-x"
    state.expected_entry_qty = 8
    state.entry_order_ref = "iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:pending-x"
    state.extras["pending_decision_since_et"] = datetime.now(ET) - timedelta(seconds=9)
    state.extras["pending_decision_flat_probe_count"] = 1
    state.extras["pending_decision_last_probe_et"] = datetime.now(ET) - timedelta(seconds=2)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_INTENT_SUBMITTED"
    assert strategy._symbol_state["QQQ"].pending_decision_id is not None
    assert strategy._symbol_state["QQQ"].pending_decision_id != "pending-x"
    assert "pending_decision_cleared" in [row["stage"] for row in writer.records]
    replica_ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_replica_strategy_pending_decision_not_cleared_when_active_orders_exist(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="open-1",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:open-1",
            )
        ]
    )
    replica_ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "pending_decision_min_hold_sec": 0.0,
                "pending_decision_probe_interval_sec": 0.0,
                "pending_decision_flat_probe_need": 2,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.pending_decision_id = "pending-x"

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PENDING_DECISION_BLOCKED"
    assert strategy._symbol_state["QQQ"].pending_decision_id == "pending-x"
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_pending_decision_timeout_clears_and_arms_cooldown(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(
        side_effect=[
            [
                OrderUpdate(
                    order_id="91",
                    strategy_id="index_super_win_replica_001",
                    symbol="TQQQ",
                    state="SUBMITTED",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:trigger-x",
                )
            ],
            [],
        ]
    )
    broker = MagicMock()
    broker.cancel = AsyncMock(return_value=True)
    replica_ctx.engine.execution_service = MagicMock(kernel=MagicMock(broker=broker))
    replica_ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "entry_fill_timeout_sec": 5.0,
                "entry_timeout_reentry_cooldown_sec": 75.0,
                "pending_decision_min_hold_sec": 0.0,
                "pending_decision_probe_interval_sec": 0.0,
                "pending_decision_flat_probe_need": 2,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.pending_decision_id = "pending-x"
    state.active_trigger_id = "trigger-x"
    state.expected_entry_qty = 8
    state.entry_order_ref = "iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:pending-x"
    state.extras["pending_decision_since_et"] = datetime(2026, 3, 25, 9, 44, 0, tzinfo=ET)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_TIMEOUT_COOLDOWN_BLOCKED"
    assert preview["reason"] == "entry_timeout_cooldown"
    assert strategy._symbol_state["QQQ"].pending_decision_id is None
    assert strategy._symbol_state["QQQ"].extras.get("entry_timeout_block_until_et") is not None
    broker.cancel.assert_awaited_once_with(91)
    assert "entry_fill_timeout_cleared" in [row["stage"] for row in writer.records]
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_pending_timeout_prefers_trigger_scoped_cancel(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(
        side_effect=[
            [
                OrderUpdate(
                    order_id="91",
                    strategy_id="index_super_win_replica_001",
                    symbol="TQQQ",
                    state="SUBMITTED",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:trigger-x",
                ),
                OrderUpdate(
                    order_id="92",
                    strategy_id="index_super_win_replica_001",
                    symbol="TQQQ",
                    state="SUBMITTED",
                    order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:trigger-y",
                ),
            ],
            [],
        ]
    )
    broker = MagicMock()
    broker.cancel = AsyncMock(return_value=True)
    replica_ctx.engine.execution_service = MagicMock(kernel=MagicMock(broker=broker))
    replica_ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "entry_fill_timeout_sec": 5.0,
                "entry_timeout_reentry_cooldown_sec": 75.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.pending_decision_id = "pending-x"
    state.active_trigger_id = "trigger-x"
    state.expected_entry_qty = 8
    state.entry_order_ref = "iswv2:index_super_win_replica_001:TQQQ:ENTRY:trigger-x:pending-x"
    state.extras["pending_decision_since_et"] = datetime(2026, 3, 25, 9, 44, 0, tzinfo=ET)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    broker.cancel.assert_awaited_once_with(91)
    stages = [row["stage"] for row in writer.records]
    assert "trigger_open_orders_cancelled" in stages
    timeout_rows = [row for row in writer.records if row.get("stage") == "entry_fill_timeout_cleared"]
    assert timeout_rows[-1]["payload"]["cancel_scope"] == "trigger"


@pytest.mark.asyncio
async def test_replica_strategy_entry_timeout_cooldown_blocks_new_entry(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    strategy._symbol_state["QQQ"].extras["entry_timeout_block_until_et"] = datetime(2099, 1, 1, 9, 46, 0, tzinfo=ET)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_TIMEOUT_COOLDOWN_BLOCKED"
    assert preview["reason"] == "entry_timeout_cooldown"
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_entry_timeout_cooldown_warn_is_throttled(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "entry_timeout_block_warn_interval_sec": 15.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    strategy._symbol_state["QQQ"].extras["entry_timeout_block_until_et"] = datetime(2099, 1, 1, 9, 46, 0, tzinfo=ET)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:41-04:00",
        },
    )

    blocked_rows = [row for row in writer.records if row.get("stage") == "entry_timeout_cooldown_blocked"]
    assert len(blocked_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_entry_timeout_cooldown_expires_and_allows_entry(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    strategy._symbol_state["QQQ"].extras["entry_timeout_block_until_et"] = datetime(2026, 3, 25, 9, 45, 0, tzinfo=ET)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_INTENT_SUBMITTED"
    assert strategy._symbol_state["QQQ"].extras.get("entry_timeout_block_until_et") is None
    replica_ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_replica_strategy_entry_fill_timeout_zero_cooldown_does_not_leave_block(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    open_order = OrderUpdate(
        order_id="91",
        strategy_id="index_super_win_replica_001",
        symbol="TQQQ",
        state="SUBMITTED",
        order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:trigger-x",
    )
    open_order_calls = {"count": 0}

    async def _open_orders():
        open_order_calls["count"] += 1
        if open_order_calls["count"] == 1:
            return [open_order]
        return []

    replica_ctx.open_orders = AsyncMock(side_effect=_open_orders)
    broker = MagicMock()
    broker.cancel = AsyncMock(return_value=True)
    replica_ctx.engine.execution_service = MagicMock(kernel=MagicMock(broker=broker))
    replica_ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "entry_fill_timeout_sec": 5.0,
                "entry_timeout_reentry_cooldown_sec": 0.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.pending_decision_id = "pending-x"
    state.active_trigger_id = "trigger-x"
    state.expected_entry_qty = 8
    state.entry_order_ref = "iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:pending-x"
    state.extras["pending_decision_since_et"] = datetime(2026, 3, 25, 9, 44, 0, tzinfo=ET)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    assert strategy._last_preview_by_symbol["QQQ"]["status"] == "ENTRY_INTENT_SUBMITTED"
    assert strategy._symbol_state["QQQ"].extras.get("entry_timeout_block_until_et") is None
    broker.cancel.assert_awaited_once_with(91)
    assert "entry_fill_timeout_cleared" in [row["stage"] for row in writer.records]
    replica_ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_replica_strategy_entry_confirmed_clears_timeout_cooldown(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    strategy._symbol_state["QQQ"].extras["entry_timeout_block_until_et"] = datetime(2099, 1, 1, 9, 46, 0, tzinfo=ET)

    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    assert strategy._symbol_state["QQQ"].extras.get("entry_timeout_block_until_et") is None


@pytest.mark.asyncio
async def test_replica_strategy_position_sync_clears_timeout_cooldown(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "zero_position_flat_confirmations": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.extras["entry_timeout_block_until_et"] = datetime(2099, 1, 1, 9, 46, 0, tzinfo=ET)
    state.stop_px = 48.5

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )
    assert strategy._symbol_state["QQQ"].extras.get("entry_timeout_block_until_et") is None

    strategy._symbol_state["QQQ"].extras["entry_timeout_block_until_et"] = datetime(2099, 1, 1, 9, 46, 0, tzinfo=ET)
    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=0.0,
        )
    )
    assert strategy._symbol_state["QQQ"].extras.get("entry_timeout_block_until_et") is None


@pytest.mark.asyncio
async def test_replica_strategy_position_flat_sync_cancels_owned_open_orders(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="91",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:mine",
            ),
            OrderUpdate(
                order_id="92",
                strategy_id="qqq_intraday_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:foreign",
            ),
        ]
    )
    broker = MagicMock()
    broker.cancel = AsyncMock(return_value=True)
    replica_ctx.engine = MagicMock(execution_service=MagicMock(kernel=MagicMock(broker=broker)))
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "zero_position_flat_confirmations": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_exec_symbol = "TQQQ"
    state.active_side = "LONG"
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=0.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "POSITION_FLAT_SYNCED"
    assert preview["cancelled_orders"] == 1
    broker.cancel.assert_awaited_once_with(91)


@pytest.mark.asyncio
async def test_replica_strategy_position_flat_sync_increments_success_count_when_round_confirmed(replica_ctx):
    writer = _CaptureAuditWriter()
    broker = MagicMock()
    broker.cancel = AsyncMock(return_value=True)
    replica_ctx.engine = MagicMock(audit_writer=writer, execution_service=MagicMock(kernel=MagicMock(broker=broker)))
    replica_ctx.open_orders = AsyncMock(return_value=[])
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "max_trades_per_day": 3,
                "zero_position_flat_confirmations": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_exec_symbol = "TQQQ"
    state.active_side = "LONG"
    state.expected_entry_qty = 8
    state.entry_px = 50.0
    state.stop_px = 48.5
    state.entry_atr_used = 0.8
    state.extras["live_qty"] = 8
    state.extras["round_live_confirmed"] = True
    state.extras["day_success_count"] = 1

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=0.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["success_incremented"] is True
    assert preview["success_count"] == 2
    assert state.is_traded is False
    assert state.active_exec_symbol is None
    assert state.entry_px is None
    assert state.stop_px is None
    assert state.extras["trade_target_px"] is None
    assert state.extras["add_done"] is False
    assert state.extras["stop_dist_pre"] is None
    completed_rows = [row for row in writer.records if row.get("stage") == "trade_round_completed"]
    assert len(completed_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_position_sync_derives_trade_target_and_stop_dist(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "strict_recovered_lot_mode": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.entry_atr_used = 2.0
    state.stop_px = 48.5

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    assert state.extras["trade_target_px"] == 52.0
    assert state.extras["stop_dist_pre"] == 1.5
    assert state.extras["stop_dist_cur"] == 1.5
    assert state.extras["stop_dist_max"] == 2.25
    assert state.extras["add_done"] is False


@pytest.mark.asyncio
async def test_replica_strategy_flat_orphan_sweep_cancels_owned_orders_only(replica_ctx):
    writer = _CaptureAuditWriter()
    broker = MagicMock()
    broker.cancel = AsyncMock(return_value=True)
    replica_ctx.engine = MagicMock(audit_writer=writer, execution_service=MagicMock(kernel=MagicMock(broker=broker)))
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="91",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:mine",
            ),
            OrderUpdate(
                order_id="92",
                strategy_id="qqq_intraday_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:foreign",
            ),
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "flat_orphan_sweep_interval_sec": 30.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "intent_target_qty": 0,
            "signal_reason": "idle",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    broker.cancel.assert_awaited_once_with(91)
    sweep_rows = [row for row in writer.records if row.get("stage") == "flat_orphan_orders_cancelled"]
    assert len(sweep_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_restore_replica_runtime_coerces_datetime_extras(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "zero_position_flat_confirmations": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    strategy.state["replica_runtime"] = {
        "QQQ": {
            "is_traded": True,
            "active_side": "LONG",
            "active_exec_symbol": "TQQQ",
            "expected_entry_qty": 8,
            "entry_px": 50.0,
            "stop_px": 48.5,
            "extras": {
                "entry_time_et": "2026-03-25T09:45:00-04:00",
                "pending_decision_since_et": "2026-03-25T09:44:00-04:00",
                "pending_decision_last_probe_et": "2026-03-25T09:44:05-04:00",
                "entry_timeout_block_until_et": "2026-03-25T09:46:00-04:00",
                "last_entry_timeout_block_warn_et": "2026-03-25T09:45:40-04:00",
                "last_flat_orphan_sweep_et": "2026-03-25T09:45:10-04:00",
                "last_stop_exit_at": "2026-03-25T09:42:00-04:00",
            },
        }
    }

    await strategy.initialize()

    state = strategy._symbol_state["QQQ"]
    assert isinstance(state.extras["entry_time_et"], datetime)
    assert isinstance(state.extras["pending_decision_since_et"], datetime)
    assert isinstance(state.extras["pending_decision_last_probe_et"], datetime)
    assert isinstance(state.extras["entry_timeout_block_until_et"], datetime)
    assert isinstance(state.extras["last_entry_timeout_block_warn_et"], datetime)
    assert isinstance(state.extras["last_flat_orphan_sweep_et"], datetime)
    assert isinstance(state.extras["last_stop_exit_at"], datetime)


@pytest.mark.asyncio
async def test_replica_strategy_initialize_restores_ownership_ledger_snapshot(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    strategy.state["ownership_ledger"] = {
        "lots": [
            {
                "strategy_id": "index_super_win_replica_001",
                "position_lot_id": "lot:index_super_win_replica_001:QQQ:restored",
                "signal_symbol": "QQQ",
                "exec_symbol": "TQQQ",
                "side": "LONG",
                "planned_qty": 8.0,
                "live_qty": 8.0,
                "remaining_qty": 8.0,
                "opened_at": "2026-03-25T09:45:00-04:00",
                "status": "live",
                "payload": {"restored": True},
            }
        ],
        "order_to_lot": {},
        "broker_to_lot": {},
        "perm_to_lot": {},
        "conflicts": [],
    }

    await strategy.initialize()

    active = strategy._ownership_ledger.active_lots(
        strategy_id="index_super_win_replica_001",
        signal_symbol="QQQ",
        exec_symbol="TQQQ",
    )
    assert len(active) == 1
    assert active[0].position_lot_id == "lot:index_super_win_replica_001:QQQ:restored"
    assert isinstance(active[0].opened_at, datetime)


@pytest.mark.asyncio
async def test_replica_strategy_flat_orphan_sweep_throttled_by_interval(replica_ctx):
    broker = MagicMock()
    broker.cancel = AsyncMock(return_value=True)
    replica_ctx.engine = MagicMock(execution_service=MagicMock(kernel=MagicMock(broker=broker)))
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="91",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:mine",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "flat_orphan_sweep_interval_sec": 30.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "intent_target_qty": 0,
            "signal_reason": "idle",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "intent_target_qty": 0,
            "signal_reason": "idle",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:50-04:00",
        },
    )

    broker.cancel.assert_awaited_once_with(91)


@pytest.mark.asyncio
async def test_replica_strategy_dedupes_entry_eval_within_same_second(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    features = {
        "signal_active": True,
        "intent_target_qty": 8,
        "intent_side": "LONG",
        "signal_reason": "replica_entry",
        "signal_price": 501.0,
        "now_et": "2026-03-25T09:45:10-04:00",
    }
    await strategy.on_features("QQQ", features)
    await strategy.on_features("QQQ", features)

    assert replica_ctx.submit_intent.await_count == 1


@pytest.mark.asyncio
async def test_replica_strategy_minute_only_entry_eval_blocks_same_minute_second_hit(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "entry_check_on_new_minute_only": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:10-04:00",
        },
    )
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="REJECTED",
            order_ref=str(strategy._last_preview_by_symbol["QQQ"]["order_ref"]),
        )
    )
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    assert replica_ctx.submit_intent.await_count == 1


@pytest.mark.asyncio
async def test_replica_strategy_open_orders_block_duplicate_entry(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="open-1",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:open-1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "OPEN_ORDERS_BLOCKED"
    assert preview["reason"] == "symbol_open_orders_inflight"
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_open_orders_block_warn_is_throttled(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="open-1",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:open-1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "open_orders_block_warn_interval_sec": 60.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:41-04:00",
        },
    )

    blocked_rows = [row for row in writer.records if row.get("stage") == "open_orders_blocked_entry"]
    assert len(blocked_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_open_orders_check_failed_warn_is_throttled(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(side_effect=RuntimeError("ib-down"))
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "open_orders_check_warn_interval_sec": 300.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:41-04:00",
        },
    )

    degraded_rows = [row for row in writer.records if row.get("stage") == "open_orders_check_failed"]
    assert len(degraded_rows) == 1
    replica_ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_replica_strategy_ignores_foreign_empty_strategy_id_order_update(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )
    preview_before = dict(strategy._last_preview_by_symbol["QQQ"])

    await strategy.on_order_update(
        OrderUpdate(
            order_id="x1",
            strategy_id="",
            symbol="TQQQ",
            state="SUBMITTED",
            order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:foreign",
        )
    )

    assert strategy._last_preview_by_symbol["QQQ"] == preview_before
    ignored_rows = [row for row in writer.records if row.get("stage") == "order_update_ignored"]
    assert len(ignored_rows) == 1
    assert ignored_rows[0]["reason"] == "foreign_empty_strategy_id"


@pytest.mark.asyncio
async def test_replica_strategy_accepts_owned_empty_strategy_id_order_update(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])

    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    assert strategy._last_preview_by_symbol["QQQ"]["status"] == "ENTRY_CONFIRMED"
    assert strategy._symbol_state["QQQ"].is_traded is True


@pytest.mark.asyncio
async def test_replica_strategy_open_orders_for_other_symbol_do_not_block_entry(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="open-1",
                strategy_id="index_super_win_replica_001",
                symbol="UPRO",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:UPRO:ENTRY:TRG:open-1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    },
                    {
                        "key": "spy_pair",
                        "signal_symbol": "SPY",
                        "long_exec_symbol": "UPRO",
                        "short_exec_symbol": "SPXU",
                    },
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_INTENT_SUBMITTED"
    replica_ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_replica_strategy_open_orders_empty_strategy_id_owned_ref_still_blocks(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="open-1",
                strategy_id="",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:open-1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "OPEN_ORDERS_BLOCKED"
    assert preview["reason"] == "symbol_open_orders_inflight"
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_open_orders_empty_strategy_id_foreign_ref_does_not_block(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="open-1",
                strategy_id="",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:foreign",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:40-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_INTENT_SUBMITTED"
    replica_ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_replica_strategy_stop_reentry_cooldown_blocks_same_side(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "stop_reentry_cooldown_min": 15.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.extras["last_stop_exit_at"] = datetime(2026, 3, 25, 9, 40, 0, tzinfo=ET)
    state.extras["last_stop_exit_side"] = "LONG"

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:00-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "stop_reentry_cooldown"
    assert preview["trader_reason_code"] == "STOP_REENTRY_COOLDOWN"
    blocked_rows = [row for row in writer.records if row.get("stage") == "entry_blocked_reentry_cooldown"]
    assert len(blocked_rows) == 1
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_stop_reentry_cooldown_allows_opposite_side(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "stop_reentry_cooldown_min": 15.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.extras["last_stop_exit_at"] = datetime(2026, 3, 25, 9, 40, 0, tzinfo=ET)
    state.extras["last_stop_exit_side"] = "LONG"

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "SHORT",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_et": "2026-03-25T09:45:00-04:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_INTENT_SUBMITTED"
    replica_ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_replica_strategy_real_exec_position_blocks_and_syncs_entry(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)

    async def _position_reader(symbol: str):
        if str(symbol).upper() == "TQQQ":
            return MagicMock(pos_qty=7.0)
        return MagicMock(pos_qty=0.0)

    replica_ctx.position = AsyncMock(side_effect=_position_reader)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "REAL_POSITION_BLOCKED"
    assert strategy._symbol_state["QQQ"].is_traded is True
    assert strategy._symbol_state["QQQ"].active_exec_symbol == "TQQQ"
    assert strategy._symbol_state["QQQ"].active_side == "LONG"
    assert strategy._symbol_state["QQQ"].extras["live_qty"] == 7
    assert str(strategy._symbol_state["QQQ"].extras.get("active_position_lot_id") or "").startswith(
        "lot:index_super_win_replica_001:QQQ:recovered:TQQQ:"
    )
    assert any(row["stage"] == "real_position_sync_blocked_entry" for row in writer.records)
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_live_submit_and_order_update_converge(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_breakeven_shield": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_INTENT_SUBMITTED"
    order_ref = str(preview["order_ref"])

    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    updated = strategy._last_preview_by_symbol["QQQ"]
    assert updated["status"] == "ENTRY_CONFIRMED"
    assert updated["runtime_transition"] == "pending_entry->live"
    stages = [row["stage"] for row in writer.records]
    assert "gate_evaluated" in stages
    assert "entry_submit_ready" in stages
    assert "entry_intent_requested" in stages
    assert "entry_intent_submitted" in stages
    assert "entry_confirmed" in stages


@pytest.mark.asyncio
async def test_replica_strategy_blocks_on_replica_gate_reason(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_require_market_data_healthy": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "market_data_healthy": False,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "market_data_unavailable"
    assert preview["trader_reason_code"] == "SIGNAL_GATE_FAIL_MARKET_DATA_UNAVAILABLE"
    assert any(row["stage"] == "entry_gate_blocked" and row["reason"] == "market_data_unavailable" for row in writer.records)
    block_rows = [row for row in writer.records if row.get("stage") == "entry_gate_blocked"]
    assert len(block_rows) >= 1
    payload = dict(block_rows[-1].get("payload") or {})
    assert payload.get("signal_symbol") == "QQQ"
    assert payload.get("exec_symbol") == "TQQQ"
    assert payload.get("gate_reason") == "market_data_unavailable"
    assert payload.get("trader_reason_code") == "SIGNAL_GATE_FAIL_MARKET_DATA_UNAVAILABLE"
    assert isinstance(payload.get("gate_details"), dict)
    gate_rows = [row for row in writer.records if row.get("stage") == "gate_evaluated" and row.get("reason") == "market_data_unavailable"]
    assert len(gate_rows) == 1
    gate_payload = dict(gate_rows[0].get("payload") or {})
    assert gate_payload.get("gate_allowed") is False
    assert gate_payload.get("block_stage") == "entry_gate_blocked"
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_blocks_on_seed_guard_reason(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "first_entry_require_seed_ready": True,
                "entry_seed_min_samples": 12,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "first_entry_guard_context": True,
            "seed_ready": False,
            "seed_sample_count": 8,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "first_entry_seed_not_ready"
    assert preview["gate_details"]["seed_sample_count"] == 8


@pytest.mark.asyncio
async def test_replica_strategy_blocks_with_time_window_reason_code(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "ab_time_start": "09:30:00",
                "ab_time_end": "15:59:00",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_time_et": "09:20:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "outside_ab_window"
    assert preview["trader_reason_code"] == "OUTSIDE_AB_WINDOW"


@pytest.mark.asyncio
async def test_replica_strategy_signal_inactive_block_payload_schema(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "strict_recovered_lot_mode": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "intent_target_qty": 0,
            "intent_side": None,
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "signal_inactive_or_invalid"
    assert preview["trader_reason_code"] == "SIGNAL_INACTIVE_OR_INVALID"

    block_rows = [
        row
        for row in writer.records
        if row.get("stage") == "entry_gate_blocked"
        and row.get("reason") == "signal_inactive_or_invalid"
    ]
    assert len(block_rows) == 1
    payload = dict(block_rows[0].get("payload") or {})
    assert set(["signal_symbol", "exec_symbol", "gate_reason", "trader_reason_code", "gate_details"]).issubset(payload.keys())
    assert payload["signal_symbol"] == "QQQ"
    assert payload["exec_symbol"] == "TQQQ"
    assert payload["gate_reason"] == "signal_inactive_or_invalid"
    assert payload["trader_reason_code"] == "SIGNAL_INACTIVE_OR_INVALID"
    assert isinstance(payload["gate_details"], dict)
    gate_rows = [
        row
        for row in writer.records
        if row.get("stage") == "gate_evaluated"
        and row.get("reason") == "signal_inactive_or_invalid"
    ]
    assert len(gate_rows) == 1
    gate_payload = dict(gate_rows[0].get("payload") or {})
    assert gate_payload["gate_allowed"] is False
    assert gate_payload["block_stage"] == "entry_gate_blocked"


@pytest.mark.asyncio
async def test_replica_strategy_required_data_missing_includes_missing_required_details(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "use_confirm_symbol": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "now_time_et": "18:41:00",
            "qqq_close_now": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "required_data_missing"
    assert preview["gate_details"]["missing_required"] == ["QQQ:VWAP", "QQEW:VWAP"]


@pytest.mark.asyncio
async def test_replica_strategy_offhours_uses_price_cache_session_vwap_fallback(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "session_name": "afterhours",
                "session_start_et": "16:00:00",
                "session_end_et": "19:59:59",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    replica_ctx._price_cache = MagicMock()
    replica_ctx._price_cache._bars_1m = defaultdict(lambda: deque())
    replica_ctx._price_cache._current_1m = {}
    replica_ctx._price_cache._bars_1m["QQQ"].append(
        MagicMock(
            ts=datetime(2026, 3, 26, 18, 40, 0, tzinfo=ET),
            pv=1005.0,
            volume=2,
        )
    )
    replica_ctx._price_cache._current_1m["QQQ"] = MagicMock(
        ts=datetime(2026, 3, 26, 18, 41, 0, tzinfo=ET),
        pv=1515.0,
        volume=3,
    )

    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T18:41:30-04:00",
            "qqq_close_now": 505.0,
            "atr_eff": 1.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["reason"] != "required_data_missing"


@pytest.mark.asyncio
async def test_replica_strategy_rth_does_not_use_offhours_vwap_fallback(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "session_name": "afterhours",
                "session_start_et": "16:00:00",
                "session_end_et": "19:59:59",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    replica_ctx._price_cache = MagicMock()
    replica_ctx._price_cache._bars_1m = defaultdict(lambda: deque())
    replica_ctx._price_cache._current_1m = {
        "QQQ": MagicMock(
            ts=datetime(2026, 3, 26, 10, 0, 0, tzinfo=ET),
            pv=1000.0,
            volume=2,
        )
    }
    binding = strategy._instrument_map.binding_for_signal("QQQ")
    assert binding is not None
    vwap = strategy._primary_vwap_value(
        binding=binding,
        features={},
        eval_now=datetime(2026, 3, 26, 10, 0, 30, tzinfo=ET),
    )
    assert vwap is None


@pytest.mark.asyncio
async def test_replica_strategy_offhours_uses_exec_symbol_atr_fallback(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "session_name": "afterhours",
                "session_start_et": "16:00:00",
                "session_end_et": "19:59:59",
                "atr_period": 3,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    replica_ctx._price_cache = MagicMock()
    replica_ctx._price_cache._bars_5m = defaultdict(lambda: deque())
    replica_ctx._price_cache._current_5m = {}

    replica_ctx._price_cache._bars_5m["TQQQ"].extend(
        [
            MagicMock(
                ts=datetime(2026, 3, 26, 18, 30, 0, tzinfo=ET),
                high=61.0,
                low=60.0,
                close=60.5,
            ),
            MagicMock(
                ts=datetime(2026, 3, 26, 18, 35, 0, tzinfo=ET),
                high=61.8,
                low=60.8,
                close=61.3,
            ),
            MagicMock(
                ts=datetime(2026, 3, 26, 18, 40, 0, tzinfo=ET),
                high=62.4,
                low=61.2,
                close=62.0,
            ),
        ]
    )

    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T18:41:30-04:00",
            "qqq_close_now": 505.0,
            "atr_eff": None,
            "atr": None,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["reason"] != "atr_eff_not_ready"


@pytest.mark.asyncio
async def test_replica_strategy_rth_does_not_use_exec_symbol_atr_fallback(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "session_name": "afterhours",
                "session_start_et": "16:00:00",
                "session_end_et": "19:59:59",
                "atr_period": 3,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    replica_ctx._price_cache = MagicMock()
    replica_ctx._price_cache._bars_5m = defaultdict(lambda: deque())
    replica_ctx._price_cache._current_5m = {
        "TQQQ": MagicMock(
            ts=datetime(2026, 3, 26, 10, 0, 0, tzinfo=ET),
            high=61.0,
            low=60.0,
            close=60.5,
        )
    }

    await strategy.initialize()
    atr_eff = strategy._atr_effective_value(
        features={},
        exec_symbol="TQQQ",
        eval_now=datetime(2026, 3, 26, 10, 0, 30, tzinfo=ET),
    )
    assert atr_eff is None


@pytest.mark.asyncio
async def test_replica_strategy_initialize_subscribes_exec_symbols_to_price_cache(replica_ctx):
    replica_ctx._price_cache = MagicMock()
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "session_name": "afterhours",
                "session_start_et": "16:00:00",
                "session_end_et": "19:59:59",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )

    await strategy.initialize()

    replica_ctx._price_cache.subscribe.assert_any_call(["QQQ", "TQQQ", "SQQQ"])


@pytest.mark.asyncio
async def test_replica_strategy_derives_qqq_style_entry_when_explicit_signal_missing(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "atr_rank_today": 0.3,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    minute_features = [
        ("2026-03-26T10:12:00-04:00", 582.50, 582.10, 0.26, 1.0, 43.10),
        ("2026-03-26T10:13:00-04:00", 582.80, 582.20, 0.26, 1.0, 43.18),
        ("2026-03-26T10:14:00-04:00", 583.20, 582.35, 0.26, 1.0, 43.34),
        ("2026-03-26T10:15:00-04:00", 583.94, 582.75, 0.2632205, 0.94, 43.54),
    ]
    for now_et, qqq_close_now, vwap_now, atr_eff, vol_surge_ratio, tqqq_close_now in minute_features:
        await strategy.on_features(
            "QQQ",
            {
                "now_et": now_et,
                "qqq_close_now": qqq_close_now,
                "vwap_now": vwap_now,
                "atr_eff": atr_eff,
                "vol_surge_ratio": vol_surge_ratio,
                "tqqq_close_now": tqqq_close_now,
                "data_ready": True,
                "market_data_healthy": True,
                "decision_pipeline_healthy": True,
            },
        )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "DRY_RUN_PREVIEW"
    assert preview["exec_symbol"] == "TQQQ"
    assert preview["side"] == "LONG"
    assert preview["qty"] == 160
    state = strategy._symbol_state["QQQ"]
    assert state.extras["planned_total_qty"] == 229
    assert state.extras["planned_probe_qty"] == 160
    assert state.extras["planned_add_qty"] == 69
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_derives_specific_block_reason_when_signal_sign_mismatch(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "atr_rank_today": 0.3,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    for now_et, qqq_close_now in (
        ("2026-03-26T10:12:00-04:00", 582.50),
        ("2026-03-26T10:13:00-04:00", 582.80),
        ("2026-03-26T10:14:00-04:00", 583.20),
    ):
        await strategy.on_features(
            "QQQ",
            {
                "now_et": now_et,
                "qqq_close_now": qqq_close_now,
                "vwap_now": 582.20,
                "atr_eff": 0.26,
                "vol_surge_ratio": 1.0,
                "tqqq_close_now": 43.20,
            },
        )
    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T10:15:00-04:00",
            "qqq_close_now": 583.94,
            "vwap_now": 582.75,
            "atr_eff": 0.2632205,
            "vol_surge_ratio": 0.94,
            "v3m_signed": -0.81,
            "tqqq_close_now": 43.54,
            "data_ready": True,
            "market_data_healthy": True,
            "decision_pipeline_healthy": True,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "sign_mismatch"
    assert preview["trader_reason_code"] == "SIGN_MISMATCH"
    gate_rows = [row for row in writer.records if row.get("stage") == "gate_evaluated" and row.get("reason") == "sign_mismatch"]
    assert len(gate_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_fallback_blocks_pre_0945_without_early_override(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "atr_rank_today": 0.3,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    for now_et, qqq_close_now, vwap_now, atr_eff, vol_surge_ratio, tqqq_close_now in (
        ("2026-03-26T09:42:00-04:00", 582.50, 582.10, 0.26, 1.0, 43.10),
        ("2026-03-26T09:43:00-04:00", 582.80, 582.20, 0.26, 1.0, 43.18),
        ("2026-03-26T09:44:00-04:00", 583.20, 582.35, 0.26, 1.0, 43.34),
    ):
        await strategy.on_features(
            "QQQ",
            {
                "now_et": now_et,
                "qqq_close_now": qqq_close_now,
                "vwap_now": vwap_now,
                "atr_eff": atr_eff,
                "vol_surge_ratio": vol_surge_ratio,
                "tqqq_close_now": tqqq_close_now,
                "data_ready": True,
                "market_data_healthy": True,
                "decision_pipeline_healthy": True,
            },
        )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "pre_0945_no_early_override"


@pytest.mark.asyncio
async def test_replica_strategy_fallback_uses_internal_early_override(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    minute_features = [
        ("2026-03-26T09:30:00-04:00", 582.00, 581.80, 1.0),
        ("2026-03-26T09:31:00-04:00", 582.25, 582.05, 1.0),
        ("2026-03-26T09:32:00-04:00", 582.55, 582.20, 1.0),
        ("2026-03-26T09:33:00-04:00", 582.90, 582.45, 1.0),
        ("2026-03-26T09:34:00-04:00", 583.15, 582.70, 1.0),
        ("2026-03-26T09:35:00-04:00", 583.45, 582.92, 1.0),
        ("2026-03-26T09:36:00-04:00", 583.75, 583.16, 1.0),
        ("2026-03-26T09:37:00-04:00", 584.05, 583.45, 1.0),
        ("2026-03-26T09:38:00-04:00", 584.32, 583.70, 1.0),
    ]
    for now_et, qqq_close_now, vwap_now, vol_surge_ratio in minute_features:
        await strategy.on_features(
            "QQQ",
            {
                "now_et": now_et,
                "qqq_close_now": qqq_close_now,
                "vwap_now": vwap_now,
                "atr_eff": 0.30,
                "vol_surge_ratio": vol_surge_ratio,
                "tqqq_close_now": 43.20,
                "data_ready": True,
                "market_data_healthy": True,
                "decision_pipeline_healthy": True,
            },
        )

    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T09:39:00-04:00",
            "qqq_close_now": 584.60,
            "vwap_now": 583.98,
            "atr_eff": 0.30,
            "vol_surge_ratio": 1.8,
            "v3m_signed": 1.10,
            "tqqq_close_now": 43.80,
            "data_ready": True,
            "market_data_healthy": True,
            "decision_pipeline_healthy": True,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "DRY_RUN_PREVIEW"
    assert preview["exec_symbol"] == "TQQQ"
    assert preview["side"] == "LONG"
    assert preview["qty"] > 0


@pytest.mark.asyncio
async def test_replica_strategy_fallback_blocks_atr_rank_not_ready(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T09:46:00-04:00",
            "qqq_close_now": 583.20,
            "vwap_now": 582.70,
            "atr_eff": 0.26,
            "vol_surge_ratio": 1.5,
            "tqqq_close_now": 43.34,
            "data_ready": True,
            "market_data_healthy": True,
            "decision_pipeline_healthy": True,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "atr_rank_not_ready"


@pytest.mark.asyncio
async def test_replica_strategy_fallback_blocks_delayed_confirm_not_met(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "atr_rank_today": 0.3,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    for now_et, qqq_close_now, vwap_now, atr_eff, vol_surge_ratio, tqqq_close_now in (
        ("2026-03-26T09:40:00-04:00", 582.00, 581.92, 0.26, 1.0, 43.00),
        ("2026-03-26T09:41:00-04:00", 582.01, 581.93, 0.26, 1.0, 43.01),
        ("2026-03-26T09:42:00-04:00", 582.02, 581.94, 0.26, 1.0, 43.02),
        ("2026-03-26T09:43:00-04:00", 582.01, 581.95, 0.26, 1.0, 43.02),
        ("2026-03-26T09:44:00-04:00", 582.02, 581.96, 0.26, 1.0, 43.03),
        ("2026-03-26T09:45:00-04:00", 582.03, 581.97, 0.26, 1.0, 43.03),
    ):
        await strategy.on_features(
            "QQQ",
            {
                "now_et": now_et,
                "qqq_close_now": qqq_close_now,
                "vwap_now": vwap_now,
                "atr_eff": atr_eff,
                "vol_surge_ratio": vol_surge_ratio,
                "tqqq_close_now": tqqq_close_now,
                "data_ready": True,
                "market_data_healthy": True,
                "decision_pipeline_healthy": True,
            },
        )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "delayed_confirm_not_met"


@pytest.mark.asyncio
async def test_replica_strategy_fallback_blocks_fast_cooldown_active(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "atr_rank_today": 0.3,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.extras["fast_cooldown_until_et"] = datetime.fromisoformat("2026-03-26T10:30:00-04:00")

    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T10:15:00-04:00",
            "qqq_close_now": 583.94,
            "vwap_now": 582.75,
            "atr_eff": 0.2632205,
            "vol_surge_ratio": 2.8,
            "v3m_signed": 0.81,
            "tqqq_close_now": 43.54,
            "data_ready": True,
            "market_data_healthy": True,
            "decision_pipeline_healthy": True,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "fast_cooldown_active"
    assert preview["trader_reason_code"] == "FAST_COOLDOWN_ACTIVE"


@pytest.mark.asyncio
async def test_replica_strategy_fallback_blocks_long_drift_second_confirm_failed(replica_ctx, monkeypatch):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "atr_rank_today": 0.3,
                "long_drift_second_confirm": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    monkeypatch.setattr(strategy, "_derive_drift_ok", lambda **kwargs: True)
    state = strategy._symbol_state["QQQ"]
    state.extras["primary_close_history"] = [
        {"minute": "2026-03-26T10:13:00-04:00", "close": 583.20},
        {"minute": "2026-03-26T10:14:00-04:00", "close": 583.10},
        {"minute": "2026-03-26T10:15:00-04:00", "close": 583.00},
    ]

    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T10:15:00-04:00",
            "qqq_close_now": 583.00,
            "vwap_now": 582.75,
            "atr_eff": 0.2632205,
            "vol_surge_ratio": 0.8,
            "v3m_signed": 0.20,
            "tqqq_close_now": 43.54,
            "data_ready": True,
            "market_data_healthy": True,
            "decision_pipeline_healthy": True,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "long_drift_second_confirm_failed"


@pytest.mark.asyncio
async def test_replica_strategy_fallback_blocks_v47_long_chase_guard(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "atr_rank_today": 0.3,
                "enable_fast": False,
                "enable_v47_chase_guard": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    for now_et, qqq_close_now in (
        ("2026-03-26T09:30:00-04:00", 580.00),
        ("2026-03-26T09:45:00-04:00", 581.50),
        ("2026-03-26T10:12:00-04:00", 582.50),
        ("2026-03-26T10:13:00-04:00", 582.80),
        ("2026-03-26T10:14:00-04:00", 583.20),
    ):
        await strategy.on_features(
            "QQQ",
            {
                "now_et": now_et,
                "qqq_close_now": qqq_close_now,
                "vwap_now": qqq_close_now - 0.40,
                "atr_eff": 0.26,
                "vol_surge_ratio": 1.0,
                "tqqq_close_now": 43.20,
                "data_ready": True,
                "market_data_healthy": True,
                "decision_pipeline_healthy": True,
            },
        )
    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T10:15:00-04:00",
            "qqq_close_now": 583.94,
            "vwap_now": 582.75,
            "atr_eff": 0.2632205,
            "vol_surge_ratio": 0.94,
            "v3m_signed": 0.81,
            "tqqq_close_now": 43.54,
            "data_ready": True,
            "market_data_healthy": True,
            "decision_pipeline_healthy": True,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "v47_long_chase_guard"


@pytest.mark.asyncio
async def test_replica_strategy_fallback_blocks_long_rollback_veto(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "atr_rank_today": 0.3,
                "enable_v47_veto": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    for now_et, qqq_close_now in (
        ("2026-03-26T10:12:00-04:00", 583.50),
        ("2026-03-26T10:14:30-04:00", 584.10),
    ):
        await strategy.on_features(
            "QQQ",
            {
                "now_et": now_et,
                "qqq_close_now": qqq_close_now,
                "vwap_now": 582.80,
                "atr_eff": 0.30,
                "vol_surge_ratio": 0.8,
                "tqqq_close_now": 43.40,
                "data_ready": True,
                "market_data_healthy": True,
                "decision_pipeline_healthy": True,
            },
        )
    await strategy.on_features(
        "QQQ",
        {
            "now_et": "2026-03-26T10:15:00-04:00",
            "qqq_close_now": 583.70,
            "vwap_now": 582.90,
            "atr_eff": 0.30,
            "vol_surge_ratio": 0.9,
            "v3m_signed": 0.60,
            "tqqq_close_now": 43.50,
            "data_ready": True,
            "market_data_healthy": True,
            "decision_pipeline_healthy": True,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "long_rollback_veto"


@pytest.mark.asyncio
async def test_replica_strategy_open_orders_block_emits_gate_trace(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="open-1",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="SUBMITTED",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:open-1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "OPEN_ORDERS_BLOCKED"
    gate_rows = [row for row in writer.records if row.get("stage") == "gate_evaluated" and row.get("reason") == "symbol_open_orders_inflight"]
    assert len(gate_rows) == 1
    payload = dict(gate_rows[0].get("payload") or {})
    assert payload["gate_allowed"] is False
    assert payload["block_stage"] == "open_orders_blocked"


@pytest.mark.asyncio
async def test_replica_strategy_blocks_on_opening_data_integrity_fail_closed(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "use_confirm_symbol": True,
                "fail_closed_on_data_integrity": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "qqq_age_sec": 1.0,
            "qqew_age_sec": 1.0,
            "now_time_et": "09:40:00",
            "price_base_source": "unknown",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "data_integrity_fail_closed"
    assert preview["gate_details"]["integrity_failure"]["kind"] == "price_base_source_unknown"


@pytest.mark.asyncio
async def test_replica_strategy_reports_missing_freshness_field_in_gate_details(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "use_confirm_symbol": True,
                "fail_closed_on_data_integrity": True,
                "entry_max_data_age_sec": 5.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "qqew_age_sec": 1.0,
            "now_time_et": "18:24:00",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "data_integrity_fail_closed"
    assert preview["gate_details"]["integrity_failure"]["kind"] == "freshness_field_missing"
    assert preview["gate_details"]["integrity_failure"]["symbol"] == "QQQ"
    assert preview["gate_details"]["integrity_failure"]["field"] == "qqq_age_sec"


@pytest.mark.asyncio
async def test_replica_strategy_synthesizes_binding_age_from_feature_timestamp(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "use_confirm_symbol": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    binding = strategy._instrument_map.binding_for_signal("QQQ")
    eval_now = datetime(2026, 3, 26, 18, 24, 5, tzinfo=ET)
    ts_us = int(datetime(2026, 3, 26, 18, 24, 2, tzinfo=ET).timestamp() * 1_000_000)

    normalized = strategy._normalize_binding_freshness_features(
        features={"ts": ts_us},
        binding=binding,
        eval_now=eval_now,
    )

    assert normalized["qqq_age_sec"] == pytest.approx(3.0, abs=1e-6)
    assert normalized["qqew_age_sec"] == pytest.approx(3.0, abs=1e-6)


@pytest.mark.asyncio
async def test_replica_strategy_preserves_existing_binding_age_fields(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "use_confirm_symbol": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    binding = strategy._instrument_map.binding_for_signal("QQQ")
    eval_now = datetime(2026, 3, 26, 18, 24, 5, tzinfo=ET)
    ts_us = int(datetime(2026, 3, 26, 18, 24, 2, tzinfo=ET).timestamp() * 1_000_000)

    normalized = strategy._normalize_binding_freshness_features(
        features={"ts": ts_us, "qqq_age_sec": 1.25, "freshness_age_sec_qqew": 2.5},
        binding=binding,
        eval_now=eval_now,
    )

    assert normalized["qqq_age_sec"] == pytest.approx(1.25, abs=1e-6)
    assert "qqew_age_sec" not in normalized
    assert normalized["freshness_age_sec_qqew"] == pytest.approx(2.5, abs=1e-6)


@pytest.mark.asyncio
async def test_replica_strategy_blocks_on_startup_anchor_embargo(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "startup_anchor_entry_block_enabled": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "now_time_et": "09:41:00",
            "price_base_source": "startup_anchor_10s",
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "startup_anchor_embargo"


@pytest.mark.asyncio
async def test_replica_strategy_blocks_on_direction_mismatch(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "use_confirm_symbol": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 499.0,
            "qqq_close_now": 499.0,
            "vwap_now": 500.0,
            "qqew_close_now": 501.0,
            "qqew_vwap_now": 500.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "direction_mismatch"
    assert preview["gate_details"]["direction"] == "LONG"


@pytest.mark.asyncio
async def test_replica_strategy_live_manage_submits_tp1_partial(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_breakeven_shield": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
            "entry_atr_used": 2.0,
            "trade_target_px": 52.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 52.2,
            "exec_price": 52.2,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "TP1_PARTIAL_SUBMITTED"
    assert preview["reason"] == "tp1_hit"
    assert preview["exec_symbol"] == "TQQQ"
    assert preview["qty"] == -4.0
    assert any(row["stage"] == "tp1_submitted" and row["reason"] == "tp1_hit" for row in writer.records)


@pytest.mark.asyncio
async def test_replica_strategy_short_manage_submits_tp1_partial(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_breakeven_shield": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "SHORT",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
            "entry_atr_used": 2.0,
            "trade_target_px": 48.0,
            "qqq_close_now": 48.0,
            "vwap_now": 49.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="SQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 47.8,
            "exec_price": 47.8,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "TP1_PARTIAL_SUBMITTED"
    assert preview["reason"] == "tp1_hit"
    assert preview["exec_symbol"] == "SQQQ"
    assert preview["qty"] == 4.0


@pytest.mark.asyncio
async def test_replica_strategy_live_manage_submits_breakeven_stop(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_breakeven_shield": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
            "entry_atr_used": 2.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 51.5,
            "exec_price": 51.5,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTIVE_STOP_SUBMITTED"
    assert preview["reason"] == "breakeven_shield"
    assert preview["stop_px"] == 50.2
    assert any(row["stage"] == "protective_stop_submitted" and row["reason"] == "breakeven_shield" for row in writer.records)


@pytest.mark.asyncio
async def test_replica_strategy_short_manage_submits_breakeven_stop(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_breakeven_shield": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "SHORT",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
            "entry_atr_used": 2.0,
            "qqq_close_now": 48.0,
            "vwap_now": 49.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="SQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 48.5,
            "exec_price": 48.5,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTIVE_STOP_SUBMITTED"
    assert preview["reason"] == "breakeven_shield"
    assert preview["stop_px"] == 49.8


@pytest.mark.asyncio
async def test_replica_strategy_breakeven_disabled_by_default(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "force_flatten_time": "23:59:59",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
            "entry_atr_used": 2.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 51.5,
            "exec_price": 51.5,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "LIVE_HOLD"
    assert preview["reason"] == "hold"


@pytest.mark.asyncio
async def test_replica_strategy_live_manage_submits_eod_flatten(replica_ctx, monkeypatch):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            import datetime as _dt

            value = _dt.datetime(2026, 3, 16, 15, 59, 0)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenDateTime)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.1,
            "exec_price": 50.1,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "FLATTEN_SUBMITTED"
    assert preview["reason"] == "force_flatten_1559_EOD"
    assert any(row["stage"] == "flatten_submitted" and row["reason"] == "force_flatten_1559_EOD" for row in writer.records)


@pytest.mark.asyncio
async def test_replica_strategy_gate_uses_binding_specific_feature_prefixes(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "use_confirm_symbol": True,
                "require_confirm_vwap_ready": True,
                "replica_manifests": [
                    {
                        "key": "spy_pair",
                        "signal_symbol": "SPY",
                        "confirm_symbol": "IVW",
                        "long_exec_symbol": "UPRO",
                        "short_exec_symbol": "SPXU",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "SPY",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
            "spy_close_now": 501.0,
            "spy_vwap_now": 500.0,
            "ivw_close_now": 10.0,
            "ivw_vwap_now": 0.0,
            "data_ready": True,
        },
    )

    preview = strategy._last_preview_by_symbol["SPY"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "data_not_ready"
    assert preview["gate_details"]["missing"] == "IVW:VWAP"


@pytest.mark.asyncio
async def test_replica_strategy_manage_uses_binding_specific_feature_prefixes(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_breakeven_shield": True,
                "replica_manifests": [
                    {
                        "key": "spy_pair",
                        "signal_symbol": "SPY",
                        "confirm_symbol": "IVW",
                        "long_exec_symbol": "UPRO",
                        "short_exec_symbol": "SPXU",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "SPY",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
            "entry_atr_used": 2.0,
            "spy_close_now": 50.0,
            "spy_vwap_now": 49.5,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["SPY"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="UPRO",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    await strategy.on_features(
        "SPY",
        {
            "signal_active": False,
            "signal_price": 51.5,
            "exec_price": 51.5,
            "spy_close_now": 51.5,
            "spy_vwap_now": 50.2,
        },
    )

    preview = strategy._last_preview_by_symbol["SPY"]
    assert preview["status"] == "PROTECTIVE_STOP_SUBMITTED"
    assert preview["reason"] == "breakeven_shield"


@pytest.mark.asyncio
async def test_replica_strategy_force_flatten_time_supports_second_precision(replica_ctx, monkeypatch):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "force_flatten_time": "15:58:30",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    class _FrozenBefore:
        @staticmethod
        def now(tz=None):
            value = datetime(2026, 3, 16, 15, 58, 20)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenBefore)
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.1,
            "exec_price": 50.1,
        },
    )
    preview_before = dict(strategy._last_preview_by_symbol["QQQ"])
    assert preview_before["status"] == "LIVE_HOLD"
    assert preview_before["reason"] == "hold"

    class _FrozenAfter:
        @staticmethod
        def now(tz=None):
            value = datetime(2026, 3, 16, 15, 58, 31)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenAfter)
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.1,
            "exec_price": 50.1,
        },
    )
    preview_after = strategy._last_preview_by_symbol["QQQ"]
    assert preview_after["status"] == "FLATTEN_SUBMITTED"
    assert preview_after["reason"] == "force_flatten_1559_EOD"


@pytest.mark.asyncio
async def test_replica_strategy_session_end_flatten_emits_handover_audit(replica_ctx, monkeypatch):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "session_name": "afterhours",
                "force_flatten_time": "15:58:30",
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    class _FrozenAfter:
        @staticmethod
        def now(tz=None):
            value = datetime(2026, 3, 16, 15, 58, 31)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenAfter)
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.1,
            "exec_price": 50.1,
        },
    )

    requested_rows = [row for row in writer.records if row.get("stage") == "session_end_flatten_requested"]
    assert len(requested_rows) == 1
    assert requested_rows[0]["reason"] == "force_flatten_1559_EOD"
    assert requested_rows[0]["payload"]["session_name"] == "afterhours"


@pytest.mark.asyncio
async def test_replica_strategy_session_end_flatten_dedupes_repeat_submit(replica_ctx, monkeypatch):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "session_name": "afterhours",
                "force_flatten_time": "15:58:30",
                "stop_intent_dedupe_sec": 5,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
        },
    )
    order_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=order_ref,
        )
    )

    class _FrozenAfter:
        @staticmethod
        def now(tz=None):
            value = datetime(2026, 3, 16, 15, 58, 31)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenAfter)
    before_calls = replica_ctx.submit_intent.await_count
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.1,
            "exec_price": 50.1,
        },
    )
    first_calls = replica_ctx.submit_intent.await_count
    strategy._symbol_state["QQQ"].is_flattening = False
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.1,
            "exec_price": 50.1,
        },
    )

    assert first_calls == before_calls + 1
    assert replica_ctx.submit_intent.await_count == first_calls
    assert strategy._last_preview_by_symbol["QQQ"]["status"] == "SESSION_END_FLATTEN_DEDUPED"
    deduped_rows = [row for row in writer.records if row.get("stage") == "session_end_flatten_deduped"]
    assert len(deduped_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_session_end_flatten_confirmed_on_flat_sync(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "session_name": "afterhours",
                "zero_position_flat_confirmations": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    state.extras["session_end_flatten_pending"] = True
    state.extras["session_end_flatten_reason"] = "force_flatten_1559_EOD"

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=50.0,
        )
    )

    confirmed_rows = [row for row in writer.records if row.get("stage") == "session_end_flatten_confirmed"]
    assert len(confirmed_rows) == 1
    assert confirmed_rows[0]["reason"] == "force_flatten_1559_EOD"
    assert confirmed_rows[0]["payload"]["session_name"] == "afterhours"


@pytest.mark.asyncio
async def test_replica_strategy_live_manage_submits_dynamic_stop_widen(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_dynamic_stop_widen": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.stop_px = 45.0
    state.entry_atr_used = 2.0
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    state.extras["stop_dist_pre"] = 5.0
    state.extras["stop_dist_cur"] = 5.0
    state.extras["entry_time_et"] = datetime(2026, 3, 16, 10, 0, tzinfo=ET)

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            value = datetime(2026, 3, 16, 10, 1, 0)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenDateTime)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.7,
            "exec_price": 50.7,
            "atr_live": 3.0,
        },
    )
    monkeypatch.undo()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTIVE_STOP_SUBMITTED"
    assert preview["reason"] == "stop_widened"
    assert preview["stop_px"] == 42.5


@pytest.mark.asyncio
async def test_replica_strategy_live_manage_submits_logic_retreat_flatten(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_logic_retreat": True,
                "logic_retreat_min_hold_sec": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    state.extras["entry_time_et"] = datetime(2026, 3, 16, 10, 0, tzinfo=ET)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 499.0,
            "exec_price": 50.1,
            "qqq_close_now": 499.0,
            "vwap_now": 500.0,
            "qqq_age_sec": 2.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "FLATTEN_SUBMITTED"
    assert preview["reason"] == "logic_retreat"


@pytest.mark.asyncio
async def test_replica_strategy_live_manage_uses_exec_symbol_price_and_logic_retreat_defaults_on(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "logic_retreat_min_hold_sec": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    state.extras["entry_time_et"] = datetime(2026, 3, 16, 10, 0, tzinfo=ET)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "qqq_close_now": 499.0,
            "vwap_now": 500.0,
            "qqq_age_sec": 2.0,
            "tqqq_close_now": 50.1,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "FLATTEN_SUBMITTED"
    assert preview["reason"] == "logic_retreat"


@pytest.mark.asyncio
async def test_replica_strategy_live_manage_submits_add_position(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_add_on": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.expected_entry_qty = 4
    state.extras["live_qty"] = 4
    state.extras["active_position_lot_id"] = "lot:index_super_win_replica_001:QQQ:base"
    state.extras["planned_probe_qty"] = 4
    state.extras["planned_add_qty"] = 4
    state.extras["add_done"] = False
    state.extras["entry_time_et"] = datetime(2026, 3, 16, 10, 0, tzinfo=ET)
    strategy._ownership_ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="index_super_win_replica_001",
            decision_id="base-d1",
            trigger_id="base-t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=4.0,
            ts_bar_end_us=int(datetime(2026, 3, 16, 10, 0, 0).timestamp() * 1_000_000),
            position_lot_id="lot:index_super_win_replica_001:QQQ:base",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:base",
        )
    )
    strategy._ownership_ledger.mark_entry_live(
        position_lot_id="lot:index_super_win_replica_001:QQQ:base",
        filled_qty=4.0,
    )

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            value = datetime(2026, 3, 16, 10, 1, 0)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenDateTime)

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.3,
            "exec_price": 50.3,
        },
    )
    monkeypatch.undo()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ADD_POSITION_SUBMITTED"
    assert preview["reason"] == "primary_intraday_add"
    assert preview["qty"] == 4
    assert strategy._symbol_state["QQQ"].expected_entry_qty == 8
    assert strategy._symbol_state["QQQ"].extras["add_done"] is True
    assert strategy._ownership_ledger.lot_for_identity(order_ref=preview["order_ref"]) is not None


@pytest.mark.asyncio
async def test_replica_strategy_fallback_entry_blocks_on_day_gross_notional_cap(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)

    async def _position(symbol: str):
        qty = 200.0 if str(symbol or "").strip().upper() == "TQQQ" else 0.0
        return MagicMock(pos_qty=qty)

    replica_ctx.position = AsyncMock(side_effect=_position)
    replica_ctx.get_price = MagicMock(return_value={"last": 43.54})
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "entry_check_on_new_minute_only": True,
                "notional_usd": 10000,
                "max_day_gross_notional_usd": 10000,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    strategy._sync_from_real_exec_positions = AsyncMock(return_value=False)
    await strategy.on_features(
        "QQQ",
        {
            "now_time_et": "10:15:00",
            "signal_active": True,
            "intent_target_qty": 160,
            "intent_side": "LONG",
            "signal_price": 583.94,
            "exec_price": 43.54,
            "data_ready": True,
            "market_data_healthy": True,
            "decision_pipeline_healthy": True,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "BLOCKED"
    assert preview["reason"] == "day_gross_notional_cap"
    assert preview["trader_reason_code"] == "DAY_GROSS_NOTIONAL_CAP"
    block_rows = [row for row in writer.records if row.get("stage") == "entry_gate_blocked" and row.get("reason") == "day_gross_notional_cap"]
    assert len(block_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_add_position_order_update_marks_distinct_add_lot_live(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "enable_add_on": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.expected_entry_qty = 4
    state.extras["live_qty"] = 4
    state.extras["active_position_lot_id"] = "lot:index_super_win_replica_001:QQQ:base"
    state.extras["planned_probe_qty"] = 4
    state.extras["planned_add_qty"] = 4
    state.extras["add_done"] = False
    state.extras["entry_time_et"] = datetime(2026, 3, 16, 10, 0, tzinfo=ET)
    strategy._ownership_ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="index_super_win_replica_001",
            decision_id="base-d1",
            trigger_id="base-t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=4.0,
            ts_bar_end_us=int(datetime(2026, 3, 16, 10, 0, 0).timestamp() * 1_000_000),
            position_lot_id="lot:index_super_win_replica_001:QQQ:base",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:base",
        )
    )
    strategy._ownership_ledger.mark_entry_live(
        position_lot_id="lot:index_super_win_replica_001:QQQ:base",
        filled_qty=4.0,
    )

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            value = datetime(2026, 3, 16, 10, 1, 0)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenDateTime)
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.3,
            "exec_price": 50.3,
        },
    )
    monkeypatch.undo()

    add_order_ref = strategy._last_preview_by_symbol["QQQ"]["order_ref"]
    await strategy.on_order_update(
        OrderUpdate(
            order_id=1,
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            order_ref=add_order_ref,
            state="Filled",
            broker_order_id="123",
            ib_perm_id=456,
        )
    )

    active = strategy._ownership_ledger.active_lots(
        strategy_id="index_super_win_replica_001",
        signal_symbol="QQQ",
        exec_symbol="TQQQ",
    )
    assert len(active) == 2
    assert sorted(lot.remaining_qty for lot in active) == [4.0, 4.0]


@pytest.mark.asyncio
async def test_replica_strategy_initialize_restores_live_state_and_rebuilds_stop_preview(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "protection_audit_self_heal_after_misses": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    strategy.state["replica_runtime"] = {
        "QQQ": {
            "is_traded": True,
            "active_side": "LONG",
            "active_exec_symbol": "TQQQ",
            "active_trigger_id": "restart-trg",
            "expected_entry_qty": 8,
            "entry_px": 50.0,
            "stop_px": 48.4,
            "entry_atr_used": 2.0,
            "extras": {
                "live_qty": 8,
                "entry_time_et": "2026-03-16T10:00:00-04:00",
            },
        }
    }

    await strategy.initialize()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "RESTART_PROTECTIVE_STOP_PREVIEW"
    assert preview["reason"] == "rebuild_stop_after_restart"
    assert preview["stop_px"] == 48.4
    replica_ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_initialize_blocks_unprotected_restart(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    strategy.state["replica_runtime"] = {
        "QQQ": {
            "is_traded": True,
            "active_side": "LONG",
            "active_exec_symbol": "TQQQ",
            "expected_entry_qty": 8,
            "entry_px": 50.0,
            "extras": {
                "live_qty": 8,
            },
        }
    }

    await strategy.initialize()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "RESTART_UNPROTECTED_KILL_SWITCH_PREVIEW"
    assert preview["reason"] == "restart_unprotected_no_stop_px"


@pytest.mark.asyncio
async def test_replica_strategy_initialize_rebuilds_stop_before_restart_protection(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    strategy.state["replica_runtime"] = {
        "QQQ": {
            "is_traded": True,
            "active_side": "LONG",
            "active_exec_symbol": "TQQQ",
            "expected_entry_qty": 8,
            "entry_px": 50.0,
            "entry_atr_used": 2.0,
            "extras": {
                "live_qty": 8,
            },
        }
    }

    await strategy.initialize()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "RESTART_PROTECTIVE_STOP_PREVIEW"
    assert preview["reason"] == "rebuild_stop_after_restart"
    assert preview["stop_px"] == 45.0
    assert strategy._symbol_state["QQQ"].stop_px == 45.0
    rebuilt_rows = [row for row in writer.records if row.get("stage") == "restart_protective_stop_stop_rebuilt"]
    assert len(rebuilt_rows) == 1
    assert rebuilt_rows[0]["status"] == "rebuilt"


@pytest.mark.asyncio
async def test_replica_strategy_position_update_self_heals_missing_stop(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "AUDIT_SELF_HEAL_STOP_PREVIEW"
    assert preview["reason"] == "position_reconcile_protection"
    assert preview["stop_px"] == 48.5


@pytest.mark.asyncio
async def test_replica_strategy_position_update_zero_qty_clears_live_state(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.expected_entry_qty = 8
    state.entry_order_ref = "iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:x"
    state.extras["live_qty"] = 8
    state.extras["active_position_lot_id"] = "lot:index_super_win_replica_001:QQQ:1"
    strategy._ownership_ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="index_super_win_replica_001",
            decision_id="d1",
            trigger_id="t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=8.0,
            ts_bar_end_us=int(datetime(2026, 3, 25, 10, 0, 0).timestamp() * 1_000_000),
            position_lot_id="lot:index_super_win_replica_001:QQQ:1",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:x",
        )
    )
    strategy._ownership_ledger.mark_entry_live(
        position_lot_id="lot:index_super_win_replica_001:QQQ:1",
        filled_qty=8.0,
    )

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=0.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "POSITION_FLAT_SYNCED"
    assert preview["closed_owned_qty"] == 8.0
    assert state.is_traded is False
    assert state.expected_entry_qty == 0
    assert state.entry_order_ref is None
    assert strategy._ownership_ledger.active_qty(
        strategy_id="index_super_win_replica_001",
        signal_symbol="QQQ",
    ) == 0.0


@pytest.mark.asyncio
async def test_replica_strategy_position_update_missing_stop_triggers_kill_switch(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = None

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "AUDIT_SELF_HEAL_STOP_KILL_SWITCH_PREVIEW"
    assert preview["reason"] == "position_reconcile_protection_no_stop_px"


@pytest.mark.asyncio
async def test_replica_strategy_position_update_rebuilds_stop_before_self_heal(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.stop_px = None

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "AUDIT_SELF_HEAL_STOP_PREVIEW"
    assert preview["reason"] == "position_reconcile_protection"
    assert preview["stop_px"] == 45.0
    assert strategy._symbol_state["QQQ"].stop_px == 45.0
    rebuilt_rows = [row for row in writer.records if row.get("stage") == "audit_self_heal_stop_stop_rebuilt"]
    assert len(rebuilt_rows) == 1
    assert rebuilt_rows[0]["payload"]["stop_px"] == 45.0


@pytest.mark.asyncio
async def test_replica_strategy_position_update_clears_pending_and_sets_entry_time(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.pending_decision_id = "pending-1"
    state.active_trigger_id = None
    state.extras.pop("entry_time_et", None)
    state.stop_px = 48.5

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    state = strategy._symbol_state["QQQ"]
    assert state.pending_decision_id is None
    assert state.active_trigger_id is not None
    assert state.extras.get("entry_time_et") is not None
    assert state.entry_px == 50.0
    assert state.extras["live_qty"] == 8
    assert str(state.extras.get("active_position_lot_id") or "").startswith(
        "lot:index_super_win_replica_001:QQQ:recovered:TQQQ:"
    )


@pytest.mark.asyncio
async def test_replica_strategy_position_update_rebinds_existing_owned_lot(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    strategy.state["ownership_ledger"] = {
        "lots": [
            {
                "strategy_id": "index_super_win_replica_001",
                "position_lot_id": "lot:index_super_win_replica_001:QQQ:restored",
                "signal_symbol": "QQQ",
                "exec_symbol": "TQQQ",
                "side": "LONG",
                "planned_qty": 8.0,
                "live_qty": 8.0,
                "remaining_qty": 8.0,
                "opened_at": "2026-03-25T09:45:00-04:00",
                "status": "live",
                "payload": {"restored": True},
            }
        ],
        "order_to_lot": {},
        "broker_to_lot": {},
        "perm_to_lot": {},
        "conflicts": [],
    }
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.stop_px = 48.5
    state.extras["active_position_lot_id"] = None

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    assert state.extras["active_position_lot_id"] == "lot:index_super_win_replica_001:QQQ:restored"
    rebound_rows = [row for row in writer.records if row.get("stage") == "ownership_lot_rebound"]
    assert len(rebound_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_position_update_quarantines_binding_conflict(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    strategy._ownership_ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="index_super_win_replica_001",
            decision_id="d1",
            trigger_id="t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=4.0,
            ts_bar_end_us=1,
            position_lot_id="lot:index_super_win_replica_001:QQQ:1",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:ENTRY:t1",
            payload={},
        )
    )
    strategy._ownership_ledger.mark_entry_live(position_lot_id="lot:index_super_win_replica_001:QQQ:1", filled_qty=4.0)
    strategy._ownership_ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="index_super_win_replica_001",
            decision_id="d2",
            trigger_id="t2",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=4.0,
            ts_bar_end_us=2,
            position_lot_id="lot:index_super_win_replica_001:QQQ:2",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:ADD:t2",
            payload={},
        )
    )
    strategy._ownership_ledger.mark_entry_live(position_lot_id="lot:index_super_win_replica_001:QQQ:2", filled_qty=4.0)

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "POSITION_SYNC_QUARANTINED"
    assert preview["reason"] == "ownership_binding_conflict"


@pytest.mark.asyncio
async def test_replica_strategy_position_update_quarantines_recovery_required(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "strict_recovered_lot_mode": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "POSITION_SYNC_QUARANTINED"
    assert preview["reason"] == "ownership_recovery_required"


@pytest.mark.asyncio
async def test_replica_strategy_zero_position_requires_second_confirm(replica_ctx):
    replica_ctx.open_orders = AsyncMock(return_value=[])
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "zero_position_flat_confirmations": 2,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_exec_symbol = "TQQQ"
    state.active_side = "LONG"
    state.extras["live_qty"] = 8

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=0.0,
        )
    )
    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "POSITION_FLAT_PENDING_CONFIRM"
    assert strategy._symbol_state["QQQ"].is_traded is True

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=0.0,
            avg_px=0.0,
        )
    )
    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "POSITION_FLAT_SYNCED"
    assert strategy._symbol_state["QQQ"].is_traded is False


@pytest.mark.asyncio
async def test_replica_strategy_position_update_infers_short_side_from_sqqq_symbol(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.stop_px = 48.5

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="SQQQ",
            pos_qty=6.0,
            avg_px=50.0,
        )
    )

    state = strategy._symbol_state["QQQ"]
    assert state.active_exec_symbol == "SQQQ"
    assert state.active_side == "SHORT"
    assert state.extras["live_qty"] == 6


@pytest.mark.asyncio
async def test_replica_strategy_position_update_negative_qty_triggers_unexpected_short_flatten(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=-4.0,
            avg_px=50.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "UNEXPECTED_SHORT_FLATTEN_PREVIEW"
    assert preview["reason"] == "unexpected_short_inventory"
    assert preview["qty"] == 4.0
    stages = [row["stage"] for row in writer.records]
    assert "unexpected_short_flatten_preview" in stages


@pytest.mark.asyncio
async def test_replica_strategy_position_update_negative_qty_throttles_repeat_unexpected_short(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "unexpected_short_action_interval_sec": 10.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=-4.0,
            avg_px=50.0,
        )
    )
    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=-4.0,
            avg_px=50.0,
        )
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "UNEXPECTED_SHORT_THROTTLED"
    stages = [row["stage"] for row in writer.records]
    assert stages.count("unexpected_short_flatten_preview") == 1
    assert stages.count("unexpected_short_throttled") == 1


@pytest.mark.asyncio
async def test_replica_strategy_tp1_order_update_reduces_runtime_qty(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 50.0,
            "exec_price": 50.0,
            "entry_atr_used": 2.0,
            "trade_target_px": 52.0,
        },
    )
    entry_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    await strategy.on_order_update(
        OrderUpdate(
            order_id="1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=entry_ref,
        )
    )
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 52.2,
            "exec_price": 52.2,
        },
    )
    tp1_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])
    strategy._symbol_state["QQQ"].extras["live_qty"] = 8
    strategy._symbol_state["QQQ"].extras["pending_tp1_qty"] = 4

    await strategy.on_order_update(
        OrderUpdate(
            order_id="2",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=tp1_ref,
        )
    )

    assert strategy._last_preview_by_symbol["QQQ"]["status"] == "TP1_EXIT_CONFIRMED"
    assert strategy._symbol_state["QQQ"].extras["live_qty"] == 4


@pytest.mark.asyncio
async def test_replica_strategy_tp1_order_update_allocates_owned_exit_across_lots(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.active_trigger_id = "trg-tp1"
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    state.extras["pending_tp1_qty"] = 6
    for lot_id, qty, order_ref in (
        ("lot:index_super_win_replica_001:QQQ:base", 4.0, "iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:base"),
        ("lot:index_super_win_replica_001:QQQ:add", 4.0, "iswv2:index_super_win_replica_001:TQQQ:ADD:TRG:add"),
    ):
        strategy._ownership_ledger.record_entry_intent(
            ReplicaOwnershipIntentRecord(
                strategy_id="index_super_win_replica_001",
                decision_id=lot_id,
                trigger_id="trg-tp1",
                signal_symbol="QQQ",
                exec_symbol="TQQQ",
                side="LONG",
                qty=qty,
                ts_bar_end_us=int(
                    datetime(2026, 3, 16, 10, 0 if lot_id.endswith("base") else 1, 0).timestamp() * 1_000_000
                ),
                position_lot_id=lot_id,
                order_ref=order_ref,
            )
        )
        strategy._ownership_ledger.mark_entry_live(position_lot_id=lot_id, filled_qty=qty)

    await strategy.on_order_update(
        OrderUpdate(
            order_id="tp1-1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:TP1:TRG:trg-tp1",
        )
    )

    active = strategy._ownership_ledger.active_lots(
        strategy_id="index_super_win_replica_001",
        signal_symbol="QQQ",
        exec_symbol="TQQQ",
    )
    assert len(active) == 1
    assert active[0].position_lot_id == "lot:index_super_win_replica_001:QQQ:add"
    assert active[0].remaining_qty == 2.0
    alloc_rows = [row for row in writer.records if row.get("stage") == "ownership_exit_allocated" and row.get("reason") == "tp1"]
    assert len(alloc_rows) == 1
    payload = dict(alloc_rows[0].get("payload") or {})
    assert payload["requested_qty"] == 6.0
    assert payload["allocated_qty"] == 6.0
    assert payload["owned_qty_before"] == 8.0
    assert payload["owned_qty_after"] == 2.0


@pytest.mark.asyncio
async def test_replica_strategy_tp1_submit_does_not_reduce_runtime_qty_before_fill(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    state.extras["trade_target_px"] = 52.0

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 52.2,
            "exec_price": 52.2,
        },
    )

    assert strategy._last_preview_by_symbol["QQQ"]["status"] == "TP1_PARTIAL_SUBMITTED"
    assert strategy._symbol_state["QQQ"].extras["live_qty"] == 8
    assert strategy._symbol_state["QQQ"].extras["pending_tp1_qty"] == 4


@pytest.mark.asyncio
async def test_replica_strategy_flatten_order_update_clears_live_state(replica_ctx, monkeypatch):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            import datetime as _dt

            value = _dt.datetime(2026, 3, 16, 15, 59, 0)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch.setattr("strategies.index_super_win_replica_strategy.datetime", _FrozenDateTime)
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": False,
            "signal_price": 50.1,
            "exec_price": 50.1,
        },
    )
    flatten_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])

    await strategy.on_order_update(
        OrderUpdate(
            order_id="3",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref=flatten_ref,
        )
    )

    assert strategy._last_preview_by_symbol["QQQ"]["status"] == "FLATTEN_CONFIRMED"
    assert strategy._symbol_state["QQQ"].is_traded is False
    assert strategy._symbol_state["QQQ"].extras["live_qty"] == 0


@pytest.mark.asyncio
async def test_replica_strategy_flatten_order_update_allocates_owned_exit_across_lots(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.active_trigger_id = "trg-flat"
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    for lot_id, qty, order_ref in (
        ("lot:index_super_win_replica_001:QQQ:base", 4.0, "iswv2:index_super_win_replica_001:TQQQ:ENTRY:TRG:base"),
        ("lot:index_super_win_replica_001:QQQ:add", 4.0, "iswv2:index_super_win_replica_001:TQQQ:ADD:TRG:add"),
    ):
        strategy._ownership_ledger.record_entry_intent(
            ReplicaOwnershipIntentRecord(
                strategy_id="index_super_win_replica_001",
                decision_id=lot_id,
                trigger_id="trg-flat",
                signal_symbol="QQQ",
                exec_symbol="TQQQ",
                side="LONG",
                qty=qty,
                ts_bar_end_us=int(
                    datetime(2026, 3, 16, 10, 0 if lot_id.endswith("base") else 1, 0).timestamp() * 1_000_000
                ),
                position_lot_id=lot_id,
                order_ref=order_ref,
            )
        )
        strategy._ownership_ledger.mark_entry_live(position_lot_id=lot_id, filled_qty=qty)

    await strategy.on_order_update(
        OrderUpdate(
            order_id="flat-1",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="FILLED",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:FLATTEN:TRG:trg-flat",
        )
    )

    assert strategy._ownership_ledger.active_qty(
        strategy_id="index_super_win_replica_001",
        signal_symbol="QQQ",
        exec_symbol="TQQQ",
    ) == 0.0
    alloc_rows = [row for row in writer.records if row.get("stage") == "ownership_exit_allocated" and row.get("reason") == "flatten"]
    assert len(alloc_rows) == 1
    payload = dict(alloc_rows[0].get("payload") or {})
    assert payload["requested_qty"] == 8.0
    assert payload["allocated_qty"] == 8.0
    assert payload["owned_qty_before"] == 8.0
    assert payload["owned_qty_after"] == 0.0


@pytest.mark.asyncio
async def test_replica_strategy_stop_terminal_clears_protection_active(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    state.stop_px = 48.5

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )
    stop_ref = str(strategy._last_preview_by_symbol["QQQ"]["order_ref"])

    await strategy.on_order_update(
        OrderUpdate(
            order_id="4",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="REJECTED",
            order_ref=stop_ref,
        )
    )

    assert strategy._last_preview_by_symbol["QQQ"]["status"] == "PROTECTIVE_STOP_TERMINAL"
    assert strategy._symbol_state["QQQ"].extras["protection_active"] is False


@pytest.mark.asyncio
async def test_replica_strategy_foreign_order_update_does_not_mutate_live_state(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.expected_entry_qty = 8
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = True

    await strategy.on_order_update(
        OrderUpdate(
            order_id="4b",
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            state="REJECTED",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:x:1",
        )
    )

    assert strategy._symbol_state["QQQ"].extras["protection_active"] is True
    assert strategy._symbol_state["QQQ"].extras["live_qty"] == 8


@pytest.mark.asyncio
async def test_replica_strategy_foreign_position_update_is_ignored(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = None
    state.extras["live_qty"] = 8

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    assert strategy._symbol_state["QQQ"].stop_px is None
    assert strategy._symbol_state["QQQ"].extras["live_qty"] == 8
    assert strategy._last_preview_by_symbol.get("QQQ", {}).get("status") != "AUDIT_SELF_HEAL_STOP_KILL_SWITCH_PREVIEW"


@pytest.mark.asyncio
async def test_replica_strategy_position_update_missing_strategy_id_is_ignored(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = None
    state.extras["live_qty"] = 8

    await strategy.on_position_update(
        PositionSnapshot(
            strategy_id="",
            symbol="TQQQ",
            pos_qty=8.0,
            avg_px=50.0,
        )
    )

    assert strategy._symbol_state["QQQ"].stop_px is None
    assert strategy._symbol_state["QQQ"].extras["live_qty"] == 8
    ignored_rows = [row for row in writer.records if row.get("stage") == "position_update_ignored"]
    assert len(ignored_rows) == 1
    assert ignored_rows[0]["reason"] == "missing_strategy_id"


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_marks_stop_live(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = True
    strategy._has_live_protective_stop_order = AsyncMock(return_value=None)

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_STOP_LIVE"
    assert preview["reason"] == "stop_active"


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_marks_stop_live_from_broker_orders(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="stop-1",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="Submitted",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:x:1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = False
    state.extras["protection_audit_miss_count"] = 5

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_STOP_LIVE"
    assert preview["audit_source"] == "broker_open_orders"
    assert strategy._symbol_state["QQQ"].extras["protection_audit_miss_count"] == 0
    assert strategy._symbol_state["QQQ"].extras["protection_active"] is True


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_marks_native_trail_live_from_broker_orders(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="88",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="Submitted",
                order_type="TRAIL",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:TRAIL:TRG:x:1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = False
    state.extras["protection_audit_miss_count"] = 5

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_STOP_LIVE"
    assert preview["audit_source"] == "broker_open_orders"
    assert strategy._symbol_state["QQQ"].extras["protection_active"] is True


@pytest.mark.asyncio
async def test_replica_strategy_tp1_fill_submits_native_runner_trail_replacing_fixed_stop(replica_ctx):
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="55",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="Submitted",
                order_type="STP",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:trigger-1:1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "runner_native_trail_enabled": True,
                "runner_trail_percent": 1.5,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.active_trigger_id = "trigger-1"
    state.expected_entry_qty = 4
    state.stop_px = 48.5
    state.trail_floor_px = 50.25
    state.trail_high_px = 51.0
    state.extras["live_qty"] = 4
    state.extras["pending_tp1_qty"] = 2
    state.extras["protection_active"] = True

    await strategy.on_order_update(
        OrderUpdate(
            order_id="77",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="Filled",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:TP1:TRG:trigger-1:1",
        )
    )

    submitted = replica_ctx.submit_intent.await_args.args[0]
    assert submitted.order_style.kind == "TRAIL"
    assert submitted.order_style.trail_percent == 1.5
    assert submitted.order_style.trail_stop_px == 50.25
    assert submitted.extras["replace_protective_order_id"] == 55
    assert submitted.extras["previous_stop_px"] == 48.5
    assert submitted.extras["protection_spec_mode"] == "NATIVE_TRAIL"
    assert submitted.extras["protection_position_id"] == "trigger-1"
    assert submitted.extras["runner_qty"] == 2.0
    assert strategy._symbol_state["QQQ"].extras["live_qty"] == 2
    assert strategy._symbol_state["QQQ"].extras["native_trail_pending"] is True


@pytest.mark.asyncio
async def test_replica_strategy_tp1_native_trail_rejection_does_not_mark_active(replica_ctx):
    replica_ctx.submit_intent = AsyncMock(return_value={"status": "REJECTED", "reasons": ["broker_reject"]})
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="55",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="Submitted",
                order_type="STP",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:trigger-1:1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "runner_native_trail_enabled": True,
                "runner_trail_percent": 1.5,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.active_trigger_id = "trigger-1"
    state.expected_entry_qty = 4
    state.stop_px = 48.5
    state.trail_floor_px = 50.25
    state.trail_high_px = 51.0
    state.extras["live_qty"] = 4
    state.extras["pending_tp1_qty"] = 2
    state.extras["protection_active"] = True

    await strategy.on_order_update(
        OrderUpdate(
            order_id="77",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="Filled",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:TP1:TRG:trigger-1:1",
        )
    )

    assert strategy._symbol_state["QQQ"].extras.get("native_trail_active") is not True
    assert strategy._symbol_state["QQQ"].extras["native_trail_pending"] is False
    assert strategy._symbol_state["QQQ"].stop_px == 48.5
    assert strategy._last_preview_by_symbol["QQQ"]["status"] == "RUNNER_NATIVE_TRAIL_REJECTED"


@pytest.mark.asyncio
async def test_replica_strategy_tp1_native_trail_submit_exception_clears_pending(replica_ctx):
    replica_ctx.submit_intent = AsyncMock(side_effect=RuntimeError("submit failed"))
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="55",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="Submitted",
                order_type="STP",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:trigger-1:1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "runner_native_trail_enabled": True,
                "runner_trail_percent": 1.5,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.active_trigger_id = "trigger-1"
    state.expected_entry_qty = 4
    state.stop_px = 48.5
    state.trail_floor_px = 50.25
    state.trail_high_px = 51.0
    state.extras["live_qty"] = 4
    state.extras["pending_tp1_qty"] = 2
    state.extras["protection_active"] = True

    await strategy.on_order_update(
        OrderUpdate(
            order_id="77",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="Filled",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:TP1:TRG:trigger-1:1",
        )
    )

    assert strategy._symbol_state["QQQ"].extras.get("native_trail_active") is not True
    assert strategy._symbol_state["QQQ"].extras["native_trail_pending"] is False
    assert strategy._symbol_state["QQQ"].extras["protection_pending"] is False
    assert strategy._symbol_state["QQQ"].stop_px == 48.5
    assert strategy._last_preview_by_symbol["QQQ"]["reason"] == "submit_exception"


@pytest.mark.asyncio
async def test_replica_strategy_pending_native_trail_terminal_keeps_existing_stop_active(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.active_trigger_id = "trigger-1"
    state.extras["live_qty"] = 2
    state.extras["protection_active"] = True
    state.extras["native_trail_pending"] = True
    state.extras["native_trail_active"] = False

    await strategy.on_order_update(
        OrderUpdate(
            order_id="88",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="Rejected",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:TRAIL:TRG:trigger-1:1",
        )
    )

    updated = strategy._symbol_state["QQQ"]
    assert updated.extras["protection_active"] is True
    assert updated.extras["native_trail_pending"] is False
    assert updated.extras["native_trail_active"] is False


@pytest.mark.asyncio
async def test_replica_strategy_active_native_trail_terminal_clears_protection_active(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.active_trigger_id = "trigger-1"
    state.extras["live_qty"] = 2
    state.extras["protection_active"] = True
    state.extras["native_trail_pending"] = False
    state.extras["native_trail_active"] = True

    await strategy.on_order_update(
        OrderUpdate(
            order_id="88",
            strategy_id="index_super_win_replica_001",
            symbol="TQQQ",
            state="Cancelled",
            order_ref="iswv2:index_super_win_replica_001:TQQQ:TRAIL:TRG:trigger-1:1",
        )
    )

    updated = strategy._symbol_state["QQQ"]
    assert updated.extras["protection_active"] is False
    assert updated.extras["native_trail_pending"] is False
    assert updated.extras["native_trail_active"] is False


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_emits_recovered_when_broker_stop_returns(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="stop-1",
                strategy_id="index_super_win_replica_001",
                symbol="TQQQ",
                state="Submitted",
                order_ref="iswv2:index_super_win_replica_001:TQQQ:STOP:TRG:x:1",
            )
        ]
    )
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = False
    state.extras["protection_audit_miss_count"] = 4

    await strategy.audit_live_protection()

    stages = [row["stage"] for row in writer.records]
    assert "protection_audit_recovered" in stages
    assert "protection_audit_stop_live" in stages


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_self_heals_inactive_stop(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(return_value=[])
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "protection_audit_self_heal_after_misses": 1,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = False

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_SELF_HEAL_PREVIEW"
    assert preview["reason"] == "audit_stop_inactive"
    stages = [row["stage"] for row in writer.records]
    assert "protection_audit_warning" in stages
    assert "protection_audit_self_heal_attempted" in stages
    assert "protection_audit_self_heal" in stages
    assert "protection_audit_self_heal_preview" in stages


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_waits_before_self_heal_threshold(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(return_value=[])
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "protection_audit_self_heal_after_misses": 3,
                "protection_audit_kill_switch_after_misses": 8,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = False

    await strategy.audit_live_protection()
    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_SELF_HEAL_ATTEMPTED"
    assert preview["miss_count"] == 2
    assert strategy._symbol_state["QQQ"].extras["protection_audit_miss_count"] == 2
    assert replica_ctx.submit_intent.await_count == 0
    stages = [row["stage"] for row in writer.records]
    assert stages.count("protection_audit_warning") == 2
    assert "protection_audit_self_heal" not in stages


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_dedupes_same_stop_preview(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = False
    state.extras["protection_pending"] = True
    state.extras["last_protection_fingerprint"] = strategy._protection_fingerprint(
        state=state,
        live_qty=8,
        stop_px=48.5,
        reason="audit_stop_inactive",
        status_prefix="PROTECTION_AUDIT_SELF_HEAL",
    )
    strategy._has_live_protective_stop_order = AsyncMock(return_value=None)

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_SELF_HEAL_DEDUPED"
    assert [row["stage"] for row in writer.records].count("protection_audit_self_heal_preview") == 0
    assert [row["stage"] for row in writer.records].count("protection_audit_self_heal_deduped") == 1


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_kill_switches_missing_stop(replica_ctx):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "SHORT"
    state.active_exec_symbol = "SQQQ"
    state.stop_px = None
    state.extras["live_qty"] = 8

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_KILL_SWITCH_PREVIEW"
    assert preview["reason"] == "audit_missing_stop_px"
    assert preview["qty"] == 8.0


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_rebuilds_missing_stop_before_self_heal(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "SHORT"
    state.active_exec_symbol = "SQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.stop_px = None
    state.extras["live_qty"] = 8

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_SELF_HEAL_PREVIEW"
    assert preview["reason"] == "audit_missing_stop_px"
    assert preview["stop_px"] == 45.0
    assert strategy._symbol_state["QQQ"].stop_px == 45.0
    rebuilt_rows = [row for row in writer.records if row.get("stage") == "protection_audit_self_heal_stop_rebuilt"]
    assert len(rebuilt_rows) == 1
    assert rebuilt_rows[0]["payload"]["stop_px"] == 45.0


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_kill_switches_after_miss_threshold(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    replica_ctx.open_orders = AsyncMock(return_value=[])
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "protection_audit_self_heal_after_misses": 2,
                "protection_audit_kill_switch_after_misses": 4,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = False

    for _ in range(4):
        await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_KILL_SWITCH_PREVIEW"
    assert preview["reason"] == "audit_stop_inactive_kill_switch"
    assert strategy._symbol_state["QQQ"].extras["protection_audit_miss_count"] == 4
    stages = [row["stage"] for row in writer.records]
    assert stages.count("protection_audit_warning") == 4


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_dedupes_same_kill_switch_preview(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "SHORT"
    state.active_exec_symbol = "SQQQ"
    state.stop_px = None
    state.extras["live_qty"] = 8
    state.extras["kill_switch_pending"] = True
    state.extras["last_kill_switch_fingerprint"] = strategy._kill_switch_fingerprint(
        state=state,
        live_qty=8,
        reason="audit_missing_stop_px",
        status_prefix="PROTECTION_AUDIT",
    )

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_KILL_SWITCH_DEDUPED"
    assert [row["stage"] for row in writer.records].count("protection_audit_kill_switch_preview") == 0
    assert [row["stage"] for row in writer.records].count("protection_audit_kill_switch_deduped") == 1


@pytest.mark.asyncio
async def test_replica_strategy_stop_audit_loop_honors_startup_grace(replica_ctx, monkeypatch):
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "stop_audit_interval_sec": 0.01,
                "stop_audit_startup_grace_sec": 2.0,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    strategy._running = True
    strategy.audit_live_protection = AsyncMock()
    strategy._has_live_protection_candidates = MagicMock(return_value=False)

    class _FakeLoop:
        def __init__(self):
            self.t = 0.0

        def time(self):
            return self.t

    fake_loop = _FakeLoop()
    sleep_calls = {"n": 0}

    async def _fake_sleep(delay: float):
        sleep_calls["n"] += 1
        fake_loop.t += float(delay)
        if sleep_calls["n"] >= 2:
            strategy._running = False

    monkeypatch.setattr("strategies.index_super_win_replica_strategy.asyncio.get_running_loop", lambda: fake_loop)
    monkeypatch.setattr("strategies.index_super_win_replica_strategy.asyncio.sleep", _fake_sleep)

    await strategy._audit_stop_order_loop()

    assert sleep_calls["n"] >= 2
    strategy.audit_live_protection.assert_not_awaited()


@pytest.mark.asyncio
async def test_replica_strategy_protection_audit_self_heal_rejected(replica_ctx):
    replica_ctx.submit_intent = AsyncMock(return_value={"status": "REJECTED", "reasons": ["broker_reject"]})
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    state = strategy._symbol_state["QQQ"]
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.stop_px = 48.5
    state.extras["live_qty"] = 8
    state.extras["protection_active"] = False

    await strategy.audit_live_protection()

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "PROTECTION_AUDIT_SELF_HEAL_REJECTED"
    assert preview["reason"] == "submit:REJECTED"
    assert strategy._symbol_state["QQQ"].extras["protection_active"] is False


@pytest.mark.asyncio
async def test_replica_strategy_entry_blocks_on_nonflat_broker_position_guard(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    async def _position(symbol):
        if symbol == "TQQQ":
            return MagicMock(pos_qty=7.0)
        return MagicMock(pos_qty=0.0)

    replica_ctx.position = AsyncMock(side_effect=_position)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "REAL_POSITION_BLOCKED"
    assert preview["reason"] == "sync_from_real_position"
    assert preview["exec_symbol"] == "TQQQ"
    assert preview["live_qty"] == 7
    replica_ctx.submit_intent.assert_not_awaited()
    blocked_rows = [
        row
        for row in writer.records
        if row["stage"] == "real_position_sync_blocked_entry"
        and row["reason"] == "sync_from_real_position"
    ]
    assert len(blocked_rows) == 1
    payload = dict(blocked_rows[0].get("payload") or {})
    assert payload.get("exec_symbol") == "TQQQ"
    assert payload.get("live_qty") == 7
    assert float(payload.get("broker_pos_qty")) == 7.0


@pytest.mark.asyncio
async def test_replica_strategy_real_position_sync_quarantines_crossed_inventory(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)

    async def _position(symbol: str):
        symbol_u = str(symbol or "").strip().upper()
        if symbol_u == "TQQQ":
            return MagicMock(pos_qty=7.0)
        if symbol_u == "SQQQ":
            return MagicMock(pos_qty=5.0)
        return MagicMock(pos_qty=0.0)

    replica_ctx.position = AsyncMock(side_effect=_position)
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": True,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()

    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "REAL_POSITION_QUARANTINED"
    assert preview["reason"] == "crossed_inventory_detected"
    crossed_rows = [row for row in writer.records if row.get("stage") == "crossed_inventory_detected"]
    assert len(crossed_rows) == 1


@pytest.mark.asyncio
async def test_replica_strategy_entry_position_guard_fail_open_when_ctx_position_reader_missing(replica_ctx):
    writer = _CaptureAuditWriter()
    replica_ctx.engine = MagicMock(audit_writer=writer)
    del replica_ctx.position
    strategy = IndexSuperWinReplicaStrategy(
        replica_ctx,
        StrategyParams(
            cadence_s=5,
            ttl_ms=2000,
            extras={
                "dry_run": False,
                "replica_manifests": [
                    {
                        "key": "qqq_pair",
                        "signal_symbol": "QQQ",
                        "confirm_symbol": "QQEW",
                        "long_exec_symbol": "TQQQ",
                        "short_exec_symbol": "SQQQ",
                    }
                ],
            },
        ),
    )
    await strategy.initialize()
    await strategy.on_features(
        "QQQ",
        {
            "signal_active": True,
            "intent_target_qty": 8,
            "intent_side": "LONG",
            "signal_reason": "replica_entry",
            "signal_price": 501.0,
        },
    )

    preview = strategy._last_preview_by_symbol["QQQ"]
    assert preview["status"] == "ENTRY_INTENT_SUBMITTED"
    replica_ctx.submit_intent.assert_awaited()
    assert any(
        row["stage"] == "entry_position_guard_degraded"
        and row.get("payload", {}).get("error") == "ctx_position_reader_missing"
        for row in writer.records
    )
