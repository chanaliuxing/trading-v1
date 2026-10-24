#!/usr/bin/env python3
"""
Auto-hedging execution script for portfolio risk management.

This script consumes hedge_intent from control plans and executes hedging strategies
using index/ETF options or equity delta hedges.

Features:
- Multi-instrument hedge support (SPY/QQQ/IWM options, stock shorts)
- Dry-run and live execution modes
- Confidence-based sizing
- Correlation-based instrument selection
- Audit logging and metrics

Usage:
    # Dry-run mode (default)
    python scripts/options/auto_hedge.py --symbols TSLA,NVDA --notional-pct 0.5

    # Live execution
    python scripts/options/auto_hedge.py --symbols TSLA,NVDA --notional-pct 0.5 --live

    # Full hedge with urgency
    python scripts/options/auto_hedge.py --symbols AAPL --notional-pct 1.0 --urgency high
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Any, Literal
from zoneinfo import ZoneInfo
import os
from dotenv import load_dotenv

import pandas as pd

from libs.logger import get_logger
from libs.options.occ import build_occ_symbol
from libs import metrics

NY_TZ = ZoneInfo("America/New_York")
LOG = get_logger(__name__)

load_dotenv()


# === Hedge Configuration ===

HEDGE_INSTRUMENTS = {
    "SPY": {"name": "SPDR S&P 500 ETF", "priority": 1, "beta_spy": 1.0, "min_liquidity": 100_000},
    "QQQ": {"name": "Invesco QQQ Trust", "priority": 2, "beta_spy": 1.15, "min_liquidity": 80_000},
    "IWM": {"name": "iShares Russell 2000 ETF", "priority": 3, "beta_spy": 0.95, "min_liquidity": 50_000},
    "DIA": {"name": "SPDR Dow Jones Industrial Average ETF", "priority": 4, "beta_spy": 0.90, "min_liquidity": 30_000},
}

# Correlation-based instrument selection (placeholder - should be loaded from data)
SYMBOL_CORRELATIONS = {
    "TSLA": {"SPY": 0.65, "QQQ": 0.75, "IWM": 0.45},
    "NVDA": {"SPY": 0.70, "QQQ": 0.80, "IWM": 0.50},
    "AAPL": {"SPY": 0.75, "QQQ": 0.85, "IWM": 0.55},
    "AMZN": {"SPY": 0.72, "QQQ": 0.82, "IWM": 0.52},
}


@dataclass
class HedgeIntent:
    """Hedge intent from control plan."""
    symbols: List[str]
    notional_pct: float  # 0.0-1.0, percentage of position notional to hedge
    urgency: Literal["low", "medium", "high"] = "medium"
    instrument: Optional[str] = None  # Preferred hedge instrument (SPY, QQQ, etc.)
    strategy: Literal["options_put", "options_collar", "stock_short"] = "options_put"
    confidence: Optional[float] = None  # Signal confidence (0-1), affects sizing
    max_cost_bps: int = 100  # Maximum hedge cost in basis points of notional


@dataclass
class HedgeOrder:
    """Generated hedge order."""
    instrument: str
    side: Literal["buy", "sell"]
    qty: int
    order_type: Literal["market", "limit", "mid"]
    limit_price: Optional[float] = None
    symbol_type: Literal["option", "stock"] = "option"
    occ_symbol: Optional[str] = None  # For options
    expiry: Optional[str] = None
    strike: Optional[float] = None
    right: Optional[str] = None  # C or P
    estimated_cost: Optional[float] = None
    hedge_ratio: Optional[float] = None  # Delta hedge ratio


@dataclass
class HedgeResult:
    """Execution result."""
    success: bool
    orders: List[HedgeOrder] = field(default_factory=list)
    executed_orders: List[Dict[str, Any]] = field(default_factory=list)
    total_cost: float = 0.0
    hedge_effectiveness: Optional[float] = None
    error: Optional[str] = None
    dry_run: bool = True


# === Core Hedge Execution Engine ===

class AutoHedgeExecutor:
    """Auto-hedging execution engine."""

    def __init__(
        self,
        dry_run: bool = True,
        audit_dir: Optional[Path] = None,
        use_real_data: bool = False,
        paper_mode: bool = False,
    ):
        """
        Args:
            dry_run: If True, only simulate orders without execution
            audit_dir: Directory for audit logs
            use_real_data: If True, use real IBKR data instead of mocks
        """
        self.dry_run = dry_run
        self.use_real_data = use_real_data
        # Backward-compatible execution mode flag retained for older callers/tests.
        self.paper_mode = paper_mode
        self.audit_dir = audit_dir or Path("runs/ai_trader") / date.today().isoformat()
        self.audit_dir.mkdir(parents=True, exist_ok=True)

        # IBKR options provider (lazy init)
        self.options_provider = None
        trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        self.ibkr_port = 4001 if trading_mode == "live" else 4002

        LOG.info(
            "hedge_executor_initialized",
            dry_run=dry_run,
            trading_mode=trading_mode,
            use_real_data=use_real_data,
            ibkr_port=self.ibkr_port,
            audit_dir=str(self.audit_dir),
        )

    async def _ensure_options_provider(self):
        """Lazy initialization of IBKR options provider."""
        if not self.use_real_data:
            return  # Use mock data

        if self.options_provider is None:
            try:
                from services.market.options_data import create_options_provider
                self.options_provider = create_options_provider(
                    'ibkr',
                    host='127.0.0.1',
                    port=self.ibkr_port,
                )
                LOG.info("ibkr_options_provider_initialized", port=self.ibkr_port)
            except Exception as e:
                LOG.error("ibkr_options_provider_init_failed", error=str(e), exc_info=True)
                # Fall back to mock data
                self.use_real_data = False

    async def execute_hedge(self, intent: HedgeIntent) -> HedgeResult:
        """Execute hedge based on intent.

        Args:
            intent: Hedge intent specification

        Returns:
            HedgeResult with execution details
        """
        LOG.info(
            "hedge_execution_started",
            symbols=intent.symbols,
            notional_pct=intent.notional_pct,
            urgency=intent.urgency,
            strategy=intent.strategy,
            dry_run=self.dry_run,
            use_real_data=self.use_real_data,
        )

        try:
            # 0. Initialize options provider if using real data
            await self._ensure_options_provider()

            # 1. Select hedge instrument
            hedge_instrument = self._select_hedge_instrument(intent)
            LOG.info("hedge_instrument_selected", instrument=hedge_instrument)

            # 2. Get current positions for symbols
            positions = await self._get_positions(intent.symbols)
            if not positions:
                LOG.warning("hedge_skipped_no_positions", symbols=intent.symbols)
                return HedgeResult(
                    success=True,
                    dry_run=self.dry_run,
                    error="no_positions_to_hedge",
                )

            # 3. Calculate hedge sizing
            hedge_notional = sum(p["notional"] for p in positions.values()) * intent.notional_pct

            # Adjust for confidence if provided
            if intent.confidence is not None:
                size_multiplier = self._confidence_to_size_multiplier(intent.confidence)
                hedge_notional *= size_multiplier
                LOG.info(
                    "hedge_sized_by_confidence",
                    confidence=intent.confidence,
                    multiplier=size_multiplier,
                    adjusted_notional=hedge_notional,
                )

            # 4. Generate hedge orders
            if intent.strategy == "options_put":
                orders = await self._generate_put_hedge(
                    hedge_instrument, hedge_notional, intent
                )
            elif intent.strategy == "options_collar":
                orders = await self._generate_collar_hedge(
                    hedge_instrument, hedge_notional, intent
                )
            elif intent.strategy == "stock_short":
                orders = await self._generate_stock_hedge(
                    hedge_instrument, hedge_notional, intent
                )
            else:
                raise ValueError(f"Unknown hedge strategy: {intent.strategy}")

            LOG.info("hedge_orders_generated", count=len(orders))

            # 5. Validate cost constraints
            total_cost = sum(o.estimated_cost or 0 for o in orders)
            cost_bps = int(total_cost / hedge_notional * 10000) if hedge_notional > 0 else 0

            if cost_bps > intent.max_cost_bps:
                LOG.warning(
                    "hedge_cost_exceeded",
                    cost_bps=cost_bps,
                    max_cost_bps=intent.max_cost_bps,
                    total_cost=total_cost,
                )
                return HedgeResult(
                    success=False,
                    orders=orders,
                    total_cost=total_cost,
                    error=f"hedge_cost_too_high: {cost_bps} bps > {intent.max_cost_bps} bps",
                    dry_run=self.dry_run,
                )

            # 6. Execute orders (or simulate if dry_run)
            executed_orders = []
            if self.dry_run:
                LOG.info("hedge_dry_run_mode_orders_simulated", orders=[self._order_to_dict(o) for o in orders])
                executed_orders = [{"status": "simulated", "order": self._order_to_dict(o)} for o in orders]
            else:
                executed_orders = await self._submit_orders(orders)

            # 7. Calculate hedge effectiveness
            hedge_effectiveness = self._calculate_effectiveness(
                positions, orders, hedge_instrument
            )

            result = HedgeResult(
                success=True,
                orders=orders,
                executed_orders=executed_orders,
                total_cost=total_cost,
                hedge_effectiveness=hedge_effectiveness,
                dry_run=self.dry_run,
            )

            # 8. Audit logging
            self._write_audit_log(intent, result)

            # 9. Update metrics
            self._update_metrics(intent, result)

            LOG.info(
                "hedge_execution_completed",
                success=True,
                total_cost=total_cost,
                cost_bps=cost_bps,
                effectiveness=hedge_effectiveness,
                orders_count=len(orders),
            )

            return result

        except Exception as exc:
            LOG.error("hedge_execution_failed", error=str(exc), exc_info=True)
            return HedgeResult(
                success=False,
                error=str(exc),
                dry_run=self.dry_run,
            )

    def _select_hedge_instrument(self, intent: HedgeIntent) -> str:
        """Select best hedge instrument based on correlations and preferences."""
        if intent.instrument and intent.instrument in HEDGE_INSTRUMENTS:
            return intent.instrument

        # Select based on correlation with symbols
        best_instrument = "SPY"  # Default
        best_score = 0.0

        for symbol in intent.symbols:
            correlations = SYMBOL_CORRELATIONS.get(symbol, {})
            for instrument, config in HEDGE_INSTRUMENTS.items():
                corr = correlations.get(instrument, 0.5)  # Default correlation
                score = corr / config["priority"]  # Higher correlation, lower priority number = higher score
                if score > best_score:
                    best_score = score
                    best_instrument = instrument

        return best_instrument

    async def _get_positions(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get current positions for symbols.

        In production, this would query the broker/position service.
        For now, returns mock data.
        """
        # TODO: Integrate with actual position service
        # from services.position.position_service import get_positions

        # Mock positions for dry-run
        positions = {}
        for symbol in symbols:
            positions[symbol] = {
                "symbol": symbol,
                "qty": 100,
                "avg_price": 250.0,
                "notional": 25000.0,
                "side": "long",
            }

        return positions

    def _confidence_to_size_multiplier(self, confidence: float) -> float:
        """Convert signal confidence to hedge size multiplier.

        Logic:
        - conf < 0.6: Full hedge (multiplier = 1.0)
        - 0.6 <= conf < 0.8: Partial hedge (multiplier = 0.5)
        - conf >= 0.8: Light hedge (multiplier = 0.25)
        """
        if confidence < 0.6:
            return 1.0
        elif confidence < 0.8:
            return 0.5
        else:
            return 0.25

    async def _generate_put_hedge(
        self, instrument: str, notional: float, intent: HedgeIntent
    ) -> List[HedgeOrder]:
        """Generate protective put hedge orders."""
        # 1. Get current price of hedge instrument
        current_price = await self._get_current_price(instrument)

        # 2. Try to fetch real option chain data if enabled
        if self.use_real_data and self.options_provider:
            try:
                contracts = await self.options_provider.get_chain(
                    underlying=instrument,
                    dte_candidates=[30, 28, 32],  # ~30 days with ±2 day tolerance
                    right='P',
                )

                if contracts:
                    # Find contract with delta closest to 0.30 (good balance of protection/cost)
                    target_delta = 0.30
                    best_contract = None
                    min_delta_diff = float('inf')

                    for contract in contracts:
                        if contract.delta is None:
                            continue
                        # Put delta is negative
                        abs_delta = abs(contract.delta)
                        delta_diff = abs(abs_delta - target_delta)

                        if delta_diff < min_delta_diff:
                            min_delta_diff = delta_diff
                            best_contract = contract

                    if best_contract:
                        # Use real option data
                        actual_delta = abs(best_contract.delta)
                        contracts_needed = int(notional / (current_price * 100 * actual_delta))
                        contracts_needed = max(1, contracts_needed)

                        estimated_cost = best_contract.mid * contracts_needed * 100

                        LOG.info(
                            "real_option_selected",
                            instrument=instrument,
                            occ_symbol=best_contract.symbol,
                            strike=best_contract.strike,
                            delta=best_contract.delta,
                            mid_price=best_contract.mid,
                            contracts=contracts_needed,
                            cost=estimated_cost,
                        )

                        order = HedgeOrder(
                            instrument=instrument,
                            side="buy",
                            qty=contracts_needed,
                            order_type="mid" if not self.dry_run else "market",
                            symbol_type="option",
                            occ_symbol=best_contract.symbol,
                            expiry=best_contract.expiry_date,
                            strike=best_contract.strike,
                            right="P",
                            estimated_cost=estimated_cost,
                            hedge_ratio=actual_delta,
                        )

                        return [order]
            except Exception as e:
                LOG.error("real_option_fetch_failed", instrument=instrument, error=str(e))
                # Fall through to mock implementation

        # Fall back to estimated/mock option data
        expiry = self._select_expiry(days_out=30)
        strike = self._round_strike(current_price * 0.95)
        estimated_delta = 0.30
        contracts_needed = int(notional / (current_price * 100 * estimated_delta))
        contracts_needed = max(1, contracts_needed)

        occ_symbol = build_occ_symbol(
            underlying=instrument,
            expiry_date=expiry.strftime("%Y%m%d"),
            right="P",
            strike=strike,
        )

        estimated_premium = current_price * 0.02
        estimated_cost = estimated_premium * contracts_needed * 100

        order = HedgeOrder(
            instrument=instrument,
            side="buy",
            qty=contracts_needed,
            order_type="mid" if not self.dry_run else "market",
            symbol_type="option",
            occ_symbol=occ_symbol,
            expiry=expiry.strftime("%Y-%m-%d"),
            strike=strike,
            right="P",
            estimated_cost=estimated_cost,
            hedge_ratio=estimated_delta,
        )

        return [order]

    async def _generate_collar_hedge(
        self, instrument: str, notional: float, intent: HedgeIntent
    ) -> List[HedgeOrder]:
        """Generate collar hedge (long put + short call)."""
        current_price = await self._get_current_price(instrument)
        expiry = self._select_expiry(days_out=30)

        # Put leg (protective put at 95% of current price)
        put_strike = self._round_strike(current_price * 0.95)
        put_occ = build_occ_symbol(instrument, expiry.strftime("%Y%m%d"), "P", put_strike)

        # Call leg (covered call at 105% of current price)
        call_strike = self._round_strike(current_price * 1.05)
        call_occ = build_occ_symbol(instrument, expiry.strftime("%Y%m%d"), "C", call_strike)

        contracts = max(1, int(notional / (current_price * 100 * 0.30)))

        # Cost: put premium - call premium (net debit, but lower than naked put)
        put_premium = current_price * 0.02
        call_premium = current_price * 0.015
        net_cost = (put_premium - call_premium) * contracts * 100

        return [
            HedgeOrder(
                instrument=instrument,
                side="buy",
                qty=contracts,
                order_type="mid",
                symbol_type="option",
                occ_symbol=put_occ,
                expiry=expiry.strftime("%Y-%m-%d"),
                strike=put_strike,
                right="P",
                estimated_cost=put_premium * contracts * 100,
                hedge_ratio=0.30,
            ),
            HedgeOrder(
                instrument=instrument,
                side="sell",
                qty=contracts,
                order_type="mid",
                symbol_type="option",
                occ_symbol=call_occ,
                expiry=expiry.strftime("%Y-%m-%d"),
                strike=call_strike,
                right="C",
                estimated_cost=-call_premium * contracts * 100,
                hedge_ratio=-0.30,
            ),
        ]

    async def _generate_stock_hedge(
        self, instrument: str, notional: float, intent: HedgeIntent
    ) -> List[HedgeOrder]:
        """Generate stock short hedge."""
        current_price = await self._get_current_price(instrument)
        qty = int(notional / current_price)
        qty = max(1, qty)

        return [
            HedgeOrder(
                instrument=instrument,
                side="sell",
                qty=qty,
                order_type="market",
                symbol_type="stock",
                limit_price=current_price,
                estimated_cost=0.0,  # No premium for stock short (but borrow costs exist)
                hedge_ratio=1.0,
            )
        ]

    async def _get_current_price(self, symbol: str) -> float:
        """Get current market price for symbol."""
        # Use real IBKR data if enabled
        if self.use_real_data:
            if not self.options_provider:
                raise RuntimeError(f"use_real_data=True but options_provider not initialized")

            try:
                price = await self.options_provider.get_underlying_price(symbol)
                if price and price > 0:
                    LOG.info("real_price_fetched", symbol=symbol, price=price)
                    return price
                else:
                    raise ValueError(f"Invalid price received from IBKR for {symbol}: {price}")
            except Exception as e:
                LOG.error("real_price_fetch_failed", symbol=symbol, error=str(e))
                raise  # Re-raise when use_real_data is True

        # Fall back to mock prices only when use_real_data=False
        mock_prices = {"SPY": 580.0, "QQQ": 510.0, "IWM": 220.0, "DIA": 440.0}
        return mock_prices.get(symbol, 500.0)

    def _select_expiry(self, days_out: int = 30) -> datetime:
        """Select option expiry date (prefer monthly expirations for liquidity)."""
        today = datetime.now(NY_TZ).date()
        target_date = today + timedelta(days=days_out)

        # Find next Friday (monthly options typically expire on 3rd Friday)
        while target_date.weekday() != 4:  # 4 = Friday
            target_date += timedelta(days=1)

        return datetime.combine(target_date, datetime.min.time()).replace(tzinfo=NY_TZ)

    def _round_strike(self, price: float) -> float:
        """Round strike to nearest standard increment."""
        if price < 50:
            return round(price * 2) / 2  # 0.5 increments
        elif price < 200:
            return round(price)  # 1.0 increments
        else:
            return round(price / 5) * 5  # 5.0 increments

    async def _submit_orders(self, orders: List[HedgeOrder]) -> List[Dict[str, Any]]:
        """Submit orders to broker.

        In production, this would call the execution service.
        """
        # TODO: Integrate with execution service
        # from services.exec.execution_service import submit_order

        LOG.warning("hedge_orders_not_submitted_integration_pending", orders_count=len(orders))

        return [{"status": "pending_integration", "order": self._order_to_dict(o)} for o in orders]

    def _calculate_effectiveness(
        self,
        positions: Dict[str, Dict[str, Any]],
        orders: List[HedgeOrder],
        hedge_instrument: str,
    ) -> float:
        """Calculate expected hedge effectiveness (correlation-based)."""
        # Simple effectiveness estimate based on hedge ratio and correlations
        total_hedge_delta = sum(o.hedge_ratio or 0 for o in orders if o.hedge_ratio)

        # Average correlation between positions and hedge instrument
        avg_corr = 0.7  # Placeholder
        for symbol in positions.keys():
            corrs = SYMBOL_CORRELATIONS.get(symbol, {})
            avg_corr = corrs.get(hedge_instrument, 0.7)
            break

        effectiveness = min(1.0, abs(total_hedge_delta) * avg_corr)
        return round(effectiveness, 3)

    def _order_to_dict(self, order: HedgeOrder) -> Dict[str, Any]:
        """Convert HedgeOrder to dict for serialization."""
        return {
            "instrument": order.instrument,
            "side": order.side,
            "qty": order.qty,
            "order_type": order.order_type,
            "symbol_type": order.symbol_type,
            "occ_symbol": order.occ_symbol,
            "expiry": order.expiry,
            "strike": order.strike,
            "right": order.right,
            "limit_price": order.limit_price,
            "estimated_cost": order.estimated_cost,
            "hedge_ratio": order.hedge_ratio,
        }

    def _write_audit_log(self, intent: HedgeIntent, result: HedgeResult) -> None:
        """Write audit log entry."""
        audit_file = self.audit_dir / "hedge_audit.jsonl"

        record = {
            "ts": datetime.now(NY_TZ).isoformat(),
            "intent": {
                "symbols": intent.symbols,
                "notional_pct": intent.notional_pct,
                "urgency": intent.urgency,
                "strategy": intent.strategy,
                "confidence": intent.confidence,
                "max_cost_bps": intent.max_cost_bps,
            },
            "result": {
                "success": result.success,
                "orders_count": len(result.orders),
                "executed_count": len(result.executed_orders),
                "total_cost": result.total_cost,
                "hedge_effectiveness": result.hedge_effectiveness,
                "error": result.error,
                "dry_run": result.dry_run,
            },
            "orders": [self._order_to_dict(o) for o in result.orders],
        }

        with audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        LOG.info("hedge_audit_written", file=str(audit_file))

    def _update_metrics(self, intent: HedgeIntent, result: HedgeResult) -> None:
        """Update Prometheus metrics."""
        try:
            # Hedge execution counter
            status = "success" if result.success else "failure"
            metrics.hedge_executions_total.labels(
                strategy=intent.strategy,
                status=status,
                dry_run=str(result.dry_run),
            ).inc()

            # Hedge cost
            if result.success and result.total_cost > 0:
                metrics.hedge_cost_usd.labels(strategy=intent.strategy).observe(result.total_cost)

            # Hedge effectiveness
            if result.hedge_effectiveness is not None:
                metrics.hedge_effectiveness.labels(strategy=intent.strategy).set(result.hedge_effectiveness)

        except Exception as exc:
            LOG.warning("hedge_metrics_update_failed", error=str(exc))


# === CLI Entry Point ===

async def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Auto-hedging execution script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--symbols",
        type=str,
        required=True,
        help="Comma-separated list of symbols to hedge (e.g., TSLA,NVDA)",
    )
    parser.add_argument(
        "--notional-pct",
        type=float,
        required=True,
        help="Percentage of position notional to hedge (0.0-1.0)",
    )
    parser.add_argument(
        "--urgency",
        type=str,
        choices=["low", "medium", "high"],
        default="medium",
        help="Hedge urgency level",
    )
    parser.add_argument(
        "--instrument",
        type=str,
        choices=list(HEDGE_INSTRUMENTS.keys()),
        help="Preferred hedge instrument (default: auto-select)",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        choices=["options_put", "options_collar", "stock_short"],
        default="options_put",
        help="Hedge strategy",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        help="Signal confidence (0.0-1.0), affects hedge sizing",
    )
    parser.add_argument(
        "--max-cost-bps",
        type=int,
        default=100,
        help="Maximum hedge cost in basis points",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute orders (default: dry-run mode)",
    )
    parser.add_argument(
        "--use-real-data",
        action="store_true",
        help="Use real IBKR market data instead of mocks (requires IBKR connection)",
    )
    parser.add_argument(
        "--audit-dir",
        type=Path,
        help="Custom audit log directory",
    )

    args = parser.parse_args()

    # Parse symbols
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    # Validate notional_pct
    if not 0.0 <= args.notional_pct <= 1.0:
        print(f"Error: --notional-pct must be between 0.0 and 1.0, got {args.notional_pct}")
        sys.exit(1)

    # Validate confidence
    if args.confidence is not None and not 0.0 <= args.confidence <= 1.0:
        print(f"Error: --confidence must be between 0.0 and 1.0, got {args.confidence}")
        sys.exit(1)
    
    # Determine dry_run status
    trading_mode_env = os.getenv("TRADING_MODE", "shadow").lower()
    is_live_mode = args.live or trading_mode_env == "live"
    is_paper_mode = (not args.live and trading_mode_env == "paper")
    
    current_dry_run_status = not (is_live_mode or is_paper_mode)

    # Build intent
    intent = HedgeIntent(
        symbols=symbols,
        notional_pct=args.notional_pct,
        urgency=args.urgency,
        instrument=args.instrument,
        strategy=args.strategy,
        confidence=args.confidence,
        max_cost_bps=args.max_cost_bps,
    )

    # Initialize executor
    executor = AutoHedgeExecutor(
        dry_run=current_dry_run_status,
        audit_dir=args.audit_dir,
        use_real_data=args.use_real_data,
    )

    # Execute hedge
    result = await executor.execute_hedge(intent)

    # Print result
    print("\n" + "=" * 80)
    print("HEDGE EXECUTION RESULT")
    print("=" * 80)
    print(f"Success: {result.success}")
    print(f"Dry Run: {result.dry_run}")
    print(f"Orders Generated: {len(result.orders)}")
    print(f"Orders Executed: {len(result.executed_orders)}")
    print(f"Total Cost: ${result.total_cost:.2f}")
    print(f"Hedge Effectiveness: {result.hedge_effectiveness or 'N/A'}")

    if result.error:
        print(f"Error: {result.error}")

    if result.orders:
        print("\nOrders:")
        for i, order in enumerate(result.orders, 1):
            print(f"  {i}. {order.side.upper()} {order.qty} {order.instrument} {order.symbol_type}")
            if order.occ_symbol:
                print(f"     OCC: {order.occ_symbol}")
                print(f"     Expiry: {order.expiry}, Strike: ${order.strike}, Right: {order.right}")
            print(f"     Est. Cost: ${order.estimated_cost:.2f}")
            print(f"     Hedge Ratio: {order.hedge_ratio}")

    print("=" * 80 + "\n")

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    asyncio.run(main())
