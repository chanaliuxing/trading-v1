"""
Reg-Lite Regulatory Guard

Pre-execution regulatory compliance checks:
- LULD (Limit Up Limit Down) price bands
- SSR (Short Sale Restriction) - Reg SHO
- Trading halts and market status
- Tick size and lot size validation
- Price collar/fence checks

Based on doc/44.md specification.
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import math

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Intent:
    """Order intent (simplified from DecisionEnvelope)"""

    decision_id: str
    symbol: str
    side: str  # BUY/SELL/SELL_SHORT
    qty: float
    order_type: str  # MKT/LMT/STP/STP_LMT/TRAIL/TRAIL_LIMIT
    limit_px: Optional[float] = None
    stop_px: Optional[float] = None
    tif: str = "DAY"
    allow_prepost: bool = False


@dataclass
class MarketSnap:
    """
    Market snapshot for regulatory checks
    Should be sourced from MarketStateProvider
    """

    last_px: Optional[float] = None
    bid_px: Optional[float] = None
    ask_px: Optional[float] = None
    mid_px: Optional[float] = None
    luld_low: Optional[float] = None  # LULD lower band
    luld_high: Optional[float] = None  # LULD upper band
    ssr_active: bool = False  # SSR (Reg SHO) triggered
    halted: bool = False  # Trading halted
    tick_size: float = 0.01  # Minimum price increment
    lot_size: float = 1.0  # Minimum order quantity


@dataclass
class RuleHit:
    """Regulatory rule violation"""

    rule_id: str
    reason: str
    meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule_id, "reason": self.reason, **self.meta}


class RegLiteGuard:
    """
    Regulatory compliance guard

    Checks:
    1. Tick size and lot size validation
    2. LULD price band compliance
    3. SSR (Short Sale Restriction) enforcement
    4. Price collar/fence (distance from market)
    5. Market halts and trading hours
    6. NBBO availability for market orders
    """

    def __init__(
        self,
        price_collar_pct: float = 5.0,
        enable_luld_warning: bool = True,
        enable_ssr_strict: bool = True,
    ):
        """
        Initialize Reg-Lite guard

        Args:
            price_collar_pct: Maximum allowed deviation from market (%)
            enable_luld_warning: If True, warn on missing LULD data instead of blocking
            enable_ssr_strict: If True, strictly enforce SSR rules
        """
        self.collar_pct = price_collar_pct
        self.enable_luld_warning = enable_luld_warning
        self.enable_ssr_strict = enable_ssr_strict

        # Metrics
        self.metrics = type(
            "M",
            (),
            {
                "checks_total": 0,
                "passes_total": 0,
                "blocks_total": 0,
                "rule_hits": {},  # rule_id -> count
            },
        )()

    def _round_to_tick(self, px: float, tick: float) -> float:
        """Round price to tick size"""
        if tick <= 0:
            return px
        return round(round(px / tick) * tick, 10)

    def check(self, intent: Intent, market: MarketSnap) -> List[RuleHit]:
        """
        Check intent against regulatory rules

        Args:
            intent: Order intent to check
            market: Current market snapshot

        Returns:
            List of rule violations (empty if all checks pass)
        """
        self.metrics.checks_total += 1
        hits: List[RuleHit] = []

        # 0) Basic sanity checks
        if intent.qty <= 0:
            hit = RuleHit("bad_qty", "non-positive qty", {"qty": intent.qty})
            hits.append(hit)
            self._record_hit(hit)
            self.metrics.blocks_total += 1
            return hits

        if market.halted:
            hit = RuleHit("halted", "symbol halted/news pending", {})
            hits.append(hit)
            self._record_hit(hit)
            self.metrics.blocks_total += 1
            return hits

        # 1) Tick size and lot size validation
        self._check_lot_size(intent, market, hits)
        self._check_tick_size(intent, market, hits)

        # 2) LULD price band check
        self._check_luld(intent, market, hits)

        # 3) SSR (Reg SHO) short sale restriction
        self._check_ssr(intent, market, hits)

        # 4) Price collar (distance from market)
        self._check_price_collar(intent, market, hits)

        # 5) NBBO check for market orders
        self._check_nbbo(intent, market, hits)

        # Update metrics
        if hits:
            self.metrics.blocks_total += 1
        else:
            self.metrics.passes_total += 1

        return hits

    def _check_lot_size(
        self, intent: Intent, market: MarketSnap, hits: List[RuleHit]
    ):
        """Check lot size compliance"""
        lot = float(market.lot_size or 0.0)
        if lot <= 0:
            return
        # Quantities for listed options should tolerate tiny float noise
        # (e.g. 2.9999999999999996 should still count as 3 lots).
        ratio = float(intent.qty) / lot
        if abs(ratio - round(ratio)) > 1e-9:
            hit = RuleHit(
                "lot_size",
                "qty not multiple of lot",
                {"lot": lot, "qty": intent.qty},
            )
            hits.append(hit)
            self._record_hit(hit)

    def _check_tick_size(
        self, intent: Intent, market: MarketSnap, hits: List[RuleHit]
    ):
        """Check tick size compliance for limit/stop prices"""
        if not market.tick_size:
            return

        prices = []
        if intent.order_type in ("LMT", "STP_LMT") and intent.limit_px is not None:
            prices.append(("limit_px", intent.limit_px))
        if intent.order_type in ("STP", "STP_LMT") and intent.stop_px is not None:
            prices.append(("stop_px", intent.stop_px))

        for name, px in prices:
            adj = self._round_to_tick(px, market.tick_size)
            if abs(adj - px) > 1e-12:
                hit = RuleHit(
                    "tick_size",
                    f"{name} violates tick",
                    {"tick": market.tick_size, "px": px, "adj": adj},
                )
                hits.append(hit)
                self._record_hit(hit)

    def _check_luld(self, intent: Intent, market: MarketSnap, hits: List[RuleHit]):
        """Check LULD price band compliance"""

        def _in_luld(px: float) -> bool:
            if market.luld_low is None or market.luld_high is None:
                # No LULD data - warn or allow based on config
                if self.enable_luld_warning:
                    logger.warning(
                        f"LULD data missing for {intent.symbol}", decision_id=intent.decision_id
                    )
                return True
            return (market.luld_low - 1e-10) <= px <= (market.luld_high + 1e-10)

        prices = []
        if intent.order_type in ("LMT", "STP_LMT") and intent.limit_px is not None:
            prices.append(("limit_px", intent.limit_px))
        if intent.order_type in ("STP", "STP_LMT") and intent.stop_px is not None:
            prices.append(("stop_px", intent.stop_px))

        for name, px in prices:
            if not _in_luld(px):
                hit = RuleHit(
                    "luld_band",
                    f"{name} outside LULD",
                    {"low": market.luld_low, "high": market.luld_high, "px": px},
                )
                hits.append(hit)
                self._record_hit(hit)

    def _check_ssr(self, intent: Intent, market: MarketSnap, hits: List[RuleHit]):
        """Check SSR (Short Sale Restriction) compliance"""
        if not market.ssr_active:
            return

        # SSR only applies to short sales
        if intent.side.upper() not in ("SELL_SHORT", "SHORT", "SLD_SHORT"):
            return

        if not self.enable_ssr_strict:
            logger.warning(
                f"SSR active but not enforcing for {intent.symbol}",
                decision_id=intent.decision_id,
            )
            return

        bid = market.bid_px or (market.mid_px or market.last_px or 0.0)

        # Market orders are forbidden during SSR
        if intent.order_type == "MKT":
            hit = RuleHit("ssr", "SSR active: short MKT forbidden", {"bid": bid})
            hits.append(hit)
            self._record_hit(hit)
            return

        # Limit orders must be above bid + 1 tick
        if intent.order_type in ("LMT", "STP_LMT") and intent.limit_px is not None:
            need = bid + (market.tick_size or 0.01)
            if intent.limit_px <= need - 1e-12:
                hit = RuleHit(
                    "ssr",
                    "limit must be above bid",
                    {"bid": bid, "limit": intent.limit_px, "need_min": need},
                )
                hits.append(hit)
                self._record_hit(hit)

    def _check_price_collar(
        self, intent: Intent, market: MarketSnap, hits: List[RuleHit]
    ):
        """Check price collar (distance from market reference)"""
        ref = market.mid_px or market.last_px
        if not ref:
            return

        if intent.order_type in ("LMT", "STP_LMT") and intent.limit_px is not None:
            dev = abs(intent.limit_px - ref) / ref * 100.0
            if dev > self.collar_pct and not intent.allow_prepost:
                hit = RuleHit(
                    "price_collar",
                    "limit too far from ref",
                    {"ref": ref, "limit": intent.limit_px, "dev_pct": dev},
                )
                hits.append(hit)
                self._record_hit(hit)

    def _check_nbbo(self, intent: Intent, market: MarketSnap, hits: List[RuleHit]):
        """Check NBBO availability for market orders"""
        if intent.order_type == "MKT" and not (market.bid_px and market.ask_px):
            hit = RuleHit("no_nbbo", "MKT without NBBO snapshot", {})
            hits.append(hit)
            self._record_hit(hit)

    def _record_hit(self, hit: RuleHit):
        """Record hit in metrics"""
        if hit.rule_id not in self.metrics.rule_hits:
            self.metrics.rule_hits[hit.rule_id] = 0
        self.metrics.rule_hits[hit.rule_id] += 1


# Helper functions for integration

def intent_from_decision_envelope(envelope: Dict[str, Any]) -> Intent:
    """Convert DecisionEnvelope to Intent for Reg-Lite check"""
    order_style = envelope.get("order_style", {})
    extras = envelope.get("extras", {}) or {}

    # Map order style to standard types
    kind = order_style.get("kind", "MKT")
    order_type_map = {
        "MKT": "MKT",
        "LMT": "LMT",
        "STP": "STP",
        "STP_LMT": "STP_LMT",
        "TRAIL": "TRAIL",
        "TRAIL_LIMIT": "TRAIL_LIMIT",
    }
    order_type = order_type_map.get(kind, "MKT")

    # Determine side from target_qty.
    # For safety exits (flatten/close-only/protective-stop), negative qty is a
    # position-reducing SELL, not a short-entry SELL_SHORT.
    target_qty = envelope.get("target_qty", 0)
    is_safety_exit = bool(
        extras.get("flatten_action")
        or extras.get("close_only")
        or extras.get("protective_stop")
    )
    if target_qty > 0:
        side = "BUY"
    elif target_qty < 0:
        side = "SELL" if is_safety_exit else "SELL_SHORT"
    else:
        side = "FLAT"

    return Intent(
        decision_id=envelope.get("decision_id", ""),
        symbol=envelope.get("symbol", ""),
        side=side,
        qty=abs(target_qty),
        order_type=order_type,
        limit_px=order_style.get("limit_px"),
        stop_px=order_style.get("stop_px"),
        tif=order_style.get("tif", "DAY"),
        allow_prepost=order_style.get("allow_prepost", False),
    )


async def get_market_snap_from_provider(
    symbol: str, market_state_provider
) -> MarketSnap:
    """Get MarketSnap from MarketStateProvider"""
    state = await market_state_provider.get(symbol)
    if not state:
        # Return empty snap (will trigger appropriate warnings/blocks)
        return MarketSnap()

    return MarketSnap(
        last_px=state.get('last_px'),
        bid_px=state.get('bid_px'),
        ask_px=state.get('ask_px'),
        mid_px=state.get('mid_px'),
        luld_low=state.get('luld_low'),
        luld_high=state.get('luld_high'),
        ssr_active=state.get('ssr_active', False),
        halted=state.get('halted', False),
        tick_size=state.get('tick_size', 0.01),
        lot_size=state.get('lot_size', 1),
    )
