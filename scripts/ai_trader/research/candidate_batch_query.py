from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_BATCH_RECORD_PATH = "runs/ai_trader/research/state/release_candidate_batch_records.json"
DEFAULT_AUDIT_SUMMARY_PATH = "runs/ai_trader/research/state/release_candidate_batch_audit_summary.json"


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


def _load_json_status(path: str | Path) -> tuple[str, str, Dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return "unavailable", "missing_record_file", {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return "degraded", "corrupt_record_file", {}
    if not isinstance(payload, dict):
        return "degraded", "invalid_record_payload", {}
    return "ok", "ready", payload


def _parse_dt(text: str | None) -> datetime | None:
    value = str(text or "").strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def query_candidate_batch_records(
    *,
    batch_id: str | None = None,
    candidate_batch_status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    latest_n: int = 0,
    records_path: str | Path = DEFAULT_BATCH_RECORD_PATH,
    audit_summary_path: str | Path = DEFAULT_AUDIT_SUMMARY_PATH,
    export_path: str | Path | None = None,
) -> Dict[str, Any]:
    status, reason, payload = _load_json_status(records_path)
    audit_status, audit_reason, audit_payload = _load_json_status(audit_summary_path)
    filters = {
        "batch_id": str(batch_id or ""),
        "candidate_batch_status": str(candidate_batch_status or ""),
        "start_time": str(start_time or ""),
        "end_time": str(end_time or ""),
        "latest_n": max(0, int(latest_n)),
    }

    if status != "ok":
        result = {
            "status": status,
            "reason": reason,
            "matched_records": [],
            "total_matches": 0,
            "latest_match": {},
            "applied_filters": filters,
            "export_path": str(export_path) if export_path else "",
            "source_paths": {
                "records": str(records_path),
                "audit_summary": str(audit_summary_path),
            },
            "audit_summary_status": {"status": audit_status, "reason": audit_reason},
        }
    else:
        records = [_as_dict(x) for x in _as_list(payload.get("records"))]
        matched: list[Dict[str, Any]] = []
        start_dt = _parse_dt(start_time)
        end_dt = _parse_dt(end_time)
        for row in records:
            if batch_id and str(row.get("batch_id") or "") != str(batch_id):
                continue
            if candidate_batch_status and str(row.get("candidate_batch_status") or "") != str(candidate_batch_status):
                continue
            row_dt = _parse_dt(str(row.get("recorded_at") or ""))
            if start_dt and row_dt and row_dt < start_dt:
                continue
            if end_dt and row_dt and row_dt > end_dt:
                continue
            matched.append(row)

        if latest_n > 0:
            matched = matched[-max(0, int(latest_n)) :]

        result = {
            "status": "ok",
            "reason": "ready",
            "matched_records": matched,
            "total_matches": len(matched),
            "latest_match": _as_dict(matched[-1]) if matched else {},
            "applied_filters": filters,
            "export_path": str(export_path) if export_path else "",
            "source_paths": {
                "records": str(records_path),
                "audit_summary": str(audit_summary_path),
            },
            "audit_summary_status": {
                "status": audit_status,
                "reason": audit_reason,
                "latest_total_records": int(_as_dict(audit_payload).get("total_records") or 0),
            },
        }

    if export_path:
        target = Path(export_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Query and export release candidate batch records")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--candidate-batch-status", default="")
    parser.add_argument("--start-time", default="")
    parser.add_argument("--end-time", default="")
    parser.add_argument("--latest-n", type=int, default=0)
    parser.add_argument("--export-path", default="")
    args = parser.parse_args()

    result = query_candidate_batch_records(
        batch_id=str(args.batch_id or "") or None,
        candidate_batch_status=str(args.candidate_batch_status or "") or None,
        start_time=str(args.start_time or "") or None,
        end_time=str(args.end_time or "") or None,
        latest_n=int(args.latest_n or 0),
        export_path=str(args.export_path or "") or None,
    )
    print("query_status:", result.get("status"))
    print("total_matches:", result.get("total_matches"))
    if result.get("export_path"):
        print("query_export:", result.get("export_path"))


if __name__ == "__main__":
    main()

