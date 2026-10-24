import pytest

from services.strategy_runtime_v2.runtime_store import (
    InMemoryRuntimeStore,
    RuntimeLifecycle,
    RuntimeState,
    get_shared_runtime_store,
)


@pytest.mark.asyncio
async def test_runtime_store_put_get_and_list_are_strategy_scoped():
    store = InMemoryRuntimeStore()

    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_001",
            symbol="aapl",
            lifecycle=RuntimeLifecycle.PENDING_ENTRY,
            target_qty=100.0,
            active_trigger_id="trg1",
        )
    )
    await store.put_state(
        RuntimeState(
            strategy_id="index_super_win_002",
            symbol="msft",
            lifecycle=RuntimeLifecycle.FLAT,
        )
    )

    state = await store.get_state("index_super_win_001", "AAPL")
    assert state is not None
    assert state.symbol == "AAPL"
    assert state.lifecycle == RuntimeLifecycle.PENDING_ENTRY

    only_first = await store.list_states("index_super_win_001")
    assert [item.symbol for item in only_first] == ["AAPL"]


@pytest.mark.asyncio
async def test_runtime_store_mutate_state_receives_previous_value():
    store = InMemoryRuntimeStore()

    created = await store.mutate_state(
        "index_super_win_001",
        "nvda",
        lambda current: RuntimeState(
            strategy_id="index_super_win_001",
            symbol="NVDA",
            lifecycle=RuntimeLifecycle.PENDING_ENTRY,
            target_qty=50.0 if current is None else current.target_qty,
        ),
    )
    updated = await store.mutate_state(
        "index_super_win_001",
        "nvda",
        lambda current: current.evolve(
            lifecycle=RuntimeLifecycle.LIVE,
            live_qty=50.0,
            target_qty=current.target_qty,
        ),
    )

    assert created.lifecycle == RuntimeLifecycle.PENDING_ENTRY
    assert updated.lifecycle == RuntimeLifecycle.LIVE
    assert updated.live_qty == 50.0


def test_get_shared_runtime_store_returns_same_instance_for_same_scope():
    s1 = get_shared_runtime_store("index_super_win")
    s2 = get_shared_runtime_store("index_super_win")
    s3 = get_shared_runtime_store("index_super_win_alt")

    assert s1 is s2
    assert s1 is not s3
