#!/usr/bin/env python3
"""
Canary Deployment Controller

Gradual rollout: shadow → canary → ramp → full
Based on PRODUCTION_READINESS.md requirements.

Stages:
- Canary: 5% capital, 2 symbols, 1 week
- Ramp25: 25% capital, 10 symbols, 1 week
- Ramp50: 50% capital, 20 symbols, 1 week
- Full: 100% capital, all symbols

Auto-rollback on error rate > 1% or latency > p95 threshold.
"""
import sys
import time
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum

import asyncpg
import aiohttp

from libs.logger import get_logger

logger = get_logger(__name__)


class DeploymentStage(Enum):
    """Deployment stage"""
    SHADOW = "shadow"
    CANARY = "canary"
    RAMP_25 = "ramp25"
    RAMP_50 = "ramp50"
    FULL = "full"


@dataclass
class StageConfig:
    """Stage configuration"""
    name: str
    capital_pct: int
    symbols: int
    duration_days: int
    error_threshold_pct: float = 1.0
    latency_p95_threshold_ms: float = 500.0


@dataclass
class StageMetrics:
    """Stage performance metrics"""
    stage: str
    error_rate_pct: float
    latency_p95_ms: float
    fill_rate_pct: float
    total_orders: int
    total_fills: int
    total_errors: int
    uptime_pct: float
    pnl_usd: float


@dataclass
class RolloutDecision:
    """Rollout decision"""
    current_stage: str
    next_stage: Optional[str]
    action: str  # promote, rollback, hold
    reason: str
    metrics: StageMetrics


class CanaryRolloutController:
    """
    Canary Deployment Controller

    Manages gradual rollout with automatic promotion/rollback.
    """

    def __init__(
        self,
        db_pool: asyncpg.Pool,
        prometheus_url: str = "http://localhost:9090"
    ):
        """
        Initialize canary controller

        Args:
            db_pool: PostgreSQL connection pool
            prometheus_url: Prometheus URL for metrics
        """
        self.db = db_pool
        self.prometheus_url = prometheus_url

        # Define rollout stages
        self.stages = {
            DeploymentStage.SHADOW: StageConfig(
                name="shadow",
                capital_pct=0,
                symbols=5,
                duration_days=14,
                error_threshold_pct=0.0,  # No errors in shadow
                latency_p95_threshold_ms=1000.0
            ),
            DeploymentStage.CANARY: StageConfig(
                name="canary",
                capital_pct=5,
                symbols=2,
                duration_days=7,
                error_threshold_pct=1.0,
                latency_p95_threshold_ms=500.0
            ),
            DeploymentStage.RAMP_25: StageConfig(
                name="ramp25",
                capital_pct=25,
                symbols=10,
                duration_days=7,
                error_threshold_pct=0.5,
                latency_p95_threshold_ms=400.0
            ),
            DeploymentStage.RAMP_50: StageConfig(
                name="ramp50",
                capital_pct=50,
                symbols=20,
                duration_days=7,
                error_threshold_pct=0.3,
                latency_p95_threshold_ms=400.0
            ),
            DeploymentStage.FULL: StageConfig(
                name="full",
                capital_pct=100,
                symbols=0,  # All symbols
                duration_days=0,
                error_threshold_pct=0.2,
                latency_p95_threshold_ms=300.0
            )
        }

        logger.info("Canary rollout controller initialized", stages=list(self.stages.keys()))

    async def get_current_stage(self) -> Optional[DeploymentStage]:
        """Get current deployment stage"""
        query = """
            SELECT stage, started_at
            FROM deployment_stages
            WHERE active = true
            ORDER BY started_at DESC
            LIMIT 1
        """
        row = await self.db.fetchrow(query)

        if not row:
            return None

        return DeploymentStage(row["stage"])

    async def evaluate_rollout(self) -> RolloutDecision:
        """
        Evaluate current stage and decide next action

        Returns:
            RolloutDecision with promotion/rollback/hold action
        """
        current_stage = await self.get_current_stage()

        if not current_stage:
            logger.info("No active stage, starting with SHADOW")
            return RolloutDecision(
                current_stage="none",
                next_stage=DeploymentStage.SHADOW.value,
                action="promote",
                reason="initial_deployment",
                metrics=None
            )

        # Get stage config and metrics
        config = self.stages[current_stage]
        metrics = await self._collect_metrics(current_stage)

        # Check if stage duration completed
        stage_start = await self._get_stage_start_time(current_stage)
        duration_days = (datetime.utcnow() - stage_start).days

        if duration_days < config.duration_days:
            logger.info(
                "Stage duration not completed",
                stage=current_stage.value,
                duration_days=duration_days,
                required_days=config.duration_days
            )
            return RolloutDecision(
                current_stage=current_stage.value,
                next_stage=None,
                action="hold",
                reason=f"duration_incomplete ({duration_days}/{config.duration_days} days)",
                metrics=metrics
            )

        # Check metrics
        if metrics.error_rate_pct > config.error_threshold_pct:
            logger.error(
                "Error rate exceeds threshold, rolling back",
                stage=current_stage.value,
                error_rate=metrics.error_rate_pct,
                threshold=config.error_threshold_pct
            )
            return RolloutDecision(
                current_stage=current_stage.value,
                next_stage=self._get_previous_stage(current_stage),
                action="rollback",
                reason=f"error_rate_high ({metrics.error_rate_pct:.2f}% > {config.error_threshold_pct}%)",
                metrics=metrics
            )

        if metrics.latency_p95_ms > config.latency_p95_threshold_ms:
            logger.error(
                "Latency exceeds threshold, rolling back",
                stage=current_stage.value,
                latency_p95=metrics.latency_p95_ms,
                threshold=config.latency_p95_threshold_ms
            )
            return RolloutDecision(
                current_stage=current_stage.value,
                next_stage=self._get_previous_stage(current_stage),
                action="rollback",
                reason=f"latency_high ({metrics.latency_p95_ms:.0f}ms > {config.latency_p95_threshold_ms:.0f}ms)",
                metrics=metrics
            )

        # Promote to next stage
        next_stage = self._get_next_stage(current_stage)

        if next_stage:
            logger.info(
                "Metrics passed, promoting to next stage",
                current_stage=current_stage.value,
                next_stage=next_stage
            )
            return RolloutDecision(
                current_stage=current_stage.value,
                next_stage=next_stage,
                action="promote",
                reason="metrics_passed",
                metrics=metrics
            )
        else:
            logger.info("Already at final stage", stage=current_stage.value)
            return RolloutDecision(
                current_stage=current_stage.value,
                next_stage=None,
                action="hold",
                reason="final_stage",
                metrics=metrics
            )

    async def execute_rollout_decision(self, decision: RolloutDecision):
        """
        Execute rollout decision

        Args:
            decision: Rollout decision
        """
        if decision.action == "promote":
            await self._promote_stage(decision.next_stage)

        elif decision.action == "rollback":
            await self._rollback_stage(decision.next_stage)

        elif decision.action == "hold":
            logger.info("Holding current stage", stage=decision.current_stage)

    async def _promote_stage(self, next_stage: str):
        """Promote to next stage"""
        logger.info("Promoting to next stage", next_stage=next_stage)

        # Deactivate current stage
        await self.db.execute("UPDATE deployment_stages SET active = false WHERE active = true")

        # Activate next stage
        query = """
            INSERT INTO deployment_stages (stage, started_at, active, capital_pct, symbols)
            VALUES ($1, $2, true, $3, $4)
        """

        stage_enum = DeploymentStage(next_stage)
        config = self.stages[stage_enum]

        await self.db.execute(
            query,
            next_stage,
            datetime.utcnow(),
            config.capital_pct,
            config.symbols
        )

        # Update system configuration
        await self._update_system_config(config)

        logger.info("Stage promoted", stage=next_stage, config=config)

    async def _rollback_stage(self, prev_stage: str):
        """Rollback to previous stage"""
        logger.error("Rolling back to previous stage", prev_stage=prev_stage)

        # Deactivate current stage
        await self.db.execute("UPDATE deployment_stages SET active = false WHERE active = true")

        # Activate previous stage
        query = """
            INSERT INTO deployment_stages (stage, started_at, active, capital_pct, symbols)
            VALUES ($1, $2, true, $3, $4)
        """

        stage_enum = DeploymentStage(prev_stage) if prev_stage else DeploymentStage.SHADOW
        config = self.stages[stage_enum]

        await self.db.execute(
            query,
            prev_stage or "shadow",
            datetime.utcnow(),
            config.capital_pct,
            config.symbols
        )

        # Update system configuration
        await self._update_system_config(config)

        logger.info("Stage rolled back", stage=prev_stage, config=config)

    async def _update_system_config(self, config: StageConfig):
        """Update system configuration for stage"""
        # Update capital allocation
        await self.db.execute(
            "UPDATE capital_config SET allocation_pct = $1",
            config.capital_pct
        )

        # Update symbol whitelist
        if config.symbols > 0:
            query = """
                UPDATE symbol_config
                SET enabled = (
                    SELECT id IN (
                        SELECT id FROM symbol_config
                        ORDER BY priority DESC
                        LIMIT $1
                    )
                )
            """
            await self.db.execute(query, config.symbols)

        logger.info("System config updated", config=config)

    async def _collect_metrics(self, stage: DeploymentStage) -> StageMetrics:
        """Collect metrics for stage"""
        stage_start = await self._get_stage_start_time(stage)
        stage_start_ts = int(stage_start.timestamp())

        async with aiohttp.ClientSession() as session:
            # Error rate
            error_rate = await self._query_prometheus(
                session,
                f'rate(exec_order_errors_total[5m])',
                stage_start_ts
            )

            # Latency P95
            latency_p95 = await self._query_prometheus(
                session,
                f'histogram_quantile(0.95, rate(exec_latency_ms_bucket[5m]))',
                stage_start_ts
            )

            # Fill rate
            fill_rate = await self._query_prometheus(
                session,
                f'rate(exec_fills_total[5m]) / rate(exec_orders_submitted_total[5m])',
                stage_start_ts
            )

            # Order counts
            total_orders = await self._query_prometheus_counter(
                session,
                'exec_orders_submitted_total',
                stage_start_ts
            )

            total_fills = await self._query_prometheus_counter(
                session,
                'exec_fills_total',
                stage_start_ts
            )

            total_errors = await self._query_prometheus_counter(
                session,
                'exec_order_errors_total',
                stage_start_ts
            )

            # Uptime
            uptime = await self._query_prometheus(
                session,
                'avg_over_time(up[1h])',
                stage_start_ts
            )

        # Get PnL from database
        pnl = await self._get_stage_pnl(stage)

        return StageMetrics(
            stage=stage.value,
            error_rate_pct=(error_rate or 0) * 100,
            latency_p95_ms=latency_p95 or 0,
            fill_rate_pct=(fill_rate or 0) * 100,
            total_orders=int(total_orders or 0),
            total_fills=int(total_fills or 0),
            total_errors=int(total_errors or 0),
            uptime_pct=(uptime or 0) * 100,
            pnl_usd=pnl
        )

    async def _query_prometheus(
        self,
        session: aiohttp.ClientSession,
        query: str,
        start_ts: int
    ) -> Optional[float]:
        """Query Prometheus for metric"""
        try:
            url = f"{self.prometheus_url}/api/v1/query"
            params = {"query": query, "time": int(time.time())}

            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data["data"]["result"]:
                        return float(data["data"]["result"][0]["value"][1])
        except Exception as e:
            logger.error("Prometheus query failed", query=query, error=str(e))

        return None

    async def _query_prometheus_counter(
        self,
        session: aiohttp.ClientSession,
        metric: str,
        start_ts: int
    ) -> Optional[float]:
        """Query Prometheus counter increase"""
        query = f'increase({metric}[1h])'
        return await self._query_prometheus(session, query, start_ts)

    async def _get_stage_start_time(self, stage: DeploymentStage) -> datetime:
        """Get stage start time"""
        query = """
            SELECT started_at
            FROM deployment_stages
            WHERE stage = $1 AND active = true
            LIMIT 1
        """
        row = await self.db.fetchrow(query, stage.value)
        return row["started_at"] if row else datetime.utcnow()

    async def _get_stage_pnl(self, stage: DeploymentStage) -> float:
        """Get stage PnL"""
        stage_start = await self._get_stage_start_time(stage)

        query = """
            SELECT SUM(realized_pnl) as total_pnl
            FROM fills
            WHERE ts_fill >= $1
        """
        row = await self.db.fetchrow(query, stage_start)
        return float(row["total_pnl"]) if row and row["total_pnl"] else 0.0

    def _get_next_stage(self, current: DeploymentStage) -> Optional[str]:
        """Get next deployment stage"""
        stages = [
            DeploymentStage.SHADOW,
            DeploymentStage.CANARY,
            DeploymentStage.RAMP_25,
            DeploymentStage.RAMP_50,
            DeploymentStage.FULL
        ]

        try:
            idx = stages.index(current)
            if idx < len(stages) - 1:
                return stages[idx + 1].value
        except ValueError:
            pass

        return None

    def _get_previous_stage(self, current: DeploymentStage) -> Optional[str]:
        """Get previous deployment stage"""
        stages = [
            DeploymentStage.SHADOW,
            DeploymentStage.CANARY,
            DeploymentStage.RAMP_25,
            DeploymentStage.RAMP_50,
            DeploymentStage.FULL
        ]

        try:
            idx = stages.index(current)
            if idx > 0:
                return stages[idx - 1].value
        except ValueError:
            pass

        return DeploymentStage.SHADOW.value


async def run_rollout_check(db_pool: asyncpg.Pool):
    """
    Run rollout check (called by scheduler)

    Should run daily to evaluate stage progression.
    """
    controller = CanaryRolloutController(db_pool)

    # Evaluate rollout
    decision = await controller.evaluate_rollout()

    logger.info(
        "Rollout evaluation complete",
        current_stage=decision.current_stage,
        next_stage=decision.next_stage,
        action=decision.action,
        reason=decision.reason
    )

    # Execute decision
    await controller.execute_rollout_decision(decision)

    # Log metrics
    if decision.metrics:
        logger.info(
            "Stage metrics",
            stage=decision.metrics.stage,
            error_rate_pct=decision.metrics.error_rate_pct,
            latency_p95_ms=decision.metrics.latency_p95_ms,
            fill_rate_pct=decision.metrics.fill_rate_pct,
            uptime_pct=decision.metrics.uptime_pct,
            pnl_usd=decision.metrics.pnl_usd
        )


async def main():
    """Main entry point for manual rollout check"""
    import asyncpg

    db = await asyncpg.create_pool(
        host="localhost",
        port=5432,
        user="postgres",
        password="postgres",
        database="test"
    )

    await run_rollout_check(db)
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
