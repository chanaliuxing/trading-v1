"""
Hedge Intent Executor - Bridge between AI Brain and HedgeExecutor.

This module consumes hedge_intent from DailyPlan and translates it into
execution via the HedgeExecutor service.

Flow:
1. Brain generates DailyPlan with hedge_intent
2. Orchestrator calls process_hedge_intent()
3. Convert hedge_intent → exposure + delta_gap
4. Call HedgeExecutor.execute()
5. Log results and write audit trail
"""

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.db.clickhouse import ClickHouseDB
from llm_trader.plan_types import HedgeIntent
from services.risk.hedge_executor import HedgeExecutor
from scripts.ai_trader.risk.options_selector import HedgeIntent as OptionsHedgeIntent

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


class HedgeIntentExecutor:
    """
    Processes hedge_intent from Brain and executes via HedgeExecutor.

    This bridges the AI brain's strategic hedge decisions with the
    quantitative hedge execution system.
    """

    def __init__(
        self,
        hedge_executor: Optional[HedgeExecutor] = None,
        runs_dir: Optional[Path] = None,
    ):
        """Initialize hedge intent executor.

        Args:
            hedge_executor: HedgeExecutor instance (created if None)
            runs_dir: Directory for audit trail (default: runs/ai_trader)
        """
        self.hedge_executor = hedge_executor or HedgeExecutor()
        self.runs_dir = runs_dir or Path("runs/ai_trader")
        self.ch = ClickHouseDB()

        logger.info(
            "HedgeIntentExecutor initialized",
            executor_enabled=self.hedge_executor.enabled,
            dry_run=self.hedge_executor.dry_run,
            runs_dir=str(self.runs_dir),
        )

    async def process_hedge_intent(
        self,
        hedge_intent: Optional[HedgeIntent],
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Process hedge intent from brain and execute.

        Args:
            hedge_intent: HedgeIntent from DailyPlan (can be None)
            force: Force execution even if intent is None (uses current exposure)

        Returns:
            Dict with execution results
        """
        if hedge_intent is None and not force:
            logger.info("hedge_intent_skip", reason="No hedge intent in plan")
            return {
                "success": False,
                "skipped": True,
                "reason": "No hedge intent provided",
            }

        # Get current portfolio exposure
        exposure = await self._get_portfolio_exposure(
            symbols=hedge_intent.symbols if hedge_intent else None
        )

        if exposure["gross"] <= 0:
            logger.info("hedge_intent_skip", reason="No portfolio exposure")
            return {
                "success": False,
                "skipped": True,
                "reason": "No portfolio exposure",
                "exposure": exposure,
            }

        # Calculate target hedge delta based on intent
        if hedge_intent:
            # Brain specified notional_pct (0.0-1.0)
            target_notional_pct = hedge_intent.notional_pct
            target_hedge_delta = -1.0 * exposure["net"] * target_notional_pct

            # Convert brain HedgeIntent to options HedgeIntent
            options_intent = self._convert_to_options_intent(hedge_intent)
        else:
            # Force mode: use default 50% hedge
            target_hedge_delta = -1.0 * exposure["net"] * 0.5
            options_intent = None

        # Get current hedge position
        current_hedge_delta = await self._get_current_hedge_delta()

        # Calculate delta gap
        delta_gap = target_hedge_delta - current_hedge_delta

        logger.info(
            "hedge_intent_processing",
            symbols=hedge_intent.symbols if hedge_intent else None,
            notional_pct=hedge_intent.notional_pct if hedge_intent else 0.5,
            urgency=hedge_intent.urgency if hedge_intent else "medium",
            strategy=hedge_intent.strategy if hedge_intent else "options_put",
            net_exposure=exposure["net"],
            gross_exposure=exposure["gross"],
            target_hedge_delta=target_hedge_delta,
            current_hedge_delta=current_hedge_delta,
            delta_gap=delta_gap,
        )

        # Execute via HedgeExecutor
        result = await self.hedge_executor.execute(
            delta_gap_usd=delta_gap,
            exposure=exposure,
            intent=options_intent,
        )

        # Write audit trail
        await self._write_audit_trail(
            hedge_intent=hedge_intent,
            exposure=exposure,
            delta_gap=delta_gap,
            result=result,
        )

        logger.info(
            "hedge_intent_result",
            success=result.success,
            action=result.action,
            reason=result.reason,
            qty=result.qty,
            notional=result.notional_usd,
        )

        return {
            "success": result.success,
            "action": result.action,
            "reason": result.reason,
            "contract": result.contract,
            "qty": result.qty,
            "notional_usd": result.notional_usd,
            "exposure": exposure,
            "delta_gap": delta_gap,
            "error": result.error,
        }

    async def _get_portfolio_exposure(
        self,
        symbols: Optional[list] = None,
    ) -> Dict[str, float]:
        """
        Get current portfolio exposure from ClickHouse.

        Args:
            symbols: Optional list of symbols to filter (if None, all symbols)

        Returns:
            Dict with net and gross exposure
        """
        try:
            # Query recent positions
            if symbols:
                symbol_filter = f"AND symbol IN {tuple(symbols)}"
            else:
                symbol_filter = ""

            sql = f"""
            SELECT
                symbol,
                SUM(pnl_pos_5s) as position_usd
            FROM trading.signals_realtime
            WHERE ts >= now() - INTERVAL 60 SECOND
              AND strategy_id != 'sys_hedger'
              {symbol_filter}
            GROUP BY symbol
            """

            rows = self.ch.query(sql)

            if not rows:
                return {"net": 0.0, "gross": 0.0}

            net_delta = sum(float(row.get("position_usd", 0.0)) for row in rows)
            gross_delta = sum(abs(float(row.get("position_usd", 0.0))) for row in rows)

            return {"net": net_delta, "gross": gross_delta}

        except Exception as e:
            logger.error("get_portfolio_exposure_error", error=str(e), exc_info=True)
            return {"net": 0.0, "gross": 0.0}

    async def _get_current_hedge_delta(self) -> float:
        """Get current hedge position delta from sys_hedger positions."""
        try:
            sql = """
            SELECT SUM(pnl_pos_5s) as hedge_delta
            FROM trading.signals_realtime
            WHERE ts >= now() - INTERVAL 60 SECOND
              AND strategy_id = 'sys_hedger'
            """

            rows = self.ch.query(sql)

            if not rows or not rows[0]:
                return 0.0

            return float(rows[0].get("hedge_delta", 0.0))

        except Exception as e:
            logger.error("get_current_hedge_delta_error", error=str(e), exc_info=True)
            return 0.0

    def _convert_to_options_intent(
        self,
        brain_intent: HedgeIntent,
    ) -> OptionsHedgeIntent:
        """
        Convert brain HedgeIntent to options HedgeIntent.

        Args:
            brain_intent: HedgeIntent from llm_trader.plan_types

        Returns:
            OptionsHedgeIntent from scripts.ai_trader.risk.options_selector
        """
        # Map brain strategy to intent type
        strategy_map = {
            "options_put": "drawdown_protect",
            "options_collar": "vol_spike",
            "stock_short": "beta_balance",
        }

        intent_type = strategy_map.get(
            brain_intent.strategy,
            "drawdown_protect"
        )

        # Map urgency to size
        urgency_to_size = {
            "low": "small",
            "medium": "medium",
            "high": "large",
        }

        size = urgency_to_size.get(brain_intent.urgency, "medium")

        return OptionsHedgeIntent(
            intent_type=intent_type,
            instrument=brain_intent.instrument,  # SPY/QQQ or None for auto
            size=size,
            urgency=brain_intent.urgency,
            note=f"Brain confidence: {brain_intent.confidence}, max_cost: {brain_intent.max_cost_bps}bps",
        )

    async def _write_audit_trail(
        self,
        hedge_intent: Optional[HedgeIntent],
        exposure: Dict[str, float],
        delta_gap: float,
        result: Any,
    ):
        """Write hedge execution audit trail to JSONL file."""
        try:
            today = datetime.now(ET).date().isoformat()
            audit_dir = self.runs_dir / today
            audit_dir.mkdir(parents=True, exist_ok=True)

            audit_file = audit_dir / "hedge_intent_audit.jsonl"

            audit_record = {
                "ts": datetime.now(ET).isoformat(),
                "intent": asdict(hedge_intent) if hedge_intent else None,
                "exposure": exposure,
                "delta_gap": delta_gap,
                "result": {
                    "success": result.success,
                    "action": result.action,
                    "reason": result.reason,
                    "qty": result.qty,
                    "notional_usd": result.notional_usd,
                    "contract": result.contract,
                    "error": result.error,
                },
            }

            with open(audit_file, "a") as f:
                f.write(json.dumps(audit_record) + "\n")

            logger.debug("hedge_audit_written", file=str(audit_file))

        except Exception as e:
            logger.warning("write_audit_trail_error", error=str(e))


async def execute_from_plan_file(
    plan_file: Path,
    hedge_executor: Optional[HedgeExecutor] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Execute hedge from DailyPlan JSON file.

    This is a convenience function for scripts that need to load
    a plan file and execute the hedge intent.

    Args:
        plan_file: Path to plan JSON file
        hedge_executor: Optional HedgeExecutor instance
        force: Force execution even if no hedge_intent

    Returns:
        Execution result dict
    """
    try:
        plan_data = json.loads(plan_file.read_text())
        hedge_intent_data = plan_data.get("hedge_intent")

        if hedge_intent_data:
            hedge_intent = HedgeIntent(**hedge_intent_data)
        else:
            hedge_intent = None

        executor = HedgeIntentExecutor(hedge_executor=hedge_executor)
        return await executor.process_hedge_intent(
            hedge_intent=hedge_intent,
            force=force,
        )

    except Exception as e:
        logger.error("execute_from_plan_file_error", file=str(plan_file), error=str(e), exc_info=True)
        return {
            "success": False,
            "error": str(e),
        }


# CLI interface
async def main():
    """CLI entry point for manual hedge intent execution."""
    import argparse

    parser = argparse.ArgumentParser(description="Execute hedge from plan file")
    parser.add_argument("--plan-file", type=Path, required=True, help="Path to plan JSON file")
    parser.add_argument("--force", action="store_true", help="Force execution even without hedge_intent")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run mode (override config)")
    args = parser.parse_args()

    # Create executor
    executor = HedgeExecutor()
    if args.dry_run:
        executor.dry_run = True

    # Execute
    result = await execute_from_plan_file(
        plan_file=args.plan_file,
        hedge_executor=executor,
        force=args.force,
    )

    print(json.dumps(result, indent=2))

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
