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


def persist_operator_audit(
    *,
    cycle_id: str,
    output_dir: str | Path,
    operator_controls: Mapping[str, Any],
    capital_governor_report: Mapping[str, Any],
    session_control_report: Mapping[str, Any],
) -> Dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "operator_audit_log.jsonl"
    index_path = out / "control_decision_index.json"

    rows: list[Dict[str, Any]] = []
    controls = _as_dict(operator_controls)
    rows.append(
        {
            "ts": _iso_now(),
            "candidate_id": "*",
            "decision_type": "operator_controls",
            "decision": "loaded",
            "reason": f"global_pause={bool(controls.get('global_pause'))}, live_enabled={bool(controls.get('live_enabled'))}",
            "source_module": "operator_controls",
        }
    )

    for item in _as_list(_as_dict(capital_governor_report).get("items")):
        row = _as_dict(item)
        rows.append(
            {
                "ts": str(row.get("evaluated_at") or _iso_now()),
                "candidate_id": str(row.get("candidate_id") or ""),
                "decision_type": "capital_governor",
                "decision": str(row.get("decision") or ""),
                "reason": ",".join(str(x) for x in _as_list(row.get("reasons"))),
                "source_module": "capital_governor",
            }
        )

    for item in _as_list(_as_dict(session_control_report).get("items")):
        row = _as_dict(item)
        decision = "allow" if bool(row.get("session_allowed")) else "pause"
        rows.append(
            {
                "ts": str(row.get("evaluated_at") or _iso_now()),
                "candidate_id": str(row.get("candidate_id") or ""),
                "decision_type": "session_control",
                "decision": decision,
                "reason": ",".join(str(x) for x in _as_list(row.get("reasons"))),
                "source_module": "session_control",
            }
        )
        if bool(row.get("flatten_required")):
            rows.append(
                {
                    "ts": str(row.get("evaluated_at") or _iso_now()),
                    "candidate_id": str(row.get("candidate_id") or ""),
                    "decision_type": "session_control",
                    "decision": "flatten",
                    "reason": ",".join(str(x) for x in _as_list(row.get("reasons"))),
                    "source_module": "session_control",
                }
            )

    with log_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    index_payload = {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "counts": {
            "capital_reject": sum(1 for x in rows if x["decision_type"] == "capital_governor" and x["decision"] == "rejected"),
            "pause": sum(1 for x in rows if x["decision"] == "pause"),
            "flatten": sum(1 for x in rows if x["decision"] == "flatten"),
        },
        "samples": {
            "capital_reject": next((x for x in rows if x["decision_type"] == "capital_governor" and x["decision"] == "rejected"), None),
            "pause": next((x for x in rows if x["decision"] == "pause"), None),
            "flatten": next((x for x in rows if x["decision"] == "flatten"), None),
        },
    }
    index_path.write_text(json.dumps(index_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"operator_audit_log_jsonl": str(log_path), "control_decision_index_json": str(index_path)}
