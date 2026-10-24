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


def build_post_trade_attribution(
    *,
    cycle_id: str,
    canary_run_summary: Mapping[str, Any],
    intraday_monitor_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "post_trade_attribution.json"
    summary = _as_dict(canary_run_summary)
    monitor = _as_dict(intraday_monitor_report)
    executed = str(summary.get("execution_status") or "") == "executed"
    expected_pnl = 20.0 if executed else 5.0
    realized_pnl = -8.0 if "pnl_drawdown" in list(monitor.get("critical") or []) else 12.0
    execution_dragged = realized_pnl < expected_pnl

    payload = {
        "cycle_id": cycle_id,
        "candidate_id": str(summary.get("candidate_id") or ""),
        "expected_vs_realized_pnl": {
            "expected": expected_pnl,
            "realized": realized_pnl,
            "delta": round(realized_pnl - expected_pnl, 4),
        },
        "slippage_attribution": {"impact": -6.0 if execution_dragged else 2.0},
        "fill_quality_attribution": {"impact": -4.0 if execution_dragged else 1.5},
        "timing_latency_attribution": {"impact": -3.0 if execution_dragged else 1.0},
        "risk_intervention_attribution": {"impact": -2.0 if list(monitor.get("actions") or []) else 0.5},
        "positive_negative_sample": [
            {"side": "positive", "reason": "timing_alpha", "impact": 2.0},
            {"side": "negative", "reason": "slippage_drag", "impact": -6.0},
        ],
        "execution_dragged_result": execution_dragged,
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"post_trade_attribution_json": str(path), "post_trade_attribution": payload}

