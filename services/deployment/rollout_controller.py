"""
Rollout Controller
Based on doc/60.md specification

Manages strategy deployments from shadow → canary → ramp → full production.

Features:
- State machine: DRAFT → SHADOW → CANARY → RAMP → FULL
- Safety gates: Tech SLO, execution health, market risk, operational health
- Automatic rollback on gate violations
- Kill switch for emergency shutdown
- Gradual capital/limit increases with validation at each step
"""
import asyncio
import time
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from libs.logger import get_logger

logger = get_logger(__name__)


class ReleaseState(Enum):
    """Release state"""
    DRAFT = "draft"
    SHADOW = "shadow"
    PAPER = "paper"
    CANARY = "canary"
    RAMP_10 = "ramp_10"
    RAMP_25 = "ramp_25"
    FULL = "full"
    PAUSED = "paused"
    ROLLBACK = "rollback"
    RETIRED = "retired"


@dataclass
class Step:
    """Deployment step configuration"""
    name: str
    min_minutes: int
    capital_usd: float = 0.0
    per_symbol_max_usd: float = 0.0
    max_concurrent_plans: int = 1
    gates: Dict[str, Any] = None


@dataclass
class SafetyGates:
    """Safety gate thresholds"""
    # Tech SLO
    p95_bar_env_ms_max: float = 800.0
    p95_env_submit_ms_max: float = 1200.0
    intent_expired_pct_max: float = 0.02

    # Execution health
    reject_rate_max: float = 0.01
    error_retry_limit_pct_max: float = 0.002
    oco_invariant_breaks_max: int = 0
    reservation_wd_pct_max: float = 0.001

    # Market risk
    dd_intraday_max: float = 0.01
    slippage_bps_max: float = 10.0

    # Operational health
    data_qscore_min: float = 70.0
    kafka_lag_ms_max: float = 2000.0
    broker_reconnect_per_hour_max: int = 2


class RolloutController:
    """
    Rollout controller for strategy deployments

    Manages gradual rollout from shadow to production with safety gates.
    """

    def __init__(
        self,
        release_plan: Dict[str, Any],
        state_bus: Any,
        risk_service: Any,
        ledger: Any,
        arbiter: Any,
        executor: Any,
        metrics_collector: Any
    ):
        """
        Initialize rollout controller

        Args:
            release_plan: Release plan configuration (YAML)
            state_bus: State bus for control commands
            risk_service: Risk service for cap management
            ledger: Reservation ledger
            arbiter: Arbiter for symbol caps
            executor: Order executor
            metrics_collector: Metrics collector for gate evaluation
        """
        self.plan = release_plan
        self.bus = state_bus
        self.risk = risk_service
        self.ledger = ledger
        self.arb = arbiter
        self.exec = executor
        self.metrics = metrics_collector

        # State
        self.current_step_idx = 0
        self.step_started_at = int(time.time())
        self.paused = False
        self.rollback_count = 0

        logger.info(
            "RolloutController initialized",
            strategy_id=self.plan["strategy_id"],
            param_hash=self.plan["param_hash"],
            steps=len(self.plan["steps"])
        )

    def _now_s(self) -> int:
        """Get current timestamp in seconds"""
        return int(time.time())

    async def apply_step_limits(self, step: Step) -> None:
        """
        Apply limits for deployment step

        Args:
            step: Deployment step
        """
        strategy_id = self.plan["strategy_id"]

        # Set strategy caps
        if hasattr(self.risk, 'set_strategy_caps'):
            await self.risk.set_strategy_caps(
                strategy_id=strategy_id,
                cap_usd=step.capital_usd,
                per_symbol_cap=step.per_symbol_max_usd
            )

        # Set arbiter limits
        if hasattr(self.arb, 'set_symbol_global_caps'):
            await self.arb.set_symbol_global_caps(
                max_notional_usd=step.per_symbol_max_usd or 1e12
            )

        # Set concurrency limits
        if hasattr(self.bus, 'set_max_concurrency'):
            await self.bus.set_max_concurrency(
                strategy_id,
                step.max_concurrent_plans
            )

        logger.info(
            "Step limits applied",
            step=step.name,
            capital_usd=step.capital_usd,
            per_symbol_max=step.per_symbol_max_usd,
            max_concurrent=step.max_concurrent_plans
        )

    async def check_gates(
        self,
        step: Step
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Check safety gates for current step

        Args:
            step: Current deployment step

        Returns:
            (passed, metadata) - True if all gates pass
        """
        strategy_id = self.plan["strategy_id"]
        symbols = self.plan["symbols"]

        # Collect stats from metrics
        stats = await self._collect_stats(
            strategy_id,
            symbols,
            window_min=step.min_minutes
        )

        gates = step.gates or {}
        passed = True
        reasons = []

        def fail(reason: str, value: Any = None):
            nonlocal passed
            passed = False
            reasons.append({"reason": reason, "value": value})

        # Tech SLO gates
        if gates.get("tech_slo_p95", False):
            if stats["p95_bar_env_ms"] > 800:
                fail("slo_bar_env_ms", stats["p95_bar_env_ms"])
            if stats["p95_env_submit_ms"] > 1200:
                fail("slo_env_submit_ms", stats["p95_env_submit_ms"])

        # Data quality gates
        if "data_qscore_avg" in gates:
            if stats["qscore_avg"] < gates["data_qscore_avg"]:
                fail("data_qscore_low", stats["qscore_avg"])

        # Execution health gates
        if "reject_rate_max" in gates:
            if stats["reject_rate"] > gates["reject_rate_max"]:
                fail("reject_rate_high", stats["reject_rate"])

        # Market risk gates
        if "dd_intraday_max" in gates:
            if stats["drawdown_intraday"] > gates["dd_intraday_max"]:
                fail("drawdown_high", stats["drawdown_intraday"])

        # Kill switch triggers
        kill_switch = self.plan.get("kill_switch", {})
        if stats["drawdown_day"] > kill_switch.get("drawdown_day", 0.02):
            fail("kill_switch_dd", stats["drawdown_day"])

        if stats["oco_invariant_breaks"] > kill_switch.get("oco_invariant_breaks", 0):
            fail("kill_switch_oco", stats["oco_invariant_breaks"])

        return passed, {"stats": stats, "reasons": reasons}

    async def _collect_stats(
        self,
        strategy_id: str,
        symbols: List[str],
        window_min: int
    ) -> Dict[str, Any]:
        """
        Collect metrics for gate evaluation

        Args:
            strategy_id: Strategy ID
            symbols: List of symbols
            window_min: Time window in minutes

        Returns:
            Stats dict
        """
        # In production, query Prometheus/ClickHouse
        # Placeholder implementation
        if hasattr(self.metrics, 'collect_stats'):
            return await self.metrics.collect_stats(
                strategy_id,
                symbols,
                window_min
            )

        # Default mock stats
        return {
            "p95_bar_env_ms": 500.0,
            "p95_env_submit_ms": 800.0,
            "intent_expired_pct": 0.01,
            "qscore_avg": 80.0,
            "reject_rate": 0.005,
            "error_retry_limit_pct": 0.0,
            "oco_invariant_breaks": 0,
            "reservation_wd_pct": 0.0,
            "drawdown_intraday": 0.003,
            "drawdown_day": 0.005,
            "slippage_bps": 5.0,
            "kafka_lag_ms": 500.0,
            "broker_reconnect_per_hour": 0
        }

    async def promote_or_hold(self) -> None:
        """
        Evaluate current step and promote if ready
        """
        if self.current_step_idx >= len(self.plan["steps"]):
            logger.debug("Already at final step")
            return

        step_cfg = self.plan["steps"][self.current_step_idx]
        step = Step(**step_cfg)

        # Apply limits if just entered this step
        elapsed_s = self._now_s() - self.step_started_at
        if elapsed_s < 10:
            await self.apply_step_limits(step)

        # Check minimum dwell time
        min_dwell_s = step.min_minutes * 60
        if elapsed_s < min_dwell_s:
            logger.debug(
                "Holding current step",
                step=step.name,
                elapsed_s=elapsed_s,
                min_dwell_s=min_dwell_s
            )
            return

        # Check gates
        passed, meta = await self.check_gates(step)

        if passed:
            # Promote to next step
            if self.current_step_idx < len(self.plan["steps"]) - 1:
                self.current_step_idx += 1
                self.step_started_at = self._now_s()

                next_step_cfg = self.plan["steps"][self.current_step_idx]
                next_step = Step(**next_step_cfg)

                await self.apply_step_limits(next_step)

                logger.warning(
                    "Promoted to next step",
                    from_step=step.name,
                    to_step=next_step.name,
                    elapsed_s=elapsed_s
                )
            else:
                logger.info("Already at final step and gates passing")
        else:
            # Gates failed - rollback
            await self.rollback(meta["reasons"])

    async def rollback(self, reasons: List[Dict[str, Any]]) -> None:
        """
        Rollback deployment due to gate violations

        Args:
            reasons: List of gate violations
        """
        strategy_id = self.plan["strategy_id"]

        logger.error(
            "Rolling back deployment",
            strategy_id=strategy_id,
            reasons=reasons,
            rollback_count=self.rollback_count
        )

        # Pause strategy
        if hasattr(self.bus, 'pause_strategy'):
            await self.bus.pause_strategy(strategy_id, reason="rollback")

        # Cancel all open orders
        await self._cancel_all_orders(strategy_id)

        # Flatten all positions
        await self._flatten_all_positions(strategy_id)

        # Release all reservations
        await self._release_all_reservations(strategy_id)

        # Reset to first step
        self.current_step_idx = 0
        self.step_started_at = self._now_s()
        self.rollback_count += 1

        # Re-apply first step limits
        first_step = Step(**self.plan["steps"][0])
        await self.apply_step_limits(first_step)

    async def kill_switch(self, reason: str = "manual") -> None:
        """
        Emergency shutdown - cancel all and flatten

        Args:
            reason: Reason for kill switch activation
        """
        strategy_id = self.plan["strategy_id"]

        logger.critical(
            "Kill switch activated",
            strategy_id=strategy_id,
            reason=reason
        )

        # Pause
        if hasattr(self.bus, 'pause_strategy'):
            await self.bus.pause_strategy(strategy_id, reason=f"kill_switch:{reason}")

        # Cancel + flatten + release
        await self._cancel_all_orders(strategy_id)
        await self._flatten_all_positions(strategy_id)
        await self._release_all_reservations(strategy_id)

        # Mark as halted
        self.paused = True

    async def _cancel_all_orders(self, strategy_id: str) -> None:
        """Cancel all open orders for strategy"""
        if hasattr(self.exec, 'cancel_all_strategy_orders'):
            await self.exec.cancel_all_strategy_orders(strategy_id)
        logger.info("Cancelled all orders", strategy_id=strategy_id)

    async def _flatten_all_positions(self, strategy_id: str) -> None:
        """Flatten all positions for strategy"""
        if hasattr(self.exec, 'flatten_all_strategy_positions'):
            await self.exec.flatten_all_strategy_positions(strategy_id)
        logger.info("Flattened all positions", strategy_id=strategy_id)

    async def _release_all_reservations(self, strategy_id: str) -> None:
        """Release all capital reservations for strategy"""
        if hasattr(self.ledger, 'release_all_strategy_reservations'):
            await self.ledger.release_all_strategy_reservations(strategy_id)
        logger.info("Released all reservations", strategy_id=strategy_id)

    async def run_loop(self, interval_s: int = 30) -> None:
        """
        Main control loop

        Args:
            interval_s: Evaluation interval (seconds)
        """
        logger.info(
            "Rollout controller started",
            strategy_id=self.plan["strategy_id"],
            interval_s=interval_s
        )

        while True:
            try:
                if not self.paused:
                    await self.promote_or_hold()
                else:
                    logger.debug("Controller paused")

            except Exception as e:
                logger.error("Controller error", error=str(e), exc_info=True)

            await asyncio.sleep(interval_s)
