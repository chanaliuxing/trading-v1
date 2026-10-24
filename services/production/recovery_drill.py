from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _report_payload(report: Any) -> dict[str, Any]:
    if report is None:
        return {}
    if is_dataclass(report):
        payload = asdict(report)
    else:
        payload = dict(getattr(report, "__dict__", {}) or {})
    ts = payload.get("timestamp")
    if isinstance(ts, datetime):
        payload["timestamp"] = ts.isoformat()
    return payload


class RuntimeRecoveryDrillRunner:
    """
    Machine-checkable runtime recovery drill harness.

    The runner is intentionally thin: callers provide a RuntimeReconciler wired
    with fake or staging broker/DB/order services.  The runner executes the
    recovery cycles, captures health transitions and order submissions, and
    writes a go/no-go JSON artifact.
    """

    def __init__(self, *, artifact_dir: str | Path = "runtime/readiness/drills") -> None:
        self.artifact_dir = Path(artifact_dir)

    async def run(
        self,
        *,
        reconciler: Any,
        drill_id: str,
        expected_final_event: str = "system_resumed",
        cycles: int = 2,
    ) -> dict[str, Any]:
        reports: list[dict[str, Any]] = []
        for _ in range(max(1, int(cycles))):
            report = await reconciler.run_once()
            reports.append(_report_payload(report))

        controller = getattr(reconciler, "system_health_controller", None)
        health_events = list(getattr(controller, "events", []) or [])
        order_manager = getattr(reconciler, "recovery_order_manager", None)
        execution_service = getattr(order_manager, "execution_service", None)
        submitted_orders = list(getattr(execution_service, "submitted", []) or [])
        final_report = reports[-1] if reports else {}
        final_event_types = [str(event.get("event_type") or "") for event in health_events if isinstance(event, dict)]

        go = (
            str(expected_final_event) in final_event_types
            and int(final_report.get("ghosts_found") or 0) == 0
            and int(final_report.get("orphans_found") or 0) == 0
            and int(final_report.get("mismatches") or 0) == 0
            and int(final_report.get("position_mismatches") or 0) == 0
        )
        artifact = {
            "artifact_type": "runtime_recovery_drill",
            "drill_id": str(drill_id or "runtime_recovery_drill"),
            "go_no_go": "go" if go else "no_go",
            "expected_final_event": str(expected_final_event),
            "health_events": health_events,
            "reports": reports,
            "submitted_orders": submitted_orders,
            "duplicate_order_count": max(0, len(submitted_orders) - len({str(o.get("decision_id") or "") for o in submitted_orders if isinstance(o, dict)})),
            "final_truth": {
                "ghosts_found": int(final_report.get("ghosts_found") or 0),
                "orphans_found": int(final_report.get("orphans_found") or 0),
                "mismatches": int(final_report.get("mismatches") or 0),
                "position_mismatches": int(final_report.get("position_mismatches") or 0),
            },
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        self._write_artifact(artifact)
        return artifact

    def _write_artifact(self, artifact: dict[str, Any]) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        drill_id = str(artifact.get("drill_id") or "runtime_recovery_drill").replace("/", "_")
        path = self.artifact_dir / f"{drill_id}.json"
        path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
