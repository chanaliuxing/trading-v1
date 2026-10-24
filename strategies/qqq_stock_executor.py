from __future__ import annotations

import asyncio
from typing import Any, Dict


async def enforce_stock_execution_blockers(
    *,
    strategy: Any,
    extras: Dict[str, Any],
    logger: Any,
) -> bool:
    """
    Apply stock-only execution blockers after shared signal publication.

    Returns True when stock execution must be blocked for this cycle.
    """
    try:
        open_orders = await strategy.ctx.open_orders()
        if any(
            (getattr(o, "strategy_id", None) == strategy.strategy_id)
            and (not strategy._is_terminal_order_state(o))
            for o in open_orders
        ):
            warn_interval_sec = max(10.0, float(extras.get("open_orders_block_warn_interval_sec", 60.0)))
            loop_now = asyncio.get_running_loop().time()
            if (loop_now - strategy._last_open_orders_block_warn_ts) >= warn_interval_sec:
                strategy._last_open_orders_block_warn_ts = loop_now
                logger.warning(
                    "qqq_entry_blocked_by_open_orders",
                    strategy_id=strategy.strategy_id,
                    warn_interval_sec=warn_interval_sec,
                )
            return True
    except Exception as exc:
        warn_interval_sec = max(10.0, float((strategy.params.extras or {}).get("open_orders_check_warn_interval_sec", 300.0)))
        loop_now = asyncio.get_running_loop().time()
        if (loop_now - strategy._last_open_orders_check_warn_ts) >= warn_interval_sec:
            strategy._last_open_orders_check_warn_ts = loop_now
            logger.warning(
                "qqq_open_orders_check_failed",
                strategy_id=strategy.strategy_id,
                error=str(exc),
                warn_interval_sec=warn_interval_sec,
            )
        else:
            logger.debug("qqq_open_orders_check_failed_suppressed", strategy_id=strategy.strategy_id, error=str(exc))

    try:
        qty_t = abs(await strategy._current_exec_qty_for_symbol("TQQQ"))
        qty_s = abs(await strategy._current_exec_qty_for_symbol("SQQQ"))
        if qty_t > 0 or qty_s > 0:
            if not strategy._is_traded:
                warn_interval_sec = max(10.0, float(extras.get("real_position_block_warn_interval_sec", 60.0)))
                loop_now = asyncio.get_running_loop().time()
                if (loop_now - strategy._last_real_position_block_warn_ts) >= warn_interval_sec:
                    strategy._last_real_position_block_warn_ts = loop_now
                    logger.warning(
                        "qqq_entry_blocked_by_real_position",
                        strategy_id=strategy.strategy_id,
                        tqqq_qty=qty_t,
                        sqqq_qty=qty_s,
                        warn_interval_sec=warn_interval_sec,
                    )
                strategy._is_traded = True
                strategy._round_live_confirmed = True
                strategy._persist_runtime_state(reason="sync_from_real_position")
            return True
    except Exception as exc:
        logger.warning("qqq_real_position_check_failed", strategy_id=strategy.strategy_id, error=str(exc))

    return False

