from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.policy_notice_lifecycle import (
    apply_notice_lifecycle_read,
)


DEFAULT_POLICY_COMPATIBILITY_SUMMARY_PATH = "runs/ai_trader/research/state/policy_compatibility_summary.json"
DEFAULT_POLICY_OBSERVABILITY_CONSUMERS_PATH = "runs/ai_trader/research/state/policy_observability_consumers.json"
DEFAULT_POLICY_NOTICE_LIFECYCLE_PATH = "runs/ai_trader/research/state/policy_observability_notice_lifecycle.json"

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


def _sorted_recent(rows: Any) -> list[Dict[str, Any]]:
    items = [_as_dict(x) for x in _as_list(rows)]
    items.sort(key=lambda row: str(row.get("cycle_id") or ""), reverse=True)
    out: list[Dict[str, Any]] = []
    for row in items[:3]:
        out.append(
            {
                "cycle_id": str(row.get("cycle_id") or ""),
                "policy_state": str(row.get("policy_state") or "unknown"),
                "current_policy": _as_dict(row.get("current_policy")),
                "anomalies": [str(x) for x in _as_list(row.get("anomalies")) if str(x)],
                "legacy_hints": [str(x) for x in _as_list(row.get("legacy_hints")) if str(x)],
            }
        )
    return out


def _build_migration_notice(
    payload: Mapping[str, Any],
    *,
    migration_progress: Mapping[str, Any],
) -> Dict[str, Any]:
    progress = _as_dict(migration_progress)
    progress_status = str(progress.get("status") or "ok")
    if progress_status in {"unavailable", "degraded"}:
        level = "warn"
        return {
            "status": progress_status,
            "level": level,
            "summary": "Migration notice unavailable due to inventory state.",
            "details": {
                "reason": str(progress.get("reason") or "inventory_unavailable"),
                "source_path": str(progress.get("source_path") or ""),
            },
            "throttled": True,
            "throttle_key": f"{progress_status}:{progress.get('reason')}",
            "legacy_fields": ["policy_compatibility", "recent_hits"],
            "recommended_field": "policy_compatibility_observability",
            "top_legacy_consumers": [],
        }

    summary = _as_dict(_as_dict(payload).get("summary"))
    runtime_legacy = int(summary.get("runtime_legacy_consumers") or 0)
    total_runtime = int(summary.get("runtime_consumers") or 0)
    migrated_runtime = int(summary.get("runtime_new_only_consumers") or 0)
    docs_tests_only = int(summary.get("docs_or_tests_legacy_refs") or 0)
    progress_ratio = float(progress.get("progress_ratio") or 1.0)

    if runtime_legacy <= 0:
        return {
            "status": "none",
            "level": "none",
            "legacy_runtime_consumer_count": 0,
            "summary": "No runtime legacy consumer detected.",
            "details": {
                "runtime_legacy_consumer_count": 0,
                "total_runtime_consumers": total_runtime,
                "migrated_runtime_consumers": migrated_runtime,
                "docs_tests_only_references": docs_tests_only,
                "progress_ratio": progress_ratio,
            },
            "throttled": True,
            "throttle_key": "none:0",
            "recommended_field": "policy_compatibility_observability",
            "legacy_fields": ["policy_compatibility", "recent_hits"],
            "top_legacy_consumers": [],
        }
    entries = [_as_dict(x) for x in _as_list(_as_dict(payload).get("entries"))]
    runtime_legacy_rows = [
        row
        for row in entries
        if str(row.get("reference_type") or "") == "runtime" and bool(row.get("is_legacy"))
    ]
    runtime_legacy_rows.sort(key=lambda row: str(row.get("file_path") or ""))
    top = [
        {
            "file_path": str(row.get("file_path") or ""),
            "legacy_fields": [str(x) for x in _as_list(row.get("legacy_fields")) if str(x)],
            "migration_priority": str(row.get("migration_priority") or "high"),
        }
        for row in runtime_legacy_rows[:5]
    ]
    if runtime_legacy <= 2:
        level = "info"
    elif runtime_legacy <= 5:
        level = "warn"
    else:
        level = "high"

    include_top = level in {"warn", "high"}
    top_rows = top if include_top else []
    return {
        "status": "active",
        "level": level,
        "legacy_runtime_consumer_count": runtime_legacy,
        "summary": f"Legacy runtime consumers: {runtime_legacy}/{max(total_runtime, 0)} (progress {round(progress_ratio * 100.0, 1)}%).",
        "details": {
            "runtime_legacy_consumer_count": runtime_legacy,
            "total_runtime_consumers": total_runtime,
            "migrated_runtime_consumers": migrated_runtime,
            "docs_tests_only_references": docs_tests_only,
            "progress_ratio": progress_ratio,
        },
        "throttled": True,
        "throttle_key": f"{level}:{runtime_legacy}:{total_runtime}:{round(progress_ratio, 6)}",
        "recommended_field": "policy_compatibility_observability",
        "legacy_fields": ["policy_compatibility", "recent_hits"],
        "top_legacy_consumers": top_rows,
    }


def apply_migration_notice_lifecycle(
    *,
    notice: Mapping[str, Any],
    progress: Mapping[str, Any],
    state_path: str | Path,
    now_iso: str | None = None,
    silence_seconds: int = 3600,
    use_cache: bool = False,
) -> Dict[str, Any]:
    _ = progress
    return apply_notice_lifecycle_read(
        notice=notice,
        state_path=state_path,
        now_iso=now_iso,
        silence_seconds=silence_seconds,
        use_cache=use_cache,
    )


def _build_migration_progress(
    payload: Mapping[str, Any],
    *,
    inventory_exists: bool,
    source_path: str,
) -> Dict[str, Any]:
    if not inventory_exists:
        return {
            "status": "unavailable",
            "reason": "missing_inventory_file",
            "source_path": source_path,
            "runtime_legacy_consumer_count": 0,
            "total_runtime_consumers": 0,
            "migrated_runtime_consumers": 0,
            "docs_tests_only_references": 0,
            "progress_ratio": 0.0,
        }
    summary = _as_dict(_as_dict(payload).get("summary"))
    if not summary:
        return {
            "status": "degraded",
            "reason": "corrupt_inventory_file",
            "source_path": source_path,
            "runtime_legacy_consumer_count": 0,
            "total_runtime_consumers": 0,
            "migrated_runtime_consumers": 0,
            "docs_tests_only_references": 0,
            "progress_ratio": 0.0,
        }
    total_runtime = int(summary.get("runtime_consumers") or 0)
    runtime_legacy = int(summary.get("runtime_legacy_consumers") or 0)
    migrated_runtime = max(0, total_runtime - runtime_legacy)
    docs_tests_only_refs = int(summary.get("docs_or_tests_legacy_refs") or 0)
    if total_runtime <= 0:
        progress_ratio = 1.0
    else:
        progress_ratio = round(migrated_runtime / float(total_runtime), 6)
    return {
        "status": "ok",
        "reason": "ready",
        "source_path": source_path,
        "runtime_legacy_consumer_count": runtime_legacy,
        "total_runtime_consumers": total_runtime,
        "migrated_runtime_consumers": migrated_runtime,
        "docs_tests_only_references": docs_tests_only_refs,
        "progress_ratio": progress_ratio,
    }


def build_policy_compatibility_observability(
    path: str | Path = DEFAULT_POLICY_COMPATIBILITY_SUMMARY_PATH,
    consumers_inventory_path: str | Path = DEFAULT_POLICY_OBSERVABILITY_CONSUMERS_PATH,
    notice_lifecycle_state_path: str | Path | None = None,
) -> Dict[str, Any]:
    raw_path = str(path)
    target = Path(raw_path)
    inventory_source_path = str(consumers_inventory_path)
    inventory_exists = Path(inventory_source_path).exists()
    inventory_payload = _load_json(consumers_inventory_path)
    migration_progress = _build_migration_progress(
        inventory_payload,
        inventory_exists=inventory_exists,
        source_path=inventory_source_path,
    )
    migration_notice = _build_migration_notice(
        inventory_payload,
        migration_progress=migration_progress,
    )
    lifecycle_path = Path(notice_lifecycle_state_path) if notice_lifecycle_state_path else Path(str(consumers_inventory_path)).with_name("policy_observability_notice_lifecycle.json")
    lifecycle_bundle = apply_migration_notice_lifecycle(
        notice=migration_notice,
        progress=migration_progress,
        state_path=lifecycle_path,
        use_cache=True,
    )
    migration_notice = _as_dict(lifecycle_bundle.get("notice"))
    notice_lifecycle = _as_dict(lifecycle_bundle.get("lifecycle"))
    notice_lifecycle_state_status = str(lifecycle_bundle.get("state_status") or "ok")
    empty_counts = {
        "known": 0,
        "legacy": 0,
        "unknown": 0,
        "degraded": 0,
        "digest_changed_without_version_bump": 0,
    }
    empty_examples = {
        "legacy": [],
        "unknown": [],
        "degraded": [],
        "digest_changed_without_version_bump": [],
    }
    if not target.exists():
        return {
            "status": "unavailable",
            "reason": "missing_summary_file",
            "source_path": raw_path,
            "counts": empty_counts,
            "recent_examples": empty_examples,
            "recent_hits": empty_examples,
            "current_cycle": {},
            "migration_notice": migration_notice,
            "migration_progress": migration_progress,
            "notice_lifecycle": notice_lifecycle,
            "notice_lifecycle_state_status": notice_lifecycle_state_status,
        }

    payload = _load_json(target)
    if not payload:
        return {
            "status": "degraded",
            "reason": "corrupt_summary_file",
            "source_path": raw_path,
            "counts": empty_counts,
            "recent_examples": empty_examples,
            "recent_hits": empty_examples,
            "current_cycle": {},
            "migration_notice": migration_notice,
            "migration_progress": migration_progress,
            "notice_lifecycle": notice_lifecycle,
            "notice_lifecycle_state_status": notice_lifecycle_state_status,
        }

    summary = _as_dict(payload.get("summary"))
    recent_hits = _as_dict(payload.get("recent_hits"))
    current_cycle = _as_dict(payload.get("current_cycle"))
    recent_examples = {
        "legacy": _sorted_recent(recent_hits.get("legacy")),
        "unknown": _sorted_recent(recent_hits.get("unknown")),
        "degraded": _sorted_recent(recent_hits.get("degraded")),
        "digest_changed_without_version_bump": _sorted_recent(recent_hits.get("digest_changed_without_version_bump")),
    }
    return {
        "status": "ok",
        "reason": "ready",
        "source_path": raw_path,
        "counts": {
            "known": int(summary.get("known_count") or 0),
            "legacy": int(summary.get("legacy_count") or 0),
            "unknown": int(summary.get("unknown_count") or 0),
            "degraded": int(summary.get("degraded_count") or 0),
            "digest_changed_without_version_bump": int(summary.get("digest_changed_without_version_bump_count") or 0),
        },
        "recent_examples": recent_examples,
        "recent_hits": recent_examples,
        "current_cycle": {
            "cycle_id": str(current_cycle.get("cycle_id") or ""),
            "policy_state": str(current_cycle.get("policy_state") or "unknown"),
            "current_policy": _as_dict(current_cycle.get("current_policy")),
            "history_summary": _as_dict(current_cycle.get("history_summary")),
            "anomaly_count": int(current_cycle.get("anomaly_count") or 0),
            "legacy_hint_count": int(current_cycle.get("legacy_hint_count") or 0),
            "advisory_count": int(current_cycle.get("advisory_count") or 0),
            "anomalies": [str(x) for x in _as_list(current_cycle.get("anomalies")) if str(x)],
            "legacy_hints": [str(x) for x in _as_list(current_cycle.get("legacy_hints")) if str(x)],
            "advisories": [str(x) for x in _as_list(current_cycle.get("advisories")) if str(x)],
            "degraded": bool(current_cycle.get("degraded", False)),
        },
        "migration_notice": migration_notice,
        "migration_progress": migration_progress,
        "notice_lifecycle": notice_lifecycle,
        "notice_lifecycle_state_status": notice_lifecycle_state_status,
    }


def build_legacy_policy_compatibility(
    observability: Mapping[str, Any],
) -> Dict[str, Any]:
    obs = _as_dict(observability)
    current = _as_dict(obs.get("current_cycle"))
    history = _as_dict(current.get("history_summary"))
    return {
        "policy_state": str(current.get("policy_state") or "unknown"),
        "current_policy": _as_dict(current.get("current_policy")),
        "history_count": int(history.get("history_count") or 0),
        "anomaly_count": int(current.get("anomaly_count") or 0),
        "legacy_hint_count": int(current.get("legacy_hint_count") or 0),
        "advisory_count": int(current.get("advisory_count") or 0),
        "anomalies": [str(x) for x in _as_list(current.get("anomalies")) if str(x)][:5],
        "legacy_hints": [str(x) for x in _as_list(current.get("legacy_hints")) if str(x)][:5],
        "advisories": [str(x) for x in _as_list(current.get("advisories")) if str(x)][:5],
    }
