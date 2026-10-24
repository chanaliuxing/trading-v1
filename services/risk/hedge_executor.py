"""
Hedge Executor - Translates delta gaps into option orders.

This module bridges the hedging service (which determines WHAT to hedge)
with the order routing system (which executes HOW to hedge).

Core responsibilities:
1. Calculate hedge action (add/reduce/hold)
2. Select optimal option contract via OptionsSelector
3. Calculate quantity and limit price
4. Submit orders via order router
5. Handle failures with degradation logic
"""

import asyncio
import math
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.config import config
from scripts.ai_trader.risk.options_selector import (
    OptionsSelector,
    OptionsPolicy,
    HedgeIntent,
    SelectionResult,
)
from services.market.options_data import OptionsDataProvider

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class HedgeResult:
    """Result of hedge execution attempt."""

    success: bool
    action: str  # add, reduce, hold, reject
    reason: str
    contract: Optional[Dict[str, Any]] = None
    qty: int = 0
    notional_usd: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for serialization."""
        return asdict(self)


class HedgeExecutor:
    """
    Executes hedge adjustments via option orders.

    Decision flow:
    1. Analyze delta_gap to determine action (add/reduce/hold)
    2. Use OptionsSelector to pick contract
    3. Calculate quantity based on contract delta
    4. Submit order via router (or fallback to linear hedge)
    5. Track failures and trigger degradation
    """

    def __init__(
        self,
        options_selector: Optional[OptionsSelector] = None,
        order_router: Optional[Any] = None,  # Will be injected
    ):
        """Initialize executor.

        Args:
            options_selector: OptionsSelector instance (created from config if None)
            order_router: Order routing service (for placing orders)
        """
        # Load config
        cfg = config.get("risk_management.hedge.options", {}) or {}
        self.enabled = cfg.get("enabled", False)
        self.whitelist = cfg.get("instruments", ["SPY", "QQQ", "IWM", "DIA"])
        self.max_notional_per_day = cfg.get("max_notional_usd_per_day", 100000.0)
        self.max_contracts_per_day = cfg.get("max_contracts_per_day", 50)
        self.slippage_bps = cfg.get("slippage_bps", 10.0)
        self.tif = cfg.get("tif", "DAY")
        self.dry_run = cfg.get("dry_run", True)
        self.allow_linear_fallback = cfg.get("allow_linear_fallback", False)

        # OptionsSelector
        if options_selector is None:
            policy = OptionsPolicy.from_config()
            self.options_selector = OptionsSelector(policy=policy)
        else:
            self.options_selector = options_selector

        # Order router (injected by caller)
        self.order_router = order_router

        # Failure tracking
        self.consecutive_failures = 0
        self.max_consecutive_failures = 3
        self.cooldown_after_failure_sec = 900  # 15 minutes
        self.last_failure_ts = 0.0

        # Daily usage tracking
        self.daily_stats = {
            "date": datetime.now(ET).date(),
            "contracts": 0,
            "notional": 0.0,
        }

        logger.info(
            "HedgeExecutor initialized",
            enabled=self.enabled,
            whitelist=self.whitelist,
            max_notional_per_day=self.max_notional_per_day,
            max_contracts_per_day=self.max_contracts_per_day,
            dry_run=self.dry_run,
            allow_linear_fallback=self.allow_linear_fallback,
        )

    async def execute(
        self,
        delta_gap_usd: float,
        exposure: Dict[str, float],
        intent: Optional[HedgeIntent] = None,
    ) -> HedgeResult:
        """
        Execute hedge adjustment based on delta gap.

        Args:
            delta_gap_usd: Target hedge delta - current hedge delta
                          Negative = need more protection (buy puts)
                          Positive = over-hedged (reduce/close puts)
            exposure: Portfolio exposure dict with 'net' and 'gross'
            intent: Optional HedgeIntent from AI brain

        Returns:
            HedgeResult with action taken and details
        """
        # Reset daily stats if new day
        self._check_daily_reset()

        # Check if disabled
        if not self.enabled:
            return HedgeResult(
                success=False,
                action="reject",
                reason="Options hedging disabled in config",
            )

        # Check if in failure cooldown
        if self._in_failure_cooldown():
            return HedgeResult(
                success=False,
                action="hold",
                reason=f"In cooldown after {self.consecutive_failures} consecutive failures",
            )

        # Determine action based on delta gap
        action = self._determine_action(delta_gap_usd)

        if action == "hold":
            return HedgeResult(
                success=True,
                action="hold",
                reason="Delta gap within tolerance",
            )

        # Calculate target notional for the adjustment
        target_notional_usd = abs(delta_gap_usd)

        # Check daily limits
        # Notional-only pre-check; contracts enforced after selection
        if self.daily_stats["notional"] + target_notional_usd > self.max_notional_per_day:
            logger.warning(
                "hedge_daily_limit_exceeded",
                contracts_today=self.daily_stats["contracts"],
                max_contracts=self.max_contracts_per_day,
                notional_today=self.daily_stats["notional"],
                max_notional=self.max_notional_per_day,
            )
            return HedgeResult(
                success=False,
                action="reject",
                reason=f"Daily notional limit exceeded (${self.daily_stats['notional']:.0f}/${self.max_notional_per_day:.0f})",
            )

        # Select contract
        try:
            selection = await self.options_selector.select_hedge_contract(
                exposure=exposure,
                intent=intent,
                target_notional_usd=target_notional_usd,
            )

            if not selection.success:
                logger.warning("hedge_contract_selection_failed", error=selection.error)

                # Try linear fallback if enabled
                if self.allow_linear_fallback:
                    return await self._execute_linear_fallback(delta_gap_usd, exposure)

                # Record failure
                self._record_failure()
                return HedgeResult(
                    success=False,
                    action="reject",
                    reason=f"Contract selection failed: {selection.error}",
                    error=selection.error,
                )

            contract = selection.contract

            # Check instrument whitelist
            if contract.underlying not in self.whitelist:
                logger.warning(
                    "hedge_instrument_not_whitelisted",
                    instrument=contract.underlying,
                    whitelist=self.whitelist,
                )
                return HedgeResult(
                    success=False,
                    action="reject",
                    reason=f"Instrument {contract.underlying} not in whitelist",
                )

            # Calculate quantity
            # For options: notional = qty * abs(delta) * underlying_px * 100
            # So: qty = ceil(target_notional / (abs(delta) * underlying_px * 100))
            underlying_px = await self._get_underlying_price(contract.underlying)
            if underlying_px <= 0:
                return HedgeResult(
                    success=False,
                    action="reject",
                    reason=f"Could not get underlying price for {contract.underlying}",
                )

            qty = contract.quantity  # OptionsSelector already calculated this

            # Enforce daily limits with actual qty/notional
            if not self._check_daily_limits(add_contracts=qty, add_notional=contract.notional_usd):
                return HedgeResult(
                    success=False,
                    action="reject",
                    reason=(
                        f"Daily limits exceeded (contracts: {self.daily_stats['contracts'] + qty}/"
                        f"{self.max_contracts_per_day}, notional: "
                        f"${self.daily_stats['notional'] + contract.notional_usd:.0f}/"
                        f"${self.max_notional_per_day:.0f})"
                    ),
                )

            # Calculate limit price with slippage
            limit_price = self._calculate_limit_price(
                mid_price=contract.mid_price,
                side="buy" if action == "add" else "sell",
            )

            # Submit order (or log in dry-run)
            if self.dry_run:
                logger.info(
                    "hedge_dry_run",
                    action=action,
                    contract=contract.occ_symbol,
                    qty=qty,
                    limit_price=limit_price,
                    notional=contract.notional_usd,
                    delta=contract.delta,
                    rationale=contract.rationale,
                )

                # Update daily stats (even in dry-run for testing)
                self.daily_stats["contracts"] += qty
                self.daily_stats["notional"] += contract.notional_usd

                return HedgeResult(
                    success=True,
                    action=action,
                    reason=f"Dry-run: {contract.rationale}",
                    contract=contract.to_dict(),
                    qty=qty,
                    notional_usd=contract.notional_usd,
                )

            # Real execution
            if self.order_router is None:
                logger.error("hedge_no_order_router", msg="Order router not injected")

                # Try linear fallback
                if self.allow_linear_fallback:
                    return await self._execute_linear_fallback(delta_gap_usd, exposure)

                self._record_failure()
                return HedgeResult(
                    success=False,
                    action="reject",
                    reason="Order router not available",
                )

            # Place option order
            try:
                side = "BUY" if action == "add" else "SELL"

                if hasattr(self.order_router, "place_option"):
                    order_result = await self.order_router.place_option(
                        occ_symbol=contract.occ_symbol,
                        side=side,
                        qty=qty,
                        limit_price=limit_price,
                        tif=self.tif,
                        strategy_id="sys_hedger",
                    )
                else:
                    order_result = {"success": False, "error": "Order router lacks place_option"}

                if order_result.get("success"):
                    logger.info(
                        "hedge_executed",
                        action=action,
                        contract=contract.occ_symbol,
                        qty=qty,
                        limit_price=limit_price,
                        order_id=order_result.get("order_id"),
                    )

                    # Update daily stats
                    self.daily_stats["contracts"] += qty
                    self.daily_stats["notional"] += contract.notional_usd

                    # Reset failure counter
                    self.consecutive_failures = 0

                    return HedgeResult(
                        success=True,
                        action=action,
                        reason=f"Order placed: {contract.rationale}",
                        contract=contract.to_dict(),
                        qty=qty,
                        notional_usd=contract.notional_usd,
                    )
                else:
                    raise Exception(order_result.get("error", "Unknown order error"))

            except Exception as e:
                logger.error(
                    "hedge_order_failed",
                    contract=contract.occ_symbol,
                    error=str(e),
                    exc_info=True,
                )

                # Try linear fallback
                if self.allow_linear_fallback:
                    return await self._execute_linear_fallback(delta_gap_usd, exposure)

                self._record_failure()
                return HedgeResult(
                    success=False,
                    action="reject",
                    reason=f"Order execution failed: {str(e)}",
                    error=str(e),
                )

        except Exception as e:
            logger.error("hedge_execution_error", error=str(e), exc_info=True)
            self._record_failure()
            return HedgeResult(
                success=False,
                action="reject",
                reason=f"Execution error: {str(e)}",
                error=str(e),
            )

    def _determine_action(self, delta_gap_usd: float) -> str:
        """Determine hedge action based on delta gap.

        Args:
            delta_gap_usd: Target - Current hedge delta

        Returns:
            "add", "reduce", or "hold"
        """
        # delta_gap < 0: need more protection (target more negative than current)
        # delta_gap > 0: over-hedged (target less negative than current)

        if delta_gap_usd < -1000:  # Need to add protection
            return "add"
        elif delta_gap_usd > 1000:  # Need to reduce protection
            return "reduce"
        else:
            return "hold"

    def _check_daily_limits(self, add_contracts: int, add_notional: float) -> bool:
        """Check if this trade would exceed daily limits."""
        if self.daily_stats["contracts"] + add_contracts > self.max_contracts_per_day:
            return False
        if self.daily_stats["notional"] + add_notional > self.max_notional_per_day:
            return False
        return True

    def _check_daily_reset(self):
        """Reset daily stats if it's a new day."""
        today = datetime.now(ET).date()
        if self.daily_stats["date"] != today:
            logger.info(
                "hedge_daily_reset",
                prev_date=str(self.daily_stats["date"]),
                prev_contracts=self.daily_stats["contracts"],
                prev_notional=self.daily_stats["notional"],
            )
            self.daily_stats = {
                "date": today,
                "contracts": 0,
                "notional": 0.0,
            }

    def _calculate_limit_price(self, mid_price: float, side: str) -> float:
        """Calculate limit price with slippage.

        Args:
            mid_price: Contract mid price
            side: "buy" or "sell"

        Returns:
            Limit price with slippage applied
        """
        slippage_mult = self.slippage_bps / 10000.0

        if side == "buy":
            # Pay up to mid + slippage
            return mid_price * (1.0 + slippage_mult)
        else:
            # Accept down to mid - slippage
            return mid_price * (1.0 - slippage_mult)

    async def _get_underlying_price(self, symbol: str) -> float:
        """Get current underlying price.

        TODO: Use market data service for real price
        For now, delegate to options data provider
        """
        try:
            # Options provider has underlying price capability
            if hasattr(self.options_selector.data_provider, 'get_underlying_price'):
                return await self.options_selector.data_provider.get_underlying_price(symbol)

            logger.warning("underlying_price_unavailable", symbol=symbol)
            return 0.0
        except Exception as e:
            logger.error("get_underlying_price_error", symbol=symbol, error=str(e))
            return 0.0

    def _record_failure(self):
        """Record a failure and update cooldown."""
        self.consecutive_failures += 1
        self.last_failure_ts = datetime.now(ET).timestamp()

        logger.warning(
            "hedge_failure_recorded",
            consecutive_failures=self.consecutive_failures,
            max_failures=self.max_consecutive_failures,
            cooldown_sec=self.cooldown_after_failure_sec,
        )

        # Check if we should disable
        if self.consecutive_failures >= self.max_consecutive_failures:
            logger.error(
                "hedge_auto_disabled",
                consecutive_failures=self.consecutive_failures,
                msg="Too many consecutive failures - manual reset required",
            )

    def _in_failure_cooldown(self) -> bool:
        """Check if we're in failure cooldown period."""
        if self.consecutive_failures == 0:
            return False

        now = datetime.now(ET).timestamp()
        elapsed = now - self.last_failure_ts
        return elapsed < self.cooldown_after_failure_sec

    async def _execute_linear_fallback(
        self,
        delta_gap_usd: float,
        exposure: Dict[str, float],
    ) -> HedgeResult:
        """Fallback to linear hedge using ETF stock orders.

        This is a safety fallback when options execution fails.
        Uses SPY or QQQ stock orders to achieve delta hedge.

        Args:
            delta_gap_usd: Delta gap to hedge
            exposure: Portfolio exposure

        Returns:
            HedgeResult for linear hedge
        """
        logger.warning(
            "hedge_linear_fallback",
            delta_gap=delta_gap_usd,
            msg="Falling back to linear ETF hedge",
        )

        # Determine instrument (SPY for broad exposure, QQQ for tech)
        net = exposure.get("net", 0.0)
        instrument = "SPY"  # Default to SPY

        # Get current price
        price = await self._get_underlying_price(instrument)
        if price <= 0:
            self._record_failure()
            return HedgeResult(
                success=False,
                action="reject",
                reason="Linear fallback failed: could not get ETF price",
            )

        # Calculate shares
        # delta_gap < 0: need to short ETF (or buy inverse)
        # delta_gap > 0: need to buy ETF (or reduce short)
        shares = int(abs(delta_gap_usd) / price)

        if shares == 0:
            return HedgeResult(
                success=True,
                action="hold",
                reason="Linear fallback: delta gap too small for ETF shares",
            )

        side = "SELL" if delta_gap_usd < 0 else "BUY"

        notional = shares * price

        # Enforce limits (treat linear hedge as single contract for counter)
        if not self._check_daily_limits(add_contracts=1, add_notional=notional):
            return HedgeResult(
                success=False,
                action="reject",
                reason=(
                    f"Linear fallback daily limits exceeded (contracts:{self.daily_stats['contracts'] + 1}/"
                    f"{self.max_contracts_per_day}, notional:${self.daily_stats['notional'] + notional:.0f}/"
                    f"${self.max_notional_per_day:.0f})"
                ),
            )

        logger.info(
            "linear_hedge_plan",
            instrument=instrument,
            side=side,
            shares=shares,
            price=price,
            notional=notional,
        )

        # Dry-run path
        if self.dry_run or not self.order_router:
            if not self.order_router:
                logger.warning("linear_hedge_no_router", reason="Order router not available for linear hedge")
            # Update stats to reflect intent
            self.daily_stats["contracts"] += 1
            self.daily_stats["notional"] += notional
            return HedgeResult(
                success=True,
                action="add" if delta_gap_usd < 0 else "reduce",
                reason=f"Linear hedge dry-run: {side} {shares} {instrument} @ {price:.2f}",
                qty=shares,
                notional_usd=notional,
            )

        # Real submission via router
        try:
            if hasattr(self.order_router, "place_stock_market"):
                order_result = await self.order_router.place_stock_market(
                    symbol=instrument,
                    side=side,
                    qty=shares,
                    tif=self.tif,
                    strategy_id="sys_hedger",
                )
            else:
                order_result = {"success": False, "error": "Order router lacks place_stock_market"}

            if order_result.get("success"):
                self.daily_stats["contracts"] += 1
                self.daily_stats["notional"] += notional
                self.consecutive_failures = 0
                return HedgeResult(
                    success=True,
                    action="add" if delta_gap_usd < 0 else "reduce",
                    reason=f"Linear hedge placed: {side} {shares} {instrument}",
                    qty=shares,
                    notional_usd=notional,
                )

            # Failure path
            self._record_failure()
            return HedgeResult(
                success=False,
                action="reject",
                reason=f"Linear hedge failed: {order_result.get('error')}",
                error=order_result.get("error"),
            )
        except Exception as exc:  # pragma: no cover - runtime protection
            self._record_failure()
            logger.error("linear_hedge_error", error=str(exc), exc_info=True)
            return HedgeResult(
                success=False,
                action="reject",
                reason=f"Linear hedge error: {str(exc)}",
                error=str(exc),
            )

    def reset_failures(self):
        """Manually reset failure counter (for admin intervention)."""
        old_count = self.consecutive_failures
        self.consecutive_failures = 0
        self.last_failure_ts = 0.0
        logger.info(
            "hedge_failures_reset",
            old_consecutive_failures=old_count,
            msg="Failure counter manually reset",
        )
