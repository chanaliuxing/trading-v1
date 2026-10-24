from __future__ import annotations

import json
import os
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except Exception:
        return None


def _jsonl_records(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                yield payload


def _git_head(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or None
    except Exception:
        return None


def _config_hash(root: Path) -> str:
    config_paths = [
        path
        for pattern in ("config.yml", "config.yaml", "configs/**/*.yml", "configs/**/*.yaml", "configs/**/*.json")
        for path in root.glob(pattern)
        if path.is_file()
    ]
    digest = hashlib.sha256()
    for path in sorted(config_paths):
        rel = str(path.relative_to(root))
        digest.update(rel.encode("utf-8"))
        try:
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        except Exception:
            digest.update(b"\0missing")
    return digest.hexdigest()


def _record_ts(record: dict[str, Any]) -> datetime | None:
    return _parse_dt(record.get("ts") or record.get("created_at"))


def _within(record: dict[str, Any], *, start: datetime | None, end: datetime | None) -> bool:
    ts = _record_ts(record)
    if ts is None:
        return False
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


def _truth_clean(payload: dict[str, Any]) -> bool:
    return (
        int(payload.get("ghosts") or payload.get("ghosts_found") or 0) == 0
        and int(payload.get("orphans_unresolved") or payload.get("unresolved_orphans") or 0) == 0
        and int(payload.get("mismatches") or 0) == 0
        and int(payload.get("position_mismatches") or 0) == 0
        and int(payload.get("active_quarantine_count") or 0) == 0
    )


def _startup_broker_truth_clean(payload: dict[str, Any]) -> bool:
    return (
        _truth_clean(payload)
        and payload.get("broker_connected") is True
        and payload.get("broker_truth_complete") is True
        and payload.get("order_truth_clean") is True
        and payload.get("position_ledger_clean") is True
    )


def _health_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _health_metadata(record: dict[str, Any]) -> dict[str, Any]:
    payload = _health_payload(record)
    metadata = payload.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _event_type(record: dict[str, Any]) -> str:
    return str(_health_payload(record).get("event_type") or "")


def _is_operator_drill_resume(record: dict[str, Any]) -> bool:
    payload = _health_payload(record)
    metadata = payload.get("metadata")
    reason = str(record.get("reason") or payload.get("reason") or "")
    return (
        str(payload.get("source") or "") == "run_system.operator_drill"
        or reason == "operator_paper_ib_restart_drill_verified"
        or (
            isinstance(metadata, dict)
            and str(metadata.get("drill_mode") or "") == "operator_paper"
            and reason != "production_state_soft_no_go"
        )
    )


def _is_soft_resume_heartbeat(record: dict[str, Any]) -> bool:
    payload = _health_payload(record)
    reason = str(record.get("reason") or payload.get("reason") or "")
    return (
        reason == "production_state_soft_no_go"
        or str(payload.get("source") or "") == "production_state_emitter"
    )


def _is_operator_drill_ib_event(record: dict[str, Any]) -> bool:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    reason = str(record.get("reason") or payload.get("reason") or "")
    return (
        reason.startswith("operator_paper_ib_restart_drill")
        or (bool(payload.get("simulated")) and str(payload.get("drill_mode") or "") == "operator_paper")
    )


def _timeline_item(kind: str, record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    item = {
        "kind": kind,
        "ts": str(record.get("ts") or ""),
        "component": str(record.get("component") or ""),
        "stage": str(record.get("stage") or ""),
        "status": str(record.get("status") or ""),
        "reason": str(record.get("reason") or _health_payload(record).get("reason") or ""),
    }
    item.update(extra)
    return item


def build_ib_restart_drill_artifact(
    *,
    repo_root: str | Path = ".",
    day: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
    output_dir: str | Path = "runtime/readiness/drills",
    drill_id: str = "paper_ib_restart_latest",
) -> dict[str, Any]:
    """Build a machine-checkable IB restart artifact from real runtime audit files."""
    root = Path(repo_root)
    day_s = str(day or datetime.now(ET).strftime("%Y-%m-%d"))
    audit_dir = root / "runtime" / "audit" / day_s
    start = _parse_dt(start_ts)
    end = _parse_dt(end_ts)

    system_records = [r for r in _jsonl_records(audit_dir.glob("system-*.jsonl")) if _within(r, start=start, end=end)]
    recon_records = [r for r in _jsonl_records(audit_dir.glob("recon-*.jsonl")) if _within(r, start=start, end=end)]
    system_records.sort(key=lambda r: _record_ts(r) or datetime.min.replace(tzinfo=ET))
    recon_records.sort(key=lambda r: _record_ts(r) or datetime.min.replace(tzinfo=ET))

    all_disconnects = [
        r
        for r in system_records
        if str(r.get("component") or "") == "market_data_service"
        and str(r.get("stage") or "") in {"ib_disconnected", "service_stopped"}
    ]
    all_reconnects = [
        r
        for r in system_records
        if str(r.get("component") or "") == "market_data_service"
        and str(r.get("stage") or "") in {"ib_connected", "service_started"}
    ]
    all_recovering = [r for r in system_records if str(r.get("component") or "") == "system_health_controller" and _event_type(r) == "system_recovering"]
    all_resumed = [r for r in system_records if str(r.get("component") or "") == "system_health_controller" and _event_type(r) == "system_resumed"]
    all_clean_recon = [
        r
        for r in recon_records
        if str(r.get("stage") or "") == "position_audit_snapshot"
        and _truth_clean(r.get("payload") if isinstance(r.get("payload"), dict) else {})
    ]

    last_disconnect = all_disconnects[-1] if all_disconnects else None
    last_disconnect_ts = _record_ts(last_disconnect) if last_disconnect else None
    if last_disconnect_ts is not None:
        disconnects = [r for r in all_disconnects if (_record_ts(r) or datetime.min.replace(tzinfo=ET)) >= last_disconnect_ts]
        reconnects = [r for r in all_reconnects if (_record_ts(r) or datetime.min.replace(tzinfo=ET)) >= last_disconnect_ts]
        recovering = [r for r in all_recovering if (_record_ts(r) or datetime.min.replace(tzinfo=ET)) >= last_disconnect_ts]
        resumed = [r for r in all_resumed if (_record_ts(r) or datetime.min.replace(tzinfo=ET)) >= last_disconnect_ts]
        clean_recon = [r for r in all_clean_recon if (_record_ts(r) or datetime.min.replace(tzinfo=ET)) >= last_disconnect_ts]
    else:
        disconnects = list(all_disconnects)
        reconnects = list(all_reconnects)
        recovering = list(all_recovering)
        resumed = list(all_resumed)
        clean_recon = list(all_clean_recon)
    reconnect_after_disconnect = [
        r for r in reconnects if last_disconnect_ts is None or ((_record_ts(r) or datetime.min.replace(tzinfo=ET)) >= last_disconnect_ts)
    ]
    resumed_after_reconnect: list[dict[str, Any]] = []
    if reconnect_after_disconnect:
        reconnect_ts = _record_ts(reconnect_after_disconnect[-1])
        resumed_after_reconnect = [
            r for r in resumed if reconnect_ts is None or ((_record_ts(r) or datetime.min.replace(tzinfo=ET)) >= reconnect_ts)
        ]

    operator_drill_resumed = [r for r in resumed_after_reconnect if _is_operator_drill_resume(r)]
    final_resumed = (
        operator_drill_resumed[-1]
        if operator_drill_resumed
        else (resumed_after_reconnect[-1] if resumed_after_reconnect else None)
    )
    final_resumed_ts = _record_ts(final_resumed) if final_resumed else None
    final_metadata = _health_metadata(final_resumed) if final_resumed else {}
    recovery_readiness = final_metadata.get("recovery_readiness") if isinstance(final_metadata.get("recovery_readiness"), dict) else {}
    final_recon = clean_recon[-1] if clean_recon else None
    final_recon_payload = final_recon.get("payload") if isinstance(final_recon, dict) and isinstance(final_recon.get("payload"), dict) else {}
    startup_broker_truth_clean = bool(
        final_recon is None
        and final_resumed is not None
        and str(final_resumed.get("component") or "") == "system_health_controller"
        and str(final_resumed.get("reason") or _health_payload(final_resumed).get("reason") or "") == "startup_broker_truth_ready"
        and _startup_broker_truth_clean(final_metadata)
    )

    process_recovery_without_disconnect = bool(
        not disconnects
        and reconnect_after_disconnect
        and recovering
        and resumed_after_reconnect
        and (clean_recon or startup_broker_truth_clean)
    )
    blockers: list[str] = []
    if not disconnects and not process_recovery_without_disconnect:
        blockers.append("ib_disconnect_event_missing")
    if not reconnect_after_disconnect:
        blockers.append("ib_reconnect_after_disconnect_missing")
    if not recovering:
        blockers.append("system_recovering_event_missing")
    if final_resumed is None:
        blockers.append("system_resumed_after_reconnect_missing")
    if not clean_recon and not startup_broker_truth_clean:
        blockers.append("runtime_reconcile_clean_missing")
    if final_resumed is not None:
        if final_metadata.get("platform_recovery_ready") is not True:
            blockers.append("platform_recovery_ready_not_true")
        if recovery_readiness.get("ready") is not True:
            blockers.append("recovery_readiness_not_ready")
        if not _truth_clean(final_metadata):
            blockers.append("system_resumed_truth_not_clean")
    if final_recon is not None and not _truth_clean(final_recon_payload):
        blockers.append("final_runtime_reconcile_not_clean")

    max_disconnect_events = max(1, int(os.getenv("IB_RESTART_DRILL_MAX_DISCONNECT_EVENTS", "10")))
    max_reconnect_events = max(1, int(os.getenv("IB_RESTART_DRILL_MAX_RECONNECT_EVENTS", "20")))
    max_resumed_events = max(1, int(os.getenv("IB_RESTART_DRILL_MAX_RESUMED_EVENTS", "20")))
    day_max_disconnect_events = max(1, int(os.getenv("IB_RESTART_DRILL_DAY_MAX_DISCONNECT_EVENTS", "10")))
    day_max_reconnect_events = max(1, int(os.getenv("IB_RESTART_DRILL_DAY_MAX_RECONNECT_EVENTS", "30")))
    day_max_resumed_events = max(1, int(os.getenv("IB_RESTART_DRILL_DAY_MAX_RESUMED_EVENTS", "60")))
    max_time_to_resume_sec = max(30.0, float(os.getenv("IB_RESTART_DRILL_MAX_TIME_TO_RESUME_SEC", "300")))
    stable_window_sec = max(30.0, float(os.getenv("IB_RESTART_DRILL_STABLE_WINDOW_SEC", "120")))
    time_to_resume_sec = None
    if last_disconnect_ts is not None and final_resumed_ts is not None:
        time_to_resume_sec = max(0.0, (final_resumed_ts - last_disconnect_ts).total_seconds())
    stable_window_observed_sec = None
    if final_resumed_ts is not None:
        stable_window_observed_sec = max(0.0, (datetime.now(ET) - final_resumed_ts).total_seconds())
    disconnect_after_resume = [
        r for r in disconnects if final_resumed_ts is not None and ((_record_ts(r) or datetime.min.replace(tzinfo=ET)) > final_resumed_ts)
    ]
    if len(disconnects) > max_disconnect_events:
        blockers.append("ib_disconnect_flapping_threshold_exceeded")
    if len(reconnects) > max_reconnect_events:
        blockers.append("ib_reconnect_flapping_threshold_exceeded")
    if len(resumed) > max_resumed_events:
        blockers.append("system_resume_flapping_threshold_exceeded")
    hard_day_disconnects = [r for r in all_disconnects if not _is_operator_drill_ib_event(r)]
    hard_day_reconnects = [r for r in all_reconnects if not _is_operator_drill_ib_event(r)]
    hard_day_resumed = [r for r in all_resumed if not _is_soft_resume_heartbeat(r)]
    if len(hard_day_disconnects) > day_max_disconnect_events:
        blockers.append("ib_day_disconnect_flapping_threshold_exceeded")
    if len(hard_day_reconnects) > day_max_reconnect_events:
        blockers.append("ib_day_reconnect_flapping_threshold_exceeded")
    operational_warnings: list[str] = []
    if len(hard_day_resumed) > day_max_resumed_events:
        operational_warnings.append("system_day_resume_flapping_threshold_exceeded")
    if time_to_resume_sec is not None and time_to_resume_sec > max_time_to_resume_sec:
        blockers.append("ib_time_to_resume_threshold_exceeded")
    if disconnects and final_resumed_ts is not None and stable_window_observed_sec is not None and stable_window_observed_sec < stable_window_sec:
        blockers.append("post_resume_stable_window_not_met")
    if disconnect_after_resume:
        blockers.append("disconnect_after_final_resume")

    timeline: list[dict[str, Any]] = []
    if last_disconnect is not None:
        timeline.append(_timeline_item("ib_disconnected", last_disconnect))
    if reconnect_after_disconnect:
        timeline.append(_timeline_item("ib_reconnected", reconnect_after_disconnect[-1]))
    if recovering:
        timeline.append(_timeline_item("system_recovering", recovering[-1], event_type="system_recovering"))
    if final_recon is not None:
        timeline.append(_timeline_item("runtime_reconcile_clean", final_recon, **{k: final_recon_payload.get(k) for k in ("ghosts", "orphans_unresolved", "mismatches", "position_mismatches", "active_quarantine_count")}))
    elif startup_broker_truth_clean and final_resumed is not None:
        timeline.append(_timeline_item("startup_broker_truth_clean", final_resumed, **{k: final_metadata.get(k) for k in ("ghosts", "unresolved_orphans", "mismatches", "position_mismatches", "active_quarantine_count")}))
    if final_resumed is not None:
        timeline.append(_timeline_item("system_resumed", final_resumed, event_type="system_resumed"))
    timeline.sort(key=lambda item: _parse_dt(item.get("ts")) or datetime.min.replace(tzinfo=ET))

    generated_at = datetime.now(ET).isoformat()
    artifact = {
        "artifact_type": "paper_ib_restart_drill",
        "drill_id": str(drill_id or "paper_ib_restart_latest"),
        "generated_at": generated_at,
        "source_pid": os.getpid(),
        "process_start_time": start.isoformat() if start is not None else None,
        "git_head": _git_head(root),
        "config_hash": _config_hash(root),
        "day": day_s,
        "go_no_go": "go" if not blockers else "no_go",
        "blockers": blockers,
        "proof_mode": "process_recovery_without_disconnect" if process_recovery_without_disconnect else "ib_restart_drill",
        "timeline": timeline,
        "counts": {
            "disconnect_events": len(disconnects),
            "reconnect_events": len(reconnects),
            "recovering_events": len(recovering),
            "resumed_events": len(resumed),
            "clean_reconcile_events": len(clean_recon),
            "startup_broker_truth_clean_events": 1 if startup_broker_truth_clean else 0,
        },
        "day_counts": {
            "disconnect_events": len(all_disconnects),
            "reconnect_events": len(all_reconnects),
            "recovering_events": len(all_recovering),
            "resumed_events": len(all_resumed),
            "hard_disconnect_events": len(hard_day_disconnects),
            "hard_reconnect_events": len(hard_day_reconnects),
            "hard_resumed_events": len(hard_day_resumed),
            "clean_reconcile_events": len(all_clean_recon),
        },
        "operational_warnings": operational_warnings,
        "flapping_thresholds": {
            "max_disconnect_events": max_disconnect_events,
            "max_reconnect_events": max_reconnect_events,
            "max_resumed_events": max_resumed_events,
            "day_max_disconnect_events": day_max_disconnect_events,
            "day_max_reconnect_events": day_max_reconnect_events,
            "day_max_resumed_events": day_max_resumed_events,
            "max_time_to_resume_sec": max_time_to_resume_sec,
            "stable_window_sec": stable_window_sec,
            "time_to_resume_sec": time_to_resume_sec,
            "stable_window_observed_sec": stable_window_observed_sec,
            "disconnect_after_final_resume_count": len(disconnect_after_resume),
        },
        "final_truth": {
            "ghosts": int(final_recon_payload.get("ghosts") or final_metadata.get("ghosts") or 0),
            "orphans_unresolved": int(final_recon_payload.get("orphans_unresolved") or final_metadata.get("unresolved_orphans") or 0),
            "mismatches": int(final_recon_payload.get("mismatches") or final_metadata.get("mismatches") or 0),
            "position_mismatches": int(final_recon_payload.get("position_mismatches") or final_metadata.get("position_mismatches") or 0),
            "active_quarantine_count": int(final_recon_payload.get("active_quarantine_count") or final_metadata.get("active_quarantine_count") or 0),
        },
        "recovery_readiness": recovery_readiness,
        "created_at": generated_at,
    }

    out_dir = root / output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = str(artifact["drill_id"]).replace("/", "_")
    path = out_dir / f"{safe_id}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    latest = out_dir / "paper_ib_restart_latest.json"
    latest.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    artifact["artifact_path"] = str(path)
    return artifact
