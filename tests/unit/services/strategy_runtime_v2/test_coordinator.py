import pytest

from services.strategy_runtime_v2 import (
    CoordinatorConfig,
    InMemoryPositionOwnershipService,
    InMemoryRuntimeStore,
    RuntimeLifecycle,
    RuntimeState,
)
from services.strategy_runtime_v2.coordinator import RuntimeCoordinator, TradeIntent


@pytest.mark.asyncio
async def test_preview_entry_does_not_claim_or_persist_pending_state():
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=True),
    )

    decision = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            target_qty=100.0,
            side="LONG",
            reason="signal_breakout",
            signal_price=201.5,
        )
    )

    assert decision.status == "DRY_RUN_PREVIEW"
    assert decision.envelope is None
    assert decision.runtime_state.lifecycle == RuntimeLifecycle.PREVIEW
    assert str(decision.metadata.get("order_ref", "")).startswith("iswv2:index_super_win_001:AAPL:ENTRY")
    assert await runtime_store.get_state("index_super_win_001", "AAPL") is None
    ownership = await ownership_service.get("AAPL")
    assert ownership.state.value == "unclaimed"


@pytest.mark.asyncio
async def test_preview_entry_can_prepare_envelope_when_not_dry_run():
    coordinator = RuntimeCoordinator(
        runtime_store=InMemoryRuntimeStore(),
        ownership_service=InMemoryPositionOwnershipService(),
        config=CoordinatorConfig(dry_run=False, ttl_ms=3000, dlb_ms=700),
    )

    decision = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="MSFT",
            target_qty=25.0,
            side="SHORT",
            reason="signal_mean_revert",
            signal_price=412.3,
        )
    )

    assert decision.status == "ENVELOPE_READY"
    assert decision.envelope is not None
    assert decision.envelope.symbol == "MSFT"
    assert decision.envelope.extras["order_namespace"] == "iswv2"


@pytest.mark.asyncio
async def test_mark_entry_submitted_persists_submitted_runtime_without_claiming_ownership():
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=False, ttl_ms=3000, dlb_ms=700),
    )
    preview = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="MSFT",
            target_qty=25.0,
            side="SHORT",
            reason="signal_mean_revert",
            signal_price=412.3,
        )
    )

    decision = await coordinator.mark_entry_submitted(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="MSFT",
            target_qty=25.0,
            side="SHORT",
            reason="signal_mean_revert",
            signal_price=412.3,
        ),
        trigger_id=str(preview.metadata["trigger_id"]),
        order_ref=str(preview.metadata["order_ref"]),
    )

    assert decision.status == "INTENT_SUBMITTED"
    assert decision.runtime_state.lifecycle == RuntimeLifecycle.INTENT_SUBMITTED
    persisted = await runtime_store.get_state("index_super_win_001", "MSFT")
    assert persisted is not None
    assert persisted.lifecycle == RuntimeLifecycle.INTENT_SUBMITTED
    ownership = await ownership_service.get("MSFT")
    assert ownership.state.value == "unclaimed"


@pytest.mark.asyncio
async def test_mark_live_from_broker_promotes_submitted_intent_to_live_and_claims_ownership():
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=False),
    )
    preview = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            target_qty=12.0,
            side="LONG",
            reason="signal_breakout",
        )
    )
    await coordinator.mark_entry_submitted(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            target_qty=12.0,
            side="LONG",
            reason="signal_breakout",
        ),
        trigger_id=str(preview.metadata["trigger_id"]),
        order_ref=str(preview.metadata["order_ref"]),
    )

    decision = await coordinator.mark_live_from_broker(
        strategy_id="index_super_win_001",
        symbol="AAPL",
        broker_qty=12.0,
        avg_px=199.5,
        reason="broker_fill",
    )

    assert decision.status == "LIVE_FROM_BROKER"
    assert decision.runtime_state.lifecycle == RuntimeLifecycle.LIVE
    assert decision.runtime_state.broker_qty == 12.0
    assert decision.ownership.state.value == "claimed"
    assert decision.ownership.owner_strategy_id == "index_super_win_001"


@pytest.mark.asyncio
async def test_mark_live_from_broker_without_submitted_intent_quarantines_symbol():
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=True),
    )

    decision = await coordinator.mark_live_from_broker(
        strategy_id="index_super_win_001",
        symbol="AAPL",
        broker_qty=8.0,
        avg_px=199.5,
        reason="startup_scan",
    )

    assert decision.status == "QUARANTINED"
    assert decision.runtime_state.lifecycle == RuntimeLifecycle.QUARANTINE
    assert decision.ownership.state.value == "quarantined"


@pytest.mark.asyncio
async def test_preview_entry_blocks_when_same_symbol_owned_by_other_strategy():
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=False),
    )

    await ownership_service.claim(
        strategy_id="qqq_intraday_001",
        symbol="AAPL",
        qty=10.0,
        trigger_id="foreigntrg",
        order_namespace="qqqv1",
    )

    decision = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            target_qty=5.0,
            side="LONG",
            reason="same_symbol_conflict",
        )
    )

    assert decision.status == "OWNERSHIP_CONFLICT"
    assert "owned_by:qqq_intraday_001" in decision.reasons


@pytest.mark.asyncio
async def test_same_symbol_conflict_does_not_block_different_symbol_preview():
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=True),
    )

    await ownership_service.claim(
        strategy_id="qqq_intraday_001",
        symbol="AAPL",
        qty=10.0,
        trigger_id="foreigntrg",
        order_namespace="qqqv1",
    )

    blocked = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="AAPL",
            target_qty=5.0,
            side="LONG",
            reason="same_symbol_conflict",
        )
    )
    allowed = await coordinator.preview_entry(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="MSFT",
            target_qty=8.0,
            side="LONG",
            reason="different_symbol_ok",
        )
    )

    assert blocked.status == "OWNERSHIP_CONFLICT"
    assert allowed.status == "DRY_RUN_PREVIEW"
    assert allowed.runtime_state.symbol == "MSFT"


@pytest.mark.asyncio
async def test_update_protective_order_refs_persists_refs_without_changing_lifecycle():
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=False),
    )

    await coordinator.mark_entry_submitted(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="QQQ",
            target_qty=2.0,
            side="LONG",
            reason="signal_breakout",
        ),
        trigger_id="trg001",
        order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg001",
    )

    updated = await coordinator.update_protective_order_refs(
        strategy_id="index_super_win_001",
        symbol="QQQ",
        order_refs=["stop-1", "tp-1"],
    )

    assert updated.lifecycle == RuntimeLifecycle.INTENT_SUBMITTED
    assert updated.protective_order_refs == ("stop-1", "tp-1")


@pytest.mark.asyncio
async def test_mark_pending_entry_from_order_update_promotes_submitted_state():
    runtime_store = InMemoryRuntimeStore()
    ownership_service = InMemoryPositionOwnershipService()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=ownership_service,
        config=CoordinatorConfig(dry_run=False),
    )

    await coordinator.mark_entry_submitted(
        TradeIntent(
            strategy_id="index_super_win_001",
            symbol="QQQ",
            target_qty=2.0,
            side="LONG",
            reason="signal_breakout",
        ),
        trigger_id="trg002",
        order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg002",
    )

    pending = await coordinator.mark_pending_entry_from_order_update(
        strategy_id="index_super_win_001",
        symbol="QQQ",
        order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg002",
        trigger_id="trg002",
    )

    assert pending.lifecycle == RuntimeLifecycle.PENDING_ENTRY
    assert pending.active_trigger_id == "trg002"
    assert pending.entry_order_ref == "iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg002"


@pytest.mark.asyncio
async def test_mark_pending_entry_from_order_update_backfills_missing_submitted_state() -> None:
    runtime_store = InMemoryRuntimeStore()
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=InMemoryPositionOwnershipService(),
        config=CoordinatorConfig(dry_run=False),
    )

    pending = await coordinator.mark_pending_entry_from_order_update(
        strategy_id="index_super_win_001",
        symbol="QQQ",
        order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg004",
        trigger_id="trg004",
    )

    assert pending.lifecycle == RuntimeLifecycle.PENDING_ENTRY
    assert pending.active_trigger_id == "trg004"


@pytest.mark.asyncio
async def test_mark_pending_entry_from_order_update_backfills_preview_state() -> None:
    runtime_store = InMemoryRuntimeStore()
    await runtime_store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="QQQ",
            lifecycle=RuntimeLifecycle.PREVIEW,
            target_qty=2.0,
            side="LONG",
            active_trigger_id="trg005",
            entry_order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg005",
        )
    )
    coordinator = RuntimeCoordinator(
        runtime_store=runtime_store,
        ownership_service=InMemoryPositionOwnershipService(),
        config=CoordinatorConfig(dry_run=False),
    )

    pending = await coordinator.mark_pending_entry_from_order_update(
        strategy_id="index_super_win_001",
        symbol="QQQ",
        order_ref="iswv2:index_super_win_001:QQQ:ENTRY:TRG:trg005",
        trigger_id="trg005",
    )

    assert pending.lifecycle == RuntimeLifecycle.PENDING_ENTRY
    assert pending.active_trigger_id == "trg005"
