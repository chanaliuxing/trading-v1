"""Options position tracker for monitoring open hedges.

This module tracks open option positions, their Greeks, P&L, and expiry status.
Integrates with IBKR for position reconciliation and provides alerts for expiring contracts.
"""

from __future__ import annotations
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict

from libs.options.occ import parse_occ_symbol, get_contract_description

# Import metrics
try:
    from libs.metrics import (
        hedging_positions_open,
        hedging_positions_expiring_soon,
        hedging_portfolio_greeks,
        hedging_portfolio_pnl,
        hedging_daily_contracts,
        hedging_daily_notional,
        is_enabled as metrics_enabled,
    )
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


@dataclass
class OptionPosition:
    """Represents an open option position."""

    occ_symbol: str
    underlying: str
    expiry_date: str  # YYYY-MM-DD
    strike: float
    right: str  # C or P
    quantity: int  # Positive = long, negative = short
    avg_entry_price: float
    current_price: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None
    pnl_unrealized: Optional[float] = None
    entry_date: Optional[str] = None
    last_updated: Optional[str] = None

    @property
    def dte(self) -> int:
        """Days to expiration."""
        exp = datetime.strptime(self.expiry_date, '%Y-%m-%d')
        return (exp - datetime.now()).days

    @property
    def notional(self) -> float:
        """Notional value (100 shares per contract)."""
        if self.current_price:
            return abs(self.quantity) * 100 * self.current_price
        return abs(self.quantity) * 100 * self.avg_entry_price

    @property
    def is_expiring_soon(self) -> bool:
        """Check if expiring within 3 days."""
        return self.dte <= 3

    @property
    def description(self) -> str:
        """Human-readable description."""
        return get_contract_description(self.occ_symbol)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for serialization."""
        return {
            **asdict(self),
            'dte': self.dte,
            'notional': self.notional,
            'is_expiring_soon': self.is_expiring_soon,
            'description': self.description,
        }


class OptionsPositionTracker:
    """Tracks open option positions and provides monitoring."""

    def __init__(self, runs_dir: str):
        """Initialize tracker.

        Args:
            runs_dir: Base runs directory (e.g., runs/ai_trader)
        """
        self.runs_dir = Path(runs_dir)
        self.positions_file = self.runs_dir / "options_positions.json"

    def load_positions(self) -> Dict[str, OptionPosition]:
        """Load current positions from file.

        Returns:
            Dict mapping OCC symbol to OptionPosition
        """
        if not self.positions_file.exists():
            return {}

        try:
            with self.positions_file.open('r', encoding='utf-8') as f:
                data = json.load(f)

            positions = {}
            for symbol, pos_data in data.items():
                positions[symbol] = OptionPosition(**pos_data)

            return positions
        except Exception:
            return {}

    def save_positions(self, positions: Dict[str, OptionPosition]):
        """Save positions to file.

        Args:
            positions: Dict mapping OCC symbol to OptionPosition
        """
        self.positions_file.parent.mkdir(parents=True, exist_ok=True)

        data = {}
        for symbol, pos in positions.items():
            # Only save essential fields
            data[symbol] = {
                'occ_symbol': pos.occ_symbol,
                'underlying': pos.underlying,
                'expiry_date': pos.expiry_date,
                'strike': pos.strike,
                'right': pos.right,
                'quantity': pos.quantity,
                'avg_entry_price': pos.avg_entry_price,
                'current_price': pos.current_price,
                'delta': pos.delta,
                'gamma': pos.gamma,
                'vega': pos.vega,
                'theta': pos.theta,
                'pnl_unrealized': pos.pnl_unrealized,
                'entry_date': pos.entry_date,
                'last_updated': pos.last_updated,
            }

        with self.positions_file.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    def add_position(
        self,
        occ_symbol: str,
        quantity: int,
        entry_price: float,
        delta: Optional[float] = None,
    ):
        """Add or update a position.

        Args:
            occ_symbol: OCC symbol
            quantity: Number of contracts (positive = long)
            entry_price: Entry price per contract
            delta: Initial delta
        """
        positions = self.load_positions()

        # Parse OCC symbol
        try:
            parsed = parse_occ_symbol(occ_symbol)
        except ValueError:
            return  # Invalid symbol

        if occ_symbol in positions:
            # Update existing position (average price)
            existing = positions[occ_symbol]
            total_qty = existing.quantity + quantity
            if total_qty == 0:
                # Closed out
                del positions[occ_symbol]
            else:
                # Average entry price
                total_cost = (existing.quantity * existing.avg_entry_price +
                              quantity * entry_price)
                avg_price = total_cost / total_qty
                existing.quantity = total_qty
                existing.avg_entry_price = avg_price
                existing.last_updated = datetime.now().isoformat()
        else:
            # New position
            positions[occ_symbol] = OptionPosition(
                occ_symbol=occ_symbol,
                underlying=parsed['underlying'],
                expiry_date=parsed['expiry_date'],
                strike=parsed['strike'],
                right=parsed['right'],
                quantity=quantity,
                avg_entry_price=entry_price,
                delta=delta,
                entry_date=date.today().isoformat(),
                last_updated=datetime.now().isoformat(),
            )

        self.save_positions(positions)

    async def update_market_data(self, provider=None):
        """Update current prices and Greeks from data provider.

        Args:
            provider: OptionsDataProvider instance (created if None)
        """
        positions = self.load_positions()
        if not positions:
            return

        if provider is None:
            from services.market.options_data import create_options_provider
            from libs.config import config as CFG

            backend = CFG.get('risk_management.hedge.options.data_provider', 'stub')
            if backend == 'ibkr':
                host = CFG.get('risk_management.hedge.options.ibkr_host', '127.0.0.1')
                port = int(CFG.get('risk_management.hedge.options.ibkr_port', 4002))
                provider = create_options_provider('ibkr', host=host, port=port)
            else:
                provider = create_options_provider(backend)

        # Group by underlying for efficient fetching
        by_underlying: Dict[str, List[OptionPosition]] = {}
        for pos in positions.values():
            by_underlying.setdefault(pos.underlying, []).append(pos)

        # Fetch chains and update
        for underlying, pos_list in by_underlying.items():
            try:
                # Get all DTEs needed
                dtes = [pos.dte for pos in pos_list]

                chain = await provider.get_chain(underlying, dtes)

                # Match and update
                for pos in pos_list:
                    for contract in chain:
                        if (contract.strike == pos.strike and
                            contract.right == pos.right and
                            contract.expiry_date == pos.expiry_date):
                            # Update market data
                            pos.current_price = contract.mid
                            pos.delta = contract.delta
                            pos.gamma = contract.gamma
                            pos.vega = contract.vega
                            pos.theta = contract.theta

                            # Calculate P&L
                            if pos.current_price and pos.avg_entry_price:
                                pos.pnl_unrealized = (
                                    pos.quantity * 100 *
                                    (pos.current_price - pos.avg_entry_price)
                                )

                            pos.last_updated = datetime.now().isoformat()
                            break

            except Exception:
                # Skip this underlying if fetch fails
                continue

        # Save updated positions
        self.save_positions(positions)

        # Cleanup provider
        if hasattr(provider, 'disconnect'):
            await provider.disconnect()

    def get_expiring_soon(self, days: int = 3) -> List[OptionPosition]:
        """Get positions expiring within N days.

        Args:
            days: Number of days threshold

        Returns:
            List of expiring positions
        """
        positions = self.load_positions()
        expiring = []

        for pos in positions.values():
            if pos.dte <= days:
                expiring.append(pos)

        return sorted(expiring, key=lambda p: p.dte)

    def get_summary(self) -> Dict[str, Any]:
        """Get portfolio-level summary.

        Returns:
            Dict with total positions, notional, Greeks, P&L
        """
        positions = self.load_positions()

        if not positions:
            return {
                'total_positions': 0,
                'total_notional': 0.0,
                'total_delta': 0.0,
                'total_gamma': 0.0,
                'total_vega': 0.0,
                'total_theta': 0.0,
                'total_pnl': 0.0,
                'expiring_soon': 0,
            }

        total_notional = 0.0
        total_delta = 0.0
        total_gamma = 0.0
        total_vega = 0.0
        total_theta = 0.0
        total_pnl = 0.0
        expiring_count = 0

        for pos in positions.values():
            # Notional
            total_notional += pos.notional

            # Greeks (scaled by quantity)
            if pos.delta:
                total_delta += pos.quantity * 100 * pos.delta
            if pos.gamma:
                total_gamma += pos.quantity * 100 * pos.gamma
            if pos.vega:
                total_vega += pos.quantity * 100 * pos.vega
            if pos.theta:
                total_theta += pos.quantity * 100 * pos.theta

            # P&L
            if pos.pnl_unrealized:
                total_pnl += pos.pnl_unrealized

            # Expiring
            if pos.is_expiring_soon:
                expiring_count += 1

        return {
            'total_positions': len(positions),
            'total_notional': total_notional,
            'total_delta': total_delta,
            'total_gamma': total_gamma,
            'total_vega': total_vega,
            'total_theta': total_theta,
            'total_pnl': total_pnl,
            'expiring_soon': expiring_count,
        }

    def check_duplicate_hedge(
        self,
        underlying: str,
        right: str,
        dte_range: tuple[int, int] = (5, 20),
    ) -> bool:
        """Check if there's already a similar hedge position.

        Args:
            underlying: Underlying symbol
            right: Option type (C or P)
            dte_range: DTE range to consider duplicate (min, max)

        Returns:
            True if duplicate exists
        """
        positions = self.load_positions()

        for pos in positions.values():
            if (pos.underlying == underlying and
                pos.right == right and
                dte_range[0] <= pos.dte <= dte_range[1]):
                return True

        return False

    async def reconcile_with_ibkr(self, ibkr_provider) -> Dict[str, Any]:
        """Reconcile tracked positions with actual IBKR positions.

        Args:
            ibkr_provider: Connected IBKR provider instance

        Returns:
            Dict with reconciliation report
        """
        # This would fetch actual positions from IBKR and compare
        # For now, placeholder
        return {
            'status': 'not_implemented',
            'message': 'IBKR reconciliation requires position API integration'
        }

    def update_metrics(self):
        """Update Prometheus metrics from current positions."""
        if not METRICS_AVAILABLE or not metrics_enabled():
            return

        positions = self.load_positions()
        summary = self.get_summary()

        # Update position counts by underlying
        underlying_counts: Dict[str, int] = {}
        for pos in positions.values():
            underlying_counts[pos.underlying] = underlying_counts.get(pos.underlying, 0) + 1

        for underlying, count in underlying_counts.items():
            hedging_positions_open.labels(underlying=underlying).set(count)

        # Update expiring soon count
        hedging_positions_expiring_soon.set(summary['expiring_soon'])

        # Update portfolio Greeks
        hedging_portfolio_greeks.labels(greek='delta').set(summary['total_delta'])
        hedging_portfolio_greeks.labels(greek='gamma').set(summary['total_gamma'])
        hedging_portfolio_greeks.labels(greek='vega').set(summary['total_vega'])
        hedging_portfolio_greeks.labels(greek='theta').set(summary['total_theta'])

        # Update P&L
        hedging_portfolio_pnl.set(summary['total_pnl'])

    def update_daily_stats(self, contracts_today: int, notional_today: float):
        """Update daily execution metrics.

        Args:
            contracts_today: Number of contracts executed today
            notional_today: Total notional USD executed today
        """
        if not METRICS_AVAILABLE or not metrics_enabled():
            return

        hedging_daily_contracts.set(contracts_today)
        hedging_daily_notional.set(notional_today)


def get_position_tracker(runs_dir: str = 'runs/ai_trader') -> OptionsPositionTracker:
    """Factory function to get position tracker instance.

    Args:
        runs_dir: Base runs directory

    Returns:
        OptionsPositionTracker instance
    """
    return OptionsPositionTracker(runs_dir)


if __name__ == "__main__":
    """Test harness."""
    import asyncio

    async def test():
        tracker = get_position_tracker('runs/ai_trader')

        # Add test position
        tracker.add_position(
            occ_symbol="SPY   251116P00450000",
            quantity=5,
            entry_price=5.20,
            delta=-0.40,
        )

        # Get summary
        summary = tracker.get_summary()
        print(json.dumps(summary, indent=2))

        # Check expiring
        expiring = tracker.get_expiring_soon(days=30)
        print(f"\nExpiring soon: {len(expiring)}")
        for pos in expiring:
            print(f"  {pos.description} - DTE={pos.dte}")

        # Update market data (stub)
        await tracker.update_market_data()

        # Get updated summary
        summary = tracker.get_summary()
        print("\nAfter market data update:")
        print(json.dumps(summary, indent=2))

    asyncio.run(test())
