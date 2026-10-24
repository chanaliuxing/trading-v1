import pytest

from services.strategy_runtime_v2.ownership_service import (
    InMemoryPositionOwnershipService,
    OwnershipConflictError,
    OwnershipQuarantinedError,
    OwnershipState,
    get_shared_ownership_service,
)


@pytest.mark.asyncio
async def test_claim_conflict_is_rejected_for_other_strategy():
    svc = InMemoryPositionOwnershipService()

    first = await svc.claim(
        strategy_id="index_super_win_001",
        symbol="AAPL",
        qty=100.0,
        trigger_id="trg1",
    )

    assert first.state == OwnershipState.CLAIMED
    with pytest.raises(OwnershipConflictError):
        await svc.claim(
            strategy_id="index_super_win_002",
            symbol="AAPL",
            qty=50.0,
            trigger_id="trg2",
        )


@pytest.mark.asyncio
async def test_quarantine_blocks_future_claims_until_released():
    svc = InMemoryPositionOwnershipService()

    quarantined = await svc.quarantine(
        symbol="MSFT",
        reason="broker_unknown_position",
        broker_qty=25.0,
    )
    assert quarantined.state == OwnershipState.QUARANTINED
    assert quarantined.broker_qty == 25.0

    with pytest.raises(OwnershipQuarantinedError):
        await svc.claim(
            strategy_id="index_super_win_001",
            symbol="MSFT",
            qty=25.0,
        )

    released = await svc.release(
        strategy_id="index_super_win_001",
        symbol="MSFT",
        force=True,
    )
    assert released.state == OwnershipState.UNCLAIMED


@pytest.mark.asyncio
async def test_is_owned_by_reflects_claimed_strategy():
    svc = InMemoryPositionOwnershipService()

    await svc.claim(
        strategy_id="index_super_win_001",
        symbol="NVDA",
        qty=10.0,
    )

    assert await svc.is_owned_by(strategy_id="index_super_win_001", symbol="NVDA")
    assert not await svc.is_owned_by(strategy_id="index_super_win_002", symbol="NVDA")


def test_get_shared_ownership_service_returns_same_instance_for_same_scope():
    s1 = get_shared_ownership_service("index_super_win")
    s2 = get_shared_ownership_service("index_super_win")
    s3 = get_shared_ownership_service("index_super_win_alt")

    assert s1 is s2
    assert s1 is not s3
