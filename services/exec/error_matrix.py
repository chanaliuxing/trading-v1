"""
IB Error Matrix - Error Code to Action Mapping

Table-driven error handling for IB execution errors with:
- Retry policies (exponential backoff)
- Adjust-and-retry (price correction)
- Abort (permanent failure)
- Degrade (reduce rate/volume)

Based on doc/44.md specification.
"""
import re
import asyncio
import random
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable, List
import math

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExecErrorAction:
    """Action to take in response to execution error"""

    action: str  # RETRY / ADJUST_AND_RETRY / ABORT / DEGRADE
    reason: str
    params: Dict[str, Any]
    retry_after_ms: int = 0


# Error classification rules (pattern -> action)
_IB_ERROR_RULES: List[tuple] = [
    # Price invalid/out of range → Adjust and retry
    (
        re.compile(r"(10147|Invalid price|Price out of range)", re.I),
        lambda ctx: ExecErrorAction(
            "ADJUST_AND_RETRY",
            "price_out_of_range",
            {"adjust": "to_tick_or_luld"},
            retry_after_ms=100,
        ),
    ),
    # Contract/account invalid → Abort
    (
        re.compile(r"(201|The account.*is invalid|No security definition)", re.I),
        lambda ctx: ExecErrorAction("ABORT", "invalid_account_or_contract", {}),
    ),
    # Pacing violation → Degrade (reduce rate)
    (
        re.compile(r"(pacing|Exceeded max rate of messages)", re.I),
        lambda ctx: ExecErrorAction(
            "DEGRADE",
            "pacing_violation",
            {"reduce_rate": True},
            retry_after_ms=1200,
        ),
    ),
    # Market closed/not permitted → Abort
    (
        re.compile(r"(order is not permitted|outside trading hours|halted)", re.I),
        lambda ctx: ExecErrorAction("ABORT", "not_permitted_now", {}),
    ),
    # SSR violation (broker-side check) → Adjust price and retry
    (
        re.compile(r"(short sale|SSR)", re.I),
        lambda ctx: ExecErrorAction(
            "ADJUST_AND_RETRY",
            "ssr_violation",
            {"adjust": "lift_above_bid"},
            retry_after_ms=150,
        ),
    ),
    # Order already cancelled → Abort (idempotent)
    (
        re.compile(r"(already cancelled|already filled)", re.I),
        lambda ctx: ExecErrorAction("ABORT", "already_terminal", {}),
    ),
    # Insufficient margin/buying power → Abort
    (
        re.compile(r"(insufficient|margin|buying power)", re.I),
        lambda ctx: ExecErrorAction("ABORT", "insufficient_funds", {}),
    ),
    # Order size too large → Degrade (reduce quantity)
    (
        re.compile(r"(order size|quantity)", re.I),
        lambda ctx: ExecErrorAction(
            "DEGRADE", "order_size_too_large", {"reduce_qty": True}, retry_after_ms=200
        ),
    ),
]


def classify_ib_error(err_msg: str, ctx: Dict[str, Any]) -> ExecErrorAction:
    """
    Classify IB error message and return action

    Args:
        err_msg: Error message from IB
        ctx: Context dict with order details

    Returns:
        ExecErrorAction specifying what to do
    """
    for pattern, action_fn in _IB_ERROR_RULES:
        if pattern.search(err_msg or ""):
            return action_fn(ctx)

    # Default: retry with backoff
    return ExecErrorAction("RETRY", "unknown_ib_error", {}, retry_after_ms=200)


class RetryBudget:
    """
    Retry budget with exponential backoff and jitter
    """

    def __init__(self, max_tries: int = 5, base_ms: int = 100, jitter_ms: int = 50):
        self.max = max_tries
        self.base = base_ms
        self.jitter = jitter_ms

    def delay_ms(self, attempt: int) -> int:
        """Calculate delay for given attempt"""
        expo = self.base * (2 ** max(0, attempt - 1))
        jitter = random.randint(0, self.jitter)
        return min(3000, int(expo + jitter))


async def exec_with_policy(
    op_fn: Callable,
    *,
    args: tuple = (),
    kwargs: Dict[str, Any] = None,
    ctx: Dict[str, Any],
    retry_budget: RetryBudget,
    adjust_price_fn: Optional[Callable] = None,
) -> Any:
    """
    Execute operation with error handling policy

    Args:
        op_fn: Operation function to execute
        args: Positional arguments
        kwargs: Keyword arguments
        ctx: Execution context (symbol, side, prices, market data)
        retry_budget: Retry budget configuration
        adjust_price_fn: Optional custom price adjustment function

    Returns:
        Result of successful operation

    Raises:
        Exception if all retries exhausted or action is ABORT
    """
    if kwargs is None:
        kwargs = {}

    attempt = 0
    last_error = None

    while True:
        try:
            result = await op_fn(*args, **kwargs)
            logger.info(
                f"Operation succeeded",
                symbol=ctx.get("symbol"),
                attempt=attempt,
            )
            return result

        except Exception as e:
            attempt += 1
            last_error = e

            # Classify error
            action = classify_ib_error(str(e), ctx)

            logger.warning(
                f"Operation failed: {action.action}",
                symbol=ctx.get("symbol"),
                attempt=attempt,
                error=str(e),
                action=action.action,
                reason=action.reason,
            )

            # ABORT: stop immediately
            if action.action == "ABORT":
                logger.error(
                    "Operation aborted",
                    symbol=ctx.get("symbol"),
                    reason=action.reason,
                    error=str(e),
                )
                raise

            # Check retry budget
            if attempt >= retry_budget.max:
                logger.error(
                    "Retry budget exhausted",
                    symbol=ctx.get("symbol"),
                    attempts=attempt,
                    error=str(e),
                )
                raise

            # DEGRADE: apply degradation
            if action.action == "DEGRADE":
                if action.params.get("reduce_rate"):
                    logger.info(
                        "Degrading: reducing rate",
                        symbol=ctx.get("symbol"),
                    )
                    # TODO: Notify rate limiter to reduce rate
                if action.params.get("reduce_qty"):
                    logger.info(
                        "Degrading: reducing quantity",
                        symbol=ctx.get("symbol"),
                    )
                    # TODO: Reduce quantity in ctx/kwargs

            # ADJUST_AND_RETRY: adjust price
            if action.action == "ADJUST_AND_RETRY":
                if adjust_price_fn:
                    adjustments = await adjust_price_fn(ctx)
                else:
                    adjustments = await _default_adjust_price(ctx)

                # Update context and kwargs
                ctx.update(adjustments)
                for k, v in adjustments.items():
                    if k in kwargs:
                        kwargs[k] = v

                logger.info(
                    "Price adjusted",
                    symbol=ctx.get("symbol"),
                    adjustments=adjustments,
                )

            # Delay before retry
            delay = action.retry_after_ms or retry_budget.delay_ms(attempt)
            logger.debug(
                f"Retrying after {delay}ms",
                symbol=ctx.get("symbol"),
                attempt=attempt,
            )
            await asyncio.sleep(delay / 1000.0)


async def _default_adjust_price(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Default price adjustment logic

    Adjustments:
    - Round to tick size
    - Clamp to LULD bands
    - Lift above bid for SSR short sales

    Args:
        ctx: Context with symbol, side, prices, market data

    Returns:
        Dict of adjusted prices
    """
    tick = ctx.get("tick", 0.01)

    def to_tick(px: float) -> float:
        if px is None:
            return None
        return round(round(px / tick) * tick, 10)

    out = {}

    # Adjust limit price
    if ctx.get("limit_px") is not None:
        px = to_tick(ctx["limit_px"])

        # LULD clamping
        low = ctx.get("luld_low")
        high = ctx.get("luld_high")
        if low is not None:
            px = max(px, to_tick(low))
        if high is not None:
            px = min(px, to_tick(high))

        # SSR: lift above bid for short sales
        if ctx.get("ssr_active") and ctx.get("side", "").upper().startswith("SELL"):
            bid = ctx.get("bid") or 0.0
            px = max(px, to_tick(bid + tick))

        out["limit_px"] = px

    # Adjust stop price
    if ctx.get("stop_px") is not None:
        px = to_tick(ctx["stop_px"])

        # LULD clamping
        low = ctx.get("luld_low")
        high = ctx.get("luld_high")
        if low is not None:
            px = max(px, to_tick(low))
        if high is not None:
            px = min(px, to_tick(high))

        out["stop_px"] = px

    return out


# Convenience wrappers for common operations

async def place_order_with_policy(
    adapter,
    symbol: str,
    side: str,
    qty: float,
    order_type: str,
    limit_px: Optional[float] = None,
    stop_px: Optional[float] = None,
    tif: str = "DAY",
    order_ref: Optional[str] = None,
    oca_group: Optional[str] = None,
    market_state: Optional[Dict[str, Any]] = None,
    retry_budget: Optional[RetryBudget] = None,
) -> Any:
    """
    Place order with error handling policy

    Args:
        adapter: IB adapter
        symbol: Symbol
        side: BUY/SELL
        qty: Quantity
        order_type: MKT/LMT/STP/STP_LMT
        limit_px: Limit price
        stop_px: Stop price
        tif: Time in force
        order_ref: Order reference
        oca_group: OCA group
        market_state: Market state dict (bid, ask, luld, ssr, tick)
        retry_budget: Retry configuration

    Returns:
        Order result
    """
    if retry_budget is None:
        retry_budget = RetryBudget(max_tries=5, base_ms=120, jitter_ms=80)

    # Build context
    ctx = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "order_type": order_type,
        "limit_px": limit_px,
        "stop_px": stop_px,
        "tif": tif,
    }

    # Add market state
    if market_state:
        ctx.update(
            {
                "tick": market_state.get("tick_size", 0.01),
                "luld_low": market_state.get("luld_low"),
                "luld_high": market_state.get("luld_high"),
                "ssr_active": market_state.get("ssr_active", False),
                "bid": market_state.get("bid_px"),
                "ask": market_state.get("ask_px"),
            }
        )

    # Select adapter method based on order type
    if order_type == "MKT":
        op_fn = adapter.place_market
        args = (symbol, side, qty, tif, order_ref, oca_group)
    elif order_type == "LMT":
        op_fn = adapter.place_limit
        args = (symbol, side, qty, limit_px, tif, order_ref, oca_group)
    elif order_type == "STP":
        op_fn = adapter.place_stop
        args = (symbol, side, qty, stop_px, tif, order_ref, oca_group)
    elif order_type == "STP_LMT":
        op_fn = adapter.place_stop_limit
        args = (symbol, side, qty, stop_px, limit_px, tif, order_ref, oca_group)
    else:
        raise ValueError(f"Unknown order type: {order_type}")

    return await exec_with_policy(
        op_fn, args=args, ctx=ctx, retry_budget=retry_budget
    )
