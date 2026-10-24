from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_PREFLIGHT_RELEASE_CHECK_PATH = "runs/ai_trader/research/state/preflight_release_check.json"
DEFAULT_FREEZE_GATE_REPORT_PATH = "runs/ai_trader/research/state/research_safety_contract_gate_report.json"
DEFAULT_RELEASE_SIGNOFF_SUMMARY_PATH = "runs/ai_trader/research/state/release_signoff_summary.json"
DEFAULT_BATCH_RECORD_PATH = "runs/ai_trader/research/state/release_candidate_batch_records.json"


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


def _load_json(path: str | Path) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _is_preflight_pass(payload: Mapping[str, Any]) -> bool:
    data = _as_dict(payload)
    status = str(data.get("status") or "").strip().lower()
    if status in {"pass", "passed", "ok", "ready"}:
        return True
    overall_status = str(data.get("overall_status") or "").strip().lower()
    return overall_status in {"ready", "pass", "passed", "ok"}


def append_candidate_batch_record(
    *,
    batch_id: str,
    run_ids: list[str] | None = None,
    cycle_ids: list[str] | None = None,
    preflight_release_check_path: str | Path = DEFAULT_PREFLIGHT_RELEASE_CHECK_PATH,
    freeze_gate_report_path: str | Path = DEFAULT_FREEZE_GATE_REPORT_PATH,
    release_signoff_summary_path: str | Path = DEFAULT_RELEASE_SIGNOFF_SUMMARY_PATH,
    output_path: str | Path = DEFAULT_BATCH_RECORD_PATH,
    recorded_at: str | None = None,
) -> Dict[str, Any]:
    signoff = _load_json(release_signoff_summary_path)
    gate = _load_json(freeze_gate_report_path)
    preflight = _load_json(preflight_release_check_path)

    preflight_status = str(signoff.get("preflight_status") or ("pass" if _is_preflight_pass(preflight) else "fail"))
    freeze_gate_status = str(signoff.get("freeze_gate_status") or gate.get("gate_status") or "hold")
    signoff_ready = bool(signoff.get("signoff_ready", False))
    blockers = [str(x) for x in _as_list(signoff.get("blockers")) if str(x)]

    artifact_paths = {
        "preflight_release_check": str(preflight_release_check_path),
        "freeze_gate_report": str(freeze_gate_report_path),
        "release_signoff_summary": str(release_signoff_summary_path),
    }
    artifact_path_integrity = {
        name: bool(Path(path_text).exists())
        for name, path_text in artifact_paths.items()
    }
    missing_reports = [name for name, exists in artifact_path_integrity.items() if not exists]

    if missing_reports:
        signoff_ready = False
        blockers = sorted(set(blockers + [f"missing_report:{name}" for name in missing_reports]))

    candidate_batch_status = "candidate_batch_verified" if signoff_ready else "candidate_batch_blocked"
    entry = {
        "batch_id": str(batch_id),
        "run_ids": [str(x) for x in _as_list(run_ids) if str(x)],
        "cycle_ids": [str(x) for x in _as_list(cycle_ids) if str(x)],
        "preflight_status": preflight_status,
        "freeze_gate_status": freeze_gate_status,
        "signoff_ready": signoff_ready,
        "candidate_batch_status": candidate_batch_status,
        "blockers": blockers,
        "artifact_paths": artifact_paths,
        "artifact_path_integrity": artifact_path_integrity,
        "recorded_at": str(recorded_at or _iso_now()),
    }

    target = Path(output_path)
    existing = _load_json(target)
    records = [_as_dict(x) for x in _as_list(existing.get("records"))]
    records.append(entry)

    payload = {
        "records": records,
        "latest": entry,
        "updated_at": _iso_now(),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "release_candidate_batch_records_json": str(target),
        "entry": entry,
        "records_count": len(records),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Append release candidate batch verification record")
    parser.add_argument("--batch-id", required=False, default=f"batch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--run-id", action="append", default=[])
    parser.add_argument("--cycle-id", action="append", default=[])
    args = parser.parse_args()

    result = append_candidate_batch_record(
        batch_id=str(args.batch_id),
        run_ids=[str(x) for x in args.run_id],
        cycle_ids=[str(x) for x in args.cycle_id],
    )
    print("release_candidate_batch_records:", result["release_candidate_batch_records_json"])
    print("candidate_batch_status:", result["entry"].get("candidate_batch_status"))


if __name__ == "__main__":
    main()

