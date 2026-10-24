from __future__ import annotations
import json
from datetime import date
from pathlib import Path
from typing import Dict, Any


def evaluate_kill_switch(runs_dir: str, thresholds: Dict[str, Any] | None = None, brain=None) -> Dict[str, Any]:
    """Evaluate simple kill-switch; if brain provided, ask risk_monitor for action.

    thresholds: { dd_intraday_pct, dd_5d_pct, vol_spike_mult }
    Returns {risk_status, action, updated_limits, explanation}; persists to risk_status.json
    """
    thr = thresholds or {"dd_intraday_pct": 0.015, "dd_5d_pct": 0.04, "vol_spike_mult": 1.5}
    # TODO: wire real metrics (drawdown, realized vol, correlations). For now, placeholders
    metrics = {"dd_intraday": 0.0, "dd_5d": 0.0, "rv_mult": 1.0, "corr_spike": 0.0}
    status = {"risk_status": "OK", "action": "NONE", "updated_limits": {}, "explanation": "placeholder"}
    if brain is not None:
        try:
            ctx = {"metrics": metrics, "thresholds": thr}
            status = brain.decide("risk_monitor", ctx) or status
        except Exception:
            pass
    runs = Path(runs_dir) / date.today().isoformat()
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "risk_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status

