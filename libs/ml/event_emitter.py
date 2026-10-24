#!/usr/bin/env python3
"""
ML Event Emitter - Meta v3.4
Unified event emission for all ML/Research/Self-Healing activities.

This module provides a simple interface to emit events to ml.events table,
ensuring all autonomous actions are tracked for observability and audit trail.

Usage:
    from libs.ml.event_emitter import emit_self_heal_event, emit_ml_job_event

    # Self-healing action
    emit_self_heal_event(
        action_type="generate_label",
        action_id="heal_20251203_001",
        status="success",
        symbol="TSLA",
        duration_sec=15.3,
        rows_affected=1200
    )

    # ML training job
    emit_ml_job_event(
        event="ml.job.complete",
        experiment_id="exp_20251203_001",
        model_name="lightgbm",
        status="success",
        sharpe=1.85,
        duration_sec=120.5
    )
"""
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import structlog

logger = structlog.get_logger(__name__)

# Lazy load ClickHouse client to avoid circular imports
_ch_client = None


def _get_ch_client():
    """Lazy load ClickHouse client"""
    global _ch_client
    if _ch_client is None:
        try:
            from libs.db.ch_pool import get_client
            _ch_client = get_client()
        except Exception as e:
            logger.warning(f"Failed to initialize ClickHouse client: {e}")
            _ch_client = None
    return _ch_client


def emit_event(
    event: str,
    component: str = "",
    action_id: str = "",
    symbol: str = "",
    status: str = "",
    reason: str = "",
    duration_sec: float = 0.0,
    rows_affected: int = 0,
    action_type: str = "",
    is_auto_executed: bool = False,
    experiment_id: str = "",
    model_name: str = "",
    sharpe: float = 0.0,
    null_ratio: float = 0.0,
    coverage_ratio: float = 0.0,
    ev: float = None,
    beta_corr: float = None,
    write_jsonl: bool = True,
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Emit a generic event to ml.events table.

    Args:
        event: Event type (e.g., 'ml.job.start', 'self_heal.action_complete')
        component: Component name (e.g., 'data_healer', 'auto_remediation')
        action_id: Unique action ID for tracking
        symbol: Symbol if applicable
        status: Status (e.g., 'success', 'failed', 'in_progress')
        reason: Failure reason or action reason
        duration_sec: Action duration in seconds
        rows_affected: Rows inserted/updated/deleted
        action_type: Self-healing action type
        is_auto_executed: True if auto-executed
        experiment_id: Experiment ID for ML jobs
        model_name: Model name
        sharpe: Model performance metric
        null_ratio: NULL ratio for data quality
        coverage_ratio: Coverage ratio
        metadata: Additional context as dict

    Returns:
        True if event was successfully emitted, False otherwise
    """
    try:
        client = _get_ch_client()
        if client is None:
            logger.warning("event_emission_skipped_no_client", event=event)
            return False

        # Serialize metadata to JSON.
        # Keep non-schema metrics in metadata for backward compatibility.
        metadata_obj = dict(metadata or {})
        if ev is not None:
            metadata_obj.setdefault("ev", ev)
        if beta_corr is not None:
            metadata_obj.setdefault("beta_corr", beta_corr)
        metadata_json = json.dumps(metadata_obj)

        # Prepare event data
        event_data = {
            "ts": datetime.now(timezone.utc),
            "event": event,
            "component": component,
            "action_id": action_id,
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "duration_sec": duration_sec,
            "rows_affected": rows_affected,
            "action_type": action_type,
            "is_auto_executed": 1 if is_auto_executed else 0,
            "experiment_id": experiment_id,
            "model_name": model_name,
            "sharpe": sharpe,
            "null_ratio": null_ratio,
            "coverage_ratio": coverage_ratio,
            "metadata": metadata_json
        }

        # Insert into ml.events with explicit column order.
        # clickhouse_connect expects row arrays when column_names are provided.
        column_names = list(event_data.keys())
        row = [event_data[col] for col in column_names]
        client.insert("ml.events", [row], column_names=column_names)

        logger.debug(
            "event_emitted",
            emitted_event=event,
            component=component,
            action_id=action_id,
            status=status
        )

        return True

    except Exception as e:
        logger.error(
            "event_emission_failed",
            event_type=event,
            component=component,
            error=str(e),
            exc_info=True
        )
        return False


def emit_self_heal_event(
    action_type: str,
    action_id: str,
    status: str,
    symbol: str = "",
    duration_sec: float = 0.0,
    rows_affected: int = 0,
    reason: str = "",
    is_auto_executed: bool = True,
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Emit a self-healing event.

    Args:
        action_type: Action type (e.g., 'generate_label', 'rebuild_features', 'backfill_bars')
        action_id: Unique action ID
        status: Status ('success', 'failed', 'in_progress')
        symbol: Symbol if applicable
        duration_sec: Action duration
        rows_affected: Rows affected
        reason: Reason for action or failure
        is_auto_executed: True if auto-executed
        metadata: Additional context

    Returns:
        True if successful
    """
    # Determine event name based on status
    if status == "success":
        event = "self_heal.action_complete"
    elif status == "failed":
        event = "self_heal.action_failed"
    else:
        event = "self_heal.action_start"

    return emit_event(
        event=event,
        component="data_healer_v2",
        action_id=action_id,
        symbol=symbol,
        status=status,
        reason=reason,
        duration_sec=duration_sec,
        rows_affected=rows_affected,
        action_type=action_type,
        is_auto_executed=is_auto_executed,
        metadata=metadata
    )


def emit_auto_remediation_event(
    action_taken: str,
    status: str,
    symbol: str,
    duration_sec: float = 0.0,
    reason: str = "",
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Emit an auto-remediation event.

    Args:
        action_taken: Action taken (e.g., 'sync_features_active', 'window_adjustment')
        status: Status ('success', 'failed')
        symbol: Symbol
        duration_sec: Duration
        reason: Reason
        metadata: Additional context

    Returns:
        True if successful
    """
    event = f"auto_remediation.{action_taken}"

    return emit_event(
        event=event,
        component="auto_remediator",
        symbol=symbol,
        status=status,
        reason=reason,
        duration_sec=duration_sec,
        action_type=action_taken,
        is_auto_executed=True,
        metadata=metadata
    )


def emit_ml_job_event(
    event: str,
    experiment_id: str,
    model_name: str = "",
    status: str = "",
    sharpe: float = 0.0,
    ev: float = None,
    beta_corr: float = None,
    duration_sec: float = 0.0,
    symbol: str = "",
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Emit an ML training job event.

    Args:
        event: Event type ('ml.job.start', 'ml.job.complete', 'ml.job.failed')
        experiment_id: Experiment ID
        model_name: Model name
        status: Status ('success', 'failed', 'in_progress')
        sharpe: Sharpe ratio
        ev: Expected Value (optional)
        beta_corr: Beta correlation (optional)
        duration_sec: Training duration
        symbol: Symbol if applicable
        metadata: Additional context

    Returns:
        True if successful
    """
    return emit_event(
        event=event,
        component="ml_scheduler",
        experiment_id=experiment_id,
        model_name=model_name,
        status=status,
        sharpe=sharpe,
        ev=ev,
        beta_corr=beta_corr,
        duration_sec=duration_sec,
        symbol=symbol,
        metadata=metadata
    )


def emit_data_sync_event(
    status: str,
    symbol: str,
    rows_synced: int = 0,
    duration_sec: float = 0.0,
    reason: str = "",
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Emit a data sync event.

    Args:
        status: Status ('success', 'failed', 'in_progress')
        symbol: Symbol
        rows_synced: Rows synced
        duration_sec: Duration
        reason: Reason for sync or failure
        metadata: Additional context

    Returns:
        True if successful
    """
    if status == "success":
        event = "active_table.sync.complete"
    elif status == "failed":
        event = "active_table.sync.failed"
    else:
        event = "active_table.sync.start"

    return emit_event(
        event=event,
        component="active_table_manager",
        symbol=symbol,
        status=status,
        reason=reason,
        duration_sec=duration_sec,
        rows_affected=rows_synced,
        is_auto_executed=True,
        metadata=metadata
    )


def emit_preflight_event(
    status: str,
    symbol: str,
    reason: str = "",
    null_ratio: float = 0.0,
    coverage_ratio: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Emit a preflight check event.

    Args:
        status: Status ('passed', 'failed', 'needs_healing')
        symbol: Symbol
        reason: Failure reason
        null_ratio: NULL ratio
        coverage_ratio: Coverage ratio
        metadata: Additional context

    Returns:
        True if successful
    """
    event = f"preflight.check_{status}"

    return emit_event(
        event=event,
        component="preflight",
        symbol=symbol,
        status=status,
        reason=reason,
        null_ratio=null_ratio,
        coverage_ratio=coverage_ratio,
        metadata=metadata
    )


# Convenience functions for common patterns

def emit_heal_start(action_type: str, action_id: str, symbol: str = "", metadata: Optional[Dict[str, Any]] = None) -> bool:
    """Emit event when self-healing action starts"""
    return emit_self_heal_event(
        action_type=action_type,
        action_id=action_id,
        status="in_progress",
        symbol=symbol,
        metadata=metadata
    )


def emit_heal_success(
    action_type: str,
    action_id: str,
    symbol: str = "",
    duration_sec: float = 0.0,
    rows_affected: int = 0,
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """Emit event when self-healing action succeeds"""
    return emit_self_heal_event(
        action_type=action_type,
        action_id=action_id,
        status="success",
        symbol=symbol,
        duration_sec=duration_sec,
        rows_affected=rows_affected,
        metadata=metadata
    )


def emit_heal_failure(
    action_type: str,
    action_id: str,
    symbol: str = "",
    duration_sec: float = 0.0,
    reason: str = "",
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """Emit event when self-healing action fails"""
    return emit_self_heal_event(
        action_type=action_type,
        action_id=action_id,
        status="failed",
        symbol=symbol,
        duration_sec=duration_sec,
        reason=reason,
        metadata=metadata
    )
