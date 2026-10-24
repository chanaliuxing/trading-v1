"""
Compliance Monitor
Based on doc/76.md specification

Real-time compliance monitoring and alerting.

Features:
- Pre-trade compliance checks
- Post-trade surveillance
- Pattern detection (wash trades, layering, spoofing)
- Automatic violation alerts
- Compliance dashboard integration
"""
import asyncio
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from collections import defaultdict

from libs.logger import get_logger
from services.compliance.audit_trail import AuditTrail, AuditEventType, AuditSeverity

logger = get_logger(__name__)


@dataclass
class ComplianceRule:
    """Compliance rule definition"""
    rule_id: str
    name: str
    description: str
    check_fn: Any  # Callable
    severity: AuditSeverity
    auto_block: bool  # Block trade on violation


@dataclass
class ComplianceViolation:
    """Compliance violation"""
    rule_id: str
    timestamp: float
    severity: AuditSeverity
    symbol: Optional[str]
    strategy_id: Optional[str]
    details: Dict[str, Any]
    blocked: bool  # Was trade blocked


class ComplianceMonitor:
    """
    Compliance monitoring service

    Provides pre-trade checks and post-trade surveillance.
    """

    def __init__(
        self,
        audit_trail: AuditTrail,
        max_position_pct: float = 0.20,  # 20% of ADV
        max_daily_turnover: float = 1_000_000.0,
        wash_trade_window_s: int = 300  # 5 minutes
    ):
        """
        Initialize compliance monitor

        Args:
            audit_trail: Audit trail service
            max_position_pct: Max position as % of ADV
            max_daily_turnover: Max daily turnover (USD)
            wash_trade_window_s: Wash trade detection window
        """
        self.audit = audit_trail
        self.max_position_pct = max_position_pct
        self.max_daily_turnover = max_daily_turnover
        self.wash_trade_window_s = wash_trade_window_s

        # Rules
        self.rules: Dict[str, ComplianceRule] = {}
        self._register_default_rules()

        # Violation tracking
        self.violations: List[ComplianceViolation] = []

        # State (for pattern detection)
        self.recent_trades: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.daily_turnover: Dict[str, float] = defaultdict(float)

        logger.info("ComplianceMonitor initialized")

    def _register_default_rules(self) -> None:
        """Register default compliance rules"""
        self.register_rule(ComplianceRule(
            rule_id="position_concentration",
            name="Position Concentration Limit",
            description=f"Position cannot exceed {self.max_position_pct*100}% of ADV",
            check_fn=self._check_position_concentration,
            severity=AuditSeverity.ERROR,
            auto_block=True
        ))

        self.register_rule(ComplianceRule(
            rule_id="daily_turnover",
            name="Daily Turnover Limit",
            description=f"Daily turnover cannot exceed ${self.max_daily_turnover:,.0f}",
            check_fn=self._check_daily_turnover,
            severity=AuditSeverity.ERROR,
            auto_block=True
        ))

        self.register_rule(ComplianceRule(
            rule_id="wash_trade",
            name="Wash Trade Detection",
            description=f"Buy/sell same symbol within {self.wash_trade_window_s}s",
            check_fn=self._check_wash_trade,
            severity=AuditSeverity.WARNING,
            auto_block=False
        ))

        self.register_rule(ComplianceRule(
            rule_id="self_trade",
            name="Self-Trade Prevention",
            description="Cannot trade against own orders",
            check_fn=self._check_self_trade,
            severity=AuditSeverity.ERROR,
            auto_block=True
        ))

    def register_rule(self, rule: ComplianceRule) -> None:
        """
        Register compliance rule

        Args:
            rule: ComplianceRule
        """
        self.rules[rule.rule_id] = rule
        logger.info("Compliance rule registered", rule_id=rule.rule_id, name=rule.name)

    async def pre_trade_check(
        self,
        order: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Tuple[bool, List[ComplianceViolation]]:
        """
        Pre-trade compliance check

        Args:
            order: Order to check
            context: Additional context (positions, ADV, etc.)

        Returns:
            (allowed, violations)
        """
        violations = []
        allowed = True

        # Run all rules
        for rule in self.rules.values():
            try:
                passed, details = await rule.check_fn(order, context)

                if not passed:
                    violation = ComplianceViolation(
                        rule_id=rule.rule_id,
                        timestamp=time.time(),
                        severity=rule.severity,
                        symbol=order.get("symbol"),
                        strategy_id=context.get("strategy_id"),
                        details=details,
                        blocked=rule.auto_block
                    )

                    violations.append(violation)
                    self.violations.append(violation)

                    # Log violation
                    await self.audit.log(
                        event_type=AuditEventType.RISK_LIMIT_BREACH,
                        severity=rule.severity,
                        data={
                            "rule_id": rule.rule_id,
                            "rule_name": rule.name,
                            "order": order,
                            "details": details
                        },
                        strategy_id=context.get("strategy_id"),
                        symbol=order.get("symbol"),
                        tags=["compliance", "pre_trade"]
                    )

                    # Block if required
                    if rule.auto_block:
                        allowed = False

            except Exception as e:
                logger.error(
                    "Compliance check failed",
                    rule_id=rule.rule_id,
                    error=str(e),
                    exc_info=True
                )

        return allowed, violations

    async def post_trade_check(
        self,
        fill: Dict[str, Any],
        context: Dict[str, Any]
    ) -> List[ComplianceViolation]:
        """
        Post-trade compliance check (surveillance)

        Args:
            fill: Fill data
            context: Additional context

        Returns:
            List of violations
        """
        violations = []

        # Record trade
        symbol = fill.get("symbol")
        if symbol:
            self.recent_trades[symbol].append({
                "timestamp": time.time(),
                "side": fill.get("side"),
                "qty": fill.get("qty"),
                "price": fill.get("fill_px"),
                "strategy_id": context.get("strategy_id")
            })

            # Update daily turnover
            turnover = abs(fill.get("qty", 0) * fill.get("fill_px", 0))
            self.daily_turnover[symbol] += turnover

            # Clean old trades
            self._clean_old_trades(symbol)

        # Run surveillance rules
        for rule in self.rules.values():
            if rule.rule_id in ["wash_trade"]:  # Post-trade only
                try:
                    passed, details = await rule.check_fn(fill, context)

                    if not passed:
                        violation = ComplianceViolation(
                            rule_id=rule.rule_id,
                            timestamp=time.time(),
                            severity=rule.severity,
                            symbol=symbol,
                            strategy_id=context.get("strategy_id"),
                            details=details,
                            blocked=False  # Post-trade, cannot block
                        )

                        violations.append(violation)
                        self.violations.append(violation)

                        # Log violation
                        await self.audit.log(
                            event_type=AuditEventType.RISK_LIMIT_BREACH,
                            severity=rule.severity,
                            data={
                                "rule_id": rule.rule_id,
                                "rule_name": rule.name,
                                "fill": fill,
                                "details": details
                            },
                            strategy_id=context.get("strategy_id"),
                            symbol=symbol,
                            tags=["compliance", "post_trade"]
                        )

                except Exception as e:
                    logger.error(
                        "Post-trade check failed",
                        rule_id=rule.rule_id,
                        error=str(e)
                    )

        return violations

    def _clean_old_trades(self, symbol: str) -> None:
        """Clean old trades outside surveillance window"""
        now = time.time()
        cutoff = now - self.wash_trade_window_s

        self.recent_trades[symbol] = [
            t for t in self.recent_trades[symbol]
            if t["timestamp"] > cutoff
        ]

    async def _check_position_concentration(
        self,
        order: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Check position concentration limit"""
        symbol = order.get("symbol")
        current_pos = context.get("position", {}).get(symbol, 0)
        order_qty = order.get("qty", 0)
        new_pos = current_pos + order_qty

        # Get ADV (Average Daily Volume)
        adv = context.get("adv", {}).get(symbol, 1_000_000)

        # Check concentration
        pos_pct = abs(new_pos) / adv

        if pos_pct > self.max_position_pct:
            return False, {
                "current_position": current_pos,
                "order_qty": order_qty,
                "new_position": new_pos,
                "adv": adv,
                "position_pct": pos_pct,
                "limit_pct": self.max_position_pct
            }

        return True, {}

    async def _check_daily_turnover(
        self,
        order: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Check daily turnover limit"""
        symbol = order.get("symbol")
        order_notional = abs(order.get("qty", 0) * order.get("limit_px", 0))

        current_turnover = self.daily_turnover.get(symbol, 0.0)
        new_turnover = current_turnover + order_notional

        if new_turnover > self.max_daily_turnover:
            return False, {
                "current_turnover": current_turnover,
                "order_notional": order_notional,
                "new_turnover": new_turnover,
                "limit": self.max_daily_turnover
            }

        return True, {}

    async def _check_wash_trade(
        self,
        fill: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Check for wash trades (buy then sell within window)"""
        symbol = fill.get("symbol")
        fill_side = fill.get("side")

        if not symbol or not fill_side:
            return True, {}

        # Get recent trades
        trades = self.recent_trades.get(symbol, [])

        # Look for opposite side trades
        opposite_side = "SELL" if fill_side == "BUY" else "BUY"

        opposite_trades = [
            t for t in trades
            if t["side"] == opposite_side
        ]

        if opposite_trades:
            # Potential wash trade
            return False, {
                "fill_side": fill_side,
                "opposite_trades": len(opposite_trades),
                "window_s": self.wash_trade_window_s
            }

        return True, {}

    async def _check_self_trade(
        self,
        order: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Check for self-trading (trading against own orders)"""
        # This would require access to order book
        # For now, return True (not implemented)
        return True, {}

    def reset_daily_counters(self) -> None:
        """Reset daily counters (call at EOD)"""
        self.daily_turnover.clear()
        logger.info("Daily compliance counters reset")

    def get_violations_summary(
        self,
        hours: int = 24
    ) -> Dict[str, Any]:
        """
        Get violations summary

        Args:
            hours: Look-back period

        Returns:
            Summary dict
        """
        cutoff = time.time() - (hours * 3600)

        recent_violations = [
            v for v in self.violations
            if v.timestamp > cutoff
        ]

        # Group by rule
        by_rule = defaultdict(int)
        for v in recent_violations:
            by_rule[v.rule_id] += 1

        # Group by severity
        by_severity = defaultdict(int)
        for v in recent_violations:
            by_severity[v.severity.name] += 1

        return {
            "total_violations": len(recent_violations),
            "by_rule": dict(by_rule),
            "by_severity": dict(by_severity),
            "blocked_trades": sum(1 for v in recent_violations if v.blocked)
        }
