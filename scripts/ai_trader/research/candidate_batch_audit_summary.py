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


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _retained_by_batch(records: list[Dict[str, Any]], keep_last_batches: int) -> list[Dict[str, Any]]:
    if keep_last_batches <= 0:
        return records
    retained: list[Dict[str, Any]] = []
    seen_batch_ids: set[str] = set()
    for row in reversed(records):
        batch_id = str(row.get("batch_id") or "")
        if batch_id and batch_id not in seen_batch_ids:
            seen_batch_ids.add(batch_id)
        if batch_id in seen_batch_ids and len(seen_batch_ids) <= keep_last_batches:
            retained.append(row)
    retained = list(reversed(retained))
    if records and records[-1] not in retained:
        retained.append(records[-1])
    return retained


def _apply_retention(
    records: list[Dict[str, Any]],
    *,
    keep_last_records: int,
    keep_last_batches: int,
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    all_records = list(records)
    retained = all_records
    mode = "none"
    if keep_last_batches > 0:
        retained = _retained_by_batch(all_records, keep_last_batches)
        mode = "batch_window"
    elif keep_last_records > 0:
        retained = all_records[-keep_last_records:]
        mode = "record_window"

    if all_records and all_records[-1] not in retained:
        retained.append(all_records[-1])

    pruned_count = max(0, len(all_records) - len(retained))
    pruned_batches = sorted({str(row.get("batch_id") or "") for row in all_records if row not in retained and str(row.get("batch_id") or "")})
    retention = {
        "mode": mode,
        "keep_last_records": int(keep_last_records),
        "keep_last_batches": int(keep_last_batches),
        "retained_records_count": len(retained),
        "pruned_records_count": pruned_count,
        "archive_hint": "trimmed_from_audit_view_only" if pruned_count > 0 else "no_trim_needed",
        "pruned_batch_ids": pruned_batches,
        "latest_record_protected": bool(not all_records or all_records[-1] in retained),
    }
    return retained, retention


def build_candidate_batch_audit_summary(
    *,
    batch_record_path: str | Path = DEFAULT_BATCH_RECORD_PATH,
    output_path: str | Path = DEFAULT_AUDIT_SUMMARY_PATH,
    keep_last_records: int = 50,
    keep_last_batches: int = 0,
) -> Dict[str, Any]:
    status, reason, payload = _load_json_status(batch_record_path)
    if status != "ok":
        summary = {
            "status": status,
            "reason": reason,
            "latest_batch": {},
            "total_records": 0,
            "verified_count": 0,
            "blocked_count": 0,
            "latest_blockers": [],
            "recent_batches": [],
            "retention": {
                "mode": "none",
                "keep_last_records": int(keep_last_records),
                "keep_last_batches": int(keep_last_batches),
                "retained_records_count": 0,
                "pruned_records_count": 0,
                "archive_hint": "no_source_records",
                "pruned_batch_ids": [],
                "latest_record_protected": True,
            },
            "source_path": str(batch_record_path),
            "generated_at": _iso_now(),
        }
    else:
        records = [_as_dict(x) for x in _as_list(payload.get("records"))]
        if not records and _as_dict(payload.get("latest")):
            records = [_as_dict(payload.get("latest"))]

        retained, retention = _apply_retention(
            records,
            keep_last_records=max(0, int(keep_last_records)),
            keep_last_batches=max(0, int(keep_last_batches)),
        )
        latest = _as_dict(records[-1]) if records else {}
        verified_count = sum(1 for row in retained if str(row.get("candidate_batch_status") or "") == "candidate_batch_verified")
        blocked_count = sum(1 for row in retained if str(row.get("candidate_batch_status") or "") == "candidate_batch_blocked")

        recent_batches = [
            {
                "batch_id": str(row.get("batch_id") or ""),
                "candidate_batch_status": str(row.get("candidate_batch_status") or "candidate_batch_blocked"),
                "signoff_ready": bool(row.get("signoff_ready", False)),
                "recorded_at": str(row.get("recorded_at") or ""),
            }
            for row in retained[-5:]
        ]

        summary = {
            "status": "ok",
            "reason": "ready",
            "latest_batch": latest,
            "total_records": len(retained),
            "verified_count": verified_count,
            "blocked_count": blocked_count,
            "latest_blockers": [str(x) for x in _as_list(latest.get("blockers")) if str(x)],
            "recent_batches": recent_batches,
            "retention": retention,
            "source_path": str(batch_record_path),
            "generated_at": _iso_now(),
        }

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "release_candidate_batch_audit_summary_json": str(target),
        "release_candidate_batch_audit_summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build read-only candidate batch audit summary")
    parser.add_argument("--keep-last-records", type=int, default=50)
    parser.add_argument("--keep-last-batches", type=int, default=0)
    args = parser.parse_args()

    bundle = build_candidate_batch_audit_summary(
        keep_last_records=int(args.keep_last_records),
        keep_last_batches=int(args.keep_last_batches),
    )
    payload = _as_dict(bundle.get("release_candidate_batch_audit_summary"))
    print("release_candidate_batch_audit_summary:", bundle.get("release_candidate_batch_audit_summary_json"))
    print("status:", payload.get("status"))


if __name__ == "__main__":
    main()

