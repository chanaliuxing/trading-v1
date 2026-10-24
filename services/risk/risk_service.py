"""
Risk Service - Orchestration Layer

Coordinates:
- Capital reservation (ReservationService)
- Regulatory compliance (RegLiteGuard)
- Capital quota enforcement (CapitalQuota)
- Decision approval workflow

Based on doc/16.md specification.
"""
import asyncio
import time
import uuid
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from libs.logger import get_logger
from services.risk.reservation_service import ReservationService
from services.risk.reg_lite import (
    RegLiteGuard,
    Intent,
    MarketSnap,
    intent_from_decision_envelope,
    get_market_snap_from_provider,
)
from services.risk.capital_quota import CapOrchestrator

logger = get_logger(__name__)


@dataclass
class RiskDecision:
    """Risk decision result"""

    decision_id: str
    approved: bool
    reason: str
    hits: List[Dict[str, Any]]  # Rule violations
    reserved_amount: Optional[float] = None
    strategy_cap_used: Optional[float] = None
    ts_us: int = 0


class RiskService:
    """
    Risk Service - Orchestration Layer

    Workflow:
    1. Validate decision envelope structure
    2. Check regulatory compliance (Reg-Lite)
    3. Check capital quota (per strategy)
    4. Reserve capital (if approved)
    5. Return decision

    Integration points:
    - Consumes: decision envelopes from strategy engine
    - Produces: risk decisions (approved/denied)
    - Updates: reservation ledger, quota tracking
    """

    def __init__(
        self,
        reservation_service: ReservationService,
        reg_lite_guard: RegLiteGuard,
        capital_quota_service: Optional[CapOrchestrator] = None,
        market_state_provider=None,
        default_buffer_bps: int = 10,
    ):
        """
        Initialize Risk Service

        Args:
            reservation_service: Capital reservation service
            reg_lite_guard: Regulatory compliance guard
            capital_quota_service: Optional capital quota service
            market_state_provider: Market state provider for Reg-Lite
            default_buffer_bps: Default slippage buffer (basis points)
        """
        self.reservation = reservation_service
        self.reg_lite = reg_lite_guard
        self.quota = capital_quota_service
        self.market_provider = market_state_provider
        self.default_buffer_bps = default_buffer_bps

        # Metrics
        self.metrics = type(
            "M",
            (),
            {
                "approvals_total": 0,
                "denials_total": 0,
                "reglite_blocks_total": 0,
                "quota_blocks_total": 0,
                "reservation_blocks_total": 0,
                "approval_latency_ms": [],
            },
        )()

    async def approve(
        self, envelope: Dict[str, Any], idempotency_key: str
    ) -> RiskDecision:
        """
        Approve or deny decision envelope

        Args:
            envelope: Decision envelope from strategy
            idempotency_key: Idempotency key for reservation

        Returns:
            RiskDecision with approval status and details
        """
        start_us = int(time.time() * 1_000_000)

        decision_id = envelope.get("decision_id", "")
        strategy_id = envelope.get("strategy_id", "")
        symbol = envelope.get("symbol", "")
        extras = dict(envelope.get("extras") or {})
        is_option = bool(
            str(extras.get("instrument_type") or "").strip().upper() == "OPTION"
            or str(symbol or "").strip().upper().startswith("OPT:")
        )

        try:
            # 1. Validate envelope structure
            if not self._validate_envelope(envelope):
                return self._deny(
                    decision_id,
                    "invalid_envelope",
                    [{"rule": "structure", "reason": "Missing required fields"}],
                )

            # 2. Regulatory compliance check (Reg-Lite)
            if not self._is_reglite_exempt(envelope):
                intent = intent_from_decision_envelope(envelope)
                market_snap = await self._get_market_snap(symbol, extras=extras, is_option=is_option)

                reg_hits = self.reg_lite.check(intent, market_snap)
                if reg_hits:
                    self.metrics.reglite_blocks_total += 1
                    return self._deny(
                        decision_id,
                        "reglite_block",
                        [h.to_dict() for h in reg_hits],
                    )
            else:
                logger.warning(
                    "reglite_bypassed_for_manual_force_flatten",
                    decision_id=decision_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                )

            # 3. Calculate notional
            notional_usd = self._calculate_notional(envelope)
            buffer_bps = self._reservation_buffer_bps(extras)

            # 3.5. Close-only safety actions must not be blocked by capital reservation.
            # They reduce existing exposure and are the emergency-risk path.
            if self._is_capital_reservation_exempt(envelope):
                self.metrics.approvals_total += 1
                latency_ms = (int(time.time() * 1_000_000) - start_us) / 1000.0
                self.metrics.approval_latency_ms.append(latency_ms)
                return RiskDecision(
                    decision_id=decision_id,
                    approved=True,
                    reason="approved_close_only_exempt",
                    hits=[],
                    reserved_amount=0.0,
                    ts_us=int(time.time() * 1_000_000),
                )

            shared_cap_hit = await self._check_shared_cap_group(
                strategy_id=str(strategy_id),
                symbol=str(symbol),
                notional_usd=float(notional_usd),
                extras=extras,
            )
            if shared_cap_hit is not None:
                return self._deny(
                    decision_id,
                    "shared_cap_group_block",
                    [shared_cap_hit],
                )

            # Note: Capital quota check would go here if CapOrchestrator
            # had a sync check_allocation method. Currently it uses
            # propose_caps/apply_caps which is async batch-oriented.

            # 4. Reserve capital
            reservation_symbol = str(extras.get("risk_symbol") or symbol)
            result = await self.reservation.approve_and_reserve(
                strategy_id=strategy_id,
                symbol=reservation_symbol,
                notional_usd=notional_usd,
                buffer_bps=buffer_bps,
                idempotency_key=idempotency_key,
            )

            # Unpack ReservationResult object
            reservation_ok = result.approved
            reserved_amt = result.reserved_usd

            if not reservation_ok:
                self.metrics.reservation_blocks_total += 1
                reservation_reason = str(result.reason or "INSUFFICIENT_CAPITAL")
                logger.warning(
                    "Reservation denied",
                    strategy_id=str(strategy_id),
                    symbol=str(symbol),
                    decision_id=str(decision_id),
                    reason=reservation_reason,
                )
                return self._deny(
                    decision_id,
                    "reservation_failed",
                    [
                        {
                            "rule": "capital_reservation",
                            "reason": reservation_reason,
                        }
                    ],
                )

            # 5. Approved
            self.metrics.approvals_total += 1
            latency_ms = (int(time.time() * 1_000_000) - start_us) / 1000.0
            self.metrics.approval_latency_ms.append(latency_ms)

            return RiskDecision(
                decision_id=decision_id,
                approved=True,
                reason="approved",
                hits=[],
                reserved_amount=reserved_amt,
                ts_us=int(time.time() * 1_000_000),
            )

        except Exception as e:
            logger.error(
                "Risk approval error",
                decision_id=decision_id,
                error=str(e),
            )
            return self._deny(
                decision_id,
                "internal_error",
                [{"rule": "system", "reason": str(e)}],
            )

    def _validate_envelope(self, envelope: Dict[str, Any]) -> bool:
        """Validate decision envelope has required fields"""
        required = ["decision_id", "strategy_id", "symbol", "target_qty", "order_style"]
        return all(k in envelope for k in required)

    def _calculate_notional(self, envelope: Dict[str, Any]) -> float:
        """Calculate notional value of order"""
        target_qty = abs(envelope.get("target_qty", 0))
        order_style = envelope.get("order_style", {})
        extras = dict(envelope.get("extras") or {})

        # Prefer pipeline-computed notional when available.
        intent_notional = extras.get("intent_notional_usd")
        try:
            if intent_notional is not None and float(intent_notional) > 0:
                return float(intent_notional)
        except Exception:
            pass

        # Use limit price if available, otherwise use a reference price
        price = (
            order_style.get("limit_px")
            or extras.get("ref_px")
            or 0.0
        )

        return target_qty * price

    def _reservation_buffer_bps(self, extras: Dict[str, Any]) -> Optional[int]:
        """
        Resolve the reservation slippage buffer.

        When the envelope does not explicitly provide a slippage buffer, return
        ``None`` and let the reservation stored procedure apply the strategy's
        configured default from ``core.core_strategy.reserve_buffer_bps``.
        """
        raw = extras.get("slippage_buffer_bps")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return self.default_buffer_bps

    @staticmethod
    def _is_capital_reservation_exempt(envelope: Dict[str, Any]) -> bool:
        extras = dict((envelope or {}).get("extras") or {})
        return bool(
            extras.get("flatten_action")
            or extras.get("close_only")
            or extras.get("protective_stop")
        )

    @staticmethod
    def _is_reglite_exempt(envelope: Dict[str, Any]) -> bool:
        """
        Hard exemption path for emergency/manual flatten.

        Goal: a force-close command must not be blocked by entry-side marketability gates
        in Reg-Lite (e.g. no_nbbo), while still going through close-only quantity guards
        and broker acceptance.
        """
        extras = dict((envelope or {}).get("extras") or {})
        if (
            extras.get("runtime_recovery_action")
            and extras.get("flatten_action")
            and extras.get("close_only")
        ):
            return True
        return bool(
            extras.get("manual_intervention")
            and extras.get("flatten_action")
            and extras.get("close_only")
        )

    async def _get_market_snap(
        self,
        symbol: str,
        *,
        extras: Optional[Dict[str, Any]] = None,
        is_option: bool = False,
    ) -> MarketSnap:
        """Get market snapshot for regulatory checks."""
        if is_option:
            data = dict(extras or {})
            override = data.get("market_snap_override")
            if isinstance(override, dict):
                try:
                    return MarketSnap(
                        last_px=override.get("last_px"),
                        bid_px=override.get("bid_px"),
                        ask_px=override.get("ask_px"),
                        mid_px=override.get("mid_px"),
                        luld_low=override.get("luld_low"),
                        luld_high=override.get("luld_high"),
                        ssr_active=bool(override.get("ssr_active", False)),
                        halted=bool(override.get("halted", False)),
                        tick_size=float(override.get("tick_size") or 0.01),
                        lot_size=float(override.get("lot_size") or 1),
                    )
                except Exception:
                    pass
            try:
                bid = float(data.get("opt_bid_px") or 0.0)
                ask = float(data.get("opt_ask_px") or 0.0)
                mid = float(data.get("opt_mid_px") or 0.0)
                last = float(data.get("opt_last_px") or 0.0)
                if mid <= 0 and bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                tick = float(data.get("opt_tick_size") or 0.01)
                lot = float(data.get("opt_lot_size") or 1)
                return MarketSnap(
                    last_px=last if last > 0 else mid,
                    bid_px=bid if bid > 0 else None,
                    ask_px=ask if ask > 0 else None,
                    mid_px=mid if mid > 0 else None,
                    tick_size=tick,
                    lot_size=lot,
                    halted=False,
                )
            except Exception:
                return MarketSnap()

        if not self.market_provider:
            return MarketSnap()  # Empty snap

        return await get_market_snap_from_provider(symbol, self.market_provider)

    def _deny(
        self, decision_id: str, reason: str, hits: List[Dict[str, Any]]
    ) -> RiskDecision:
        """Create denial decision"""
        self.metrics.denials_total += 1
        return RiskDecision(
            decision_id=decision_id,
            approved=False,
            reason=reason,
            hits=hits,
            ts_us=int(time.time() * 1_000_000),
        )

    async def _check_shared_cap_group(
        self,
        *,
        strategy_id: str,
        symbol: str,
        notional_usd: float,
        extras: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        group_id = str(extras.get("shared_cap_group_id") or "").strip()
        if not group_id:
            return None

        try:
            limit_usd = float(extras.get("shared_cap_group_limit_usd") or 0.0)
        except Exception:
            limit_usd = 0.0
        if limit_usd <= 0.0:
            return None

        raw_ids = extras.get("shared_cap_group_strategy_ids") or []
        if isinstance(raw_ids, str):
            strategy_ids = [s.strip() for s in raw_ids.split(",") if str(s).strip()]
        elif isinstance(raw_ids, list):
            strategy_ids = [str(s).strip() for s in raw_ids if str(s).strip()]
        else:
            strategy_ids = []
        if strategy_id not in strategy_ids:
            strategy_ids.append(strategy_id)

        db = getattr(self.reservation, "db", None)
        if db is None or not hasattr(db, "fetchone"):
            return None
        try:
            # `notional_usd` already prefers pipeline-computed `intent_notional_usd`.
            # For options that value already includes the contract multiplier, so
            # shared-cap checks must not multiply by `contracts_mult` a second time.
            if extras.get("intent_notional_usd") is not None:
                order_notional_usd = max(0.0, float(notional_usd))
            else:
                order_notional_usd = max(
                    0.0,
                    float(notional_usd)
                    * float(
                        extras.get("contracts_mult")
                        or (100.0 if str(symbol).upper().startswith("OPT:") else 1.0)
                    ),
                )
        except Exception:
            order_notional_usd = max(0.0, float(notional_usd))

        try:
            row = await db.fetchone(
                """
                SELECT
                  COALESCE(
                    (
                      SELECT SUM(reserved_usd)
                      FROM core_strategy_ledger
                      WHERE strategy_id = ANY($1)
                    ),
                    0
                  ) AS reserved_usd,
                  COALESCE(
                    (
                      SELECT SUM(
                        ABS(pos_qty * COALESCE(avg_px, 0)) *
                        CASE WHEN symbol LIKE 'OPT:%' THEN 100 ELSE 1 END
                      )
                      FROM core.core_position
                      WHERE strategy_id = ANY($1)
                        AND pos_qty != 0
                    ),
                    0
                  ) AS position_notional_usd
                """,
                strategy_ids,
            )
        except Exception as exc:
            logger.warning(
                "shared_cap_group_query_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                group_id=group_id,
                error=str(exc),
            )
            return None

        reserved_usd = float((row or {}).get("reserved_usd", 0.0) or 0.0)
        position_notional_usd = float((row or {}).get("position_notional_usd", 0.0) or 0.0)
        projected_used = reserved_usd + position_notional_usd + order_notional_usd
        if projected_used <= limit_usd + 1e-9:
            return None

        return {
            "rule": "shared_cap_group_limit",
            "reason": "SHARED_CAP_GROUP_LIMIT_EXCEEDED",
            "group_id": group_id,
            "group_limit_usd": float(limit_usd),
            "group_reserved_usd": float(reserved_usd),
            "group_position_notional_usd": float(position_notional_usd),
            "projected_used_usd": float(projected_used),
            "strategy_ids": strategy_ids,
        }

    async def release(self, idempotency_key: str) -> bool:
        """
        Release reserved capital

        Args:
            idempotency_key: Reservation idempotency key

        Returns:
            True if release succeeded
        """
        return await self.reservation.release(idempotency_key)

    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics"""
        approval_latency_p50 = 0.0
        approval_latency_p95 = 0.0
        approval_latency_p99 = 0.0

        if self.metrics.approval_latency_ms:
            sorted_lat = sorted(self.metrics.approval_latency_ms)
            n = len(sorted_lat)
            approval_latency_p50 = sorted_lat[int(n * 0.5)]
            approval_latency_p95 = sorted_lat[int(n * 0.95)]
            approval_latency_p99 = sorted_lat[int(n * 0.99)]

        return {
            "approvals_total": self.metrics.approvals_total,
            "denials_total": self.metrics.denials_total,
            "reglite_blocks_total": self.metrics.reglite_blocks_total,
            "quota_blocks_total": self.metrics.quota_blocks_total,
            "reservation_blocks_total": self.metrics.reservation_blocks_total,
            "approval_latency_p50_ms": approval_latency_p50,
            "approval_latency_p95_ms": approval_latency_p95,
            "approval_latency_p99_ms": approval_latency_p99,
        }
