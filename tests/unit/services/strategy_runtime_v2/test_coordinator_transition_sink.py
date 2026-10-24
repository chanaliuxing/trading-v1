from __future__ import annotations

import pytest

from services.strategy_runtime_v2 import (
    CoordinatorConfig,
    InMemoryPositionOwnershipService,
    InMemoryRuntimeStore,
    InMemoryStateTransitionRecorder,
)
from services.strategy_runtime_v2.coordinator import RuntimeCoordinator, TradeIntent


@pytest.mark.asyncio
async def test_coordinator_records_preview_submit_and_live_shadow_transitions() -> None:
    sink = InMemoryStateTransitionRecorder()
    coordinator = RuntimeCoordinator(
        runtime_store=InMemoryRuntimeStore(),
        ownership_service=InMemoryPositionOwnershipService(),
        config=CoordinatorConfig(dry_run=False),
        transition_sink=sink,
    )

    preview = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            target_qty=5.0,
            side="LONG",
            reason="signal_breakout",
        )
    )
    await coordinator.mark_entry_submitted(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            target_qty=5.0,
            side="LONG",
            reason="signal_breakout",
        ),
        trigger_id=str(preview.metadata["trigger_id"]),
        order_ref=str(preview.metadata["order_ref"]),
    )
    await coordinator.mark_live_from_broker(
        strategy_id="index_super_win_001",
        symbol="AAPL",
        broker_qty=5.0,
        reason="broker_fill",
    )

    events = sink.events()
    assert [event.event_type for event in events] == [
        "preview_entry",
        "entry_submitted",
        "broker_position_confirmed",
    ]
    assert [event.after_state for event in events] == [
        "preview",
        "intent_submitted",
        "live",
    ]


@pytest.mark.asyncio
async def test_coordinator_records_quarantine_and_flat_transitions() -> None:
    sink = InMemoryStateTransitionRecorder()
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=False),
        transition_sink=sink,
    )

    preview = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="MSFT",
            target_qty=3.0,
            side="SHORT",
            reason="signal_mean_revert",
        )
    )
    await coordinator.mark_entry_submitted(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="MSFT",
            target_qty=3.0,
            side="SHORT",
            reason="signal_mean_revert",
        ),
        trigger_id=str(preview.metadata["trigger_id"]),
        order_ref=str(preview.metadata["order_ref"]),
    )
    await coordinator.mark_quarantine(
        strategy_id="index_super_win_001",
        symbol="MSFT",
        reason="broker_mismatch",
        broker_qty=1.0,
    )
    await coordinator.mark_flat(
        strategy_id="index_super_win_001",
        symbol="MSFT",
        reason="recovered_to_flat",
    )

    events = sink.events()
    assert [event.after_state for event in events][-2:] == ["quarantine", "flat"]
    assert [event.event_type for event in events][-2:] == ["quarantine_applied", "flat_recorded"]


@pytest.mark.asyncio
async def test_coordinator_records_pending_entry_transition_from_order_update() -> None:
    sink = InMemoryStateTransitionRecorder()
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=False),
        transition_sink=sink,
    )

    await coordinator.mark_entry_submitted(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="QQQ",
            target_qty=1.0,
            side="LONG",
            reason="signal_breakout",
        ),
        trigger_id="trg003",
        order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg003",
    )

    await coordinator.mark_pending_entry_from_order_update(
        strategy_id="index_super_win_001",
        symbol="QQQ",
        order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg003",
        trigger_id="trg003",
    )

    events = sink.events()
    assert [event.event_type for event in events][-1] == "order_update_pending_entry"
    assert [event.after_state for event in events][-1] == "pending_entry"


@pytest.mark.asyncio
async def test_coordinator_backfills_missing_submitted_transition_before_pending_entry() -> None:
    sink = InMemoryStateTransitionRecorder()
    coordinator = RuntimeCoordinator(
        runtime_store=InMemoryRuntimeStore(),
        ownership_service=InMemoryPositionOwnershipService(),
        config=CoordinatorConfig(dry_run=False),
        transition_sink=sink,
    )

    await coordinator.mark_pending_entry_from_order_update(
        strategy_id="index_super_win_001",
        symbol="QQQ",
        order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg006",
        trigger_id="trg006",
    )

    events = sink.events()
    assert [event.event_type for event in events][-2:] == [
        "entry_submitted_backfilled",
        "order_update_pending_entry",
    ]
    assert [event.after_state for event in events][-2:] == [
        "intent_submitted",
        "pending_entry",
    ]
