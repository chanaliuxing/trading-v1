"""
Performance Guard Service

Monitors strategy performance (Sharpe ratio, EV) and automatically reduces risk
or pauses execution when performance degrades below configured thresholds.

Features:
- Reads PnL baseline data from ClickHouse or CSV
- Calculates rolling Sharpe and EV over configurable windows
- Triggers risk reduction or pause actions when thresholds breached
- Implements cool-off period to avoid thrashing
- Logs all actions to ClickHouse for audit trail
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd
from zoneinfo import ZoneInfo

from libs.db.ch_pool import threadsafe_command, threadsafe_insert, threadsafe_query
from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")


class PerformanceGuardConfig:
    """Configuration for performance guard."""

    def __init__(self, config: Dict):
        self.enabled = config.get("enabled", False)
        self.window_days = config.get("window_days", 20)
        self.min_sharpe = config.get("min_sharpe", 0.3)
        self.min_ev = config.get("min_ev", 0.0)
        self.action = config.get("action", "reduce")  # reduce | pause
        self.reduce_factor = config.get("reduce_factor", 0.5)
        self.cool_off_days = config.get("cool_off_days", 3)
        self.dry_run = config.get("dry_run", True)  # Safety: default to dry-run
        self.data_source = config.get("data_source", "ch")  # ch | csv
        self.csv_path = config.get("csv_path", "reports/pnl_baseline_daily.csv")


class PerformanceGuard:
    """
    Service that monitors performance and adjusts risk parameters.

    Usage:
        guard = PerformanceGuard(config)
        await guard.initialize()
        await guard.check_and_apply()  # Call periodically
    """

    def __init__(self, config: PerformanceGuardConfig):
        self.config = config
        self.ch_client = None
        self.last_action_date: Optional[datetime] = None
        self.current_state: str = "normal"  # normal | reduced | paused
        self.state_file = Path("runs/ai_trader/performance_guard_state.json")

    async def initialize(self):
        """Initialize service and load persisted state."""
        if self.config.enabled:
            if self.config.data_source == "ch":
                self.ch_client = ClickHouseDB()

            # Load previous state
            if self.state_file.exists():
                with open(self.state_file) as f:
                    state = json.load(f)
                    self.current_state = state.get("state", "normal")
                    last_action_str = state.get("last_action_date")
                    if last_action_str:
                        self.last_action_date = datetime.fromisoformat(last_action_str)

            logger.info(
                "performance_guard_initialized",
                enabled=self.config.enabled,
                window_days=self.config.window_days,
                min_sharpe=self.config.min_sharpe,
                min_ev=self.config.min_ev,
                current_state=self.current_state,
                dry_run=self.config.dry_run
            )

    async def fetch_performance_data(self) -> Optional[pd.DataFrame]:
        """
        Fetch recent performance data from configured source.

        Returns:
            DataFrame with columns: date, ret, sharpe_20, wins, losses, etc.
        """
        if self.config.data_source == "ch":
            return await self._fetch_from_clickhouse()
        elif self.config.data_source == "csv":
            return self._fetch_from_csv()
        else:
            logger.error("invalid_data_source", source=self.config.data_source)
            return None

    async def _fetch_from_clickhouse(self) -> Optional[pd.DataFrame]:
        """Fetch performance data from ClickHouse."""
        end_date = datetime.now(EASTERN).date()
        start_date = end_date - timedelta(days=self.config.window_days + 10)  # Extra buffer

        query = f"""
        SELECT
            date,
            ret,
            sharpe_20,
            sharpe_60,
            wins,
            losses,
            pnl,
            pnl_cum
        FROM trading.pnl_baseline
        WHERE date >= '{start_date}' AND date <= '{end_date}'
        ORDER BY date DESC
        LIMIT {self.config.window_days}
        """

        try:
            result = await asyncio.to_thread(threadsafe_query, self.ch_client, query)
            # ch_pool.query returns a list[dict] fast-path; older clients return QueryResult
            if isinstance(result, list):
                df = pd.DataFrame(result)
            elif hasattr(result, "result_set"):
                df = pd.DataFrame(result.result_set)
            else:
                df = pd.DataFrame(result)

            if len(df) > 0:
                logger.info("performance_data_fetched", source="clickhouse", rows=len(df))
                return df
            else:
                logger.warning("no_performance_data_ch")
                return None
        except Exception as e:
            logger.error("performance_data_fetch_failed", source="clickhouse", error=str(e))
            return None

    def _fetch_from_csv(self) -> Optional[pd.DataFrame]:
        """Fetch performance data from CSV file."""
        try:
            df = pd.read_csv(self.config.csv_path)
            df["date"] = pd.to_datetime(df["date"])

            # Get recent window
            df = df.sort_values("date", ascending=False).head(self.config.window_days)

            logger.info("performance_data_fetched", source="csv", rows=len(df))
            return df
        except Exception as e:
            logger.error("performance_data_fetch_failed", source="csv", error=str(e), exc_info=True)
            return None

    def calculate_recent_metrics(self, df: pd.DataFrame) -> Tuple[float, float]:
        """
        Calculate recent Sharpe ratio and EV from performance data.

        Returns:
            (sharpe, ev) tuple
        """
        if len(df) == 0:
            return (float('nan'), float('nan'))

        # Use pre-calculated Sharpe from the data
        latest_sharpe = df.iloc[0]["sharpe_20"]  # Most recent row

        # Calculate EV (expectancy)
        wins = df[df["pnl"] > 0]["pnl"]
        losses = df[df["pnl"] < 0]["pnl"]

        num_wins = len(wins)
        num_losses = len(losses)
        total_trades = num_wins + num_losses

        if total_trades == 0:
            ev = 0.0
        else:
            win_rate = num_wins / total_trades
            avg_win = wins.mean() if num_wins > 0 else 0
            avg_loss = abs(losses.mean()) if num_losses > 0 else 0
            ev = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        return (latest_sharpe, ev)

    def should_take_action(self, sharpe: float, ev: float) -> Tuple[bool, str]:
        """
        Determine if action should be taken based on metrics.

        Returns:
            (should_act, reason) tuple
        """
        # Check cool-off period
        if self.last_action_date:
            days_since_action = (datetime.now(EASTERN).date() - self.last_action_date.date()).days
            if days_since_action < self.config.cool_off_days:
                return (False, f"cool_off_active ({days_since_action}/{self.config.cool_off_days} days)")

        # Check thresholds
        reasons = []
        if sharpe < self.config.min_sharpe:
            reasons.append(f"low_sharpe ({sharpe:.3f} < {self.config.min_sharpe})")

        if ev < self.config.min_ev:
            reasons.append(f"low_ev ({ev:.3f} < {self.config.min_ev})")

        if reasons:
            return (True, ", ".join(reasons))
        else:
            return (False, "metrics_healthy")

    async def apply_action(self, action_type: str, trigger: str, sharpe: float, ev: float):
        """
        Apply risk adjustment action.

        Args:
            action_type: 'reduce' or 'pause'
            trigger: Reason for action (e.g., 'low_sharpe')
            sharpe: Current Sharpe ratio
            ev: Current EV
        """
        if self.config.dry_run:
            logger.warning(
                "performance_guard_action_dry_run",
                action=action_type,
                trigger=trigger,
                sharpe=sharpe,
                ev=ev,
                reduce_factor=self.config.reduce_factor
            )
            # Log to ClickHouse even in dry-run for audit
            await self._log_action(action_type, trigger, sharpe, ev, dry_run=True)
            return

        previous_state = self.current_state

        if action_type == "reduce":
            await self._apply_risk_reduction()
            self.current_state = "reduced"
        elif action_type == "pause":
            await self._apply_pause()
            self.current_state = "paused"

        # Update state
        self.last_action_date = datetime.now(EASTERN)
        self._save_state()

        # Log action
        await self._log_action(action_type, trigger, sharpe, ev, dry_run=False)

        logger.warning(
            "performance_guard_action_applied",
            action=action_type,
            trigger=trigger,
            previous_state=previous_state,
            new_state=self.current_state,
            sharpe=sharpe,
            ev=ev,
            reduce_factor=self.config.reduce_factor
        )

    async def _apply_risk_reduction(self):
        """Reduce risk parameters by configured factor."""
        # Write risk reduction signal to state file that other services can read
        risk_state_file = Path("runs/ai_trader/risk_reduction_active.json")
        risk_state_file.parent.mkdir(parents=True, exist_ok=True)

        risk_state = {
            "active": True,
            "reduce_factor": self.config.reduce_factor,
            "applied_at": datetime.now(EASTERN).isoformat(),
            "reason": "performance_guard_triggered"
        }

        with open(risk_state_file, "w") as f:
            json.dump(risk_state, f, indent=2)

        logger.info(
            "risk_reduction_applied",
            reduce_factor=self.config.reduce_factor,
            state_file=str(risk_state_file)
        )

    async def _apply_pause(self):
        """Pause execution by writing pause signal."""
        pause_file = Path("runs/ai_trader/execution_paused.json")
        pause_file.parent.mkdir(parents=True, exist_ok=True)

        pause_state = {
            "paused": True,
            "paused_at": datetime.now(EASTERN).isoformat(),
            "reason": "performance_guard_triggered"
        }

        with open(pause_file, "w") as f:
            json.dump(pause_state, f, indent=2)

        logger.warning(
            "execution_paused",
            state_file=str(pause_file)
        )

    async def _log_action(self, action_type: str, trigger: str, sharpe: float, ev: float, dry_run: bool = False):
        """Log action to ClickHouse for audit trail."""
        if not self.ch_client:
            return

        metadata = {
            "dry_run": dry_run,
            "config": {
                "window_days": self.config.window_days,
                "min_sharpe": self.config.min_sharpe,
                "min_ev": self.config.min_ev
            }
        }

        row = {
            "ts": datetime.now(EASTERN),
            "action_type": action_type,
            "trigger": trigger,
            "window_days": self.config.window_days,
            "sharpe": sharpe,
            "ev": ev,
            "threshold_sharpe": self.config.min_sharpe,
            "threshold_ev": self.config.min_ev,
            "reduce_factor": self.config.reduce_factor if action_type == "reduce" else 0.0,
            "previous_state": self.current_state,
            "new_state": "reduced" if action_type == "reduce" else "paused",
            "metadata": json.dumps(metadata)
        }

        try:
            def _write_action() -> None:
                create_table_sql = """
                CREATE TABLE IF NOT EXISTS trading.performance_actions (
                    ts DateTime64(6, 'UTC'),
                    action_type String,
                    trigger String,
                    window_days UInt16,
                    sharpe Float64,
                    ev Float64,
                    threshold_sharpe Float64,
                    threshold_ev Float64,
                    reduce_factor Float64,
                    previous_state String,
                    new_state String,
                    metadata String
                ) ENGINE = MergeTree()
                ORDER BY ts
                """
                threadsafe_command(self.ch_client, create_table_sql)
                threadsafe_insert(self.ch_client, "trading.performance_actions", [row])

            await asyncio.to_thread(_write_action)
            logger.info("performance_action_logged", action=action_type, dry_run=dry_run)
        except Exception as e:
            logger.error("performance_action_log_failed", error=str(e), exc_info=True)

    def _save_state(self):
        """Save current state to file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        state = {
            "state": self.current_state,
            "last_action_date": self.last_action_date.isoformat() if self.last_action_date else None,
            "updated_at": datetime.now(EASTERN).isoformat()
        }

        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    async def check_and_apply(self):
        """
        Main check method: fetch data, calculate metrics, apply action if needed.

        This should be called periodically (e.g., every 10 minutes).
        """
        if not self.config.enabled:
            return

        try:
            # Fetch performance data
            df = await self.fetch_performance_data()
            if df is None or len(df) < 10:  # Need minimum data
                logger.warning("insufficient_performance_data", rows=len(df) if df is not None else 0)
                return

            # Calculate metrics
            sharpe, ev = self.calculate_recent_metrics(df)

            logger.info(
                "performance_metrics_calculated",
                sharpe=sharpe,
                ev=ev,
                window_days=self.config.window_days,
                current_state=self.current_state
            )

            # Check if action needed
            should_act, reason = self.should_take_action(sharpe, ev)

            if should_act:
                await self.apply_action(self.config.action, reason, sharpe, ev)
            else:
                logger.info("performance_guard_check_passed", reason=reason, sharpe=sharpe, ev=ev)

        except Exception as e:
            logger.error("performance_guard_check_failed", error=str(e), exc_info=True)


# Standalone execution for testing
async def main():
    """Test performance guard with sample configuration."""
    import sys

    config = PerformanceGuardConfig({
        "enabled": True,
        "window_days": 20,
        "min_sharpe": 0.3,
        "min_ev": 0.0,
        "action": "reduce",
        "reduce_factor": 0.5,
        "cool_off_days": 3,
        "dry_run": True,  # Always dry-run for standalone test
        "data_source": "csv",
        "csv_path": "reports/pnl_baseline_daily.csv"
    })

    guard = PerformanceGuard(config)
    await guard.initialize()
    await guard.check_and_apply()

    print("\n✅ Performance guard test completed")
    print(f"Current state: {guard.current_state}")


if __name__ == "__main__":
    asyncio.run(main())
