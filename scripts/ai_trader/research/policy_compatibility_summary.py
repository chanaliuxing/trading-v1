from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"
DEFAULT_CYCLES_ROOT = "runs/ai_trader/research/cycles"


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


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _degraded(entry: Mapping[str, Any]) -> bool:
    policy_state = str(entry.get("policy_state") or "unknown")
    anomaly_count = int(entry.get("anomaly_count") or 0)
    legacy_hint_count = int(entry.get("legacy_hint_count") or 0)
    return policy_state != "known" or anomaly_count > 0 or legacy_hint_count > 0


def _build_cycle_entry(*, cycle_id: str, preflight_check: Mapping[str, Any]) -> Dict[str, Any]:
    preflight = _as_dict(preflight_check)
    compat = _as_dict(preflight.get("policy_compatibility"))
    current = _as_dict(compat.get("current_policy"))
    history = _as_dict(compat.get("history_summary"))
    anomalies = [str(x) for x in _as_list(compat.get("anomalies")) if str(x)]
    legacy_hints = [str(x) for x in _as_list(compat.get("legacy_hints")) if str(x)]
    advisories = [str(x) for x in _as_list(compat.get("advisories")) if str(x)]
    has_compat = bool(compat)
    policy_state = str(compat.get("policy_state") or ("unknown" if not has_compat else "unknown"))

    if not has_compat:
        advisories.append("missing_policy_compatibility_section")

    entry = {
        "cycle_id": cycle_id,
        "generated_at": str(preflight.get("generated_at") or ""),
        "overall_status": str(preflight.get("overall_status") or ""),
        "policy_state": policy_state,
        "current_policy": {
            "policy_id": str(current.get("policy_id") or ""),
            "version": str(current.get("version") or ""),
            "digest": str(current.get("digest") or ""),
        },
        "history_summary": {
            "history_count": int(history.get("history_count") or 0),
            "known_versions": [str(x) for x in _as_list(history.get("known_versions")) if str(x)],
            "known_digests_count": int(history.get("known_digests_count") or 0),
            "match_by_version_count": int(history.get("match_by_version_count") or 0),
            "match_by_digest_count": int(history.get("match_by_digest_count") or 0),
        },
        "anomaly_count": len(anomalies),
        "legacy_hint_count": len(legacy_hints),
        "advisory_count": len(advisories),
        "anomalies": anomalies,
        "legacy_hints": legacy_hints,
        "advisories": advisories,
        "has_policy_compatibility": has_compat,
    }
    entry["degraded"] = _degraded(entry)
    return entry


def _collect_cycle_entries(
    *,
    cycles_root: Path,
    limit: int,
) -> tuple[list[Dict[str, Any]], Dict[str, int]]:
    entries: list[Dict[str, Any]] = []
    stats = {
        "scanned_cycle_dirs": 0,
        "loaded_preflight_checks": 0,
        "missing_preflight_checks": 0,
        "corrupt_preflight_checks": 0,
    }
    if not cycles_root.exists():
        return entries, stats

    cycle_dirs = sorted([x for x in cycles_root.iterdir() if x.is_dir()], key=lambda x: x.name)
    for cycle_dir in cycle_dirs[-max(1, int(limit)) :]:
        stats["scanned_cycle_dirs"] += 1
        preflight_path = cycle_dir / "preflight_check.json"
        if not preflight_path.exists():
            stats["missing_preflight_checks"] += 1
            continue
        try:
            payload = json.loads(preflight_path.read_text(encoding="utf-8"))
        except Exception:
            stats["corrupt_preflight_checks"] += 1
            continue
        if not isinstance(payload, dict):
            stats["corrupt_preflight_checks"] += 1
            continue
        stats["loaded_preflight_checks"] += 1
        entries.append(_build_cycle_entry(cycle_id=cycle_dir.name, preflight_check=payload))
    return entries, stats


def _take_recent(entries: list[Mapping[str, Any]], *, key: str, contains: str = "", limit: int = 3) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    for row in reversed(entries):
        values = [str(x) for x in _as_list(_as_dict(row).get(key))]
        matched = False
        if contains:
            matched = any(contains == x for x in values)
        else:
            matched = bool(_as_dict(row).get(key))
        if not matched:
            continue
        out.append(
            {
                "cycle_id": str(_as_dict(row).get("cycle_id") or ""),
                "policy_state": str(_as_dict(row).get("policy_state") or "unknown"),
                "current_policy": _as_dict(_as_dict(row).get("current_policy")),
                "anomalies": [str(x) for x in _as_list(_as_dict(row).get("anomalies")) if str(x)],
                "legacy_hints": [str(x) for x in _as_list(_as_dict(row).get("legacy_hints")) if str(x)],
            }
        )
        if len(out) >= limit:
            break
    return out


def build_policy_compatibility_summary(
    *,
    cycle_id: str,
    preflight_check: Mapping[str, Any],
    cycles_root: str | Path = DEFAULT_CYCLES_ROOT,
    max_cycles: int = 120,
) -> Dict[str, Any]:
    cycles_dir = Path(cycles_root)
    history_entries, scan_stats = _collect_cycle_entries(cycles_root=cycles_dir, limit=max_cycles)

    current_entry = _build_cycle_entry(cycle_id=cycle_id, preflight_check=preflight_check)
    by_cycle = {str(x.get("cycle_id") or ""): x for x in history_entries}
    by_cycle[cycle_id] = current_entry
    entries = [by_cycle[k] for k in sorted(by_cycle.keys()) if k]

    known_count = sum(1 for x in entries if str(x.get("policy_state") or "") == "known")
    unknown_count = sum(1 for x in entries if str(x.get("policy_state") or "") == "unknown")
    legacy_count = sum(1 for x in entries if int(x.get("legacy_hint_count") or 0) > 0)
    digest_changed_count = sum(
        1
        for x in entries
        if "digest_changed_without_version_bump" in [str(v) for v in _as_list(x.get("anomalies"))]
    )
    degraded_count = sum(1 for x in entries if bool(x.get("degraded")))

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "source": {
            "cycles_root": str(cycles_dir),
            "max_cycles": int(max_cycles),
            **scan_stats,
        },
        "current_cycle": current_entry,
        "summary": {
            "total_cycles": len(entries),
            "known_count": known_count,
            "legacy_count": legacy_count,
            "unknown_count": unknown_count,
            "digest_changed_without_version_bump_count": digest_changed_count,
            "degraded_count": degraded_count,
        },
        "recent_hits": {
            "legacy": _take_recent(entries, key="legacy_hints"),
            "unknown": [
                {
                    "cycle_id": str(x.get("cycle_id") or ""),
                    "policy_state": str(x.get("policy_state") or "unknown"),
                    "current_policy": _as_dict(x.get("current_policy")),
                }
                for x in reversed(entries)
                if str(x.get("policy_state") or "") == "unknown"
            ][:3],
            "digest_changed_without_version_bump": _take_recent(
                entries,
                key="anomalies",
                contains="digest_changed_without_version_bump",
            ),
            "degraded": [
                {
                    "cycle_id": str(x.get("cycle_id") or ""),
                    "policy_state": str(x.get("policy_state") or "unknown"),
                    "anomalies": [str(v) for v in _as_list(x.get("anomalies")) if str(v)],
                    "legacy_hints": [str(v) for v in _as_list(x.get("legacy_hints")) if str(v)],
                }
                for x in reversed(entries)
                if bool(x.get("degraded"))
            ][:3],
        },
        "cycles": entries,
    }


def persist_policy_compatibility_summary(
    *,
    cycle_id: str,
    preflight_check: Mapping[str, Any],
    state_root: str | Path = DEFAULT_STATE_ROOT,
    cycles_root: str | Path = DEFAULT_CYCLES_ROOT,
    max_cycles: int = 120,
) -> Dict[str, Any]:
    payload = build_policy_compatibility_summary(
        cycle_id=cycle_id,
        preflight_check=preflight_check,
        cycles_root=cycles_root,
        max_cycles=max_cycles,
    )
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "policy_compatibility_summary.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "policy_compatibility_summary_json": str(path),
        "policy_compatibility_summary": payload,
    }

