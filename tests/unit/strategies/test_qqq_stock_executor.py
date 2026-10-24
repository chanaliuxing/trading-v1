from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.qqq_stock_executor import enforce_stock_execution_blockers


class _Log:
    def __init__(self) -> None:
        self.rows = []

    def warning(self, event: str, **kwargs):
        self.rows.append(("warning", event, kwargs))

    def debug(self, event: str, **kwargs):
        self.rows.append(("debug", event, kwargs))


def _mk_strategy() -> SimpleNamespace:
    st = SimpleNamespace()
    st.strategy_id = "qqq_intraday_001"
    st.ctx = SimpleNamespace(open_orders=AsyncMock(return_value=[]))
    st.params = SimpleNamespace(extras={})
    st._last_open_orders_block_warn_ts = 0.0
    st._last_open_orders_check_warn_ts = 0.0
    st._last_real_position_block_warn_ts = 0.0
    st._is_traded = False
    st._round_live_confirmed = False
    st._current_exec_qty_for_symbol = AsyncMock(return_value=0.0)
    st._persist_runtime_state = MagicMock(return_value=None)
    st._is_terminal_order_state = lambda o: str(getattr(o, "state", "")).upper() in {"FILLED", "CANCELLED", "REJECTED"}
    return st


@pytest.mark.asyncio
async def test_enforce_stock_execution_blockers_blocks_on_open_orders():
    strategy = _mk_strategy()
    logger = _Log()
    strategy.ctx.open_orders = AsyncMock(
        return_value=[SimpleNamespace(strategy_id="qqq_intraday_001", state="Submitted")]
    )

    blocked = await enforce_stock_execution_blockers(
        strategy=strategy,
        extras={},
        logger=logger,
    )

    assert blocked is True
    strategy._current_exec_qty_for_symbol.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_stock_execution_blockers_blocks_on_live_position_and_sets_runtime_flags():
    strategy = _mk_strategy()
    logger = _Log()
    strategy._current_exec_qty_for_symbol = AsyncMock(side_effect=lambda symbol: 2.0 if symbol == "TQQQ" else 0.0)

    blocked = await enforce_stock_execution_blockers(
        strategy=strategy,
        extras={},
        logger=logger,
    )

    assert blocked is True
    assert strategy._is_traded is True
    assert strategy._round_live_confirmed is True
    strategy._persist_runtime_state.assert_called_once_with(reason="sync_from_real_position")


@pytest.mark.asyncio
async def test_enforce_stock_execution_blockers_allows_when_no_open_order_and_no_live_position():
    strategy = _mk_strategy()
    logger = _Log()

    blocked = await enforce_stock_execution_blockers(
        strategy=strategy,
        extras={},
        logger=logger,
    )

    assert blocked is False
    assert strategy._is_traded is False
    strategy._persist_runtime_state.assert_not_called()
