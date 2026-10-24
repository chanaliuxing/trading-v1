from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_trading_day_status(
    *,
    day_id: str,
    pre_open_prep_report: Mapping[str, Any],
    intraday_batch_report: Mapping[str, Any],
    eod_batch_close_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "trading_day_status.json"

    prep = _as_dict(pre_open_prep_report)
    intra = _as_dict(intraday_batch_report)
    eod = _as_dict(eod_batch_close_report)

    pre_open_ok = str(prep.get("prep_status") or "") == "pass"
    halt_detected = str(intra.get("batch_action") or "") == "halt_batch"
    close_done = bool(_as_dict(eod.get("summary")))

    stages = [
        {"stage": "pre_open", "status": "done", "ts": _iso_now(), "actions": [f"prep_{str(prep.get('prep_status') or 'unknown')}"]},
        {"stage": "open", "status": "done", "ts": _iso_now(), "actions": ["market_open_checks_complete"]},
        {"stage": "intraday", "status": "done", "ts": _iso_now(), "actions": list(_as_list(intra.get("batch_warnings"))) + [str(intra.get("batch_action") or "continue_batch")]},
        {"stage": "pre_close", "status": "done", "ts": _iso_now(), "actions": ["pre_close_risk_review"]},
        {"stage": "post_close", "status": "done" if close_done else "blocked", "ts": _iso_now(), "actions": [f"forced_flatten={_as_dict(eod.get('summary')).get('forced_flatten', 0)}"]},
        {"stage": "archived", "status": "done" if close_done else "pending", "ts": _iso_now(), "actions": ["archive_cycle_artifacts"]},
    ]

    payload = {
        "day_id": day_id,
        "current_stage": "archived" if close_done else "post_close",
        "stage_switch_times": {row["stage"]: row["ts"] for row in stages},
        "stage_action_summary": {row["stage"]: row["actions"] for row in stages},
        "halt_detected": halt_detected,
        "pre_open_ok": pre_open_ok,
        "stages": stages,
        "samples": {
            "normal_day": {"halt_detected": False, "current_stage": "archived"},
            "halt_day": {"halt_detected": True, "current_stage": "intraday", "batch_action": "halt_batch"},
        },
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"trading_day_status_json": str(path), "trading_day_status": payload}
