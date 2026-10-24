from __future__ import annotations

from typing import Any, Dict, Iterable

from services.data_availability import get_data_availability_service


class ProductionReadinessGate:
    """Machine-checkable production startup go/no-go summary."""

    def evaluate(
        self,
        *,
        exec_enabled: bool,
        market_ready: bool,
        exec_ready: bool,
        master_ready: bool,
        master_truth_complete: bool,
        decision_pipeline_ready: bool,
        execution_service_ready: bool,
        runtime_reconciler_ready: bool,
        runtime_reconcile_clean: bool,
        runtime_reconcile_evidence: Dict[str, Any] | None = None,
        eod_supervisor_ready: bool,
        loaded_strategy_ids: Iterable[str],
        mandatory_live_strategy_ids: Iterable[str],
    ) -> Dict[str, Any]:
        loaded = {str(item).strip() for item in loaded_strategy_ids or [] if str(item).strip()}
        mandatory = {str(item).strip() for item in mandatory_live_strategy_ids or [] if str(item).strip()}
        missing_live = sorted(mandatory - loaded)
        data_snapshot = get_data_availability_service().snapshot()
        evidence = dict(runtime_reconcile_evidence or {})
        blockers = []
        if not market_ready:
            blockers.append("broker_market_not_ready")
        if exec_enabled and not exec_ready:
            blockers.append("broker_exec_not_ready")
        if exec_enabled and not master_ready:
            blockers.append("broker_master_not_ready")
        if exec_enabled and not master_truth_complete:
            blockers.append("broker_master_truth_not_complete")
        if exec_enabled and not decision_pipeline_ready:
            blockers.append("decision_pipeline_not_ready")
        if exec_enabled and not execution_service_ready:
            blockers.append("execution_service_not_ready")
        if exec_enabled and not runtime_reconciler_ready:
            blockers.append("runtime_reconciler_not_ready")
        if exec_enabled and not runtime_reconcile_clean:
            blockers.append("runtime_reconcile_not_clean")
        if exec_enabled and evidence and not bool(evidence.get("platform_recovery_ready", True)):
            blockers.append("platform_recovery_not_ready")
        if exec_enabled and not eod_supervisor_ready:
            blockers.append("platform_eod_supervisor_not_ready")
        if exec_enabled and mandatory and not (loaded & mandatory):
            blockers.append("no_live_strategies_loaded")
        if missing_live:
            blockers.append("mandatory_live_strategy_missing")
        if int(data_snapshot.get("rth_entry_blocking_count") or 0) > 0:
            blockers.append("data_availability_entry_blocking")

        return {
            "production_ready": len(blockers) == 0,
            "blockers": blockers,
            "state": "RUNNING" if len(blockers) == 0 else "NON_TRADING_DEGRADED",
            "strategy_loaded_count": len(loaded),
            "mandatory_live_strategy_count": len(mandatory),
            "missing_live_strategy_ids": missing_live,
            "data_availability": data_snapshot,
            "broker": {
                "market_ready": bool(market_ready),
                "exec_ready": bool(exec_ready),
                "master_ready": bool(master_ready),
                "master_truth_complete": bool(master_truth_complete),
            },
            "decision_pipeline_ready": bool(decision_pipeline_ready),
            "execution_service_ready": bool(execution_service_ready),
            "runtime_reconciler_ready": bool(runtime_reconciler_ready),
            "runtime_reconcile_clean": bool(runtime_reconcile_clean),
            "runtime_reconcile_evidence": evidence,
            "platform_recovery_ready": bool(evidence.get("platform_recovery_ready", True)),
            "platform_eod_supervisor_ready": bool(eod_supervisor_ready),
        }
