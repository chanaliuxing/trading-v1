#!/usr/bin/env python3
from __future__ import annotations
"""
Shared training/diagnostic gates for data/label readiness.

Provides:
    - Data quality gate (coverage/off-RTH) with optional auto-heal/backfill + retry
    - Label/feature preflight using brain.preflight with optional auto-heal + retry

Used by manual backtest/train tools so they behave like nightly flows.
"""
import json
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from scripts.ai_trader.brain.preflight import preflight_check_all

try:
    from libs.healer.data_healer_v2 import DataHealerV2
    from libs.health import Action
except Exception:  # pragma: no cover - heal is best-effort
    DataHealerV2 = None  # type: ignore
    Action = None  # type: ignore


def _parse_json_tail(raw: str) -> Dict[str, Any]:
    """Best-effort parse last non-empty line as JSON."""
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return {}
    try:
        return json.loads(lines[-1])
    except Exception:
        return {}


def _run_data_quality(symbols: List[str], days: int = 3) -> Tuple[bool, Dict[str, Any]]:
    """Run data_quality_check.py and return (passed, report)."""
    if not symbols:
        return True, {}
    cmd = [
        sys.executable,
        "tools/data_quality_check.py",
        "--symbols",
        *symbols,
        "--days",
        str(days),
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    report = _parse_json_tail(proc.stdout or "")
    unhealthy = False
    if isinstance(report, dict):
        for item in report.get("symbols", []):
            if item.get("status") in ("low_coverage", "off_rth_excess", "no_data"):
                unhealthy = True
                break
    return (not unhealthy), report


def _action_result_to_dict(res: Any) -> Dict[str, Any]:
    """Serialize ActionResult (or compatible) to a dict."""
    if res is None:
        return {"success": False, "error": "no_result"}
    out = {
        "success": bool(getattr(res, "success", False)),
        "error": getattr(res, "error", None),
        "rows": getattr(res, "rows_affected", None),
        "action_id": getattr(res, "action_id", None),
        "action_type": getattr(res, "action_type", None),
        "metadata": getattr(res, "metadata", None),
    }
    return out


def enforce_training_gates(
    symbols: List[str],
    horizons: List[int],
    window_days: int,
    features_table: str = "trading.features_1m_active",
    labels_table: str = "trading.features_1m_with_labels",
    data_quality_days: int = 3,
    auto_heal: bool = True,
    heal_lookback_days: int = 2,
) -> Dict[str, Any]:
    """
    Enforce data quality + label/feature readiness before running training/backtests.

    Returns a dict with detailed gate/heal status. Caller can inspect ["ready"].
    """
    result: Dict[str, Any] = {
        "ready": True,
        "data_quality": {},
        "preflight": {},
    }

    symbols = [s.strip().upper() for s in symbols if s.strip()]

    # ---------------- Data quality gate ----------------
    dq_ok, dq_report = _run_data_quality(symbols, days=data_quality_days)
    result["data_quality"] = {"ok": dq_ok, "report": dq_report, "heal_attempted": False}

    heal_results: List[Dict[str, Any]] = []
    if not dq_ok and auto_heal and DataHealerV2 and Action:
        healer = DataHealerV2(runs_dir="runs/ai_trader")
        # Backfill bars + rebuild features as a minimal auto-repair for coverage gaps
        for action_type, params in (
            ("backfill_bars", {"symbols": symbols, "days": heal_lookback_days}),
            ("rebuild_features", {"symbols": symbols, "lookback_days": heal_lookback_days}),
        ):
            act = Action(
                action_id=f"gate_{action_type}_{datetime.now().strftime('%H%M%S')}",
                action_type=action_type,
                priority=1,
                title=f"{action_type} via training gate",
                description=f"Auto-heal triggered by training gate for {symbols}",
                params=params,
                is_safe_auto_execute=True,
                requires_approval=False,
            )
            heal_results.append(_action_result_to_dict(healer.heal(act)))

        # Re-run data quality after healing attempt
        dq_ok, dq_report = _run_data_quality(symbols, days=data_quality_days)
        result["data_quality"] = {
            "ok": dq_ok,
            "report": dq_report,
            "heal_attempted": True,
            "heal_results": heal_results,
        }

    if not dq_ok:
        result["ready"] = False
        # If data quality is blocked, skip preflight to avoid noisy logs
        return result

    # ---------------- Label/feature preflight ----------------
    ready, report = preflight_check_all(
        symbols=symbols or ["TSLA"],
        horizons=horizons or [30],
        min_rows_per_symbol=1000,
        strict=False,  # labels are blocking, features warn
        lookback_days=max(window_days, 7),
        features_table=features_table,
        labels_table=labels_table,
        data_quality_gate=False,  # already handled above
    )
    result["preflight"] = {"ready": ready, "report": report, "heal_attempted": False}

    if ready or not (auto_heal and DataHealerV2 and Action):
        result["ready"] = result["ready"] and ready
        return result

    # Auto-heal label/feature gaps once, then re-check
    healer = DataHealerV2(runs_dir="runs/ai_trader")
    for action_type, params in (
        ("backfill_bars", {"symbols": symbols, "days": heal_lookback_days}),
        ("rebuild_features", {"symbols": symbols, "lookback_days": heal_lookback_days}),
        ("generate_label", {"symbols": symbols, "horizons": horizons, "lookback_days": window_days}),
    ):
        act = Action(
            action_id=f"gate_{action_type}_{datetime.now().strftime('%H%M%S')}",
            action_type=action_type,
            priority=1,
            title=f"{action_type} via training gate",
            description=f"Auto-heal triggered by training gate for {symbols}",
            params=params,
            is_safe_auto_execute=True,
            requires_approval=False,
        )
        heal_results.append(_action_result_to_dict(healer.heal(act)))

    ready_after, report_after = preflight_check_all(
        symbols=symbols or ["TSLA"],
        horizons=horizons or [30],
        min_rows_per_symbol=1000,
        strict=False,
        lookback_days=max(window_days, 7),
        features_table=features_table,
        labels_table=labels_table,
        data_quality_gate=False,
    )

    result["preflight"] = {
        "ready": ready_after,
        "report": report_after,
        "heal_attempted": True,
        "heal_results": heal_results,
        "report_before": report,
    }
    result["ready"] = result["ready"] and ready_after
    return result

