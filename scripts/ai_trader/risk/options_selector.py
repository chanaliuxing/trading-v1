"""Options contract selector for delta-hedging with risk controls.

This module implements the core algorithm for selecting appropriate option contracts
for portfolio hedging based on exposure, policy constraints, and market conditions.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from libs.config import config as CFG
from libs.options.occ import build_occ_symbol
from services.market.options_data import (
    OptionsDataProvider,
    OptionContract,
    create_options_provider,
)


@dataclass
class OptionsPolicy:
    """Options hedging policy parameters loaded from config."""

    enabled: bool = True
    instruments: List[str] = None
    primary: str = "delta"  # delta or vega
    delta_target_abs: float = 0.4
    dte_preference: List[int] = None
    min_open_interest: int = 1000
    max_spread_bps: float = 30.0
    min_last_trade_minutes: int = 5
    slippage_bps: float = 10.0
    tif: str = "DAY"
    max_contracts_per_day: int = 50
    max_notional_usd_per_day: float = 100000.0
    dry_run: bool = True

    def __post_init__(self):
        if self.instruments is None:
            self.instruments = ["SPY", "QQQ"]
        if self.dte_preference is None:
            self.dte_preference = [7, 14, 30]

    @classmethod
    def from_config(cls) -> OptionsPolicy:
        """Load policy from config.yml."""
        opts = CFG.get('risk_management.hedge.options', {})
        return cls(
            enabled=opts.get('enabled', True),
            instruments=opts.get('instruments', ['SPY', 'QQQ']),
            primary=opts.get('primary', 'delta'),
            delta_target_abs=opts.get('delta_target_abs', 0.4),
            dte_preference=opts.get('dte_preference', [7, 14, 30]),
            min_open_interest=opts.get('min_open_interest', 1000),
            max_spread_bps=opts.get('max_spread_bps', 30.0),
            min_last_trade_minutes=opts.get('min_last_trade_minutes', 5),
            slippage_bps=opts.get('slippage_bps', 10.0),
            tif=opts.get('tif', 'DAY'),
            max_contracts_per_day=opts.get('max_contracts_per_day', 50),
            max_notional_usd_per_day=opts.get('max_notional_usd_per_day', 100000.0),
            dry_run=opts.get('dry_run', True),
        )


@dataclass
class HedgeIntent:
    """Intent from AI brain or risk system for hedging."""

    intent_type: str  # vol_spike, beta_balance, drawdown_protect
    instrument: Optional[str] = None  # SPY, QQQ, or None for auto-select
    size: str = "medium"  # small, medium, large
    urgency: str = "normal"  # normal, high
    note: str = ""


@dataclass
class SelectedContract:
    """Selected option contract with execution details."""

    occ_symbol: str
    underlying: str
    expiry_date: str
    strike: float
    right: str  # C or P
    delta: float
    open_interest: int
    spread_bps: float
    mid_price: float
    limit_price: float
    quantity: int
    notional_usd: float
    tif: str
    rationale: str  # Why this contract was selected
    contract_details: Optional[OptionContract] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        # Remove contract_details from serialization
        d.pop('contract_details', None)
        return d


@dataclass
class SelectionResult:
    """Result of contract selection process."""

    success: bool
    contract: Optional[SelectedContract] = None
    error: Optional[str] = None
    candidates_evaluated: int = 0
    rejection_reasons: Dict[str, int] = None

    def __post_init__(self):
        if self.rejection_reasons is None:
            self.rejection_reasons = {}


class OptionsSelector:
    """Core algorithm for selecting option contracts for hedging."""

    def __init__(
        self,
        policy: Optional[OptionsPolicy] = None,
        data_provider: Optional[OptionsDataProvider] = None,
    ):
        """Initialize selector.

        Args:
            policy: Options policy (loaded from config if None)
            data_provider: Data provider (created from config if None)
        """
        self.policy = policy or OptionsPolicy.from_config()

        if data_provider is None:
            # Create provider from config
            backend = CFG.get('risk_management.hedge.options.data_provider', 'stub')
            if backend == 'ibkr':
                host = CFG.get('risk_management.hedge.options.ibkr_host', '127.0.0.1')
                port = int(CFG.get('risk_management.hedge.options.ibkr_port', 4002))
                self.data_provider = create_options_provider('ibkr', host=host, port=port)
            else:
                self.data_provider = create_options_provider(backend)
        else:
            self.data_provider = data_provider

        self._daily_stats = {'contracts': 0, 'notional': 0.0}

    async def select_hedge_contract(
        self,
        exposure: Dict[str, float],
        intent: Optional[HedgeIntent] = None,
        target_notional_usd: Optional[float] = None,
    ) -> SelectionResult:
        """Select optimal option contract for hedging.

        Args:
            exposure: Portfolio exposure dict with 'gross' and 'net' keys
            intent: Optional hedging intent from AI brain
            target_notional_usd: Optional override for notional size

        Returns:
            SelectionResult with selected contract or error
        """
        if not self.policy.enabled:
            return SelectionResult(success=False, error="Options hedging disabled")

        # Step 1: Determine hedge direction and instrument
        gross = float(exposure.get('gross', 0.0))
        net = float(exposure.get('net', 0.0))

        if gross <= 0:
            return SelectionResult(success=False, error="No portfolio exposure")

        # Determine direction: net > 0 (long bias) → buy puts, net < 0 (short bias) → buy calls
        direction = 'P' if net > 0 else 'C'

        # Select instrument
        instrument = self._select_instrument(intent)

        # Step 2: Calculate target notional
        if target_notional_usd is None:
            target_notional_usd = self._calculate_target_notional(gross, intent)

        # Step 3: Check daily limits (hard safety constraint)
        if not self._check_daily_limits(target_notional_usd):
            return SelectionResult(
                success=False,
                error=f"Daily limits exceeded (contracts: {self._daily_stats['contracts']}/{self.policy.max_contracts_per_day}, "
                f"notional: ${self._daily_stats['notional']:.0f}/${self.policy.max_notional_usd_per_day:.0f})",
            )

        # Step 4: Fetch option chain
        try:
            chain = await self.data_provider.get_chain(
                underlying=instrument,
                dte_candidates=self.policy.dte_preference,
                right=direction,
            )
        except Exception as e:
            return SelectionResult(success=False, error=f"Failed to fetch option chain: {e}")

        if not chain:
            return SelectionResult(success=False, error=f"No option contracts found for {instrument}")

        # Step 5: Get underlying price for calculations
        underlying_px = await self.data_provider.get_underlying_price(instrument)
        if not underlying_px or underlying_px <= 0:
            return SelectionResult(success=False, error=f"No underlying price for {instrument}")

        # Step 6: Filter and rank candidates
        result = self._select_best_contract(
            chain=chain,
            underlying_px=underlying_px,
            direction=direction,
            target_notional_usd=target_notional_usd,
            intent=intent,
        )

        return result

    def _select_instrument(self, intent: Optional[HedgeIntent]) -> str:
        """Select hedging instrument based on intent and policy."""
        if intent and intent.instrument and intent.instrument in self.policy.instruments:
            return intent.instrument

        # Default: first instrument in policy (usually SPY)
        return self.policy.instruments[0]

    def _calculate_target_notional(self, gross_exposure: float, intent: Optional[HedgeIntent]) -> float:
        """Calculate target notional based on exposure and intent size."""
        # Size mapping: small=20%, medium=30%, large=40%
        size_mult = {
            'small': 0.20,
            'medium': 0.30,
            'large': 0.40,
        }

        mult = size_mult.get(intent.size if intent else 'medium', 0.30)
        return gross_exposure * mult

    def _check_daily_limits(self, additional_notional: float) -> bool:
        """Check if adding this trade would exceed daily limits."""
        # Hard constraint: never exceed max_contracts_per_day or max_notional_usd_per_day
        # even if config is misconfigured

        ABSOLUTE_MAX_CONTRACTS = 100  # Safety backstop
        ABSOLUTE_MAX_NOTIONAL = 500_000.0  # Safety backstop

        if self._daily_stats['contracts'] >= min(self.policy.max_contracts_per_day, ABSOLUTE_MAX_CONTRACTS):
            return False

        projected_notional = self._daily_stats['notional'] + additional_notional
        if projected_notional > min(self.policy.max_notional_usd_per_day, ABSOLUTE_MAX_NOTIONAL):
            return False

        return True

    def _select_best_contract(
        self,
        chain: List[OptionContract],
        underlying_px: float,
        direction: str,
        target_notional_usd: float,
        intent: Optional[HedgeIntent],
    ) -> SelectionResult:
        """Select best contract from chain based on policy constraints.

        Selection criteria (in order):
        1. Risk constraints: OI >= min, spread <= max, recent trades
        2. Delta target: |delta| closest to target_abs
        3. DTE preference: earlier DTEs preferred (lower theta decay)
        4. Liquidity: higher OI preferred as tiebreaker
        """
        candidates = []
        rejection_reasons = {}
        now = datetime.now()

        for contract in chain:
            # Filter 1: Open interest
            if contract.open_interest is None or contract.open_interest < self.policy.min_open_interest:
                rejection_reasons['low_oi'] = rejection_reasons.get('low_oi', 0) + 1
                continue

            # Filter 2: Spread
            if contract.spread_bps > self.policy.max_spread_bps:
                rejection_reasons['wide_spread'] = rejection_reasons.get('wide_spread', 0) + 1
                continue

            # Filter 3: Recent trades
            if contract.last_trade_time:
                minutes_since_trade = (now - contract.last_trade_time).total_seconds() / 60
                if minutes_since_trade > self.policy.min_last_trade_minutes:
                    rejection_reasons['stale_trade'] = rejection_reasons.get('stale_trade', 0) + 1
                    continue

            # Filter 4: Valid delta
            if contract.delta is None:
                rejection_reasons['no_delta'] = rejection_reasons.get('no_delta', 0) + 1
                continue

            # Filter 5: Valid pricing
            if contract.mid <= 0:
                rejection_reasons['no_price'] = rejection_reasons.get('no_price', 0) + 1
                continue

            candidates.append(contract)

        if not candidates:
            return SelectionResult(
                success=False,
                error="No contracts passed risk filters",
                candidates_evaluated=len(chain),
                rejection_reasons=rejection_reasons,
            )

        # Rank candidates by delta proximity to target
        target_delta = self.policy.delta_target_abs
        scored_candidates = []

        for contract in candidates:
            delta_abs = abs(contract.delta)
            delta_score = abs(delta_abs - target_delta)

            # Calculate DTE
            expiry = datetime.strptime(contract.expiry_date, '%Y-%m-%d')
            dte = (expiry - now).days

            # Composite score: prioritize delta match, then shorter DTE, then higher OI
            score = (
                delta_score * 1000  # Primary: delta proximity
                + (dte / 100)  # Secondary: favor shorter DTE
                - (contract.open_interest / 1_000_000)  # Tiebreaker: favor liquidity
            )

            scored_candidates.append((score, contract))

        # Select best (lowest score)
        scored_candidates.sort(key=lambda x: x[0])
        best_contract = scored_candidates[0][1]

        # Calculate quantity
        delta_abs = abs(best_contract.delta)
        if delta_abs < 0.01:
            return SelectionResult(
                success=False,
                error="Delta too small for reliable hedging",
                candidates_evaluated=len(chain),
            )

        # Contracts needed: target_notional / (100 * underlying_px * |delta|)
        # Each contract controls 100 shares
        contracts_needed = target_notional_usd / (100 * underlying_px * delta_abs)
        quantity = max(1, round(contracts_needed))

        # Final safety check
        actual_notional = quantity * 100 * underlying_px * delta_abs
        if actual_notional > self.policy.max_notional_usd_per_day:
            quantity = max(1, int(self.policy.max_notional_usd_per_day / (100 * underlying_px * delta_abs)))
            actual_notional = quantity * 100 * underlying_px * delta_abs

        # Calculate limit price with slippage adjustment
        # For buying: limit = mid * (1 + slippage_bps/10000)
        slippage_mult = 1.0 + (self.policy.slippage_bps / 10000.0)
        limit_price = best_contract.mid * slippage_mult

        # Ensure minimum price tick (0.01 for most options)
        limit_price = round(limit_price, 2)

        # Build OCC symbol
        occ_symbol = build_occ_symbol(
            underlying=best_contract.underlying,
            expiry_date=best_contract.expiry_date,
            right=best_contract.right,
            strike=best_contract.strike,
        )

        # Build rationale
        expiry = datetime.strptime(best_contract.expiry_date, '%Y-%m-%d')
        dte = (expiry - now).days

        rationale = (
            f"Selected {best_contract.right} contract with delta={best_contract.delta:.3f} "
            f"(target={target_delta:.2f}), DTE={dte}, OI={best_contract.open_interest:,}, "
            f"spread={best_contract.spread_bps:.1f}bps, strike=${best_contract.strike:.2f}"
        )

        selected = SelectedContract(
            occ_symbol=occ_symbol,
            underlying=best_contract.underlying,
            expiry_date=best_contract.expiry_date,
            strike=best_contract.strike,
            right=best_contract.right,
            delta=best_contract.delta,
            open_interest=best_contract.open_interest,
            spread_bps=best_contract.spread_bps,
            mid_price=best_contract.mid,
            limit_price=limit_price,
            quantity=quantity,
            notional_usd=actual_notional,
            tif=self.policy.tif,
            rationale=rationale,
            contract_details=best_contract,
        )

        return SelectionResult(
            success=True,
            contract=selected,
            candidates_evaluated=len(chain),
            rejection_reasons=rejection_reasons,
        )

    def update_daily_stats(self, contracts: int, notional: float):
        """Update daily execution stats (called after successful execution)."""
        self._daily_stats['contracts'] += contracts
        self._daily_stats['notional'] += notional

    def reset_daily_stats(self):
        """Reset daily stats (call at start of new trading day)."""
        self._daily_stats = {'contracts': 0, 'notional': 0.0}


async def propose_options_hedge_with_selector(
    runs_dir: str,
    exposure: Dict[str, float],
    intent: Optional[HedgeIntent] = None,
) -> Dict[str, Any]:
    """Main entry point for options hedge proposal using selector.

    Args:
        runs_dir: Directory to write output files
        exposure: Portfolio exposure (gross, net)
        intent: Optional hedging intent

    Returns:
        Plan dict (written to options_hedge.json)
    """
    from scripts.ai_trader.risk.hedging_events import log_hedging_event

    selector = OptionsSelector()

    result = await selector.select_hedge_contract(
        exposure=exposure,
        intent=intent,
    )

    # Build output plan
    if result.success and result.contract:
        plan = {
            'need_hedge': True,
            'timestamp': datetime.now().isoformat(),
            'exposure': exposure,
            'contract': result.contract.to_dict(),
            'selection_stats': {
                'candidates_evaluated': result.candidates_evaluated,
                'rejection_reasons': result.rejection_reasons,
            },
        }

        # Log successful selection
        log_hedging_event(
            runs_dir=runs_dir,
            event_type="selection",
            mode="options_real",
            success=True,
            details={
                'contract': result.contract.to_dict(),
                'candidates_evaluated': result.candidates_evaluated,
                'rejection_reasons': result.rejection_reasons,
                'intent': intent.__dict__ if intent else None,
            },
        )
    else:
        plan = {
            'need_hedge': False,
            'timestamp': datetime.now().isoformat(),
            'exposure': exposure,
            'error': result.error,
            'selection_stats': {
                'candidates_evaluated': result.candidates_evaluated,
                'rejection_reasons': result.rejection_reasons,
            },
        }

        # Log selection failure
        log_hedging_event(
            runs_dir=runs_dir,
            event_type="rejection",
            mode="options_real",
            success=False,
            details={
                'reason': result.error or 'unknown',
                'candidates_evaluated': result.candidates_evaluated,
                'rejection_reasons': result.rejection_reasons,
                'exposure': exposure,
            },
        )

    # Write to file
    outd = Path(runs_dir) / date.today().isoformat()
    outd.mkdir(parents=True, exist_ok=True)
    (outd / "options_hedge.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

    return plan


if __name__ == "__main__":
    """Test harness for options selector."""
    import asyncio

    async def test():
        # Test with synthetic data (adjusted for 100k daily limit)
        # gross=300k × 30% (medium) = 90k target notional (under 100k limit)
        exposure = {'gross': 300_000.0, 'net': 180_000.0}
        intent = HedgeIntent(intent_type='vol_spike', size='medium')

        plan = await propose_options_hedge_with_selector(
            runs_dir='runs/ai_trader',
            exposure=exposure,
            intent=intent,
        )

        print(json.dumps(plan, indent=2))

    asyncio.run(test())
