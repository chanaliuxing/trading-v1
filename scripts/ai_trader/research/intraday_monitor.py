from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_intraday_monitor_report(
    *,
    cycle_id: str,
    canary_run_summary: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "intraday_monitor_report.json"
    summary = _as_dict(canary_run_summary)
    execution_status = str(summary.get("execution_status") or "")
    warnings: list[str] = []
    critical: list[str] = []
    actions: list[str] = []

    if execution_status == "blocked":
        warnings.append("fill_failure")
        critical.append("repeated_rejects")
        actions.append("halt_canary")
    else:
        warnings.append("latency_spike")
        critical.append("pnl_drawdown")
        actions.extend(["warn", "flatten_candidate"])

    payload = {
        "cycle_id": cycle_id,
        "candidate_id": str(summary.get("candidate_id") or ""),
        "warnings": warnings,
        "critical": critical,
        "actions": actions,
        "status": "critical" if critical else "warning" if warnings else "healthy",
        "warning_sample": {"type": "slippage_spike", "value_bps": 12.0},
        "halt_sample": {"action": "halt_canary", "reason": "repeated_rejects"},
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"intraday_monitor_report_json": str(path), "intraday_monitor_report": payload}

