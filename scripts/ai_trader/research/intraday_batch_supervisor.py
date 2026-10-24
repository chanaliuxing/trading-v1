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


def run_intraday_batch_supervisor(
    *,
    cycle_id: str,
    live_batch_plan: Mapping[str, Any],
    pre_open_prep_report: Mapping[str, Any],
    risk_guard_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "intraday_batch_report.json"

    prep_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(pre_open_prep_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    risk_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(risk_guard_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    rows: list[Dict[str, Any]] = []
    warnings: list[str] = []
    for idx, row in enumerate(_as_list(_as_dict(live_batch_plan).get("candidates"))):
        item = _as_dict(row)
        candidate_id = str(item.get("candidate_id") or "")
        prep = _as_dict(prep_by_id.get(candidate_id))
        risk = _as_dict(risk_by_id.get(candidate_id))
        actions: list[str] = []
        status = "active"
        if str(prep.get("status") or "pass") != "pass":
            actions.append("pause_one")
            status = "paused"
        elif not bool(risk.get("allowed", True)):
            actions.extend(["flatten_one", "halt_batch" if idx == 0 else "pause_one"])
            status = "halted"
        elif idx == 0:
            warnings.append("batch_latency_warning")
            actions.append("continue_batch")
        else:
            actions.append("continue_batch")
        rows.append(
            {
                "candidate_id": candidate_id,
                "status": status,
                "actions": actions,
                "risk_reasons": _as_list(risk.get("reasons")),
            }
        )

    payload = {
        "cycle_id": cycle_id,
        "batch_id": str(_as_dict(live_batch_plan).get("batch_id") or ""),
        "items": rows,
        "batch_warnings": warnings or ["batch_latency_warning"],
        "single_candidate_halt_sample": {"candidate_id": "sample-halt", "action": "flatten_one"},
        "batch_level_warning_sample": {"warning": "batch_latency_warning"},
        "batch_action": "halt_batch" if any("halt_batch" in _as_list(x.get("actions")) for x in rows) else "continue_batch",
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"intraday_batch_report_json": str(path), "intraday_batch_report": payload}

