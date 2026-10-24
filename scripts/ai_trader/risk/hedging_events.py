"""Hedging events logger for monitoring and observability."""

from __future__ import annotations
import json
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

# Import metrics
try:
    from libs.metrics import (
        hedging_selection_total,
        hedging_execution_total,
        hedging_rejection_total,
        hedging_selection_duration_seconds,
        hedging_contract_quality,
        is_enabled as metrics_enabled,
    )
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


@dataclass
class HedgingEvent:
    """Single hedging event for audit trail."""

    timestamp: str
    event_type: str  # selection, validation, execution, rejection
    mode: str  # options_real, overlay, options_proxy
    success: bool
    details: Dict[str, Any]

    def to_json(self) -> str:
        """Serialize to JSON line."""
        return json.dumps(asdict(self))


class HedgingEventsLogger:
    """Logger for hedging events with metrics aggregation."""

    def __init__(self, runs_dir: str):
        """Initialize logger.

        Args:
            runs_dir: Base runs directory (e.g., runs/ai_trader)
        """
        self.runs_dir = Path(runs_dir)
        self.events_file = self.runs_dir / date.today().isoformat() / "hedging_events.jsonl"
        self.events_file.parent.mkdir(parents=True, exist_ok=True)

    def log_selection(
        self,
        success: bool,
        mode: str,
        details: Dict[str, Any],
        duration_seconds: Optional[float] = None,
    ):
        """Log contract selection event.

        Args:
            success: Whether selection succeeded
            mode: options_real, overlay, etc.
            details: Selection details (candidates, rejection reasons, contract details)
            duration_seconds: Selection duration for metrics
        """
        event = HedgingEvent(
            timestamp=datetime.now().isoformat(),
            event_type="selection",
            mode=mode,
            success=success,
            details=details,
        )
        self._write_event(event)

        # Emit Prometheus metrics
        if METRICS_AVAILABLE and metrics_enabled():
            status = "success" if success else "rejected"
            hedging_selection_total.labels(mode=mode, status=status).inc()

            if duration_seconds is not None:
                hedging_selection_duration_seconds.labels(mode=mode).observe(duration_seconds)

            # Quality metrics from successful selections
            if success and "contract" in details:
                contract = details["contract"]
                if "spread_bps" in contract:
                    hedging_contract_quality.labels(metric="spread_bps").observe(contract["spread_bps"])
                if "open_interest" in contract:
                    hedging_contract_quality.labels(metric="open_interest").observe(contract["open_interest"])
                if "delta" in contract and "delta_target" in details:
                    delta_error = abs(abs(contract["delta"]) - abs(details["delta_target"]))
                    hedging_contract_quality.labels(metric="delta_error").observe(delta_error)

    def log_validation(
        self,
        success: bool,
        occ_symbol: str,
        details: Dict[str, Any],
    ):
        """Log pre-execution validation event.

        Args:
            success: Whether validation passed
            occ_symbol: Option contract symbol
            details: Validation checks (spread, oi, last trade, etc.)
        """
        event = HedgingEvent(
            timestamp=datetime.now().isoformat(),
            event_type="validation",
            mode="options_real",
            success=success,
            details={"occ_symbol": occ_symbol, **details},
        )
        self._write_event(event)

    def log_execution(
        self,
        success: bool,
        mode: str,
        details: Dict[str, Any],
    ):
        """Log execution event.

        Args:
            success: Whether execution succeeded
            mode: options_real, overlay, dry_run
            details: Execution details (symbol, qty, price, notional, etc.)
        """
        event = HedgingEvent(
            timestamp=datetime.now().isoformat(),
            event_type="execution",
            mode=mode,
            success=success,
            details=details,
        )
        self._write_event(event)

        # Emit Prometheus metrics
        if METRICS_AVAILABLE and metrics_enabled():
            # Determine status
            if details.get("dry_run"):
                status = "dry_run"
            elif success:
                status = "submitted"
            else:
                status = "error"

            hedging_execution_total.labels(mode=mode, status=status).inc()

    def log_rejection(
        self,
        reason: str,
        mode: str,
        details: Optional[Dict[str, Any]] = None,
    ):
        """Log rejection event.

        Args:
            reason: Rejection reason (no_exposure, daily_limit, validation_failed, etc.)
            mode: options_real, overlay
            details: Additional context
        """
        event = HedgingEvent(
            timestamp=datetime.now().isoformat(),
            event_type="rejection",
            mode=mode,
            success=False,
            details={"reason": reason, **(details or {})},
        )
        self._write_event(event)

        # Emit Prometheus metrics
        if METRICS_AVAILABLE and metrics_enabled():
            hedging_rejection_total.labels(reason=reason).inc()

    def _write_event(self, event: HedgingEvent):
        """Write event to JSONL file."""
        try:
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(event.to_json() + "\n")
        except Exception:
            # Best effort - don't fail hedging due to logging errors
            pass

    def get_daily_stats(self) -> Dict[str, Any]:
        """Aggregate daily statistics from events.

        Returns:
            Dict with counts, success rates, rejection reasons, etc.
        """
        if not self.events_file.exists():
            return {
                "total_events": 0,
                "selections": {"total": 0, "success": 0, "failure": 0},
                "validations": {"total": 0, "success": 0, "failure": 0},
                "executions": {"total": 0, "success": 0, "failure": 0},
                "rejections": {},
                "avg_spread_bps": None,
                "avg_oi": None,
            }

        stats = {
            "total_events": 0,
            "selections": {"total": 0, "success": 0, "failure": 0},
            "validations": {"total": 0, "success": 0, "failure": 0},
            "executions": {"total": 0, "success": 0, "failure": 0},
            "rejections": {},
            "spreads": [],
            "ois": [],
        }

        try:
            with self.events_file.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue

                    event = json.loads(line)
                    stats["total_events"] += 1

                    event_type = event.get("event_type")
                    success = event.get("success", False)
                    details = event.get("details", {})

                    if event_type == "selection":
                        stats["selections"]["total"] += 1
                        if success:
                            stats["selections"]["success"] += 1
                            # Extract metrics from successful selection
                            contract = details.get("contract", {})
                            if contract.get("spread_bps"):
                                stats["spreads"].append(contract["spread_bps"])
                            if contract.get("open_interest"):
                                stats["ois"].append(contract["open_interest"])
                        else:
                            stats["selections"]["failure"] += 1

                    elif event_type == "validation":
                        stats["validations"]["total"] += 1
                        if success:
                            stats["validations"]["success"] += 1
                        else:
                            stats["validations"]["failure"] += 1

                    elif event_type == "execution":
                        stats["executions"]["total"] += 1
                        if success:
                            stats["executions"]["success"] += 1
                        else:
                            stats["executions"]["failure"] += 1

                    elif event_type == "rejection":
                        reason = details.get("reason", "unknown")
                        stats["rejections"][reason] = stats["rejections"].get(reason, 0) + 1

        except Exception:
            pass

        # Calculate averages
        if stats["spreads"]:
            stats["avg_spread_bps"] = sum(stats["spreads"]) / len(stats["spreads"])
        else:
            stats["avg_spread_bps"] = None

        if stats["ois"]:
            stats["avg_oi"] = sum(stats["ois"]) / len(stats["ois"])
        else:
            stats["avg_oi"] = None

        # Remove raw lists
        del stats["spreads"]
        del stats["ois"]

        return stats


def log_hedging_event(
    runs_dir: str,
    event_type: str,
    mode: str,
    success: bool,
    details: Dict[str, Any],
):
    """Convenience function to log a hedging event.

    Args:
        runs_dir: Base runs directory
        event_type: selection, validation, execution, rejection
        mode: options_real, overlay, dry_run
        success: Whether event succeeded
        details: Event details
    """
    logger = HedgingEventsLogger(runs_dir)

    if event_type == "selection":
        logger.log_selection(success, mode, details)
    elif event_type == "validation":
        logger.log_validation(success, details.get("occ_symbol", ""), details)
    elif event_type == "execution":
        logger.log_execution(success, mode, details)
    elif event_type == "rejection":
        logger.log_rejection(details.get("reason", "unknown"), mode, details)
