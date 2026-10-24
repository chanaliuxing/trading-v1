"""
Arbitration Engine
Multi-strategy conflict resolution and capacity management
Based on doc/55.md specification

Features:
- Same-side coexistence with capacity limits
- Opposite-side conflict resolution (priority-based)
- Per-symbol notional caps
- Participation rate limits
- Preemption support (configurable)
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

from libs.strategy.base import DecisionEnvelope
from libs.logger import get_logger
from prometheus_client import Counter
from services.decision.quantity_semantics import conservative_execution_qty, stabilize_requested_qty

logger = get_logger(__name__)

# Metrics
arb_wins_total = Counter(
    'arb_wins_total',
    'Arbitration wins',
    ['reason']
)

arb_reject_total = Counter(
    'arb_reject_total',
    'Arbitration rejects',
    ['reason']
)

arb_preempt_suggest_total = Counter(
    'arb_preempt_suggest_total',
    'Preemption suggestions'
)

arb_finalize_total = Counter(
    'arb_finalize_total',
    'Arbitration finalizations',
    ['status']
)


def now_ms() -> int:
    """Current timestamp in milliseconds"""
    return int(time.time() * 1000)


@dataclass
class ArbCandidate:
    """Arbitration candidate"""
    decision_id: str
    strategy_id: str
    symbol: str
    dir: int  # +1 long, -1 short
    target_qty: float
    exp_edge_bp: float  # Expected edge (bp)
    cost_bp: float  # Estimated cost (bp)
    priority: int  # Strategy priority
    ttl_ms: int
    ts_ms: int = field(default_factory=now_ms)


@dataclass
class ActiveSymbol:
    """Active symbol state"""
    symbol: str
    dir: int  # Current active direction (+1/-1)
    total_qty: float  # Combined quantity
    winners: Dict[str, float]  # strategy_id -> allocated qty
    ts_ms: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class DecisionAllocation:
    """Accepted allocation tracked by decision_id for deterministic finalize."""
    decision_id: str
    strategy_id: str
    symbol: str
    qty: float
    dir: int
    ts_ms: int = field(default_factory=now_ms)


class ArbitrationEngine:
    """
    Arbitration engine for multi-strategy coordination

    Rules:
    - Same symbol, same direction: Coexist (up to capacity)
    - Same symbol, opposite direction: Conflict (priority wins)
    - Per-symbol notional caps enforced
    - Participation rate limits (optional)
    """

    def __init__(
        self,
        cfg: Dict[str, Any],
        reserve_ledger: Any,
        adv_provider: Optional[Any] = None
    ):
        """
        Initialize arbiter

        Args:
            cfg: Configuration dict
            reserve_ledger: Reserve ledger (for notional tracking)
            adv_provider: ADV provider (optional, for participation limits)
        """
        self.cfg = cfg
        self.ledger = reserve_ledger
        self.adv = adv_provider

        # Active symbols tracking
        self._actives: Dict[str, ActiveSymbol] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._decision_allocations: Dict[str, DecisionAllocation] = {}

        logger.info("ArbitrationEngine initialized",
                   policy=cfg.get("arbiter", {}).get("policy"),
                   preempt=cfg.get("arbiter", {}).get("preempt"))

    def _lock(self, symbol: str) -> asyncio.Lock:
        """Get or create lock for symbol"""
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]

    def _score(self, c: ArbCandidate) -> float:
        """
        Calculate candidate score

        Args:
            c: Candidate

        Returns:
            Score (higher is better)
        """
        scoring = self.cfg.get("arbiter", {}).get("scoring", {})
        w_edge = float(scoring.get("w_edge", 1.0))
        w_cost = float(scoring.get("w_cost", 1.0))
        penalty = float(scoring.get("penalty_bp", 0.0))

        return w_edge * c.exp_edge_bp - w_cost * c.cost_bp - penalty

    async def _symbol_remaining_notional(self, symbol: str) -> float:
        """
        Calculate remaining notional capacity for symbol

        Args:
            symbol: Symbol

        Returns:
            Remaining capacity (USD)
        """
        cap = float(
            self.cfg.get("arbiter", {})
            .get("symbol_cap", {})
            .get("max_notional_usd", 500000)
        )

        # Get snapshot from reserve ledger
        snap = await self.ledger.snapshot()

        # Sum up notional for this symbol
        used = 0.0
        for r in snap.get("items", {}).values():
            if r.get("symbol") == symbol:
                used += (
                    r.get("amount_remaining")
                    if r.get("amount_remaining") is not None
                    else r.get("notional", 0.0)
                )

        return max(0.0, cap - used)

    async def _participation_ok(self, symbol: str, add_notional: float) -> bool:
        """
        Check if participation rate is acceptable

        Args:
            symbol: Symbol
            add_notional: Additional notional

        Returns:
            True if ok
        """
        cap_part = float(
            self.cfg.get("arbiter", {})
            .get("symbol_cap", {})
            .get("max_participation", 0.2)
        )

        if cap_part <= 0:
            return True

        if not self.adv:
            return True  # No ADV provider, skip check

        adv = await self.adv.adv_usd(symbol)
        if not adv:
            return True

        return (add_notional / adv) <= cap_part + 1e-9

    async def admit(
        self,
        env: DecisionEnvelope
    ) -> Tuple[bool, Optional[float], Dict[str, Any]]:
        """
        Admit decision to arbitration

        Args:
            env: Decision envelope

        Returns:
            (allowed, suggested_qty, metadata)
        """
        extras = env.extras or {}
        sym = str(extras.get("risk_symbol") or env.symbol)
        dir_ = 1 if env.target_qty > 0 else -1

        # Get strategy priority
        priority_map = self.cfg.get("arbiter", {}).get("strategy_priority", {})
        pri = int(priority_map.get(env.strategy_id, 50))

        # Extract edge and cost
        edge_bp = float(env.extras.get("edge_bp", env.signal_score * 100.0))
        cost_bp = float(
            env.cost_estimates.slippage_bp +
            env.cost_estimates.fees_bp +
            env.cost_estimates.impact_bp
        )

        # Create candidate
        cand = ArbCandidate(
            decision_id=env.decision_id,
            strategy_id=env.strategy_id,
            symbol=sym,
            dir=dir_,
            target_qty=abs(
                stabilize_requested_qty(
                    env.target_qty,
                    symbol=str(env.symbol or sym),
                    extras=extras,
                )
            ),
            exp_edge_bp=edge_bp,
            cost_bp=cost_bp,
            priority=pri,
            ttl_ms=int(self.cfg.get("arbiter", {}).get("ttl_ms", 1500))
        )

        async with self._lock(sym):
            active = self._actives.get(sym)

            # 1) No active position: Grant (with capacity check)
            if not active:
                allow_qty, meta = await self._clip_by_symbol_cap(env, cand)
                if allow_qty <= 0:
                    arb_reject_total.labels(reason="symbol_cap_exhausted").inc()
                    return False, None, {"reason": "symbol_cap_exhausted"}

                self._actives[sym] = ActiveSymbol(
                    symbol=sym,
                    dir=dir_,
                    total_qty=allow_qty,
                    winners={env.strategy_id: allow_qty},
                    ts_ms=now_ms()
                )
                self._register_allocation(
                    decision_id=str(env.decision_id),
                    strategy_id=str(env.strategy_id),
                    symbol=str(sym),
                    qty=float(allow_qty),
                    dir_=int(dir_),
                )

                arb_wins_total.labels(reason="first").inc()
                return True, allow_qty, {"reason": "first", **meta}

            # 2) Same direction: Coexist (if enabled)
            coexist_enabled = self.cfg.get("arbiter", {}).get("coexist_same_side", True)
            if active.dir == dir_ and coexist_enabled:
                allow_qty, meta = await self._clip_by_symbol_cap(env, cand)
                if allow_qty <= 0:
                    arb_reject_total.labels(reason="symbol_cap_exhausted").inc()
                    return False, None, {"reason": "symbol_cap_exhausted"}

                # Add to existing
                active.total_qty += allow_qty
                active.winners[env.strategy_id] = (
                    active.winners.get(env.strategy_id, 0.0) + allow_qty
                )
                active.ts_ms = now_ms()
                self._register_allocation(
                    decision_id=str(env.decision_id),
                    strategy_id=str(env.strategy_id),
                    symbol=str(sym),
                    qty=float(allow_qty),
                    dir_=int(dir_),
                )

                arb_wins_total.labels(reason="coexist").inc()
                return True, allow_qty, {"reason": "coexist", **meta}

            # 3) Opposite direction: Conflict resolution
            # Find highest priority current holder
            cur_rep, cur_pri = None, -1
            for sid in active.winners.keys():
                p = int(priority_map.get(sid, 50))
                if p > cur_pri:
                    cur_rep = sid
                    cur_pri = p

            new_score = cand.priority
            cur_score = cur_pri

            # Check preemption policy
            preempt_policy = self.cfg.get("arbiter", {}).get("preempt", "never")
            should_preempt = (new_score > cur_score) and (
                preempt_policy in ("always", "price_better")
            )

            if should_preempt:
                arb_preempt_suggest_total.inc()
                return False, None, {
                    "reason": "preempt_request",
                    "target": cur_rep
                }

            # Default: Reject
            arb_reject_total.labels(reason="conflict").inc()
            return False, None, {
                "reason": "conflict_with_active",
                "holder": cur_rep
            }

    async def _clip_by_symbol_cap(
        self,
        env: DecisionEnvelope,
        cand: ArbCandidate
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Clip request to symbol capacity limits

        Args:
            env: Decision envelope
            cand: Candidate

        Returns:
            (allowed_qty, metadata)
        """
        remaining = await self._symbol_remaining_notional(cand.symbol)

        extras = env.extras or {}
        is_option = bool(
            str(extras.get("instrument_type") or "").strip().upper() == "OPTION"
            or str(env.symbol or "").strip().upper().startswith("OPT:")
        )

        # Get reference price (premium for options)
        ref_px = extras.get("ref_px", None)
        if not ref_px or ref_px <= 0:
            # Conservative: Use a default or skip
            ref_px = 100.0  # Placeholder

        contracts_mult = 1.0
        if is_option:
            try:
                contracts_mult = float(extras.get("contracts_mult") or 100.0)
            except Exception:
                contracts_mult = 100.0

        # Calculate requested notional
        req_notional = cand.target_qty * float(ref_px) * float(contracts_mult)
        try:
            intent_notional = extras.get("intent_notional_usd")
            if intent_notional is not None and float(intent_notional) > 0:
                req_notional = float(intent_notional)
        except Exception:
            pass

        if remaining <= 1e-6 or req_notional <= 0:
            return 0.0, {
                "remaining": remaining,
                "requested_notional": req_notional,
                "ref_px": ref_px,
                "env_max_notional": float(getattr(env, "max_notional", 0.0) or 0.0),
            }

        # Envelope.max_notional may be omitted by legacy strategies (defaults to 0.0).
        # In that case, fall back to requested notional so arbiter capacity checks
        # do not incorrectly reject every entry as symbol_cap_exhausted.
        env_max_notional = float(getattr(env, "max_notional", 0.0) or 0.0)
        effective_max_notional = env_max_notional if env_max_notional > 0 else req_notional

        # Allow up to remaining capacity and intended order size
        allow_notional = min(remaining, effective_max_notional, req_notional)

        # Check participation rate
        if not await self._participation_ok(cand.symbol, allow_notional):
            return 0.0, {
                "remaining": remaining,
                "reason": "participation_cap",
                "requested_notional": req_notional,
                "env_max_notional": env_max_notional,
                "effective_max_notional": effective_max_notional,
                "allow_notional": allow_notional,
                "ref_px": ref_px,
            }

        # Convert back to quantity
        denom = float(max(float(ref_px) * float(contracts_mult), 1e-9))
        allow_qty = min(cand.target_qty, allow_notional / denom)
        normalized_allow_qty = conservative_execution_qty(
            allow_qty,
            symbol=str(env.symbol or cand.symbol),
            extras=extras,
        )

        return max(0.0, normalized_allow_qty), {
            "remaining": remaining,
            "requested_notional": req_notional,
            "env_max_notional": env_max_notional,
            "effective_max_notional": effective_max_notional,
            "allow_notional": allow_notional,
            "ref_px": ref_px,
            "normalized_allow_qty": normalized_allow_qty,
        }

    def _register_allocation(
        self,
        *,
        decision_id: str,
        strategy_id: str,
        symbol: str,
        qty: float,
        dir_: int,
    ) -> None:
        # Replace stale mapping defensively if same decision_id is reused.
        self._decision_allocations[str(decision_id)] = DecisionAllocation(
            decision_id=str(decision_id),
            strategy_id=str(strategy_id),
            symbol=str(symbol),
            qty=max(0.0, float(qty)),
            dir=int(dir_),
        )

    def _release_allocation_locked(self, alloc: DecisionAllocation) -> None:
        active = self._actives.get(alloc.symbol)
        if not active:
            return
        if alloc.strategy_id not in active.winners:
            return

        current = float(active.winners.get(alloc.strategy_id, 0.0) or 0.0)
        remain = max(0.0, current - float(alloc.qty))
        if remain <= 1e-9:
            active.winners.pop(alloc.strategy_id, None)
        else:
            active.winners[alloc.strategy_id] = remain
        active.total_qty = max(0.0, float(active.total_qty) - float(alloc.qty))
        if not active.winners or active.total_qty <= 1e-9:
            self._actives.pop(alloc.symbol, None)

    async def finalize(self, decision: Any, status: str) -> None:
        """
        Finalize arbitration (release slot)

        Call this when:
        - Intent is submitted
        - Intent is rejected by risk
        - Intent expires

        Args:
            decision: Decision ID (preferred) or Decision envelope (legacy compatibility)
            status: Finalization status
        """
        decision_id = None
        symbol = None
        strategy_id = None
        if isinstance(decision, DecisionEnvelope):
            decision_id = str(decision.decision_id)
            symbol = str(decision.symbol)
            strategy_id = str(decision.strategy_id)
        else:
            decision_id = str(decision)

        alloc = self._decision_allocations.pop(decision_id, None)
        if alloc is None:
            logger.debug(
                "Arbitration finalize skipped (allocation_missing)",
                decision_id=decision_id,
                status=status,
                symbol=symbol,
                strategy_id=strategy_id,
            )
            return

        async with self._lock(alloc.symbol):
            self._release_allocation_locked(alloc)
            arb_finalize_total.labels(status=status).inc()

        logger.debug(
            "Arbitration finalized",
            decision_id=alloc.decision_id,
            symbol=alloc.symbol,
            strategy_id=alloc.strategy_id,
            released_qty=alloc.qty,
            status=status,
        )
