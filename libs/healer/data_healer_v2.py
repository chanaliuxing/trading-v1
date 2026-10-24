#!/usr/bin/env python3
"""
DataHealer v2 - Unified Auto-Healing Engine

Phase 2 implementation that consumes standardized Issue/Action/Diagnosis objects
and executes fixes automatically.

Key improvements over v1:
- Unified heal() interface for Issue, Action, Diagnosis, SystemHealthSnapshot
- Uses Action.is_safe_auto_execute to determine if fix is safe
- Structured logging with action IDs
- Returns ActionResult for observability
"""

import os
import fcntl
import sys
import time
import subprocess
from pathlib import Path
from typing import Union, List, Dict, Any, Optional
from datetime import datetime, timedelta, time as dtime
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
import json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.health import (
    Issue, IssueType, Severity,
    Action,
    Diagnosis,
    SystemHealthSnapshot,
)
import structlog
import json
from libs.db.clickhouse import ClickHouseDB
from libs.utils.market_calendar import is_market_closed, last_trading_day, previous_trading_day
from libs.ml.event_emitter import emit_heal_start, emit_heal_success, emit_heal_failure

logger = structlog.get_logger(__name__)


@dataclass
class ActionResult:
    """Result of executing an action"""
    action_id: str
    action_type: str
    success: bool
    duration_sec: float
    rows_affected: int = 0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "success": self.success,
            "duration_sec": self.duration_sec,
            "rows_affected": self.rows_affected,
            "error": self.error,
            "metadata": self.metadata,
        }


class DataHealerV2:
    """
    DataHealer v2 - Unified Auto-Healing Engine

    Consumes standardized Issue/Action/Diagnosis/SystemHealthSnapshot objects
    and automatically executes fixes.

    Usage:
        healer = DataHealerV2()

        # Heal from Issue
        result = healer.heal(issue)

        # Heal from Action
        result = healer.heal(action)

        # Heal from Diagnosis (executes all auto-executable actions)
        results = healer.heal(diagnosis)

        # Heal from SystemHealthSnapshot (fixes all blocking issues)
        results = healer.heal(snapshot)
    """

    def __init__(self, runs_dir: str = "runs/ai_trader", dry_run: bool = False):
        """
        Initialize DataHealer v2

        Args:
            runs_dir: Directory for run artifacts
            dry_run: If True, only log actions without executing
        """
        self.runs_dir = Path(runs_dir)
        self.dry_run = dry_run
        self.project_root = PROJECT_ROOT

        logger.info(
            "data_healer_v2_init",
            runs_dir=str(self.runs_dir),
            dry_run=self.dry_run,
            project_root=str(self.project_root)
        )

    def _is_holiday_or_weekend(self) -> bool:
        return is_market_closed()

    def _intraday_backfill_block(self) -> Optional[Dict[str, Any]]:
        """
        Return context for skipping heavy historical loads during live/paper trading hours.
        Allows an override via HEAL_ALLOW_INTRADAY=1.
        """
        if os.getenv("HEAL_ALLOW_INTRADAY", "").lower() in ("1", "true", "yes", "on"):
            return None

        trading_mode = os.getenv("TRADING_MODE", "").lower()
        if trading_mode not in {"live", "paper"}:
            return None

        if self._is_holiday_or_weekend():
            return None

        tz_name = os.getenv("MARKET_TZ", "America/New_York")
        now = datetime.now(ZoneInfo(tz_name))
        # Block roughly regular-hours plus a small buffer to avoid hammering IBKR while trading.
        if dtime(hour=9, minute=15) <= now.time() <= dtime(hour=16, minute=15):
            return {
                "now": now.isoformat(),
                "tz": tz_name,
                "trading_mode": trading_mode
            }

        return None

    def _acquire_backfill_lock(self) -> Optional[Any]:
        """
        Prevent concurrent heavy backfills from multiple processes.

        Returns an open file handle if acquired, otherwise None.
        """
        lock_path = self.runs_dir / "data_healer_backfill.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.truncate(0)
            fh.write(str(time.time()))
            fh.flush()
            return fh
        except BlockingIOError:
            try:
                fh.close()
            except Exception:
                pass
            return None

    def _release_backfill_lock(self, fh: Any) -> None:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            try:
                fh.close()
            except Exception:
                pass

    def heal(
        self,
        target: Union[Issue, Action, Diagnosis, SystemHealthSnapshot]
    ) -> Union[ActionResult, List[ActionResult]]:
        """
        Unified heal interface - dispatches to specific handlers

        Args:
            target: Issue, Action, Diagnosis, or SystemHealthSnapshot to heal

        Returns:
            ActionResult for single actions, List[ActionResult] for multiple
        """
        # Global guard: skip healing on holidays/weekends unless explicitly allowed
        if self._is_holiday_or_weekend() and os.getenv("HEAL_SKIP_HOLIDAY", "1").lower() in ("1", "true", "yes", "on"):
            logger.info("heal_skipped_holiday", target_type=type(target).__name__)
            if isinstance(target, Action):
                return ActionResult(
                    action_id=target.action_id,
                    action_type=target.action_type,
                    success=True,
                    duration_sec=0,
                    rows_affected=0,
                    metadata={"note": "skipped due to holiday/weekend"}
                )
            # For other target types, return empty list to indicate no actions executed
            return []

        if isinstance(target, Action):
            return self.heal_action(target)
        elif isinstance(target, Issue):
            return self.heal_issue(target)
        elif isinstance(target, Diagnosis):
            return self.heal_diagnosis(target)
        elif isinstance(target, SystemHealthSnapshot):
            return self.heal_snapshot(target)
        else:
            raise TypeError(f"Cannot heal object of type {type(target)}")

    def heal_action(self, action: Action) -> ActionResult:
        """
        Execute a single action

        Args:
            action: Action object to execute

        Returns:
            ActionResult with execution details
        """
        logger.info(
            "heal_action_start",
            action_id=action.action_id,
            action_type=action.action_type,
            priority=action.priority,
            is_safe=action.is_safe_auto_execute,
            requires_approval=action.requires_approval
        )

        # Meta v3.4: Emit event when action starts
        emit_heal_start(
            action_type=action.action_type,
            action_id=action.action_id,
            symbol=action.params.get("symbols", [""])[0] if action.params.get("symbols") else "",
            metadata={"priority": action.priority, "is_safe": action.is_safe_auto_execute}
        )

        # Safety check
        if not action.is_safe_auto_execute:
            logger.warning(
                "heal_action_unsafe",
                action_id=action.action_id,
                action_type=action.action_type
            )
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error="Action not marked as safe for auto-execution"
            )

        if action.requires_approval:
            logger.warning(
                "heal_action_needs_approval",
                action_id=action.action_id,
                action_type=action.action_type
            )
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error="Action requires human approval"
            )

        # Dry run check
        if self.dry_run:
            logger.info(
                "heal_action_dry_run",
                action_id=action.action_id,
                action_type=action.action_type,
                command=action.command
            )
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=True,
                duration_sec=0,
                metadata={"dry_run": True}
            )

        # Dispatch to specific handler based on action_type
        start_time = time.time()

        try:
            if action.action_type == "generate_label":
                result = self._execute_generate_label(action)
            elif action.action_type == "rebuild_features":
                result = self._execute_rebuild_features(action)
            elif action.action_type == "backfill_bars":
                result = self._execute_backfill_bars(action)
            elif action.action_type == "update_config":
                result = self._execute_update_config(action)
            elif action.action_type == "shell_command":
                result = self._execute_shell_command(action)
            elif action.action_type == "diagnose_label_quality":
                result = self._execute_diagnose_label_quality(action)
            elif action.action_type == "change_horizon":
                result = self._execute_change_horizon(action)
            elif action.action_type == "use_threshold_labeling":
                result = self._execute_use_threshold_labeling(action)
            else:
                logger.warning(
                    "heal_action_unknown_type",
                    action_id=action.action_id,
                    action_type=action.action_type
                )
                result = ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=False,
                    duration_sec=time.time() - start_time,
                    error=f"Unknown action type: {action.action_type}"
                )

            duration = time.time() - start_time
            result.duration_sec = duration

            if result.success:
                logger.info(
                    "heal_action_success",
                    action_id=action.action_id,
                    action_type=action.action_type,
                    duration_sec=duration,
                    rows_affected=result.rows_affected
                )
                # Meta v3.4: Emit success event
                emit_heal_success(
                    action_type=action.action_type,
                    action_id=action.action_id,
                    symbol=action.params.get("symbols", [""])[0] if action.params.get("symbols") else "",
                    duration_sec=duration,
                    rows_affected=result.rows_affected,
                    metadata=result.metadata
                )
            else:
                logger.error(
                    "heal_action_failed",
                    action_id=action.action_id,
                    action_type=action.action_type,
                    duration_sec=duration,
                    error=result.error
                )
                # Meta v3.4: Emit failure event
                emit_heal_failure(
                    action_type=action.action_type,
                    action_id=action.action_id,
                    symbol=action.params.get("symbols", [""])[0] if action.params.get("symbols") else "",
                    duration_sec=duration,
                    reason=result.error or "Unknown error",
                    metadata=result.metadata
                )

            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.error(
                "heal_action_exception",
                action_id=action.action_id,
                action_type=action.action_type,
                duration_sec=duration,
                error=str(e),
                exc_info=True
            )
            # Meta v3.4: Emit failure event for exceptions
            emit_heal_failure(
                action_type=action.action_type,
                action_id=action.action_id,
                symbol=action.params.get("symbols", [""])[0] if action.params.get("symbols") else "",
                duration_sec=duration,
                reason=f"Exception: {str(e)}",
                metadata={"exception_type": type(e).__name__}
            )
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=duration,
                error=str(e)
            )

    def heal_issue(self, issue: Issue) -> ActionResult:
        """
        Heal an issue by determining appropriate action and executing it

        Args:
            issue: Issue object to heal

        Returns:
            ActionResult
        """
        logger.info(
            "heal_issue_start",
            issue_id=issue.issue_id,
            issue_type=issue.issue_type,
            severity=issue.severity,
            component=issue.component
        )

        # Create action based on issue type
        action = self._issue_to_action(issue)

        if action is None:
            logger.warning(
                "heal_issue_no_action",
                issue_id=issue.issue_id,
                issue_type=issue.issue_type
            )
            return ActionResult(
                action_id=f"auto_{issue.issue_id}",
                action_type="unknown",
                success=False,
                duration_sec=0,
                error=f"No automated fix available for issue type: {issue.issue_type}"
            )

        return self.heal_action(action)

    def heal_diagnosis(self, diagnosis: Diagnosis) -> List[ActionResult]:
        """
        Heal all auto-executable actions in a diagnosis

        Args:
            diagnosis: Diagnosis object with recommended actions

        Returns:
            List of ActionResults
        """
        logger.info(
            "heal_diagnosis_start",
            diagnosis_id=diagnosis.diagnosis_id,
            total_actions=len(diagnosis.recommended_actions),
            auto_executable=len(diagnosis.get_auto_executable_actions())
        )

        results = []
        auto_actions = diagnosis.get_auto_executable_actions()

        if not auto_actions:
            logger.warning(
                "heal_diagnosis_no_auto_actions",
                diagnosis_id=diagnosis.diagnosis_id
            )
            return results

        sorted_actions = sorted(auto_actions, key=lambda a: a.priority)

        # Use a while loop to process actions, as new actions may be added during execution
        i = 0
        while i < len(sorted_actions):
            action = sorted_actions[i]
            i += 1
            
            result = self.heal_action(action)
            results.append(result)

            # Stop on first failure if action has high priority
            if not result.success and action.priority == 1:
                logger.warning(
                    "heal_diagnosis_critical_failure",
                    diagnosis_id=diagnosis.diagnosis_id,
                    action_id=action.action_id
                )
                break
            
            # Check for new actions generated by the executed action
            if result.success and result.metadata and "auto_fix_actions" in result.metadata:
                new_actions_data = result.metadata["auto_fix_actions"]
                if isinstance(new_actions_data, list):
                    for new_action_data in new_actions_data:
                        try:
                            # Create a new Action object
                            from libs.health import make_action_id
                            new_action = Action(
                                action_id=make_action_id(new_action_data.get("action", "unknown")),
                                action_type=new_action_data.get("action"),
                                priority=action.priority + 1,  # Lower priority than the trigger
                                title=new_action_data.get("reason", "Auto-generated action"),
                                description=new_action_data.get("reason", ""),
                                params=new_action_data.get("params", {}),
                                is_safe_auto_execute=True,  # Assume auto-fix actions from tools are safe
                                requires_approval=False
                            )
                            sorted_actions.append(new_action)
                            logger.info("heal_diagnosis_new_action_added",
                                        diagnosis_id=diagnosis.diagnosis_id,
                                        new_action_id=new_action.action_id,
                                        new_action_type=new_action.action_type)
                        except Exception as e:
                            logger.error("heal_diagnosis_new_action_creation_failed", error=str(e), data=new_action_data)


        success_count = sum(1 for r in results if r.success)
        logger.info(
            "heal_diagnosis_complete",
            diagnosis_id=diagnosis.diagnosis_id,
            total_actions=len(results),
            successful=success_count,
            failed=len(results) - success_count
        )

        return results

    def heal_snapshot(self, snapshot: SystemHealthSnapshot) -> List[ActionResult]:
        """
        Heal all blocking issues in a system health snapshot

        Args:
            snapshot: SystemHealthSnapshot with blocking issues

        Returns:
            List of ActionResults
        """
        logger.info(
            "heal_snapshot_start",
            overall_status=snapshot.overall_status,
            blocking_issues_count=len(snapshot.blocking_issues)
        )

        if snapshot.overall_status == "healthy":
            logger.info("heal_snapshot_already_healthy")
            return []

        results = []

        # Heal in order: config → bars → features → labels
        # (dependencies: labels need features, features need bars)

        # 1. Config issues
        if snapshot.config.status == "blocked":
            for problem in snapshot.config.problems:
                # Create action from config problem
                action = self._config_problem_to_action(problem)
                if action:
                    result = self.heal_action(action)
                    results.append(result)

        # 2. Bars issues (iterate through common bar sizes)
        for bar_size in ["1m", "5m"]:
            # This part assumes snapshot.bars can provide status per bar_size
            # A more robust implementation would require snapshot to have a dictionary
            # of statuses per bar_size. For now, we'll use the general status.
            if snapshot.bars.status == "blocked":
                action = self._bars_status_to_action(snapshot.bars, snapshot.symbols, bar_size)
                if action:
                    result = self.heal_action(action)
                    results.append(result)

        # 3. Features issues
        if snapshot.features.status == "blocked" or snapshot.features.rebuild_needed:
            action = self._features_status_to_action(snapshot.features, snapshot.symbols)
            if action:
                result = self.heal_action(action)
                results.append(result)

        # 4. Label issues (per symbol-horizon)
        for key, label_status in snapshot.labels.by_symbol.items():
            if label_status.status in {"blocked", "degraded"}:
                action = self._label_status_to_action(label_status)
                if action:
                    result = self.heal_action(action)
                    results.append(result)

        success_count = sum(1 for r in results if r.success)
        logger.info(
            "heal_snapshot_complete",
            total_actions=len(results),
            successful=success_count,
            failed=len(results) - success_count
        )

        return results

    # ==================== Private: Action Creators ====================

    def _issue_to_action(self, issue: Issue) -> Optional[Action]:
        """Convert Issue to Action"""
        from libs.health import make_action_id

        if issue.issue_type == IssueType.SCHEMA_MISMATCH:
            # Label column missing
            if "label_h" in issue.title.lower():
                horizon_min = issue.metrics.get("horizon_min", 120)
                symbol = issue.affected_symbols[0] if issue.affected_symbols else "TSLA"

                return Action(
                    action_id=make_action_id("generate_label"),
                    action_type="generate_label",
                    priority=1,
                    title=f"Generate missing label column",
                    description=issue.description,
                    params={
                        "symbols": issue.affected_symbols,
                        "horizons": [horizon_min],
                        "lookback_days": 90
                    },
                    is_safe_auto_execute=True,
                    requires_approval=False
                )

        elif issue.issue_type == IssueType.DATA_QUALITY_POOR:
            # Poor label quality
            if issue.component == "labels":
                return None  # Can't auto-fix poor quality (e.g., low std)

            # High NULL ratio in features
            elif issue.component == "features":
                return Action(
                    action_id=make_action_id("rebuild_features"),
                    action_type="rebuild_features",
                    priority=1,
                    title="Rebuild features to fix NULL ratio",
                    description=issue.description,
                    params={
                        "symbols": issue.affected_symbols,
                        "lookback_days": 7
                    },
                    is_safe_auto_execute=True,
                    requires_approval=False
                )

        elif issue.issue_type == IssueType.DATA_STALE:
            # Stale bars or features
            bar_size = issue.metrics.get("bar_size", "1m") # Extract bar_size from issue
            return Action(
                action_id=make_action_id("backfill_bars"),
                action_type="backfill_bars",
                priority=1,
                title=f"Backfill stale {bar_size} data",
                description=issue.description,
                params={
                    "symbols": issue.affected_symbols,
                    "days": 2,
                    "bar_size": bar_size # Pass bar_size to action
                },
                is_safe_auto_execute=True,
                requires_approval=False
            )

        return None

    def _config_problem_to_action(self, problem: Dict[str, Any]) -> Optional[Action]:
        """Convert config problem to Action"""
        # For now, config problems require manual intervention
        # In the future, could auto-update config files
        return None

    def _bars_status_to_action(self, bars, symbols: List[str], bar_size: str = "1m") -> Optional[Action]:
        """Convert bars status to backfill action"""
        from libs.health import make_action_id

        if bars.age_hours > 24:
            return Action(
                action_id=make_action_id(f"backfill_bars_{bar_size}"),
                action_type="backfill_bars",
                priority=1,
                title=f"Backfill stale {bar_size} bars",
                description=f"Bars are {bars.age_hours:.1f} hours old",
                params={"symbols": symbols, "days": 2, "bar_size": bar_size}, # Add bar_size to params
                is_safe_auto_execute=True,
                requires_approval=False
            )
        return None

    def _features_status_to_action(self, features, symbols: List[str]) -> Optional[Action]:
        """Convert features status to rebuild action"""
        from libs.health import make_action_id

        if features.rebuild_needed or features.age_hours > 24:
            bar_size = "1m" # Hardcode to 1m for now
            return Action(
                action_id=make_action_id("rebuild_features"),
                action_type="rebuild_features",
                priority=1,
                title="Rebuild features",
                description=f"Features are {features.age_hours:.1f} hours old with {features.null_ratio:.1%} NULL",
                params={"symbols": symbols, "lookback_days": 7, "bar_size": bar_size},
                is_safe_auto_execute=True,
                requires_approval=False
            )
        return None

    def _label_status_to_action(self, label_status) -> Optional[Action]:
        """Convert label status to generate action"""
        from libs.health import make_action_id

        # Trigger regeneration for any blocked status (missing/stale/high-null/zero rows)
        # rather than only very high null ratios.
        needs_regen = (
            label_status.status == "blocked"
            or label_status.rows <= 0
            or label_status.age_hours > 48
            or label_status.null_ratio > 0.5
        )

        # Also regenerate when quality is degraded (e.g., low std/zero-heavy) so we
        # don't get stuck in loops with label_h5 having zero variance.
        if needs_regen or label_status.status == "degraded" or label_status.std_bps < 10:
            return Action(
                action_id=make_action_id("generate_label"),
                action_type="generate_label",
                priority=1,
                title=f"Regenerate labels for {label_status.symbol}_h{label_status.horizon_min}",
                description=(
                    f"Labels unhealthy: rows={label_status.rows}, "
                    f"null_ratio={label_status.null_ratio:.1%}, "
                    f"age_hours={label_status.age_hours:.1f}"
                ),
                params={
                    "symbols": [label_status.symbol],
                    "horizons": [label_status.horizon_min],
                    "lookback_days": 90
                },
                is_safe_auto_execute=True,
                requires_approval=False
            )
        return None

    # ==================== Private: Action Executors ====================

    def _execute_generate_label(self, action: Action) -> ActionResult:
        """
        Execute label generation (using robust batching and schema evolution from V1)
        """
        if self._is_holiday_or_weekend():
            logger.info("generate_label_skipped_holiday", symbols=action.params.get("symbols", []))
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=True,
                duration_sec=0,
                rows_affected=0,
                metadata={"note": "skipped due to holiday/weekend"}
            )

        symbols = action.params.get("symbols", [])
        horizons = action.params.get("horizons", [120])
        lookback_days = action.params.get("lookback_days", 90)
        max_null_ratio = float(action.params.get("max_null_ratio", 0.1))
        min_rows_per_symbol = int(action.params.get("min_rows_per_symbol", 1000))
        max_age_hours = float(action.params.get("max_age_hours", os.getenv("HEAL_LABEL_MAX_AGE_HOURS", 48)))

        # Clamp lookback to a configurable ceiling to avoid huge ranges
        max_lookback = int(os.getenv("MAX_LABEL_LOOKBACK", 45))
        if lookback_days > max_lookback:
            logger.warning("label_lookback_clamped", original=lookback_days, max=max_lookback)
            lookback_days = max_lookback

        logger.info(
            "execute_generate_label_v2",
            symbols=symbols,
            horizons=horizons,
            lookback_days=lookback_days,
            max_null_ratio=max_null_ratio,
        )

        try:
            # Coverage gate: only heal missing/degenerate symbols
            missing_symbols = self._check_label_coverage(
                symbols=symbols,
                horizons=horizons,
                lookback_days=lookback_days,
                min_rows_per_symbol=min_rows_per_symbol,
                max_null_ratio=max_null_ratio,
                max_age_hours=max_age_hours,
            )
            if not missing_symbols:
                return ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=True,
                    duration_sec=0,
                    rows_affected=0,
                    metadata={"note": "labels_already_ready", "symbols_checked": symbols},
                )

            # Call the new robust helper method for batch label generation
            rows_generated = self._generate_labels_batch(
                missing_symbols,
                horizons,
                lookback_days
            )

            if rows_generated > 0:
                return ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=True,
                    duration_sec=0, # Duration will be set outside in heal_action
                    rows_affected=rows_generated,
                    metadata={"symbols": symbols, "horizons": horizons, "lookback_days": lookback_days}
                )
            else:
                return ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=False,
                    duration_sec=0, # Duration will be set outside in heal_action
                    error="Generated 0 rows"
                )

        except Exception as e:
            logger.error(
                "execute_generate_label_failed",
                action_id=action.action_id,
                action_type=action.action_type,
                error=str(e),
                exc_info=True
            )
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0, # Duration will be set outside in heal_action
                error=str(e)
            )

    def _execute_rebuild_features(self, action: Action) -> ActionResult:
        """Execute features rebuild"""
        if self._is_holiday_or_weekend():
            logger.info("rebuild_features_skipped_holiday", symbols=action.params.get("symbols", []))
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=True,
                duration_sec=0,
                rows_affected=0,
                metadata={"note": "skipped due to holiday/weekend"}
            )

        symbols = action.params.get("symbols", [])
        lookback_days = action.params.get("lookback_days", 7)
        bar_size = action.params.get("bar_size", "1m")
        source_table = f"trading.bars_{bar_size}_ibkr"

        start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        end_date = datetime.now().strftime("%Y-%m-%d")

        logger.info(
            "execute_rebuild_features",
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            source_table=source_table
        )

        script = self.project_root / "pipelines" / "rebuild_features_1m.py"

        if not script.exists():
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error=f"Feature rebuild script not found: {script}"
            )

        cmd = [
            sys.executable,
            str(script),
            "--symbols", *symbols,
            "--start", start_date,
            "--end", end_date,
        ]

        logger.info("execute_rebuild_features_cmd", cmd=" ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,  # 30 minutes
                cwd=str(self.project_root)
            )

            if result.returncode == 0:
                rows = self._extract_rows_from_output(result.stdout)
                return ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=True,
                    duration_sec=0,
                    rows_affected=rows,
                    metadata={"start_date": start_date, "end_date": end_date}
                )
            else:
                error_msg = result.stderr[:500] if result.stderr else "Unknown error"
                return ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=False,
                    duration_sec=0,
                    error=error_msg
                )

        except subprocess.TimeoutExpired:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=1800,
                error="Feature rebuild timed out after 30 minutes"
            )
        except Exception as e:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error=str(e)
            )

    def _execute_backfill_bars(self, action: Action) -> ActionResult:
        """Execute bars backfill"""
        if self._is_holiday_or_weekend():
            logger.info("backfill_skipped_holiday", symbols=action.params.get("symbols", []), days=action.params.get("days", 2))
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=True,
                duration_sec=0,
                rows_affected=0,
                metadata={"note": "skipped due to holiday/weekend"}
            )

        symbols = action.params.get("symbols", [])
        days = action.params.get("days", 2)
        bar_size = action.params.get("bar_size", "1m") # Get bar_size from action.params

        intraday_block = self._intraday_backfill_block()
        if intraday_block:
            logger.warning(
                "backfill_skipped_intraday",
                symbols=symbols,
                days=days,
                bar_size=bar_size,
                **intraday_block
            )
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=True,
                duration_sec=0,
                rows_affected=0,
                metadata={"note": "skip: intraday guard", **intraday_block}
            )

        # Construct table name dynamically based on bar_size, adding the `_ibkr` suffix
        # to match the convention in `load_hist_multi.py`
        bar_size_simple = bar_size.replace(" ", "")
        table_name = f"trading.bars_{bar_size_simple}_ibkr"

        logger.info("execute_backfill_bars", symbols=symbols, days=days, bar_size=bar_size, table_name=table_name)

        lock_handle = self._acquire_backfill_lock()
        if lock_handle is None:
            logger.warning("backfill_skipped_lock_held", symbols=symbols, table=table_name)
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=True,
                duration_sec=0,
                rows_affected=0,
                metadata={"note": "skip: backfill lock held"}
            )

        # Skip if already at or beyond last trading day
        try:
            ch = ClickHouseDB()
            last_td = last_trading_day()
            prev_td = previous_trading_day()
            safe_symbols = symbols or []
            # check per symbol, skip only if all up-to-date
            up_to_date = 0
            for sym in safe_symbols:
                res = ch.query(f"SELECT max(ts_start) as m FROM {table_name} WHERE symbol=%(s)s", {"s": sym})
                if res and res[0].get("m"):
                    latest_date = res[0]["m"].date()
                    # If latest data is at least previous trading day, don't chase non-trading days
                    if latest_date >= prev_td:
                        up_to_date += 1
            if safe_symbols and up_to_date == len(safe_symbols):
                logger.info("backfill_skipped_up_to_date", symbols=safe_symbols, last_trading_day=last_td.isoformat(), previous_trading_day=prev_td.isoformat())
                return ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=True,
                    duration_sec=0,
                    rows_affected=0,
                    metadata={"note": "skip: already up to last trading day"}
                )
        except Exception:
            pass

        script = self.project_root / "tool" / "load_hist_multi.py"

        if not script.exists():
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error=f"Backfill script not found: {script}"
            )

        cmd = [
            sys.executable,
            str(script),
            "--symbols", *symbols,
            "--days", str(days),
            "--bar-size", bar_size, # Use dynamic bar_size
            "--fill-mode", "backfill",
            "--chunk-days", "1",
            "--skip-covered-days",
        ]

        logger.info("execute_backfill_bars_cmd", cmd=" ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(self.project_root)
            )

            if result.returncode == 0:
                return ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=True,
                    duration_sec=0,
                    metadata={"days": days}
                )
            else:
                error_msg = result.stderr[:500] if result.stderr else "Unknown error"
                return ActionResult(
                    action_id=action.action_id,
                    action_type=action.action_type,
                    success=False,
                    duration_sec=0,
                    error=error_msg
                )

        except subprocess.TimeoutExpired:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=600,
                error="Backfill timed out after 10 minutes"
            )
        except Exception as e:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error=str(e)
            )
        finally:
            if lock_handle:
                self._release_backfill_lock(lock_handle)

    def _execute_update_config(self, action: Action) -> ActionResult:
        """Execute config update (placeholder - requires manual intervention)"""
        return ActionResult(
            action_id=action.action_id,
            action_type=action.action_type,
            success=False,
            duration_sec=0,
            error="Config updates require manual intervention"
        )

    def _execute_shell_command(self, action: Action) -> ActionResult:
        """Execute generic shell command"""
        if not action.command:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error="No command specified"
            )

        logger.info("execute_shell_command", command=action.command)

        try:
            result = subprocess.run(
                action.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=action.estimated_duration_sec or 600,
                cwd=str(self.project_root)
            )

            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=result.returncode == 0,
                duration_sec=0,
                error=result.stderr[:500] if result.returncode != 0 else None
            )

        except Exception as e:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error=str(e)
            )

    def _execute_diagnose_label_quality(self, action: Action) -> ActionResult:
        """Execute label quality diagnosis tool."""
        try:
            symbols = action.params.get("symbols", [])
            if isinstance(symbols, str):
                symbols = [s.strip() for s in symbols.split(",")]
            
            horizons = action.params.get("horizons", [30, 60, 120])
            if isinstance(horizons, (int, str)):
                horizons = [int(horizons)]
            
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=60)
            
            start_str = action.params.get("start", start_dt.strftime("%Y-%m-%d %H:%M:%S"))
            end_str = action.params.get("end", end_dt.strftime("%Y-%m-%d %H:%M:%S"))

            if not symbols:
                logger.warning("diagnose_label_skip_no_symbols", action_id=action.action_id)
                return ActionResult(action_id=action.action_id, action_type=action.action_type, success=True, duration_sec=0, metadata={"reason": "no_symbols"})

            cmd = [
                sys.executable,
                str(self.project_root / "tools" / "diagnose_label_quality.py"),
                "--symbols", ",".join(symbols),
                "--horizons", *[str(h) for h in horizons],
                "--start", start_str,
                "--end", end_str,
                "--machine-readable" # <-- ADDED
            ]
            
            logger.info("execute_diagnose_label_cmd", cmd=" ".join(cmd))

            if self.dry_run:
                return ActionResult(action_id=action.action_id, action_type=action.action_type, success=True, duration_sec=0, metadata={"dry_run": True, "command": " ".join(cmd)})

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=self.project_root)

            if proc.returncode != 0:
                logger.error("diagnose_label_failed", action_id=action.action_id, stderr=proc.stderr[-500:], stdout=proc.stdout[-500:])
                return ActionResult(action_id=action.action_id, action_type=action.action_type, success=False, duration_sec=0, error=proc.stderr[-500:] if proc.stderr else "Unknown error")
            
            # Parse the JSON report from stdout
            try:
                report = json.loads(proc.stdout)
                auto_fix_actions = report.get("auto_fix_actions", [])
            except json.JSONDecodeError:
                logger.warning("diagnose_label_report_parse_failed", action_id=action.action_id, stdout_tail=proc.stdout[-200:])
                auto_fix_actions = []

            logger.info("diagnose_label_success", action_id=action.action_id, stdout_tail=proc.stdout[-200:])
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=True,
                duration_sec=0,
                metadata={"symbols": symbols, "horizons": horizons, "start": start_str, "end": end_str, "auto_fix_actions": auto_fix_actions}
            )

        except subprocess.TimeoutExpired:
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=300,
                error="Label diagnosis timed out after 5 minutes"
            )
        except KeyboardInterrupt:
            logger.warning("diagnose_label_quality_interrupted", action_id=action.action_id)
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error="Process interrupted by user"
            )
        except Exception as e:
            logger.error("execute_diagnose_label_error", action_id=action.action_id, error=str(e), exc_info=True)
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=False,
                duration_sec=0,
                error=str(e)
            )
    
    def _extract_rows_from_output(self, output: str) -> int:
        """Extract number of rows from script output"""
        import re

        # Look for patterns like "12345 rows" or "Processed 12,345 rows"
        for line in output.split('\n'):
            match = re.search(r'(\d{1,3}(?:,\d{3})*|\d+)\s+rows', line.lower())
            if match:
                return int(match.group(1).replace(',', ''))

        return 0

    def _execute_change_horizon(self, action: Action) -> ActionResult:
        """Execute change_horizon action by calling the alpha_config_patcher."""
        try:
            from scripts.ai_trader.config import alpha_config_patcher
            
            to_horizon = action.params.get("to_horizon")
            if not to_horizon:
                return ActionResult(action.action_id, action.action_type, success=False, duration_sec=0, error="Missing 'to_horizon' in params")

            # The patcher expects a label profile name, e.g., 'intraday_reg_120m'
            value = f"intraday_reg_{to_horizon}m"
            
            result = alpha_config_patcher.update(
                file_path="alpha_config_enhanced.json",
                key="label_profile",
                value=value
            )
            
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=result.get("ok", False),
                duration_sec=0,
                error=result.get("error"),
                metadata=result
            )
        except Exception as e:
            return ActionResult(action.action_id, action.action_type, success=False, duration_sec=0, error=str(e))

    def _execute_use_threshold_labeling(self, action: Action) -> ActionResult:
        """Placeholder for executing use_threshold_labeling action."""
        # This would likely involve patching a different part of the config,
        # e.g., one that controls the binarization/labeling process itself.
        # For now, we'll log it as a success to not block the healing chain.
        logger.info("execute_use_threshold_labeling_placeholder", action_id=action.action_id, params=action.params)
        return ActionResult(
            action_id=action.action_id,
            action_type=action.action_type,
            success=True,
            duration_sec=0,
            metadata={"note": "This is a placeholder and did not change any configuration."}
        )

    def _execute_change_horizon(self, action: Action) -> ActionResult:
        """Execute change_horizon action by calling the alpha_config_patcher."""
        try:
            from scripts.ai_trader.config import alpha_config_patcher
            
            to_horizon = action.params.get("to_horizon")
            if not to_horizon:
                return ActionResult(action.action_id, action.action_type, success=False, duration_sec=0, error="Missing 'to_horizon' in params")

            # The patcher expects a label profile name, e.g., 'intraday_reg_120m'
            value = f"intraday_reg_{to_horizon}m"
            
            result = alpha_config_patcher.update(
                file_path="alpha_config_enhanced.json",
                key="label_profile",
                value=value
            )
            
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=result.get("ok", False),
                duration_sec=0,
                error=result.get("error"),
                metadata=result
            )
        except Exception as e:
            return ActionResult(action.action_id, action.action_type, success=False, duration_sec=0, error=str(e))

    def _execute_use_threshold_labeling(self, action: Action) -> ActionResult:
        """Placeholder for executing use_threshold_labeling action."""
        # This would likely involve patching a different part of the config,
        # e.g., one that controls the binarization/labeling process itself.
        # For now, we'll log it as a success to not block the healing chain.
        logger.info("execute_use_threshold_labeling_placeholder", action_id=action.action_id, params=action.params)
        return ActionResult(
            action_id=action.action_id,
            action_type=action.action_type,
            success=True,
            duration_sec=0,
            metadata={"note": "This is a placeholder and did not change any configuration."}
        )

    def _execute_change_horizon(self, action: Action) -> ActionResult:
        """Execute change_horizon action by calling the alpha_config_patcher."""
        try:
            from scripts.ai_trader.config import alpha_config_patcher
            
            to_horizon = action.params.get("to_horizon")
            if not to_horizon:
                return ActionResult(action.action_id, action.action_type, success=False, duration_sec=0, error="Missing 'to_horizon' in params")

            # The patcher expects a label profile name, e.g., 'intraday_reg_120m'
            value = f"intraday_reg_{to_horizon}m"
            
            result = alpha_config_patcher.update(
                file_path="alpha_config_enhanced.json",
                key="label_profile",
                value=value
            )
            
            return ActionResult(
                action_id=action.action_id,
                action_type=action.action_type,
                success=result.get("ok", False),
                duration_sec=0,
                error=result.get("error"),
                metadata=result
            )
        except Exception as e:
            return ActionResult(action.action_id, action.action_type, success=False, duration_sec=0, error=str(e))

    def _execute_use_threshold_labeling(self, action: Action) -> ActionResult:
        """Placeholder for executing use_threshold_labeling action."""
        # This would likely involve patching a different part of the config,
        # e.g., one that controls the binarization/labeling process itself.
        # For now, we'll log it as a success to not block the healing chain.
        logger.info("execute_use_threshold_labeling_placeholder", action_id=action.action_id, params=action.params)
        return ActionResult(
            action_id=action.action_id,
            action_type=action.action_type,
            success=True,
            duration_sec=0,
            metadata={"note": "This is a placeholder and did not change any configuration."}
        )

    def _generate_labels_batch(
        self,
        symbols: List[str],
        horizons: List[int],
        lookback_days: int
    ) -> int:
        """
        Generate labels for a batch of symbols using tools/recompute_labels_1m.py
        in daily chunks to prevent OOM errors. Each day is processed in its own
        temporary table to avoid memory limits.
        """
        ch_client = ClickHouseDB()
        total_rows_generated = 0
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=lookback_days)

        # Tighter defaults to avoid OOM on very liquid symbols; env overrides allowed.
        max_days_per_chunk = max(1, int(os.getenv("LABEL_MAX_DAYS_PER_CHUNK", 1)))
        max_symbols_per_chunk = max(1, int(os.getenv("LABEL_MAX_SYMBOLS_PER_CHUNK", 3)))

        logger.info(
            "label_generation_chunked_start",
            symbols=symbols,
            horizons=horizons,
            start_date=start_dt.date().isoformat(),
            end_date=end_dt.date().isoformat(),
            max_days_per_chunk=max_days_per_chunk,
            max_symbols_per_chunk=max_symbols_per_chunk,
        )

        def chunked(seq, n):
            for i in range(0, len(seq), n):
                yield seq[i : i + n]

        for sym_batch in chunked(symbols, max_symbols_per_chunk):
            current_dt = start_dt
            while current_dt <= end_dt:
                chunk_end = min(current_dt + timedelta(days=max_days_per_chunk - 1), end_dt)
                day_start_ts = current_dt.strftime("%Y-%m-%d 00:00:00")
                day_end_ts = chunk_end.strftime("%Y-%m-%d 23:59:59")

                # Unique temp table per chunk to avoid memory exhaustion
                temp_table_name = f"features_1m_with_labels_temp_{int(time.time())}_{current_dt.strftime('%Y%m%d')}_{len(sym_batch)}"
                temp_table_qualified = f"trading.{temp_table_name}"

                try:
                    cmd = [
                        sys.executable,
                        str(self.project_root / "tools" / "recompute_labels_1m.py"),
                        "--symbols", ",".join(sym_batch),
                        "--horizons", ",".join(map(str, horizons)),
                        "--start", day_start_ts,
                        "--end", day_end_ts,
                        "--table", temp_table_qualified,
                        "--source", "trading.features_1m",
                    ]

                    subprocess.run(cmd, check=True, timeout=900, capture_output=True, text=True, cwd=self.project_root)

                    count_query = f"SELECT count() as cnt FROM {temp_table_qualified}"
                    result = ch_client.query(count_query)
                    rows_this_batch = result[0]["cnt"] if result and result[0] else 0
                    total_rows_generated += rows_this_batch

                    if rows_this_batch > 0:
                        self._merge_label_table(temp_table_name, ch_client)
                    else:
                        logger.info(
                            "label_generation_zero_rows_for_chunk",
                            temp_table=temp_table_qualified,
                            start=day_start_ts,
                            end=day_end_ts,
                            symbols=sym_batch,
                        )

                finally:
                    ch_client.execute(f"DROP TABLE IF EXISTS {temp_table_qualified}")

                current_dt = chunk_end + timedelta(days=1)
            
        return total_rows_generated

    def _check_label_coverage(
        self,
        symbols: List[str],
        horizons: List[int],
        lookback_days: int,
        min_rows_per_symbol: int,
        max_null_ratio: float,
        max_age_hours: float,
    ) -> List[str]:
        """
        Lightweight coverage check to avoid regenerating healthy symbols.
        """
        if not symbols:
            return []
        ch = ClickHouseDB()
        missing: List[str] = []
        cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        label_col = f"label_h{horizons[0]}"
        for symbol in symbols:
            try:
                q = f"""
                    SELECT
                        count() as total_rows,
                        countIf({label_col} IS NULL) as null_rows,
                        max(ts) as latest_ts
                    FROM trading.features_1m_with_labels
                    WHERE symbol = %(symbol)s
                      AND toDate(ts) >= %(cutoff)s
                """
                res = ch.query(q, {"symbol": symbol, "cutoff": cutoff_date})
                if not res:
                    missing.append(symbol)
                    continue
                total = int(res[0].get("total_rows", 0) or 0)
                null_rows = int(res[0].get("null_rows", 0) or 0)
                latest_ts = res[0].get("latest_ts")
                non_null = total - null_rows
                null_ratio = null_rows / total if total > 0 else 1.0
                age_hours = None
                try:
                    if latest_ts:
                        age_hours = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
                except Exception:
                    age_hours = None

                if (
                    non_null < min_rows_per_symbol
                    or null_ratio > max_null_ratio
                    or (age_hours is not None and age_hours > max_age_hours)
                ):
                    missing.append(symbol)
            except Exception:
                missing.append(symbol)
        return missing


    def _merge_label_table(self, temp_table: str, ch_client: ClickHouseDB) -> None:
        """
        Merge temp label table into main features_1m_with_labels table
        using daily chunks to prevent OOM errors.
        """
        main_table_fq = "trading.features_1m_with_labels"
        temp_table_fq = f"trading.{temp_table}"

        # Create main table if missing
        create_query = f"""
            CREATE TABLE IF NOT EXISTS {main_table_fq}
            ENGINE = MergeTree()
            PARTITION BY toYYYYMMDD(ts)
            ORDER BY (symbol, ts)
            AS SELECT * FROM {temp_table_fq} WHERE 1 = 0
        """
        ch_client.execute(create_query)

        # Schema evolution
        try:
            source_info = ch_client.query(f"DESCRIBE TABLE {temp_table_fq}") or []
            target_info = ch_client.query(f"DESCRIBE TABLE {main_table_fq}") or []
            source_cols = {row["name"]: row.get("type") for row in source_info}
            target_cols = {row["name"]: row.get("type") for row in target_info}
            missing_cols = [col for col in source_cols if col not in target_cols]
            for col in missing_cols:
                col_type = source_cols.get(col, "Nullable(Float64)")
                ch_client.execute(f"ALTER TABLE {main_table_fq} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                logger.info("label_table_column_added", column=col, col_type=col_type)
        except Exception as exc:
            logger.warning("label_table_describe_failed", source=temp_table, error=str(exc))
            return
        
        # Determine date range for chunking
        try:
            date_range_query = f"SELECT min(ts), max(ts) FROM {temp_table_fq}"
            res = ch_client.query(date_range_query)
            min_ts, max_ts = res[0]['min(ts)'], res[0]['max(ts)']
            if not min_ts or not max_ts:
                logger.warning("merge_label_table_skipped_no_data", temp_table=temp_table_fq)
                return
        except Exception as e:
            logger.error("merge_label_table_get_range_failed", error=str(e))
            return

        # Chunked Insert
        current_date = min_ts.date()
        end_date = max_ts.date()
        common_cols = [col for col in source_cols.keys() if col in (target_cols.keys() | set(missing_cols))]
        cols_csv = ", ".join(f"`{c}`" for c in common_cols)

        # Build aggregation to de-duplicate within a day and prefer non-null labels
        def _agg_expr(col: str) -> str:
            if col.startswith("label_h"):
                return f"argMax({col}, isNotNull({col})) AS `{col}`"
            if col == "updated_at":
                return f"max(updated_at) AS `updated_at`"
            if col in ("ts", "symbol"):
                return f"`{col}`"
            return f"anyLast({col}) AS `{col}`"

        agg_select = ", ".join(_agg_expr(c) for c in common_cols)
        
        settings = f"""
        SETTINGS
            max_bytes_before_external_group_by = 2000000000,
            max_bytes_before_external_sort = 2000000000,
            max_memory_usage = {int(os.getenv("MERGE_LABEL_MAX_MEM", 6_000_000_000))},
            max_threads = {int(os.getenv("MERGE_LABEL_MAX_THREADS", 1))},
            use_uncompressed_cache = 0
        """

        while current_date <= end_date:
            start_of_day = datetime.combine(current_date, datetime.min.time())
            end_of_day = datetime.combine(current_date, datetime.max.time())
            
            logger.info("merge_label_table_chunk", date=current_date.isoformat())

            insert_query = f"""
                INSERT INTO {main_table_fq} ({cols_csv})
                SELECT {agg_select}
                FROM {temp_table_fq}
                WHERE ts >= '{start_of_day.strftime('%Y-%m-%d %H:%M:%S')}' AND ts <= '{end_of_day.strftime('%Y-%m-%d %H:%M:%S')}'
                GROUP BY symbol, ts
                {settings}
            """
            try:
                ch_client.execute(insert_query)
            except Exception as e:
                logger.error("merge_label_table_chunk_failed", date=current_date.isoformat(), error=str(e))

            current_date += timedelta(days=1)

        logger.info("label_table_merged_by_chunk", source=temp_table, columns=len(common_cols))
