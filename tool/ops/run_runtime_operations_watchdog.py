#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_PROCESS_START_TS = datetime.now(timezone.utc).timestamp()

from services.notifications.base import NotificationEvent
from services.notifications.factory import build_notification_manager
from services.data_availability import configure_data_availability_service
from services.production.ledger_integrity_service import (
    disabled_ledger_integrity_status,
    failed_ledger_integrity_status,
    query_position_ledger_integrity,
    write_ledger_integrity_artifact,
)
from services.production.fill_attribution_service import (
    disabled_fill_attribution_status,
    failed_fill_attribution_status,
    query_fill_attribution_readiness,
    write_fill_attribution_artifact,
)
from services.production.broker_truth_snapshot_service import (
    build_live_broker_truth_snapshot_from_manager,
    disabled_broker_truth_snapshot_status,
    failed_broker_truth_snapshot_status,
    query_broker_truth_snapshot,
    write_broker_truth_snapshot_artifact,
    write_live_broker_truth_snapshot_artifact,
)
from services.production.contract_selection_service import build_contract_selection_status
from services.production.option_trigger_ledger_service import (
    build_option_trigger_ledger,
    disabled_option_trigger_ledger_status,
    failed_option_trigger_ledger_status,
    write_option_trigger_ledger_artifact,
)
from services.production.strategy_manifest_gate_service import build_strategy_manifest_gate
from services.production.strategy_tradability import build_strategy_tradability_snapshot
from services.production.strategy_tradability import read_strategy_tradability_heartbeats
from libs.market_calendar import is_trading_day
from tool.ops.build_opening_health_report import ET, _find_run_system_pids, _pid_alive
from tool.ops.qqq_live_strategy_aliases import matched_qqq_live_strategy_ids
from tool.ops.runtime_reconciliation_health import build_runtime_reconciliation_health
from tool.ops.system_health_artifact import latest_system_health_transition


CRITICAL_RUNTIME_MODULES = [
    "run_system.py",
    "services/health/controller.py",
    "services/production/production_state_emitter.py",
    "services/production/recovery_readiness.py",
    "services/production/position_safety_supervisor.py",
    "services/production/broker_truth_snapshot_service.py",
    "services/production/contract_selection_service.py",
    "services/production/fill_attribution_service.py",
    "services/production/ib_restart_drill_artifact.py",
    "services/production/ledger_integrity_service.py",
    "services/production/option_trigger_ledger_service.py",
    "services/production/recovery_incident_service.py",
    "services/production/session_end_supervisor.py",
    "services/production/strategy_manifest_gate_service.py",
    "services/production/strategy_tradability.py",
    "services/reconciliation/broker_truth_convergence.py",
    "services/reconciliation/runtime_reconciler.py",
    "services/reconciliation/contracts.py",
    "services/reconciliation/order_quarantine.py",
    "services/risk/risk_service.py",
    "services/strategy/engine.py",
    "services/telegram/manual_flatten_service.py",
    "services/exec/safety_kernel.py",
    "services/exec/execution_service.py",
    "strategies/qqq_intraday_strategy.py",
    "strategies/stocks_kernel/strategy.py",
    "strategies/stocks_kernel/modules/order_lifecycle.py",
    "strategies/stocks_kernel/modules/protection.py",
    "strategies/options_kernel/base.py",
    "strategies/options_universal/base_strategy.py",
    "strategies/options_universal/builder.py",
    "strategies/options_universal/plugin.py",
    "strategies/options_universal/template_plugin.py",
    "strategies/options_plugins/qqq_intraday_option.py",
    "strategies/qqq_0dte_opt_live_strategy.py",
    "tool/ops/run_runtime_operations_watchdog.py",
]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_config(path: Path) -> dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _telegram_status(config: dict[str, Any]) -> dict[str, Any]:
    ncfg = (config.get("notifications") or {}) if isinstance(config, dict) else {}
    tcfg = (ncfg.get("telegram") or {}) if isinstance(ncfg, dict) else {}
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", str(tcfg.get("bot_token") or "")).strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", str(tcfg.get("chat_id") or "")).strip()
    enabled_env = os.getenv("TELEGRAM_NOTIFY_ENABLED", "").strip().lower()
    enabled_cfg = bool(ncfg.get("enabled", True)) and bool(tcfg.get("enabled", True))
    if enabled_env in {"0", "false", "no", "off"}:
        enabled_cfg = False
    return {
        "enabled": enabled_cfg,
        "bot_token_present": bool(bot_token),
        "chat_id_present": bool(chat_id),
        "available": enabled_cfg and bool(bot_token) and bool(chat_id),
    }


def _disk_space_status(*, root: Path, config: dict[str, Any]) -> dict[str, Any]:
    ops_cfg = config.get("operations") if isinstance(config, dict) else {}
    watchdog_cfg = (ops_cfg or {}).get("runtime_watchdog") if isinstance(ops_cfg, dict) else {}
    enabled = bool((watchdog_cfg or {}).get("check_disk_space", True))
    min_free_bytes = int(
        os.getenv(
            "PRODUCTION_MIN_FREE_DISK_BYTES",
            str((watchdog_cfg or {}).get("min_free_disk_bytes", 1024 * 1024 * 1024)),
        )
        or 0
    )
    configured_paths = (watchdog_cfg or {}).get("disk_space_paths") if isinstance(watchdog_cfg, dict) else None
    raw_paths = configured_paths if isinstance(configured_paths, list) and configured_paths else [str(root)]
    records: list[dict[str, Any]] = []
    blockers: list[str] = []
    lookup_failed = False
    for raw in raw_paths:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            usage = shutil.disk_usage(path)
        except Exception as exc:
            lookup_failed = True
            records.append({"path": str(path), "lookup_failed": True, "error": str(exc)})
            blockers.append("disk_space_lookup_failed")
            continue
        free_bytes = int(usage.free)
        total_bytes = int(usage.total)
        used_bytes = int(usage.used)
        free_ratio = (free_bytes / total_bytes) if total_bytes > 0 else 0.0
        record = {
            "path": str(path),
            "lookup_failed": False,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "free_gib": round(free_bytes / float(1024**3), 3),
            "free_ratio": free_ratio,
            "min_free_bytes": min_free_bytes,
        }
        if free_bytes < min_free_bytes:
            record["blocker"] = "disk_free_space_below_minimum"
            blockers.append("disk_free_space_below_minimum")
        records.append(record)
    blockers = sorted(set(blockers))
    return {
        "artifact_type": "disk_space",
        "enabled": enabled,
        "go_no_go": "go" if enabled and not blockers else ("go" if not enabled else "no_go"),
        "lookup_failed": lookup_failed,
        "min_free_bytes": min_free_bytes,
        "records": records,
        "blockers": blockers if enabled else [],
        "clear_conditions": [] if (not enabled or not blockers) else ["free_disk_space_or_raise_configured_threshold"],
    }


def _disk_space_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(status.get("enabled", True)):
        return []
    failures: list[dict[str, Any]] = []
    if bool(status.get("lookup_failed")):
        failures.append(
            {
                "issue_key": "disk_space_lookup_failed",
                "severity": "ERROR",
                "service": "DISK_SPACE",
                "error_type": "disk_space_lookup_failed",
                "detail": "failed_to_query_disk_usage",
                "records": list(status.get("records") or []),
            }
        )
    for record in list(status.get("records") or []):
        if not isinstance(record, dict):
            continue
        if str(record.get("blocker") or "") == "disk_free_space_below_minimum":
            failures.append(
                {
                    "issue_key": "disk_free_space_below_minimum",
                    "severity": "ERROR",
                    "service": "DISK_SPACE",
                    "error_type": "disk_free_space_below_minimum",
                    "detail": "free_disk_space_below_configured_minimum",
                    "path": record.get("path"),
                    "free_bytes": record.get("free_bytes"),
                    "free_gib": record.get("free_gib"),
                    "min_free_bytes": record.get("min_free_bytes"),
                }
            )
    return failures


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    return max(0.0, (now - ts.astimezone(ET)).total_seconds())


def _read_last_jsonl_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return {}
    for raw in reversed(lines):
        text = raw.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _strategy_audit_summary(audit_dir: Path, strategy_ids: str | list[str] | tuple[str, ...]) -> dict[str, Any]:
    if isinstance(strategy_ids, str):
        expected_strategy_ids = [strategy_ids]
    else:
        expected_strategy_ids = [str(item).strip() for item in strategy_ids if str(item).strip()]
    expected_strategy_id_set = set(expected_strategy_ids)
    row_count = 0
    last_ts: datetime | None = None
    observed_strategy_ids: set[str] = set()
    for path in sorted(audit_dir.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for raw_line in handle:
                    text = raw_line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    strategy_id = str(row.get("strategy_id") or "").strip()
                    if strategy_id not in expected_strategy_id_set:
                        continue
                    observed_strategy_ids.add(strategy_id)
                    row_count += 1
                    row_ts = _parse_iso(row.get("ts") or row.get("ts_now_et"))
                    if row_ts is not None:
                        row_ts = row_ts.replace(tzinfo=ET) if row_ts.tzinfo is None else row_ts.astimezone(ET)
                    if row_ts is not None and (last_ts is None or row_ts > last_ts):
                        last_ts = row_ts
        except Exception:
            continue
    return {
        "row_count": row_count,
        "last_ts": last_ts.isoformat() if last_ts else None,
        "audit_dir": str(audit_dir),
        "expected_strategy_ids": expected_strategy_ids,
        "matched_strategy_ids": matched_qqq_live_strategy_ids(observed_strategy_ids),
    }


def _qqq_stock_strategy_ids(config: dict[str, Any]) -> list[str]:
    strategies = config.get("strategies") if isinstance(config, dict) else []
    ids: list[str] = []
    if isinstance(strategies, list):
        for row in strategies:
            if not isinstance(row, dict) or not bool(row.get("enabled", False)):
                continue
            strategy_id = str(row.get("id") or "").strip()
            params = row.get("params") if isinstance(row.get("params"), dict) else {}
            extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
            plugin_cls = str(extras.get("plugin_cls") or "")
            if strategy_id == "qqq_intraday_plugin_001" or "QQQIntradayStockPlugin" in plugin_cls:
                ids.append(strategy_id)
    if ids:
        return ids
    if isinstance(strategies, list):
        for row in strategies:
            if isinstance(row, dict) and row.get("id") == "qqq_intraday_001" and bool(row.get("enabled", False)):
                return ["qqq_intraday_001"]
    return ["qqq_intraday_001"]


def _qqq_plugin_heartbeat_liveness_inputs(
    *,
    root: Path,
    strategy_id: str,
    now_et: datetime,
) -> tuple[dict[str, Any], float | None, dict[str, Any], float | None, Path, Path]:
    heartbeat_path = root / "runtime/readiness/strategy_tradability/heartbeats" / f"{strategy_id}.json"
    heartbeat = _read_json(heartbeat_path)
    details = heartbeat.get("details") if isinstance(heartbeat.get("details"), dict) else {}
    generated_at = _parse_iso(heartbeat.get("generated_at"))
    heartbeat_age = _age_seconds(generated_at, now_et)
    data_age_sec = _as_float(details.get("data_age_sec"))
    obs = {
        "artifact_type": "strategy_tradability_heartbeat",
        "strategy_id": strategy_id,
        "ready": bool(heartbeat.get("tradable")) or bool(details.get("readiness_ready")),
        "missing_required": [],
        "required_values": {"QQQ": {"age_sec": data_age_sec}},
        "source": heartbeat.get("source"),
    }
    diag = {
        "ts": heartbeat.get("generated_at"),
        "strategy_session_open": True,
        "loop_last_stage": "",
        "stream_invalid": False,
        "data_age_qqq_sec": data_age_sec,
        "readiness_reason": details.get("readiness_reason"),
        "session": details.get("session"),
    }
    return obs, heartbeat_age, diag, heartbeat_age, heartbeat_path, heartbeat_path


def _issue_key(kind: str, detail: str = "") -> str:
    return f"{kind}:{detail}" if detail else kind


def _run_system_status(root: Path) -> dict[str, Any]:
    lock_path = root / "runtime/locks/run_system.lock"
    lock_pid_text = lock_path.read_text(encoding="utf-8").strip() if lock_path.exists() else ""
    lock_pid = int(lock_pid_text) if lock_pid_text.isdigit() else None
    lock_pid_alive = bool(lock_pid and _pid_alive(lock_pid))
    raw_process_pids = sorted({pid for pid in _find_run_system_pids(root) if _pid_alive(pid)})
    process_pids: list[int] = []
    ignored_process_pids: list[int] = []
    for pid in raw_process_pids:
        actual = _is_actual_run_system_process(pid)
        if actual is False:
            ignored_process_pids.append(pid)
            continue
        process_pids.append(pid)
    if lock_pid_alive and lock_pid:
        actual = _is_actual_run_system_process(lock_pid)
        if actual is False:
            ignored_process_pids.append(lock_pid)
        elif lock_pid not in process_pids:
            process_pids.append(lock_pid)
    process_pids = sorted(set(process_pids))
    ignored_process_pids = sorted(set(ignored_process_pids) - set(process_pids))
    alive = bool(process_pids) or lock_pid_alive
    effective_pids = list(process_pids)
    effective_pids = sorted(effective_pids)
    return {
        "lock_path": str(lock_path),
        "lock_pid": lock_pid,
        "lock_pid_alive": lock_pid_alive,
        "process_pids": effective_pids,
        "ignored_process_pids": ignored_process_pids,
        "lock_matches_process": bool(lock_pid and lock_pid_alive and lock_pid in effective_pids),
        "stale_lock": bool(lock_pid and not lock_pid_alive and process_pids),
        "alive": alive,
    }


def _is_actual_run_system_process(pid: int) -> bool | None:
    try:
        args = subprocess.check_output(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None
    if not args:
        return None
    try:
        argv = shlex.split(args)
    except Exception:
        argv = args.split()
    if not argv:
        return None
    if not any(Path(str(item)).name == "run_system.py" or str(item).endswith("/run_system.py") for item in argv):
        return False
    executable = str(argv[0]).lower()
    return "python" in executable


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _qqq_liveness_summary(
    *,
    obs: dict[str, Any],
    obs_age: float | None,
    diag_last: dict[str, Any],
    diag_age: float | None,
    qqq_audit: dict[str, Any],
    stale_sec: float,
) -> dict[str, Any]:
    missing_required = [str(item).strip() for item in list(obs.get("missing_required") or []) if str(item).strip()]
    loop_last_stage = str(diag_last.get("loop_last_stage") or "").strip()
    stream_invalid = bool(diag_last.get("stream_invalid"))
    session_open = bool(diag_last.get("strategy_session_open", True))
    qqq_data_age_sec = _as_float(
        ((obs.get("required_values") or {}).get("QQQ") or {}).get("age_sec")
    )
    if qqq_data_age_sec is None:
        qqq_data_age_sec = _as_float(diag_last.get("data_age_qqq_sec"))
    if qqq_data_age_sec is None:
        qqq_data_age_sec = _as_float(diag_last.get("data_age_qqq"))
    heartbeat_alive = all(age is not None and age <= stale_sec for age in (obs_age, diag_age))
    ready = bool(obs.get("ready"))
    market_data_fresh = qqq_data_age_sec is not None and qqq_data_age_sec <= stale_sec
    audit_row_count = int(qqq_audit.get("row_count") or 0)

    blocking_reasons: list[str] = []
    if not heartbeat_alive:
        blocking_reasons.append("heartbeat_stale")
    if session_open and not ready:
        blocking_reasons.append("not_ready")
    if session_open and missing_required:
        blocking_reasons.extend(f"missing_required:{item}" for item in missing_required)
    if session_open and stream_invalid:
        blocking_reasons.append("stream_invalid")
    if session_open and loop_last_stage == "system_paused":
        blocking_reasons.append("system_paused")
    if session_open and not market_data_fresh:
        blocking_reasons.append("qqq_market_data_stale")
    if audit_row_count == 0:
        blocking_reasons.append("runtime_audit_missing")
    data_blockers: list[str] = []
    if session_open and not ready:
        data_blockers.append("not_ready")
    if session_open and missing_required:
        data_blockers.extend(f"missing_required:{item}" for item in missing_required)
    if session_open and stream_invalid:
        data_blockers.append("stream_invalid")
    if session_open and not market_data_fresh:
        data_blockers.append("qqq_market_data_stale")
    if audit_row_count == 0:
        data_blockers.append("runtime_audit_missing")
    system_blockers = ["system_paused"] if session_open and loop_last_stage == "system_paused" else []
    alive = heartbeat_alive
    configured_live = True
    entry_window_open = session_open
    data_ready = not data_blockers
    system_ready = not system_blockers
    risk_ready = True
    execution_ready = True
    tradable_entry_ready = (
        alive
        and configured_live
        and session_open
        and entry_window_open
        and data_ready
        and system_ready
        and risk_ready
        and execution_ready
    )

    return {
        "alive": alive,
        "configured_live": configured_live,
        "session_open": session_open,
        "entry_window_open": entry_window_open,
        "data_ready": data_ready,
        "system_ready": system_ready,
        "risk_ready": risk_ready,
        "execution_ready": execution_ready,
        "tradable_entry_ready": tradable_entry_ready,
        "close_only_allowed": alive and configured_live,
        "idle_reason": "offsession" if alive and not session_open else None,
        "data_blockers": data_blockers,
        "system_blockers": system_blockers,
        "risk_blockers": [],
        "execution_blockers": [],
        "tradable": tradable_entry_ready,
        "strategy_session_open": session_open,
        "ready": ready,
        "market_data_fresh": market_data_fresh,
        "missing_required": missing_required,
        "stream_invalid": stream_invalid,
        "loop_last_stage": loop_last_stage or None,
        "qqq_data_age_sec": qqq_data_age_sec,
        "runtime_audit_row_count": audit_row_count,
        "blocking_reasons": blocking_reasons,
    }


def _opening_artifact_status(*, root: Path, day_id: str, now_et: datetime) -> dict[str, Any]:
    expectations = [
        ("preopen", time(9, 25)),
        ("open", time(9, 32)),
        ("acceptance", time(9, 40)),
    ]
    checked_phases: list[str] = []
    present_phases: list[str] = []
    missing_phases: list[str] = []
    invalid_phases: list[str] = []
    artifacts: list[dict[str, Any]] = []
    target_date = datetime.strptime(day_id, "%Y%m%d").date()
    if now_et.date() != target_date:
        return {
            "checked_phases": [],
            "present_phases": [],
            "missing_phases": [],
            "invalid_phases": [],
            "artifacts": [],
        }
    for phase, threshold in expectations:
        if now_et.timetz().replace(tzinfo=None) < threshold:
            continue
        checked_phases.append(phase)
        path = root / "runtime/state/opening_day" / f"{day_id}_{phase}_supervisor.json"
        payload = _read_json(path)
        exists = path.exists()
        valid_final = exists and isinstance(payload.get("final"), dict)
        if exists:
            present_phases.append(phase)
        else:
            missing_phases.append(phase)
        if exists and not valid_final:
            invalid_phases.append(phase)
        artifacts.append(
            {
                "phase": phase,
                "path": str(path),
                "present": exists,
                "valid_final": valid_final,
            }
        )
    return {
        "checked_phases": checked_phases,
        "present_phases": present_phases,
        "missing_phases": missing_phases,
        "invalid_phases": invalid_phases,
        "artifacts": artifacts,
    }


def _opening_artifact_check(*, root: Path, day_id: str, now_et: datetime) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    status = _opening_artifact_status(root=root, day_id=day_id, now_et=now_et)
    for artifact in status.get("artifacts") or []:
        phase = str(artifact.get("phase") or "").strip()
        path = str(artifact.get("path") or "").strip()
        if not artifact.get("present"):
            failures.append(
                {
                    "issue_key": _issue_key("opening_artifact_missing", phase),
                    "severity": "ERROR",
                    "service": "OPENING_SUPERVISOR",
                    "error_type": "opening_artifact_missing",
                    "detail": phase,
                    "path": path,
                }
            )
            continue
        if not artifact.get("valid_final"):
            failures.append(
                {
                    "issue_key": _issue_key("opening_artifact_invalid", phase),
                    "severity": "ERROR",
                    "service": "OPENING_SUPERVISOR",
                    "error_type": "opening_artifact_invalid",
                    "detail": phase,
                    "path": path,
                }
            )
    return failures


def _platform_eod_status(*, root: Path, day_id: str, now_et: datetime) -> dict[str, Any]:
    target_date = datetime.strptime(day_id, "%Y%m%d").date()
    if now_et.date() != target_date:
        return {"required": False, "artifact_present": False, "manual_locks": []}
    required = now_et.timetz().replace(tzinfo=None) >= time(15, 59)
    artifact_day = f"{day_id[:4]}-{day_id[4:6]}-{day_id[6:8]}"
    artifact_path = root / "runtime/readiness/eod" / f"platform_eod_flatten_{artifact_day}.json"
    artifact = _read_json(artifact_path)
    manual_lock_dir = root / "runtime/readiness/manual_locks"
    manual_locks: list[dict[str, Any]] = []
    for path in sorted(manual_lock_dir.glob(f"platform_eod_manual_lock_{artifact_day}*.json")):
        payload = _read_json(path)
        if not payload:
            continue
        manual_locks.append(
            {
                "path": str(path),
                "lock_id": payload.get("lock_id"),
                "state": payload.get("state"),
                "reason": payload.get("reason"),
            }
        )
    return {
        "required": required,
        "artifact_path": str(artifact_path),
        "artifact_present": artifact_path.exists(),
        "artifact": artifact,
        "go_no_go": artifact.get("go_no_go"),
        "state": artifact.get("state"),
        "verified_flat": bool(artifact.get("verified_flat")),
        "awaiting_market_reopen": bool(artifact.get("awaiting_market_reopen")),
        "awaiting_market_reopen_not_before": str(artifact.get("awaiting_market_reopen_not_before") or ""),
        "recovery_incident_ids": list(artifact.get("recovery_incident_ids") or []),
        "manual_lock_ids": list(artifact.get("manual_lock_ids") or []),
        "manual_locks": manual_locks,
    }


def _platform_eod_resolved_by_current_flat_truth(
    *,
    status: dict[str, Any],
    position_safety: dict[str, Any],
    recovery_incidents: dict[str, Any],
    manual_locks: dict[str, Any],
) -> bool:
    if not bool(status.get("required")) or not bool(status.get("artifact_present")):
        return False
    if str(status.get("go_no_go") or "").strip().lower() == "go" and bool(status.get("verified_flat")):
        return False
    if bool(position_safety.get("stale")) or bool(position_safety.get("lookup_failed")):
        return False
    target_symbols = _platform_eod_target_symbols(status)
    if target_symbols:
        return _platform_eod_target_scopes_are_flat(
            target_symbols=target_symbols,
            position_safety=position_safety,
            recovery_incidents=recovery_incidents,
            manual_locks=manual_locks,
        )
    if str(position_safety.get("go_no_go") or "").strip().lower() != "go":
        return False
    if int(position_safety.get("broker_position_count") or 0) != 0:
        return False
    if int(position_safety.get("active_quarantine_count") or 0) != 0:
        return False
    if int(recovery_incidents.get("active_count") or 0) != 0:
        return False
    if bool(manual_locks.get("lookup_failed")):
        return False
    if int(manual_locks.get("active_count") or 0) != 0:
        return False
    return True


def _platform_eod_target_symbols(status: dict[str, Any]) -> set[str]:
    artifact = status.get("artifact") if isinstance(status.get("artifact"), dict) else {}
    symbols: set[str] = set()
    for key in ("symbols", "target_symbols"):
        for symbol in artifact.get(key) or status.get(key) or ():
            symbol_s = str(symbol or "").strip().upper()
            if symbol_s:
                symbols.add(symbol_s)
    for item in artifact.get("order_results") or status.get("order_results") or ():
        if not isinstance(item, dict):
            continue
        symbol_s = str(item.get("symbol") or "").strip().upper()
        if symbol_s:
            symbols.add(symbol_s)
    for incident_id in status.get("recovery_incident_ids") or artifact.get("recovery_incident_ids") or ():
        parsed = _symbol_from_recovery_incident_id(str(incident_id or ""))
        if parsed:
            symbols.add(parsed)
    return symbols


def _symbol_from_recovery_incident_id(incident_id: str) -> str:
    parts = [part for part in str(incident_id or "").split(":") if part]
    if len(parts) < 3 or parts[0] != "recovery_incident":
        return ""
    if len(parts) >= 7 and parts[2] == "OPT":
        return ":".join(parts[2:7]).strip().upper()
    return parts[2].strip().upper()


def _symbol_from_payload(payload: dict[str, Any]) -> str:
    for key in ("symbol", "contract_symbol", "local_symbol"):
        symbol_s = str(payload.get(key) or "").strip().upper()
        if symbol_s:
            return symbol_s
    incident_id = str(payload.get("incident_id") or payload.get("lock_id") or "").strip()
    return _symbol_from_recovery_incident_id(incident_id)


def _platform_eod_target_scopes_are_flat(
    *,
    target_symbols: set[str],
    position_safety: dict[str, Any],
    recovery_incidents: dict[str, Any],
    manual_locks: dict[str, Any],
) -> bool:
    live_snapshot = position_safety.get("live_broker_snapshot")
    if not isinstance(live_snapshot, dict):
        live_snapshot = {}
    positions = live_snapshot.get("positions")
    open_orders = live_snapshot.get("open_orders")
    if positions is None:
        positions = position_safety.get("broker_positions") or position_safety.get("positions") or []
    if open_orders is None:
        open_orders = position_safety.get("broker_open_orders") or position_safety.get("open_orders") or []

    if not isinstance(positions, list) or not isinstance(open_orders, list):
        return False
    for row in positions:
        if not isinstance(row, dict):
            continue
        symbol = _symbol_from_payload(row)
        if symbol not in target_symbols:
            continue
        try:
            qty = abs(float(row.get("qty") or row.get("position") or row.get("pos_qty") or 0.0))
        except Exception:
            return False
        if qty > 1e-9:
            return False
    for row in open_orders:
        if not isinstance(row, dict):
            continue
        if _symbol_from_payload(row) in target_symbols:
            return False

    for incident in recovery_incidents.get("incidents") or []:
        if not isinstance(incident, dict) or _symbol_from_payload(incident) not in target_symbols:
            continue
        if not bool(incident.get("terminal")):
            return False
        if bool(incident.get("active_quarantine")) or bool(incident.get("active_recovery_intent")):
            return False

    if bool(manual_locks.get("lookup_failed")):
        return False
    for lock in manual_locks.get("locks") or []:
        if not isinstance(lock, dict):
            continue
        if _symbol_from_payload(lock) in target_symbols:
            return False
    return True


def _reconcile_platform_eod_with_current_flat_truth(
    *,
    status: dict[str, Any],
    position_safety: dict[str, Any],
    recovery_incidents: dict[str, Any],
    manual_locks: dict[str, Any],
    now_et: datetime,
) -> dict[str, Any]:
    if not _platform_eod_resolved_by_current_flat_truth(
        status=status,
        position_safety=position_safety,
        recovery_incidents=recovery_incidents,
        manual_locks=manual_locks,
    ):
        return status
    reconciled = dict(status)
    reconciled.update(
        {
            "source_go_no_go": status.get("go_no_go"),
            "source_state": status.get("state"),
            "source_verified_flat": bool(status.get("verified_flat")),
            "source_awaiting_market_reopen": bool(status.get("awaiting_market_reopen")),
            "source_manual_lock_ids": list(status.get("manual_lock_ids") or []),
            "source_manual_locks": list(status.get("manual_locks") or []),
            "go_no_go": "go",
            "state": "VERIFIED_FLAT_BY_CURRENT_BROKER_TRUTH",
            "verified_flat": True,
            "awaiting_market_reopen": False,
            "manual_lock_ids": [],
            "manual_locks": [],
            "resolved_from_current_broker_truth": True,
            "resolved_at": now_et.isoformat(timespec="seconds"),
            "clear_evidence": {
                "position_safety_go_no_go": position_safety.get("go_no_go"),
                "broker_position_count": position_safety.get("broker_position_count"),
                "active_quarantine_count": position_safety.get("active_quarantine_count"),
                "recovery_incident_active_count": recovery_incidents.get("active_count"),
                "manual_lock_active_count": manual_locks.get("active_count"),
            },
        }
    )
    return reconciled


def _platform_eod_failures(status: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if not bool(status.get("required")):
        return failures
    if not bool(status.get("artifact_present")):
        failures.append(
            {
                "issue_key": "platform_eod_artifact_missing",
                "severity": "ERROR",
                "service": "PLATFORM_EOD",
                "error_type": "platform_eod_artifact_missing",
                "detail": "platform_eod_flatten_artifact_missing_after_cutoff",
                "path": status.get("artifact_path"),
            }
        )
        return failures
    if str(status.get("go_no_go") or "").strip().lower() != "go" or not bool(status.get("verified_flat")):
        state = str(status.get("state") or "unknown")
        failure = {
            "issue_key": "platform_eod_no_go",
            "severity": "ERROR",
            "service": "PLATFORM_EOD",
            "error_type": "platform_eod_no_go",
            "detail": state,
            "path": status.get("artifact_path"),
        }
        if bool(status.get("awaiting_market_reopen")):
            failure.update(
                {
                    "error_type": "platform_eod_awaiting_market_reopen",
                    "awaiting_market_reopen_not_before": status.get("awaiting_market_reopen_not_before"),
                    "recovery_incident_ids": status.get("recovery_incident_ids"),
                    "clear_condition": "market_reopened_and_platform_eod_retries_to_verified_flat_or_manual_required",
                }
            )
        failures.append(failure)
    if status.get("manual_lock_ids") or status.get("manual_locks"):
        failure = {
            "issue_key": "platform_eod_manual_lock_active",
            "severity": "ERROR",
            "service": "PLATFORM_EOD",
            "error_type": "platform_eod_manual_lock_active",
            "detail": "manual_lock_created_by_platform_eod",
            "manual_lock_ids": status.get("manual_lock_ids"),
        }
        if bool(status.get("awaiting_market_reopen")):
            failure.update(
                {
                    "awaiting_market_reopen_not_before": status.get("awaiting_market_reopen_not_before"),
                    "clear_condition": "manual_lock_cleared_after_verified_flat_or_manual_required_acknowledged",
                }
            )
        failures.append(failure)
    return failures


def _platform_eod_check(*, root: Path, day_id: str, now_et: datetime) -> list[dict[str, Any]]:
    return _platform_eod_failures(_platform_eod_status(root=root, day_id=day_id, now_et=now_et))


def _session_end_status(
    *,
    root: Path,
    config: dict[str, Any],
    now_et: datetime,
    max_age_sec: float = 90.0,
) -> dict[str, Any]:
    production_cfg = config.get("production") if isinstance(config, dict) else {}
    session_cfg = (production_cfg or {}).get("session_end_supervisor") if isinstance(production_cfg, dict) else {}
    enabled = bool((session_cfg or {}).get("enabled", False))
    path = root / "runtime/readiness/session_end/session_end_latest.json"
    if not enabled:
        return {
            "artifact_type": "session_end_no_overnight",
            "enabled": False,
            "artifact_path": str(path),
            "artifact_present": path.exists(),
            "go_no_go": "go",
            "violation_count": 0,
            "violations": [],
        }
    artifact = _read_json(path)
    generated_at = _parse_iso(artifact.get("generated_at")) if artifact else None
    age_sec = _age_seconds(generated_at, now_et)
    stale = age_sec is None or age_sec > max_age_sec
    return {
        "artifact_type": "session_end_no_overnight",
        "enabled": True,
        "artifact_path": str(path),
        "artifact_present": path.exists(),
        "generated_at": artifact.get("generated_at"),
        "age_sec": age_sec,
        "max_age_sec": max_age_sec,
        "stale": stale,
        "go_no_go": artifact.get("go_no_go"),
        "lookup_failed": bool(artifact.get("lookup_failed")),
        "violation_count": int(artifact.get("violation_count") or 0),
        "manual_required_count": int(artifact.get("manual_required_count") or 0),
        "violations": list(artifact.get("violations") or []),
        "artifact": artifact,
    }


def _session_end_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if not bool(status.get("enabled")):
        return failures
    if not bool(status.get("artifact_present")):
        failures.append(
            {
                "issue_key": "session_end_artifact_missing",
                "severity": "ERROR",
                "service": "SESSION_END",
                "error_type": "session_end_artifact_missing",
                "detail": "session_end_latest_missing",
                "path": status.get("artifact_path"),
            }
        )
        return failures
    if bool(status.get("stale")):
        failures.append(
            {
                "issue_key": "session_end_artifact_stale",
                "severity": "ERROR",
                "service": "SESSION_END",
                "error_type": "session_end_artifact_stale",
                "detail": "session_end_latest_stale",
                "age_sec": status.get("age_sec"),
                "path": status.get("artifact_path"),
            }
        )
    if bool(status.get("lookup_failed")):
        failures.append(
            {
                "issue_key": "session_end_broker_truth_unavailable",
                "severity": "ERROR",
                "service": "SESSION_END",
                "error_type": "session_end_broker_truth_unavailable",
                "detail": "session_end_supervisor_cannot_verify_broker_positions",
                "path": status.get("artifact_path"),
            }
        )
    if str(status.get("go_no_go") or "").strip().lower() != "go":
        failures.append(
            {
                "issue_key": "session_end_no_go",
                "severity": "ERROR",
                "service": "SESSION_END",
                "error_type": "session_end_no_go",
                "detail": "session_end_no_overnight_invariant_failed",
                "violation_count": status.get("violation_count"),
                "path": status.get("artifact_path"),
            }
        )
    if int(status.get("violation_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "session_end_no_overnight_position",
                "severity": "ERROR",
                "service": "SESSION_END",
                "error_type": "session_end_no_overnight_position",
                "detail": "no_overnight_strategy_has_nonflat_broker_position_after_deadline",
                "violations": status.get("violations"),
                "path": status.get("artifact_path"),
            }
        )
    if int(status.get("manual_required_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "session_end_manual_required",
                "severity": "ERROR",
                "service": "SESSION_END",
                "error_type": "session_end_manual_required",
                "detail": "session_end_violation_escalated_to_manual_required",
                "violations": status.get("violations"),
                "path": status.get("artifact_path"),
            }
        )
    return failures


async def _query_postgres_manual_locks() -> dict[str, Any]:
    from libs.db.postgres import PostgresDB

    pg = PostgresDB(min_conn=1, max_conn=2)
    try:
        await pg.connect()
        rows = await pg.fetchall(
            """
            SELECT strategy_id,
                   symbol,
                   reason,
                   extract(epoch from created_at) AS created_ts,
                   extract(epoch from expire_at) AS expire_ts,
                   context
            FROM core.manual_intervention_lock
            WHERE expire_at > now()
            ORDER BY created_at DESC
            LIMIT 100
            """
        )
        locks: list[dict[str, Any]] = []
        for row in rows or []:
            context = row.get("context") if isinstance(row, dict) else {}
            if isinstance(context, str):
                try:
                    context = json.loads(context)
                except Exception:
                    context = {}
            locks.append(
                {
                    "strategy_id": str(row.get("strategy_id") or ""),
                    "symbol": str(row.get("symbol") or ""),
                    "reason": str(row.get("reason") or ""),
                    "created_ts": float(row.get("created_ts") or 0.0),
                    "expire_ts": float(row.get("expire_ts") or 0.0),
                    "context": dict(context or {}),
                }
            )
        return {"enabled": True, "lookup_failed": False, "active_count": len(locks), "locks": locks}
    finally:
        await pg.close()


def _postgres_manual_lock_status(*, config: dict[str, Any]) -> dict[str, Any]:
    ops_cfg = config.get("operations") if isinstance(config, dict) else {}
    watchdog_cfg = (ops_cfg or {}).get("runtime_watchdog") if isinstance(ops_cfg, dict) else {}
    enabled = bool((watchdog_cfg or {}).get("check_db_manual_locks", False))
    if not enabled:
        return {"enabled": False, "lookup_failed": False, "active_count": 0, "locks": []}
    try:
        return asyncio.run(_query_postgres_manual_locks())
    except Exception as exc:
        return {
            "enabled": True,
            "lookup_failed": True,
            "active_count": 0,
            "locks": [],
            "error": str(exc),
        }


def _manual_lock_symbol(lock: dict[str, Any]) -> str:
    return str(lock.get("symbol") or "").strip().upper()


def _awaiting_market_reopen_by_symbol_from_recovery_incidents(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for incident in list(status.get("incidents") or []):
        if not isinstance(incident, dict):
            continue
        intent = incident.get("active_recovery_intent")
        if not isinstance(intent, dict):
            continue
        if str(intent.get("next_action") or "") != "wait_until_market_reopen":
            continue
        symbol = str(incident.get("symbol") or "").strip().upper()
        not_before = str(intent.get("next_retry_after") or "").strip()
        if not symbol or not not_before:
            continue
        context = contexts.setdefault(
            symbol,
            {
                "awaiting_market_reopen_not_before": not_before,
                "awaiting_market_reopen_recovery_incident_ids": [],
            },
        )
        if not_before < str(context.get("awaiting_market_reopen_not_before") or not_before):
            context["awaiting_market_reopen_not_before"] = not_before
        incident_id = str(incident.get("incident_id") or incident.get("id") or "").strip()
        if incident_id:
            context["awaiting_market_reopen_recovery_incident_ids"].append(incident_id)
    for context in contexts.values():
        ids = sorted(set(str(item) for item in context.get("awaiting_market_reopen_recovery_incident_ids") or [] if item))
        context["awaiting_market_reopen_recovery_incident_ids"] = ids
    return contexts


def _annotate_manual_locks_with_recovery_incident_context(
    status: dict[str, Any],
    *,
    recovery_incidents: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(status, dict) or not isinstance(recovery_incidents, dict):
        return status
    by_symbol = _awaiting_market_reopen_by_symbol_from_recovery_incidents(recovery_incidents)
    if not by_symbol:
        return status
    annotated = dict(status)
    locks: list[dict[str, Any]] = []
    changed = False
    for item in list(status.get("locks") or []):
        if not isinstance(item, dict):
            continue
        lock = dict(item)
        symbol = _manual_lock_symbol(lock)
        inferred = by_symbol.get(symbol)
        if inferred:
            context = dict(lock.get("context") or {}) if isinstance(lock.get("context"), dict) else {}
            if not str(context.get("awaiting_market_reopen_not_before") or "").strip():
                context["awaiting_market_reopen_not_before"] = inferred["awaiting_market_reopen_not_before"]
                context["awaiting_market_reopen_context_source"] = "recovery_incidents"
                context["awaiting_market_reopen_recovery_incident_ids"] = list(
                    inferred.get("awaiting_market_reopen_recovery_incident_ids") or []
                )
                lock["context"] = context
                changed = True
        locks.append(lock)
    if changed:
        annotated["locks"] = locks
        annotated["awaiting_market_reopen_context_inferred"] = True
    return annotated


def _postgres_manual_lock_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(status.get("enabled")):
        return []
    if bool(status.get("lookup_failed")):
        return [
            {
                "issue_key": "manual_intervention_lock_lookup_failed",
                "severity": "ERROR",
                "service": "MANUAL_LOCK",
                "error_type": "manual_intervention_lock_lookup_failed",
                "detail": str(status.get("error") or "postgres_lookup_failed"),
            }
        ]
    if int(status.get("active_count") or 0) <= 0:
        return []
    locks = [item for item in list(status.get("locks") or []) if isinstance(item, dict)]
    awaiting_values: list[str] = []
    for lock in locks:
        context = lock.get("context") if isinstance(lock.get("context"), dict) else {}
        value = str(context.get("awaiting_market_reopen_not_before") or "").strip()
        if value:
            awaiting_values.append(value)
    detail = "active_db_manual_intervention_lock"
    error_type = "manual_intervention_lock_active"
    extra: dict[str, Any] = {}
    if awaiting_values:
        extra = {
            "awaiting_market_reopen_count": len(awaiting_values),
            "awaiting_market_reopen_not_before": min(awaiting_values),
            "manual_lock_without_awaiting_context_count": max(
                0,
                int(status.get("active_count") or 0) - len(awaiting_values),
            ),
            "clear_condition": "market_reopened_and_linked_recovery_incidents_reach_verified_flat_or_manual_required",
        }
        if len(awaiting_values) == int(status.get("active_count") or 0):
            detail = "manual_intervention_lock_waiting_for_market_reopen"
            error_type = "manual_intervention_lock_awaiting_market_reopen"
    return [
        {
            "issue_key": "manual_intervention_lock_active",
            "severity": "ERROR",
            "service": "MANUAL_LOCK",
            "error_type": error_type,
            "detail": detail,
            "active_count": int(status.get("active_count") or 0),
            "locks": locks,
            **extra,
        }
    ]


def _enabled_strategy_scope(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    strategy_ids: set[str] = set()
    symbols: set[str] = set()
    for spec in list((config or {}).get("strategies") or []):
        if not isinstance(spec, dict) or not bool(spec.get("enabled", True)):
            continue
        strategy_id = str(spec.get("id") or spec.get("strategy_id") or "").strip()
        if strategy_id:
            strategy_ids.add(strategy_id)
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        for key in ("symbol", "underlying"):
            value = params.get(key) or spec.get(key)
            if value:
                symbols.add(str(value).strip().upper())
        extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
        contract = extras.get("data_contract") if isinstance(extras.get("data_contract"), dict) else {}
        for symbol in list(contract.get("symbols") or []):
            if symbol:
                symbols.add(str(symbol).strip().upper())
    return sorted(strategy_ids), sorted(symbols)


async def _query_postgres_ledger_integrity(
    *,
    strategy_ids: list[str],
    symbols: list[str],
    generated_at: datetime,
) -> dict[str, Any]:
    from libs.db.postgres import PostgresDB

    pg = PostgresDB(min_conn=1, max_conn=2)
    try:
        await pg.connect()
        return await query_position_ledger_integrity(
            pg=pg,
            strategy_ids=strategy_ids,
            symbols=symbols,
            generated_at=generated_at,
        )
    finally:
        await pg.close()


def _postgres_ledger_integrity_status(*, config: dict[str, Any], repo_root: Path, generated_at: datetime) -> dict[str, Any]:
    ops_cfg = config.get("operations") if isinstance(config, dict) else {}
    watchdog_cfg = (ops_cfg or {}).get("runtime_watchdog") if isinstance(ops_cfg, dict) else {}
    enabled = bool((watchdog_cfg or {}).get("check_position_ledger_integrity", False))
    if not enabled:
        status = disabled_ledger_integrity_status(generated_at=generated_at)
        write_ledger_integrity_artifact(repo_root=repo_root, status=status)
        return status
    strategy_ids, symbols = _enabled_strategy_scope(config)
    try:
        status = asyncio.run(
            _query_postgres_ledger_integrity(
                strategy_ids=strategy_ids,
                symbols=symbols,
                generated_at=generated_at,
            )
        )
        write_ledger_integrity_artifact(repo_root=repo_root, status=status)
        return status
    except Exception as exc:
        status = failed_ledger_integrity_status(generated_at=generated_at, error=str(exc))
        write_ledger_integrity_artifact(repo_root=repo_root, status=status)
        return status


def _postgres_ledger_integrity_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(status.get("enabled")):
        return []
    if bool(status.get("lookup_failed")):
        return [
            {
                "issue_key": "position_ledger_integrity_lookup_failed",
                "severity": "ERROR",
                "service": "POSITION_LEDGER",
                "error_type": "position_ledger_integrity_lookup_failed",
                "detail": str(status.get("error") or "lookup_failed"),
            }
        ]
    failures: list[dict[str, Any]] = []
    if int(status.get("issue_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "position_ledger_unknown_account_or_null_lot",
                "severity": "ERROR",
                "service": "POSITION_LEDGER",
                "error_type": "position_ledger_unknown_account_or_null_lot",
                "detail": "nonzero_unknown_account_or_null_lot_ledger_rows",
                "issue_count": int(status.get("issue_count") or 0),
            }
        )
    if int(status.get("account_divergence_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "position_ledger_account_divergence",
                "severity": "ERROR",
                "service": "POSITION_LEDGER",
                "error_type": "position_ledger_account_divergence",
                "detail": "nonzero_ledger_rows_span_multiple_accounts",
                "account_divergence_count": int(status.get("account_divergence_count") or 0),
            }
        )
    if int(status.get("zero_qty_notional_residual_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "position_ledger_zero_qty_notional_residual",
                "severity": "ERROR",
                "service": "POSITION_LEDGER",
                "error_type": "position_ledger_zero_qty_notional_residual",
                "detail": "zero_qty_rows_have_nonzero_notional_sum",
                "residual_count": int(status.get("zero_qty_notional_residual_count") or 0),
                "threshold_usd": float(status.get("zero_qty_notional_residual_threshold") or 1.0),
            }
        )
    return failures


async def _query_postgres_broker_truth_snapshot(
    *,
    strategy_ids: list[str],
    symbols: list[str],
    generated_at: datetime,
    live_broker_snapshot: dict[str, Any] | None = None,
    require_live_broker_truth: bool = True,
) -> dict[str, Any]:
    from libs.db.postgres import PostgresDB

    pg = PostgresDB(min_conn=1, max_conn=2)
    try:
        await pg.connect()
        return await query_broker_truth_snapshot(
            pg=pg,
            generated_at=generated_at,
            strategy_ids=strategy_ids,
            symbols=symbols,
            live_broker_snapshot=live_broker_snapshot,
            require_live_broker_truth=require_live_broker_truth,
        )
    finally:
        await pg.close()


def _postgres_broker_truth_snapshot_status(
    *,
    config: dict[str, Any],
    repo_root: Path,
    generated_at: datetime,
    runtime_version: dict[str, Any],
    live_broker_snapshot: dict[str, Any] | None = None,
    max_age_sec: float = 90.0,
) -> dict[str, Any]:
    ops_cfg = config.get("operations") if isinstance(config, dict) else {}
    watchdog_cfg = (ops_cfg or {}).get("runtime_watchdog") if isinstance(ops_cfg, dict) else {}
    enabled = bool((watchdog_cfg or {}).get("check_broker_truth_snapshot", True))
    require_live = bool((watchdog_cfg or {}).get("require_live_broker_truth_snapshot", True))
    if live_broker_snapshot is None:
        live_broker_snapshot = _latest_live_broker_truth_snapshot(root=repo_root, now_et=generated_at)
    if (
        live_broker_snapshot is None
        and require_live
        and bool((watchdog_cfg or {}).get("produce_live_broker_truth_snapshot", True))
    ):
        account = str(
            (watchdog_cfg or {}).get("broker_truth_account")
            or os.getenv("IB_ACCOUNT")
            or os.getenv("IBKR_ACCOUNT")
            or ""
        ).strip() or None
        live_broker_snapshot = asyncio.run(
            build_live_broker_truth_snapshot_from_manager(generated_at=generated_at, account=account)
        )
        write_live_broker_truth_snapshot_artifact(repo_root=repo_root, status=live_broker_snapshot)
    if not enabled:
        status = disabled_broker_truth_snapshot_status(generated_at=generated_at)
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_broker_truth_snapshot_artifact(repo_root=repo_root, status=status)
        return status
    strategy_ids, symbols = _enabled_strategy_scope(config)
    try:
        status = asyncio.run(
            _query_postgres_broker_truth_snapshot(
                strategy_ids=strategy_ids,
                symbols=symbols,
                generated_at=generated_at,
                live_broker_snapshot=live_broker_snapshot,
                require_live_broker_truth=require_live,
            )
        )
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_broker_truth_snapshot_artifact(repo_root=repo_root, status=status)
        return status
    except Exception as exc:
        status = failed_broker_truth_snapshot_status(generated_at=generated_at, error=str(exc))
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_broker_truth_snapshot_artifact(repo_root=repo_root, status=status)
        return status


def _latest_live_broker_truth_snapshot(*, root: Path, now_et: datetime, max_age_sec: float = 90.0) -> dict[str, Any] | None:
    paths = [
        root / "runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json",
        root / "runtime/readiness/broker_truth_snapshot/live_broker_truth_snapshot_latest.json",
    ]
    for path in paths:
        payload = _read_json(path)
        if not payload:
            continue
        generated_at = _parse_iso(payload.get("generated_at"))
        age_sec = _age_seconds(generated_at, now_et)
        snapshot = dict(payload)
        snapshot["artifact_path"] = str(path)
        snapshot["age_sec"] = age_sec
        snapshot["stale"] = bool(snapshot.get("stale")) or bool(age_sec is None or age_sec > float(max_age_sec))
        return snapshot
    return None


def _postgres_broker_truth_snapshot_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(status.get("enabled")):
        return []
    failures = _artifact_runtime_contract_failures(
        status,
        issue_prefix="broker_truth_snapshot",
        service="BROKER_TRUTH_SNAPSHOT",
    )
    if bool(status.get("lookup_failed")):
        failures.append(
            {
                "issue_key": "broker_truth_snapshot_lookup_failed",
                "severity": "ERROR",
                "service": "BROKER_TRUTH_SNAPSHOT",
                "error_type": "broker_truth_snapshot_lookup_failed",
                "detail": str(status.get("error") or "lookup_failed"),
            }
        )
    if bool(status.get("require_live_broker_truth")) and not bool(status.get("broker_truth_backed")):
        failures.append(
            {
                "issue_key": "broker_truth_live_snapshot_missing",
                "severity": "ERROR",
                "service": "BROKER_TRUTH_SNAPSHOT",
                "error_type": "broker_truth_live_snapshot_missing",
                "detail": "broker_truth_snapshot_must_be_backed_by_fresh_live_broker_position_safety_artifact",
                "truth_source": status.get("truth_source"),
                "live_broker_snapshot_path": status.get("live_broker_snapshot_path"),
            }
        )
    if int(status.get("order_truth_active_quarantine_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "broker_truth_active_order_quarantine",
                "severity": "ERROR",
                "service": "BROKER_TRUTH_SNAPSHOT",
                "error_type": "broker_truth_active_order_quarantine",
                "detail": "active_order_quarantine_blocks_clean_broker_truth_projection",
                "count": int(status.get("order_truth_active_quarantine_count") or 0),
            }
        )
    if int(status.get("projection_divergence_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "broker_truth_position_projection_divergence",
                "severity": "ERROR",
                "service": "BROKER_TRUTH_SNAPSHOT",
                "error_type": "broker_truth_position_projection_divergence",
                "detail": "position_ledger_current_and_core_position_diverge",
                "count": int(status.get("projection_divergence_count") or 0),
            }
        )
    return failures


async def _query_postgres_fill_attribution(
    *,
    strategy_ids: list[str],
    symbols: list[str],
    generated_at: datetime,
) -> dict[str, Any]:
    from libs.db.postgres import PostgresDB

    pg = PostgresDB(min_conn=1, max_conn=2)
    try:
        await pg.connect()
        return await query_fill_attribution_readiness(
            pg=pg,
            generated_at=generated_at,
            strategy_ids=strategy_ids,
            symbols=symbols,
        )
    finally:
        await pg.close()


def _postgres_fill_attribution_status(
    *,
    config: dict[str, Any],
    repo_root: Path,
    generated_at: datetime,
    runtime_version: dict[str, Any],
    max_age_sec: float = 90.0,
) -> dict[str, Any]:
    ops_cfg = config.get("operations") if isinstance(config, dict) else {}
    watchdog_cfg = (ops_cfg or {}).get("runtime_watchdog") if isinstance(ops_cfg, dict) else {}
    enabled = bool((watchdog_cfg or {}).get("check_fill_attribution", True))
    if not enabled:
        status = disabled_fill_attribution_status(generated_at=generated_at)
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_fill_attribution_artifact(repo_root=repo_root, status=status)
        return status
    strategy_ids, symbols = _enabled_strategy_scope(config)
    try:
        status = asyncio.run(
            _query_postgres_fill_attribution(
                strategy_ids=strategy_ids,
                symbols=symbols,
                generated_at=generated_at,
            )
        )
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_fill_attribution_artifact(repo_root=repo_root, status=status)
        return status
    except Exception as exc:
        status = failed_fill_attribution_status(generated_at=generated_at, error=str(exc))
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_fill_attribution_artifact(repo_root=repo_root, status=status)
        return status


def _postgres_fill_attribution_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(status.get("enabled")):
        return []
    failures = _artifact_runtime_contract_failures(
        status,
        issue_prefix="fill_attribution",
        service="FILL_ATTRIBUTION",
    )
    if bool(status.get("lookup_failed")):
        failures.append(
            {
                "issue_key": "fill_attribution_lookup_failed",
                "severity": "ERROR",
                "service": "FILL_ATTRIBUTION",
                "error_type": "fill_attribution_lookup_failed",
                "detail": str(status.get("error") or "lookup_failed"),
            }
        )
    if int(status.get("unknown_fill_count") or 0) <= 0 and int(status.get("ambiguous_fill_count") or 0) <= 0:
        return failures
    failures.append(
        {
            "issue_key": "unknown_or_ambiguous_broker_fills_active",
            "severity": "ERROR",
            "service": "FILL_ATTRIBUTION",
            "error_type": "unknown_or_ambiguous_broker_fills_active",
            "detail": "active_unknown_or_ambiguous_fills_must_be_adjudicated",
            "unknown_fill_count": int(status.get("unknown_fill_count") or 0),
            "ambiguous_fill_count": int(status.get("ambiguous_fill_count") or 0),
        }
    )
    return failures


def _recovery_incidents_status(*, root: Path, now_et: datetime, max_age_sec: float = 90.0) -> dict[str, Any]:
    path = root / "runtime/readiness/recovery_incidents/recovery_incidents_latest.json"
    if not path.exists():
        return {
            "artifact_type": "recovery_incidents",
            "present": False,
            "go_no_go": "no_go",
            "active_count": 0,
            "terminal_count": 0,
            "stale": False,
            "path": str(path),
            "error": "missing_recovery_incidents_artifact",
        }
    payload = _read_json(path)
    generated_at = _parse_iso(payload.get("generated_at"))
    age_sec = _age_seconds(generated_at, now_et)
    stale = age_sec is None or age_sec > max_age_sec
    active_count = int(payload.get("active_count") or 0)
    lookup_failed = bool(payload.get("lookup_failed"))
    return {
        **payload,
        "present": True,
        "path": str(path),
        "age_sec": age_sec,
        "max_age_sec": max_age_sec,
        "stale": stale,
        "lookup_failed": lookup_failed,
        "go_no_go": "go" if not stale and not lookup_failed and active_count <= 0 else "no_go",
    }


def _recovery_incidents_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(status.get("present")):
        return [
            {
                "issue_key": "recovery_incidents_missing",
                "severity": "ERROR",
                "service": "RECOVERY_INCIDENTS",
                "error_type": "recovery_incidents_missing",
                "detail": str(status.get("error") or "missing"),
                "path": status.get("path"),
            }
        ]
    if bool(status.get("stale")):
        return [
            {
                "issue_key": "recovery_incidents_stale",
                "severity": "ERROR",
                "service": "RECOVERY_INCIDENTS",
                "error_type": "recovery_incidents_stale",
                "detail": "artifact_stale",
                "age_sec": status.get("age_sec"),
                "max_age_sec": status.get("max_age_sec"),
                "path": status.get("path"),
            }
        ]
    if bool(status.get("lookup_failed")):
        return [
            {
                "issue_key": "recovery_incidents_lookup_failed",
                "severity": "ERROR",
                "service": "RECOVERY_INCIDENTS",
                "error_type": "recovery_incidents_lookup_failed",
                "detail": str(status.get("error") or "lookup_failed"),
                "path": status.get("path"),
            }
        ]
    active_count = int(status.get("active_count") or 0)
    if active_count > 0:
        active_incidents = [
            item for item in list(status.get("incidents") or [])
            if isinstance(item, dict)
            and str(item.get("state") or "") not in {
                "verified_flat",
                "verified_protected",
                "manual_required_acknowledged",
                "broker_truth_unavailable_halt",
            }
        ]
        awaiting = [
            item for item in active_incidents
            if str((item.get("active_recovery_intent") or {}).get("next_action") or "") == "wait_until_market_reopen"
        ]
        if awaiting and len(awaiting) == active_count:
            not_before_values = [
                str((item.get("active_recovery_intent") or {}).get("next_retry_after") or "")
                for item in awaiting
                if str((item.get("active_recovery_intent") or {}).get("next_retry_after") or "")
            ]
            return [
                {
                    "issue_key": "recovery_incidents_awaiting_market_reopen",
                    "severity": "ERROR",
                    "service": "RECOVERY_INCIDENTS",
                    "error_type": "active_recovery_incidents_awaiting_market_reopen",
                    "detail": "recovery_incidents_waiting_for_market_reopen",
                    "active_count": active_count,
                    "awaiting_market_reopen_count": len(awaiting),
                    "awaiting_market_reopen_not_before": min(not_before_values) if not_before_values else "",
                    "clear_condition": "market_reopened_and_recovery_intents_retried_to_verified_flat_or_manual_required",
                    "path": status.get("path"),
                }
            ]
        return [
            {
                "issue_key": "active_recovery_incidents_present",
                "severity": "ERROR",
                "service": "RECOVERY_INCIDENTS",
                "error_type": "active_recovery_incidents_present",
                "detail": "active_recovery_incidents_must_reach_terminal_state",
                "active_count": active_count,
                "path": status.get("path"),
            }
        ]
    failures: list[dict[str, Any]] = []
    terminal_states = {
        "verified_flat",
        "verified_protected",
        "manual_required_acknowledged",
        "broker_truth_unavailable_halt",
    }
    for incident in list(status.get("incidents") or []):
        if not isinstance(incident, dict):
            continue
        state = str(incident.get("state") or "").strip()
        if state not in terminal_states and not bool(incident.get("terminal")):
            continue
        incident_id = str(incident.get("incident_id") or "")
        if bool(incident.get("active_quarantine")):
            failures.append(
                {
                    "issue_key": "recovery_incident_terminal_active_quarantine",
                    "severity": "ERROR",
                    "service": "RECOVERY_INCIDENTS",
                    "error_type": "recovery_incident_terminal_inconsistent",
                    "detail": "terminal_recovery_incident_still_marks_active_quarantine",
                    "incident_id": incident_id,
                    "state": state,
                    "path": status.get("path"),
                }
            )
        if bool(incident.get("protected_or_flattening")):
            failures.append(
                {
                    "issue_key": "recovery_incident_terminal_still_protected_or_flattening",
                    "severity": "ERROR",
                    "service": "RECOVERY_INCIDENTS",
                    "error_type": "recovery_incident_terminal_inconsistent",
                    "detail": "terminal_recovery_incident_still_marks_protected_or_flattening",
                    "incident_id": incident_id,
                    "state": state,
                    "path": status.get("path"),
                }
            )
        if isinstance(incident.get("active_recovery_intent"), dict):
            failures.append(
                {
                    "issue_key": "recovery_incident_terminal_active_intent",
                    "severity": "ERROR",
                    "service": "RECOVERY_INCIDENTS",
                    "error_type": "recovery_incident_terminal_inconsistent",
                    "detail": "terminal_recovery_incident_still_has_active_recovery_intent",
                    "incident_id": incident_id,
                    "state": state,
                    "path": status.get("path"),
                }
            )
        if state == "verified_flat" and not incident.get("verified_flat_at"):
            failures.append(
                {
                    "issue_key": "recovery_incident_verified_flat_missing_evidence",
                    "severity": "ERROR",
                    "service": "RECOVERY_INCIDENTS",
                    "error_type": "recovery_incident_terminal_missing_evidence",
                    "detail": "verified_flat_incident_requires_verified_flat_at",
                    "incident_id": incident_id,
                    "state": state,
                    "path": status.get("path"),
                }
            )
        if state == "manual_required_acknowledged" and (
            not incident.get("manual_required_at") or not list(incident.get("manual_lock_ids") or [])
        ):
            failures.append(
                {
                    "issue_key": "recovery_incident_manual_required_missing_evidence",
                    "severity": "ERROR",
                    "service": "RECOVERY_INCIDENTS",
                    "error_type": "recovery_incident_terminal_missing_evidence",
                    "detail": "manual_required_acknowledged_requires_manual_required_at_and_lock_ids",
                    "incident_id": incident_id,
                    "state": state,
                    "path": status.get("path"),
                }
            )
    if failures:
        return failures
    return []


def _position_safety_status(*, root: Path, now_et: datetime, max_age_sec: float = 90.0) -> dict[str, Any]:
    path = root / "runtime/readiness/position_safety/position_safety_latest.json"
    payload = _read_json(path)
    generated_at = _parse_iso(payload.get("generated_at"))
    age_sec = _age_seconds(generated_at, now_et)
    stale = bool(path.exists()) and (age_sec is None or age_sec > float(max_age_sec))
    return {
        "artifact_path": str(path),
        "artifact_present": path.exists(),
        "artifact": payload,
        "generated_at": payload.get("generated_at"),
        "age_sec": age_sec,
        "max_age_sec": float(max_age_sec),
        "stale": stale,
        "go_no_go": payload.get("go_no_go"),
        "lookup_failed": bool(payload.get("lookup_failed")),
        "unsafe_position_count": int(payload.get("unsafe_position_count") or 0),
        "broker_position_count": int(payload.get("broker_position_count") or 0),
        "active_quarantine_count": int(payload.get("active_quarantine_count") or 0),
        "records": list(payload.get("records") or []),
        "live_broker_snapshot": dict(payload.get("live_broker_snapshot") or {}),
        "dependency_state": dict(payload.get("dependency_state") or {}),
        "blockers": list(payload.get("blockers") or []),
    }


def _position_safety_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(status.get("artifact_present")):
        return [
            {
                "issue_key": "position_safety_artifact_missing",
                "severity": "ERROR",
                "service": "POSITION_SAFETY",
                "error_type": "position_safety_artifact_missing",
                "detail": "position_safety_latest_missing",
                "path": status.get("artifact_path"),
            }
        ]
    failures: list[dict[str, Any]] = []
    if bool(status.get("stale")):
        failures.append(
            {
                "issue_key": "position_safety_artifact_stale",
                "severity": "ERROR",
                "service": "POSITION_SAFETY",
                "error_type": "position_safety_artifact_stale",
                "detail": "position_safety_latest_stale",
                "age_sec": status.get("age_sec"),
                "max_age_sec": status.get("max_age_sec"),
                "path": status.get("artifact_path"),
            }
        )
    if str(status.get("go_no_go") or "").strip().lower() != "go":
        failures.append(
            {
                "issue_key": "position_safety_no_go",
                "severity": "ERROR",
                "service": "POSITION_SAFETY",
                "error_type": "position_safety_no_go",
                "detail": "broker_position_safety_invariant_failed",
                "path": status.get("artifact_path"),
            }
        )
    dependency_state = dict(status.get("dependency_state") or {})
    blocker_map = {
        "broker_truth_missing": "position_safety_broker_truth_missing",
        "broker_truth_stale": "position_safety_broker_truth_stale",
        "broker_truth_not_broker_backed": "position_safety_broker_truth_not_broker_backed",
        "broker_truth_lookup_failed": "position_safety_broker_truth_lookup_failed",
        "broker_truth_invalid_account": "position_safety_broker_truth_invalid_account",
        "broker_truth_invalid_schema": "position_safety_broker_truth_invalid_schema",
    }
    for blocker in list(status.get("blockers") or []):
        issue_key = blocker_map.get(str(blocker))
        if not issue_key:
            continue
        failures.append(
            {
                "issue_key": issue_key,
                "severity": "ERROR",
                "service": "POSITION_SAFETY",
                "error_type": issue_key,
                "detail": str(blocker),
                "dependency_state": dependency_state,
                "path": status.get("artifact_path"),
            }
        )
    if bool(status.get("lookup_failed")):
        failures.append(
            {
                "issue_key": "position_safety_lookup_failed",
                "severity": "ERROR",
                "service": "POSITION_SAFETY",
                "error_type": "position_safety_lookup_failed",
                "detail": "broker_position_truth_unavailable",
                "path": status.get("artifact_path"),
            }
        )
    if int(status.get("unsafe_position_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "position_safety_unsafe_position",
                "severity": "ERROR",
                "service": "POSITION_SAFETY",
                "error_type": "position_safety_unsafe_position",
                "detail": "broker_position_without_protection_flatten_quarantine_or_manual_lock",
                "unsafe_position_count": int(status.get("unsafe_position_count") or 0),
            }
        )
    if int(status.get("active_quarantine_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "position_safety_active_quarantine",
                "severity": "ERROR",
                "service": "POSITION_SAFETY",
                "error_type": "position_safety_active_quarantine",
                "detail": "active_order_or_position_quarantine_present",
                "active_quarantine_count": int(status.get("active_quarantine_count") or 0),
            }
        )
    contradictory = []
    for record in list(status.get("records") or []):
        if not isinstance(record, dict):
            continue
        if bool(record.get("active_quarantine")) or str(record.get("classified_as") or "").lower() == "quarantined":
            contradictory.append(record)
    if contradictory and str(status.get("go_no_go") or "").strip().lower() == "go":
        failures.append(
            {
                "issue_key": "position_safety_contradictory_go",
                "severity": "ERROR",
                "service": "POSITION_SAFETY",
                "error_type": "position_safety_contradictory_go",
                "detail": "artifact_go_with_quarantined_records",
                "record_count": len(contradictory),
            }
        )
    return failures


def _runtime_freshness_status(status: str) -> str:
    value = str(status or "").strip().upper()
    if value in {"CURRENT", "STALE_CRITICAL_MODULE", "UNKNOWN", "MISSING_MANIFEST", "BUILD_MISMATCH"}:
        return value
    return "UNKNOWN"


def evaluate_runtime_code_freshness(
    *,
    process_start_time: str | datetime | float | int | None,
    critical_modules: list[dict[str, Any]] | None,
    git_sha: str | None = None,
    build_id: str | None = None,
    source_tree_hash: str | None = None,
    expected_git_sha: str | None = None,
    expected_build_id: str | None = None,
    expected_source_tree_hash: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    def _to_dt(value: str | datetime | float | int | None) -> datetime | None:
        if isinstance(value, datetime):
            return value.astimezone(ET) if value.tzinfo else value.replace(tzinfo=ET)
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(float(value), ET)
        if isinstance(value, str):
            return _parse_iso(value)
        return None

    generated_at = (now.astimezone(ET) if now is not None else datetime.now(ET)).isoformat()
    process_start = _to_dt(process_start_time)
    blockers: list[str] = []
    warnings: list[str] = []
    stale_modules: list[dict[str, Any]] = []
    modules = list(critical_modules or [])
    status = "CURRENT"
    explanation = "runtime_loaded_code_current"

    if not modules:
        status = "MISSING_MANIFEST"
        blockers.append("runtime_version_missing_manifest")
        explanation = "critical_module_manifest_missing"
    elif process_start is None:
        status = "UNKNOWN"
        blockers.append("runtime_version_unknown")
        explanation = "process_start_time_missing_or_invalid"
    else:
        for module in modules:
            modified_at = _to_dt(
                module.get("modified_at")
                or module.get("mtime")
                or module.get("module_mtime")
            )
            if modified_at is None:
                warnings.append(f"{module.get('path') or 'unknown'}:modified_at_missing")
                continue
            if modified_at.timestamp() > process_start.timestamp() + 1.0:
                stale_modules.append(
                    {
                        "path": module.get("path"),
                        "modified_at": modified_at.isoformat(),
                        "loaded_at": module.get("loaded_at") or process_start.isoformat(),
                        "reason": "modified_after_process_start",
                    }
                )
        if stale_modules:
            status = "STALE_CRITICAL_MODULE"
            blockers.append("runtime_version_loaded_code_stale")
            explanation = "critical_module_modified_after_process_start"

    mismatches: list[str] = []
    if expected_git_sha and git_sha and expected_git_sha != git_sha:
        mismatches.append("git_sha")
    if expected_build_id and build_id and expected_build_id != build_id:
        mismatches.append("build_id")
    if expected_source_tree_hash and source_tree_hash and expected_source_tree_hash != source_tree_hash:
        mismatches.append("source_tree_hash")
    if mismatches:
        status = "BUILD_MISMATCH"
        blockers.append("runtime_version_build_mismatch")
        explanation = "runtime_build_identity_mismatch"

    status = _runtime_freshness_status(status)
    loaded_code_current = status == "CURRENT"
    return {
        "generated_at": generated_at,
        "status": status,
        "loaded_code_current": loaded_code_current,
        "runtime_version_loaded_code_stale": status == "STALE_CRITICAL_MODULE",
        "restart_required": status in {"STALE_CRITICAL_MODULE", "BUILD_MISMATCH", "UNKNOWN", "MISSING_MANIFEST"},
        "critical_modules_stale_count": len(stale_modules),
        "stale_modules": stale_modules,
        "blockers": _dedupe_labels(blockers),
        "warnings": _dedupe_labels(warnings),
        "explanation": explanation,
        "git_sha": git_sha,
        "build_id": build_id,
        "source_tree_hash": source_tree_hash,
    }


def _runtime_version_status(root: Path) -> dict[str, Any]:
    def _git(args: list[str]) -> str:
        try:
            return subprocess.check_output(["git", *args], cwd=str(root), text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return ""

    head = _git(["rev-parse", "HEAD"])
    dirty = bool(_git(["status", "--porcelain"]))
    modules = {}
    critical_modules: list[dict[str, Any]] = []
    for rel in CRITICAL_RUNTIME_MODULES:
        path = root / rel
        try:
            st = path.stat()
            modules[rel] = {"mtime": datetime.fromtimestamp(st.st_mtime, ET).isoformat(), "size": st.st_size}
            critical_modules.append(
                {
                    "path": rel,
                    "modified_at": modules[rel]["mtime"],
                    "loaded_at": None,
                    "loaded_code_current": None,
                    "reason": None,
                }
            )
        except Exception:
            modules[rel] = {"missing": True}
            critical_modules.append({"path": rel, "missing": True, "reason": "critical_module_missing"})
    config_paths = [
        path
        for pattern in ("config.yml", "config.yaml", "configs/**/*.yml", "configs/**/*.yaml", "configs/**/*.json")
        for path in root.glob(pattern)
        if path.is_file()
    ]
    config_hash = hashlib.sha256()
    config_files: list[dict[str, Any]] = []
    for path in sorted(config_paths):
        rel = str(path.relative_to(root))
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            config_hash.update(rel.encode("utf-8"))
            config_hash.update(b"\0")
            config_hash.update(digest.encode("ascii"))
            config_files.append({"path": rel, "sha256": digest, "size": len(data)})
        except Exception:
            config_hash.update(rel.encode("utf-8"))
            config_hash.update(b"\0missing")
            config_files.append({"path": rel, "missing": True})
    run_system = _run_system_status(root)
    run_system_pids = list(run_system.get("process_pids") or [])
    current_runtime_pid = int(run_system_pids[0]) if run_system_pids else os.getpid()
    process_start_ts = _process_start_ts(current_runtime_pid) or _PROCESS_START_TS
    process_start_iso = datetime.fromtimestamp(process_start_ts, ET).isoformat()
    for module in critical_modules:
        module["loaded_at"] = process_start_iso
    source_tree_hash = hashlib.sha256(
        json.dumps(modules, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    freshness = evaluate_runtime_code_freshness(
        process_start_time=process_start_iso,
        critical_modules=critical_modules,
        git_sha=head,
        build_id=os.getenv("BUILD_ID", ""),
        source_tree_hash=source_tree_hash,
        now=datetime.now(ET),
    )
    stale_paths = {str(item.get("path")) for item in list(freshness.get("stale_modules") or [])}
    for module in critical_modules:
        if str(module.get("path")) in stale_paths:
            module["loaded_code_current"] = False
            module["reason"] = "modified_after_process_start"
        elif not module.get("missing"):
            module["loaded_code_current"] = True
    return {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "git_head": head,
        "git_sha": head,
        "git_dirty": dirty,
        "build_id": os.getenv("BUILD_ID", ""),
        "source_tree_hash": source_tree_hash,
        "process_pid": current_runtime_pid,
        "process_start_time": process_start_iso,
        "runtime_id": f"{current_runtime_pid}:{process_start_iso}",
        "restart_id": f"{current_runtime_pid}:{process_start_iso}",
        "command": " ".join(shlex.quote(arg) for arg in sys.argv),
        "run_system_process_pids": run_system_pids,
        "run_system_lock_pid": run_system.get("lock_pid"),
        "run_system_lock_pid_alive": run_system.get("lock_pid_alive"),
        "watchdog_process_pid": os.getpid(),
        "watchdog_process_start_time": datetime.fromtimestamp(_PROCESS_START_TS, ET).isoformat(),
        "modules": modules,
        "critical_modules": critical_modules,
        "loaded_code_current": bool(freshness.get("loaded_code_current")),
        "stale_modules": list(freshness.get("stale_modules") or []),
        "freshness": freshness,
        "blockers": list(freshness.get("blockers") or []),
        "warnings": list(freshness.get("warnings") or []),
        "config_hash": config_hash.hexdigest(),
        "config_files": config_files,
    }


def _process_start_ts(pid: int) -> float | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(int(pid)), "-o", "lstart="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if not out:
            return None
        parsed = datetime.strptime(out, "%a %b %d %H:%M:%S %Y")
        return parsed.replace(tzinfo=ET).timestamp()
    except Exception:
        return None


def _attach_runtime_artifact_metadata(
    status: dict[str, Any],
    *,
    runtime_version: dict[str, Any],
    generated_at: datetime,
    max_age_sec: float,
) -> dict[str, Any]:
    generated_ts = _parse_iso(status.get("generated_at"))
    process_start_ts = _parse_iso(runtime_version.get("process_start_time"))
    age_sec = _age_seconds(generated_ts, generated_at)
    status.update(
        {
            "source_process_start_time": runtime_version.get("process_start_time"),
            "source_git_head": runtime_version.get("git_head"),
            "source_config_hash": runtime_version.get("config_hash"),
            "max_age_sec": float(max_age_sec),
            "age_sec": age_sec,
            "stale": age_sec is None or age_sec > float(max_age_sec),
            "artifact_before_process_start": bool(
                generated_ts is not None and process_start_ts is not None and generated_ts < process_start_ts
            ),
        }
    )
    return status


def _artifact_runtime_contract_failures(
    status: dict[str, Any],
    *,
    issue_prefix: str,
    service: str,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if bool(status.get("artifact_before_process_start")):
        failures.append(
            {
                "issue_key": f"{issue_prefix}_artifact_before_process_start",
                "severity": "ERROR",
                "service": service,
                "error_type": f"{issue_prefix}_artifact_before_process_start",
                "detail": "artifact_generated_before_current_run_system_process_started",
                "generated_at": status.get("generated_at"),
                "process_start_time": status.get("source_process_start_time"),
            }
        )
    if bool(status.get("stale")):
        failures.append(
            {
                "issue_key": f"{issue_prefix}_artifact_stale",
                "severity": "ERROR",
                "service": service,
                "error_type": f"{issue_prefix}_artifact_stale",
                "detail": "artifact_exceeded_max_age_sec",
                "age_sec": status.get("age_sec"),
                "max_age_sec": status.get("max_age_sec"),
            }
        )
    return failures


def _runtime_version_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    freshness = dict(status.get("freshness") or {})
    blocker_keys = list(freshness.get("blockers") or status.get("blockers") or [])
    if bool(status.get("loaded_code_current", True)) and not blocker_keys:
        return []
    stale_modules = list(status.get("stale_modules") or freshness.get("stale_modules") or [])
    if not blocker_keys:
        blocker_keys = ["runtime_version_loaded_code_stale"]
    failures: list[dict[str, Any]] = []
    for key in _dedupe_labels([str(item) for item in blocker_keys]):
        detail = "run_system_process_started_before_modified_critical_modules"
        if key == "runtime_version_missing_manifest":
            detail = "critical_module_manifest_missing"
        elif key == "runtime_version_unknown":
            detail = "runtime_code_freshness_unknown"
        elif key == "runtime_version_build_mismatch":
            detail = "runtime_build_identity_mismatch"
        failures.append(
            {
                "issue_key": key,
                "severity": "ERROR",
                "service": "RUNTIME_VERSION",
                "error_type": key,
                "detail": detail,
                "process_pid": status.get("process_pid"),
                "process_start_time": status.get("process_start_time"),
                "restart_required": bool(freshness.get("restart_required", True)),
                "freshness_status": freshness.get("status"),
                "stale_modules": stale_modules[:20],
            }
        )
    return failures


def _system_health_check(system_health: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if not bool(system_health.get("present")):
        failures.append(
            {
                "issue_key": "system_health_transition_missing",
                "severity": "ERROR",
                "service": "SYSTEM_HEALTH",
                "error_type": "system_health_transition_missing",
                "detail": "missing_system_audit",
            }
        )
        return failures
    if bool(system_health.get("unhealthy")):
        state = str(system_health.get("state") or "unknown")
        stale = bool(system_health.get("stale"))
        reason = str(system_health.get("reason") or "").strip()
        if (not stale) and state == "recovering" and reason != "manual_recovery_required":
            return failures
        if not stale and state == "degraded" and reason == "production_state_no_go":
            return failures
        if not stale and reason == "manual_recovery_required":
            raw = dict(system_health.get("raw") or {})
            payload = dict(raw.get("payload") or {})
            metadata = dict(payload.get("metadata") or {})
            circuit = dict(metadata.get("ib_gateway_circuit") or {})
            failures.append(
                {
                    "issue_key": "manual_gateway_api_recovery_required",
                    "severity": "ERROR",
                    "service": "SYSTEM_HEALTH",
                    "error_type": "manual_recovery_required",
                    "detail": "operator_ack_and_authorized_gateway_recovery_proof_required",
                    "reason": reason,
                    "since": circuit.get("opened_at"),
                    "source": circuit.get("source"),
                    "required_action": "operator_ack_then_single_lease_gateway_recovery_proof",
                    "last_proof_status": circuit.get("last_proof_status"),
                }
            )
            return failures
        failures.append(
            {
                "issue_key": "system_health_transition_stale" if stale else _issue_key("system_health", state),
                "severity": "ERROR",
                "service": "SYSTEM_HEALTH",
                "error_type": "system_health_transition_stale" if stale else "system_health_not_running",
                "detail": "stale_transition" if stale else state,
                "reason": system_health.get("reason"),
                "age_sec": system_health.get("age_sec"),
                "max_age_sec": system_health.get("max_age_sec"),
            }
        )
        for blocker in list(system_health.get("recovery_readiness_blockers") or []):
            failures.append(
                {
                    "issue_key": _issue_key("recovery_readiness", blocker),
                    "severity": "ERROR",
                    "service": "SYSTEM_HEALTH",
                    "error_type": "recovery_readiness_blocker",
                    "detail": blocker,
                }
            )
    return failures


def _row_ts_in_window(row: dict[str, Any], *, since: datetime | None) -> bool:
    if since is None:
        return True
    row_ts = _parse_iso(row.get("ts") or row.get("timestamp") or row.get("generated_at"))
    if row_ts is None:
        return False
    if row_ts.tzinfo is None:
        row_ts = row_ts.replace(tzinfo=ET)
    if since.tzinfo is None:
        since = since.replace(tzinfo=ET)
    return row_ts.astimezone(ET) >= since.astimezone(ET)


def _audit_has_ib_disconnect(*, audit_dir: Path, since: datetime | None = None) -> bool:
    for path in sorted(audit_dir.glob("system-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for raw in lines:
            text = raw.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if not _row_ts_in_window(row, since=since):
                continue
            component = str(row.get("component") or "")
            stage = str(row.get("stage") or "")
            status = str(row.get("status") or "")
            if component == "market_data_service" and stage in {"ib_disconnected", "service_stopped"}:
                return True
            if component == "system_health_controller" and status in {"recovering", "degraded"}:
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                reason = str(payload.get("reason") or row.get("reason") or "")
                reason_l = reason.lower()
                if (
                    ("ib" in reason_l or "broker" in reason_l)
                    and any(token in reason_l for token in ("disconnect", "disconnected", "connection_lost", "lost"))
                ):
                    return True
    return False


def _ib_restart_drill_status(
    *,
    root: Path,
    day_id: str,
    audit_dir: Path,
    process_start_time: str | None = None,
) -> dict[str, Any]:
    artifact_day = f"{day_id[:4]}-{day_id[4:6]}-{day_id[6:8]}"
    latest_path = root / "runtime/readiness/drills/paper_ib_restart_latest.json"
    day_path = root / "runtime/readiness/drills" / f"paper_ib_restart_{day_id}_latest.json"
    artifact = _read_json(day_path) if day_path.exists() else _read_json(latest_path)
    artifact_day_value = str(artifact.get("day") or "")
    raw_generated_at = artifact.get("generated_at") or artifact.get("created_at")
    artifact_generated_at = _parse_iso(raw_generated_at)
    process_start = _parse_iso(process_start_time)
    artifact_generated_at_missing = bool(artifact) and artifact_generated_at is None
    artifact_before_process_start = False
    if artifact_generated_at is not None and process_start is not None:
        artifact_before_process_start = artifact_generated_at < process_start
    required = _audit_has_ib_disconnect(audit_dir=audit_dir, since=process_start)
    return {
        "required": required,
        "required_window": "process_start" if process_start is not None else "day",
        "artifact_present": bool(artifact),
        "artifact_path": str(day_path if day_path.exists() else latest_path),
        "artifact_day": artifact_day_value,
        "expected_day": artifact_day,
        "generated_at": raw_generated_at,
        "generated_at_missing": artifact_generated_at_missing,
        "process_start_time": process_start_time,
        "artifact_before_process_start": artifact_before_process_start,
        "go_no_go": artifact.get("go_no_go"),
        "blockers": list(artifact.get("blockers") or []),
        "timeline": list(artifact.get("timeline") or []),
        "proof_mode": artifact.get("proof_mode"),
        "counts": artifact.get("counts") if isinstance(artifact.get("counts"), dict) else {},
        "source_pid": artifact.get("source_pid"),
        "git_head": artifact.get("git_head"),
        "config_hash": artifact.get("config_hash"),
    }


def _ib_restart_drill_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if not bool(status.get("required")) and not bool(status.get("artifact_present")):
        return failures
    if not bool(status.get("artifact_present")):
        return [
            {
                "issue_key": "ib_restart_drill_artifact_missing",
                "severity": "ERROR",
                "service": "IB_RESTART_DRILL",
                "error_type": "ib_restart_drill_artifact_missing",
                "detail": "ib_disconnect_seen_without_machine_checkable_drill_artifact",
                "path": status.get("artifact_path"),
            }
        ]
    artifact_day = str(status.get("artifact_day") or "")
    expected_day = str(status.get("expected_day") or "")
    if not bool(status.get("required")) and artifact_day != expected_day:
        return failures
    if artifact_day != expected_day:
        failures.append(
            {
                "issue_key": "ib_restart_drill_artifact_stale",
                "severity": "ERROR",
                "service": "IB_RESTART_DRILL",
                "error_type": "ib_restart_drill_artifact_stale",
                "detail": "latest_drill_artifact_is_not_for_watchdog_day",
                "artifact_day": status.get("artifact_day"),
                "expected_day": status.get("expected_day"),
                "path": status.get("artifact_path"),
            }
        )
    if bool(status.get("artifact_before_process_start")):
        failures.append(
            {
                "issue_key": "ib_restart_drill_artifact_before_process_start",
                "severity": "ERROR",
                "service": "IB_RESTART_DRILL",
                "error_type": "ib_restart_drill_artifact_before_process_start",
                "detail": "drill_artifact_was_generated_before_current_run_system_process_started",
                "generated_at": status.get("generated_at"),
                "process_start_time": status.get("process_start_time"),
                "path": status.get("artifact_path"),
            }
        )
    if bool(status.get("generated_at_missing")):
        failures.append(
            {
                "issue_key": "ib_restart_drill_artifact_generated_at_missing",
                "severity": "ERROR",
                "service": "IB_RESTART_DRILL",
                "error_type": "ib_restart_drill_artifact_generated_at_missing",
                "detail": "drill_artifact_missing_generated_at_cannot_be_bound_to_current_process",
                "process_start_time": status.get("process_start_time"),
                "path": status.get("artifact_path"),
            }
        )
    if str(status.get("go_no_go") or "").lower() != "go":
        failures.append(
            {
                "issue_key": "ib_restart_drill_no_go",
                "severity": "ERROR",
                "service": "IB_RESTART_DRILL",
                "error_type": "ib_restart_drill_no_go",
                "detail": "paper_ib_restart_drill_failed",
                "blockers": list(status.get("blockers") or []),
                "path": status.get("artifact_path"),
            }
        )
    return failures


def _strategy_tradability_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if str(status.get("go_no_go") or "").lower() == "go":
        return []
    blockers = list(status.get("blockers") or [])
    blockers = [
        blocker
        for blocker in blockers
        if str(blocker or "").strip()
        and str(blocker or "").strip().split(":")[-1] != "system_paused"
        and str(blocker or "").strip().split(":")[-1] not in {"idle_not_in_entry_window", "not_in_entry_window", "offsession"}
        and str(blocker or "").strip().split(":")[-1] != "fresh_tradability_heartbeat_stale"
    ]
    if not blockers:
        return []
    failures: list[dict[str, Any]] = []
    for blocker in blockers:
        failures.append(
            {
                "issue_key": _issue_key("strategy_tradability", blocker),
                "severity": "ERROR",
                "service": "STRATEGY_TRADABILITY",
                "error_type": "strategy_not_tradable",
                "detail": str(blocker),
            }
        )
    return failures


def _data_availability_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if bool(status.get("ok")):
        return []
    gates = list(status.get("gates") or [])
    if not gates:
        return [
            {
                "issue_key": "data_availability_no_go",
                "severity": "ERROR",
                "service": "DATA_AVAILABILITY",
                "error_type": "data_availability_no_go",
                "detail": str(status.get("error") or "data_availability_not_ok"),
            }
        ]
    failures: list[dict[str, Any]] = []
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        if str(gate.get("severity") or "entry_blocking") != "entry_blocking":
            continue
        strategy_id = str(gate.get("strategy_id") or "").strip()
        symbol = str(gate.get("symbol") or "").strip()
        dataset = str(gate.get("dataset") or "").strip()
        resolution = str(gate.get("resolution") or "").strip()
        stage = str(gate.get("stage") or "").strip()
        reason = str(gate.get("reason") or "data_unavailable").strip()
        parts = [item for item in (strategy_id, symbol, dataset, resolution, stage, reason) if item]
        detail = ":".join(parts) if parts else "entry_blocking_data_gate"
        failures.append(
            {
                "issue_key": _issue_key("data_availability", detail),
                "severity": "ERROR",
                "service": "DATA_AVAILABILITY",
                "error_type": "entry_blocking_data_gate",
                "detail": detail,
            }
        )
    if failures:
        return failures
    return [
        {
            "issue_key": "data_availability_entry_blocking",
            "severity": "ERROR",
            "service": "DATA_AVAILABILITY",
            "error_type": "entry_blocking_data_gate",
            "detail": "entry_blocking_data_gate",
        }
    ]


def _recompute_data_availability_counts(status: dict[str, Any]) -> dict[str, Any]:
    result = dict(status or {})
    gates = [dict(gate) for gate in list(result.get("gates") or []) if isinstance(gate, dict)]
    ignored_gates = [
        dict(gate) for gate in list(result.get("ignored_gates") or []) if isinstance(gate, dict)
    ]
    platform_gates = [gate for gate in gates if str(gate.get("dataset") or "") != "strategy_tradability"]
    strategy_gates = [gate for gate in gates if str(gate.get("dataset") or "") == "strategy_tradability"]
    result["gates"] = gates
    result["ignored_gates"] = ignored_gates
    result["ok"] = len(gates) == 0
    result["active_gate_count"] = len(gates)
    result["ignored_gate_count"] = len(ignored_gates)
    result["entry_blocking_count"] = sum(1 for gate in gates if gate.get("severity") == "entry_blocking")
    result["rth_entry_blocking_count"] = sum(
        1 for gate in gates if gate.get("severity") == "entry_blocking" and bool(gate.get("rth"))
    )
    result["platform_entry_blocking_count"] = sum(
        1 for gate in platform_gates if gate.get("severity") == "entry_blocking"
    )
    result["rth_platform_entry_blocking_count"] = sum(
        1
        for gate in platform_gates
        if gate.get("severity") == "entry_blocking" and bool(gate.get("rth"))
    )
    result["strategy_tradability_entry_blocking_count"] = sum(
        1 for gate in strategy_gates if gate.get("severity") == "entry_blocking"
    )
    result["rth_strategy_tradability_entry_blocking_count"] = sum(
        1
        for gate in strategy_gates
        if gate.get("severity") == "entry_blocking" and bool(gate.get("rth"))
    )
    return result


def _reconcile_strategy_tradability_data_gates(
    status: dict[str, Any],
    *,
    strategy_heartbeats: dict[str, Any],
) -> dict[str, Any]:
    """Ignore per-strategy entry gates superseded by newer heartbeat truth."""
    gates = list((status or {}).get("gates") or [])
    if not gates:
        return dict(status or {})
    kept: list[dict[str, Any]] = []
    ignored = [
        dict(gate) for gate in list((status or {}).get("ignored_gates") or []) if isinstance(gate, dict)
    ]
    for raw_gate in gates:
        if not isinstance(raw_gate, dict):
            continue
        gate = dict(raw_gate)
        strategy_id = str(gate.get("strategy_id") or "").strip()
        heartbeat = strategy_heartbeats.get(strategy_id) if strategy_id else None
        if (
            str(gate.get("dataset") or "") == "strategy_tradability"
            and isinstance(heartbeat, dict)
            and bool(heartbeat.get("stale")) is not True
        ):
            details = heartbeat.get("details") if isinstance(heartbeat.get("details"), dict) else {}
            if (
                bool(details)
                and details.get("strategy_session_open") is False
                and str(gate.get("severity") or "") == "entry_blocking"
            ):
                gate["ignored_reason"] = "entry_gate_outside_strategy_session"
                gate["heartbeat_generated_at"] = heartbeat.get("generated_at")
                gate["heartbeat_loop_last_stage"] = details.get("loop_last_stage")
                ignored.append(gate)
                continue
            heartbeat_ts = _parse_iso(heartbeat.get("generated_at"))
            gate_ts = _parse_iso(gate.get("last_seen"))
            if heartbeat_ts is not None and gate_ts is not None:
                if heartbeat_ts.tzinfo is None:
                    heartbeat_ts = heartbeat_ts.replace(tzinfo=ET)
                if gate_ts.tzinfo is None:
                    gate_ts = gate_ts.replace(tzinfo=ET)
                if heartbeat_ts.astimezone(ET) + timedelta(seconds=1) >= gate_ts.astimezone(ET):
                    heartbeat_blockers = {str(item or "") for item in list(heartbeat.get("blockers") or [])}
                    gate_reason = str(gate.get("reason") or "").strip()
                    gate_stage = str(gate.get("stage") or "").strip()
                    same_gate_active = gate_reason in heartbeat_blockers or (
                        bool(gate_stage) and f"{gate_stage}:{gate_reason}" in heartbeat_blockers
                    )
                    if bool(heartbeat.get("tradable")) is True or not same_gate_active:
                        gate["ignored_reason"] = (
                            "superseded_by_fresh_tradable_heartbeat"
                            if bool(heartbeat.get("tradable")) is True
                            else "superseded_by_fresh_heartbeat_blocker_change"
                        )
                        gate["heartbeat_generated_at"] = heartbeat.get("generated_at")
                        gate["heartbeat_blockers"] = sorted(heartbeat_blockers)
                        ignored.append(gate)
                        continue
        kept.append(gate)
    result = dict(status or {})
    result["gates"] = kept
    result["ignored_gates"] = ignored
    return _recompute_data_availability_counts(result)


def _option_contract_required_strategy_ids(config: dict[str, Any]) -> list[str]:
    result: list[str] = []
    strategies = config.get("strategies") if isinstance(config, dict) else []
    if not isinstance(strategies, list):
        return result
    for spec in strategies:
        if not isinstance(spec, dict) or not bool(spec.get("enabled")):
            continue
        strategy_id = str(spec.get("id") or "").strip()
        if not strategy_id:
            continue
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
        manifest = extras.get("production_manifest") if isinstance(extras.get("production_manifest"), dict) else {}
        if not bool(manifest.get("production_blocking", True)):
            continue
        option_contract = manifest.get("option_contract") if isinstance(manifest.get("option_contract"), dict) else {}
        if bool(option_contract.get("required")) and strategy_id not in result:
            result.append(strategy_id)
    return result


def _recent_option_handoff_status(
    *,
    root: Path,
    now_et: datetime,
    required_strategy_ids: list[str],
    max_age_sec: float = 300.0,
) -> dict[str, Any]:
    path = root / "runtime/state/qqq_stock_option_handoff.jsonl"
    if not required_strategy_ids or not path.exists():
        return {"artifact_present": path.exists(), "path": str(path), "recent_required_event": False}
    required = {str(item or "").strip() for item in required_strategy_ids if str(item or "").strip()}
    latest: dict[str, Any] | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-1000:]
    except Exception as exc:
        return {"artifact_present": True, "path": str(path), "recent_required_event": False, "lookup_failed": True, "error": str(exc)}
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        consumer = str(row.get("consumer_strategy_id") or "").strip()
        if consumer not in required:
            continue
        ts = _parse_iso(row.get("ts"))
        age_sec = _age_seconds(ts, now_et)
        if age_sec is None or age_sec > float(max_age_sec):
            continue
        if latest is None or float(age_sec) < float(latest.get("age_sec") or 1e18):
            latest = dict(row)
            latest["age_sec"] = age_sec
    if latest is None:
        return {"artifact_present": True, "path": str(path), "recent_required_event": False}
    return {
        "artifact_present": True,
        "path": str(path),
        "recent_required_event": True,
        "strategy_id": str(latest.get("consumer_strategy_id") or ""),
        "trigger_id": str(latest.get("trigger_id") or latest.get("trigger_key") or ""),
        "status": str(latest.get("status") or ""),
        "reason": str(latest.get("reason") or ""),
        "age_sec": latest.get("age_sec"),
        "row": latest,
    }


def _contract_selection_status(
    *,
    root: Path,
    now_et: datetime,
    max_age_sec: float = 300.0,
    required_option_strategy_ids: list[str] | None = None,
) -> dict[str, Any]:
    return build_contract_selection_status(
        repo_root=root,
        generated_at=now_et,
        required_option_strategy_ids=list(required_option_strategy_ids or []),
        max_age_sec=max_age_sec,
    )


def _contract_selection_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if bool(status.get("ok")):
        return []
    return [
        {
            "issue_key": _issue_key(
                "contract_selection",
                f"{status.get('strategy_id')}:{status.get('symbol')}:{status.get('reason')}",
            ),
            "severity": "ERROR",
            "service": "CONTRACT_SELECTION",
            "error_type": "contract_selection_no_go",
            "detail": str(status.get("reason") or "contract_selection_failed"),
            "strategy_id": status.get("strategy_id"),
            "symbol": status.get("symbol"),
            "path": status.get("path"),
        }
    ]


def _option_trigger_ledger_status(
    *,
    root: Path,
    config: dict[str, Any],
    generated_at: datetime,
    required_strategy_ids: list[str],
    runtime_version: dict[str, Any],
) -> dict[str, Any]:
    ops_cfg = config.get("operations") if isinstance(config, dict) else {}
    watchdog_cfg = (ops_cfg or {}).get("runtime_watchdog") if isinstance(ops_cfg, dict) else {}
    enabled = bool((watchdog_cfg or {}).get("check_option_trigger_ledger", True))
    max_age_sec = float(os.getenv("OPTION_TRIGGER_LEDGER_MAX_AGE_SEC", "300"))
    if not enabled:
        status = disabled_option_trigger_ledger_status(generated_at=generated_at)
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_option_trigger_ledger_artifact(repo_root=root, status=status)
        return status
    try:
        status = build_option_trigger_ledger(
            repo_root=root,
            generated_at=generated_at,
            required_strategy_ids=required_strategy_ids,
            max_age_sec=max_age_sec,
        )
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_option_trigger_ledger_artifact(repo_root=root, status=status)
        return status
    except Exception as exc:
        status = failed_option_trigger_ledger_status(generated_at=generated_at, error=str(exc))
        _attach_runtime_artifact_metadata(
            status,
            runtime_version=runtime_version,
            generated_at=generated_at,
            max_age_sec=max_age_sec,
        )
        write_option_trigger_ledger_artifact(repo_root=root, status=status)
        return status


def _option_trigger_ledger_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(status.get("enabled")):
        return []
    required_strategy_ids = [
        str(item or "").strip()
        for item in list(status.get("required_strategy_ids") or [])
        if str(item or "").strip()
    ]
    failures = _artifact_runtime_contract_failures(
        status,
        issue_prefix="option_trigger_ledger",
        service="OPTION_TRIGGER_LEDGER",
    )
    if bool(status.get("lookup_failed")):
        failures.append(
            {
                "issue_key": "option_trigger_ledger_lookup_failed",
                "severity": "ERROR",
                "service": "OPTION_TRIGGER_LEDGER",
                "error_type": "option_trigger_ledger_lookup_failed",
                "detail": str(status.get("error") or "lookup_failed"),
            }
        )
    if int(status.get("active_trigger_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "option_trigger_active_without_terminal_verdict",
                "severity": "ERROR",
                "service": "OPTION_TRIGGER_LEDGER",
                "error_type": "option_trigger_active_without_terminal_verdict",
                "detail": "recent_option_trigger_lacks_selected_rejected_or_expired_verdict",
                "count": int(status.get("active_trigger_count") or 0),
                "required_strategy_ids": required_strategy_ids,
            }
        )
    if int(status.get("rejected_trigger_count") or 0) > 0:
        failures.append(
            {
                "issue_key": "option_trigger_rejected_contract_selection",
                "severity": "ERROR",
                "service": "OPTION_TRIGGER_LEDGER",
                "error_type": "option_trigger_rejected_contract_selection",
                "detail": "recent_option_trigger_rejected_by_contract_selection",
                "count": int(status.get("rejected_trigger_count") or 0),
                "required_strategy_ids": required_strategy_ids,
            }
        )
    return failures


def _strategy_manifest_gate_status(*, root: Path, config: dict[str, Any], generated_at: datetime) -> dict[str, Any]:
    return build_strategy_manifest_gate(config=config, generated_at=generated_at, repo_root=root)


def _strategy_manifest_gate_check(status: dict[str, Any]) -> list[dict[str, Any]]:
    if bool(status.get("ok")):
        return []
    failures: list[dict[str, Any]] = []
    for blocker in list(status.get("blockers") or []):
        blocker_s = str(blocker or "").strip()
        if not blocker_s:
            continue
        failures.append(
            {
                "issue_key": _issue_key("strategy_manifest_gate", blocker_s),
                "severity": "ERROR",
                "service": "STRATEGY_MANIFEST_GATE",
                "error_type": "strategy_manifest_gate_no_go",
                "detail": blocker_s,
            }
        )
    if failures:
        return failures
    return [
        {
            "issue_key": "strategy_manifest_gate_no_go",
            "severity": "ERROR",
            "service": "STRATEGY_MANIFEST_GATE",
            "error_type": "strategy_manifest_gate_no_go",
            "detail": "strategy_manifest_gate_not_ok",
        }
    ]


def _is_safety_production_failure(failure: dict[str, Any]) -> bool:
    key = str(failure.get("issue_key") or failure.get("error_type") or "").strip()
    service = str(failure.get("service") or "").strip().upper()
    if not key:
        return True
    if service in {"MANUAL_LOCK", "PLATFORM_EOD"} or key.startswith(
        ("manual_intervention_lock", "platform_eod")
    ):
        return False
    soft_exact = {
        "ib_restart_drill_artifact_missing",
        "ib_restart_drill_artifact_stale",
        "ib_restart_drill_no_go",
        "system_health_transition_stale",
        "telegram_notifier_unavailable",
        "qqq_runtime_audit_missing",
    }
    soft_prefixes = (
        "contract_selection:",
        "data_availability:",
        "ib_restart_drill",
        "opening_artifact_",
        "option_trigger_ledger:",
        "qqq_strategy_stale:",
        "strategy_manifest_gate:",
        "strategy_tradability:",
        "system_health:",
    )
    if key in soft_exact or key.startswith(soft_prefixes):
        return False
    if key.startswith("data_availability:"):
        return False
    hard_prefixes = (
        "active_quarantine",
        "broker",
        "decision_pipeline",
        "disk_space",
        "execution",
        "manual_intervention",
        "platform_eod",
        "position_ledger",
        "position_safety",
        "recovery_incidents",
        "recovery_readiness",
        "runtime_reconciliation",
        "runtime_version",
        "run_system",
        "session_end",
    )
    if key.startswith(hard_prefixes):
        return True
    if service in {
        "BROKER_TRUTH",
        "DECISION_PIPELINE",
        "DISK_SPACE",
        "EXECUTION",
        "FILL_ATTRIBUTION",
        "MANUAL_INTERVENTION",
        "PLATFORM_EOD",
        "POSITION_LEDGER",
        "POSITION_SAFETY",
        "RECOVERY_INCIDENTS",
        "RECOVERY_READINESS",
        "RUNTIME_RECONCILIATION",
        "RUNTIME_VERSION",
        "RUN_SYSTEM",
        "SESSION_END",
    }:
        return True
    return False


def _split_production_failures(failures: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    safety_failures: list[dict[str, Any]] = []
    entry_failures: list[dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, dict):
            safety_failures.append({"issue_key": "unknown_failure", "detail": str(failure)})
            continue
        if _is_safety_production_failure(failure):
            safety_failures.append(failure)
        else:
            entry_failures.append(failure)
    return safety_failures, entry_failures


def _failure_bucket_key(failure: dict[str, Any]) -> str:
    return str(failure.get("issue_key") or failure.get("error_type") or failure.get("detail") or "").strip()


def _failure_bucket_label(failure: dict[str, Any]) -> str:
    key = _failure_bucket_key(failure)
    detail = str(failure.get("detail") or "").strip()
    if detail and detail != key:
        return f"{key}:{detail}"
    return key or str(failure)


def _is_artifact_stale_failure(failure: dict[str, Any]) -> bool:
    key = _failure_bucket_key(failure)
    detail = str(failure.get("detail") or "").strip()
    return (
        "artifact_stale" in key
        or key.endswith("_latest_stale")
        or "fresh_tradability_heartbeat_stale" in key
        or "artifact_exceeded_max_age_sec" in detail
    )


def _is_runtime_failure(failure: dict[str, Any]) -> bool:
    key = _failure_bucket_key(failure)
    service = str(failure.get("service") or "").strip().upper()
    return key.startswith("runtime_version") or service == "RUNTIME_VERSION"


def _is_strategy_idle_failure(failure: dict[str, Any]) -> bool:
    key = _failure_bucket_key(failure)
    tail = key.split(":")[-1]
    return tail in {"idle_not_in_entry_window", "not_in_entry_window", "offsession", "no_signal", "waiting"}


def _is_global_risk_duplicate(failure: dict[str, Any], safety_labels: set[str]) -> bool:
    key = _failure_bucket_key(failure)
    if not key.startswith("strategy_tradability:"):
        return False
    tail = key.split(":")[-1]
    if tail.startswith("broker_truth"):
        return any("broker_truth" in item for item in safety_labels)
    if tail.startswith("position_safety"):
        return any("position_safety" in item for item in safety_labels)
    if tail in {"system_paused", "global_no_go"}:
        return True
    return False


def _dedupe_labels(labels: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        text = str(label or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _strategy_tradability_idle_reasons(strategy_tradability: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for record in list(strategy_tradability.get("records") or []):
        if not isinstance(record, dict):
            continue
        reason = str(record.get("idle_reason") or "").strip()
        if reason:
            reasons.append(f"{record.get('strategy_id') or 'unknown'}:{reason}")
    return _dedupe_labels(reasons)


def _strategy_tradability_stale_labels(strategy_tradability: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for record in list(strategy_tradability.get("records") or []):
        if not isinstance(record, dict):
            continue
        for blocker in list(record.get("blockers") or []):
            if str(blocker).split(":")[-1] == "fresh_tradability_heartbeat_stale":
                labels.append(f"{record.get('strategy_id') or 'unknown'}:{blocker}")
    return _dedupe_labels(labels)


def _production_state_buckets(
    *,
    failures: list[dict[str, Any]],
    safety_failures: list[dict[str, Any]],
    entry_failures: list[dict[str, Any]],
    strategy_tradability: dict[str, Any],
) -> dict[str, list[str]]:
    safety_blockers = [
        _failure_bucket_label(item)
        for item in safety_failures
        if not _is_runtime_failure(item) and not _is_artifact_stale_failure(item)
    ]
    runtime_blockers = [_failure_bucket_label(item) for item in failures if isinstance(item, dict) and _is_runtime_failure(item)]
    artifact_stale_blockers = [
        _failure_bucket_label(item)
        for item in failures
        if isinstance(item, dict) and _is_artifact_stale_failure(item) and not _is_runtime_failure(item)
    ]
    artifact_stale_blockers.extend(_strategy_tradability_stale_labels(strategy_tradability))
    safety_label_set = set(safety_blockers)
    entry_blockers = [
        _failure_bucket_label(item)
        for item in entry_failures
        if not _is_artifact_stale_failure(item)
        and not _is_strategy_idle_failure(item)
        and not _is_global_risk_duplicate(item, safety_label_set)
    ]
    return {
        "safety_blockers": _dedupe_labels(safety_blockers),
        "entry_blockers": _dedupe_labels(entry_blockers),
        "artifact_stale_blockers": _dedupe_labels(artifact_stale_blockers),
        "runtime_blockers": _dedupe_labels(runtime_blockers),
        "idle_reasons": _strategy_tradability_idle_reasons(strategy_tradability),
        "warnings": [],
    }


def _readiness_summary(report: dict[str, Any], buckets: dict[str, list[str]]) -> dict[str, str]:
    def _state(name: str, payload: dict[str, Any]) -> str:
        if not payload:
            return "unknown"
        if name == "runtime_version":
            return "go" if not buckets["runtime_blockers"] else "no_go"
        value = str(payload.get("go_no_go") or "").lower()
        if value in {"go", "no_go"}:
            return value
        if bool(payload.get("ok")):
            return "go"
        if payload.get("ok") is False:
            return "no_go"
        return "unknown"

    return {
        "broker_truth": _state("broker_truth", report.get("broker_truth_snapshot") or {}),
        "position_safety": _state("position_safety", report.get("position_safety") or {}),
        "strategy_tradability": _state("strategy_tradability", report.get("strategy_tradability") or {}),
        "session_end": _state("session_end", report.get("session_end") or {}),
        "recovery_incidents": _state("recovery_incidents", report.get("recovery_incidents") or {}),
        "runtime_version": _state("runtime_version", report.get("runtime_version") or {}),
    }


def _runtime_version_readiness_detail(runtime_version: dict[str, Any], buckets: dict[str, list[str]]) -> dict[str, Any]:
    freshness = dict(runtime_version.get("freshness") or {})
    stale_modules = list(runtime_version.get("stale_modules") or freshness.get("stale_modules") or [])
    loaded_code_current = bool(runtime_version.get("loaded_code_current", freshness.get("loaded_code_current", True)))
    return {
        "status": "go" if not buckets["runtime_blockers"] else "no_go",
        "freshness_status": freshness.get("status") or ("CURRENT" if loaded_code_current else "STALE_CRITICAL_MODULE"),
        "loaded_code_current": loaded_code_current,
        "restart_required": bool(freshness.get("restart_required", bool(buckets["runtime_blockers"]))),
        "stale_modules_count": int(
            freshness.get("critical_modules_stale_count")
            if freshness.get("critical_modules_stale_count") is not None
            else len(stale_modules)
        ),
        "blockers": list(buckets["runtime_blockers"]),
        "process_pid": runtime_version.get("process_pid"),
        "process_start_time": runtime_version.get("process_start_time"),
        "runtime_id": runtime_version.get("runtime_id"),
        "restart_id": runtime_version.get("restart_id"),
    }


def _readiness_details(report: dict[str, Any], buckets: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "runtime_version": _runtime_version_readiness_detail(report.get("runtime_version") or {}, buckets),
        "ib_gateway_operator_proof": report.get("ib_gateway_operator_proof") or {},
    }


def _ib_gateway_operator_proof_status(
    *,
    root: Path,
    now_et: datetime,
    max_age_sec: float = 900.0,
) -> dict[str, Any]:
    path = root / "runtime/recovery/ib_gateway_operator_proof/latest.json"
    payload = _read_json(path)
    generated_at = payload.get("generated_at")
    ts = _parse_iso(generated_at)
    age_sec = _age_seconds(ts, now_et)
    present = bool(payload)
    stale = bool(present and (age_sec is None or age_sec > max_age_sec))
    return {
        "artifact_type": "ib_gateway_operator_proof",
        "path": str(path),
        "present": present,
        "latest_result": payload.get("result") if present else None,
        "generated_at": generated_at if present else None,
        "source": payload.get("source") if present else None,
        "manual_required_cleared": bool(payload.get("manual_required_cleared")) if present else False,
        "age_sec": age_sec if present else None,
        "stale": stale,
    }


def _strategy_surface_summary(report: dict[str, Any]) -> dict[str, int]:
    surface = report.get("strategy_surface") or report.get("live_strategy_surface") or {}
    if not isinstance(surface, dict):
        return {
            "production_eligible_count": 0,
            "canary_only_count": 0,
            "shadow_only_count": 0,
            "disabled_until_contract_parity_count": 0,
            "unknown_path_count": 0,
        }
    summary = dict(surface.get("strategy_surface_summary") or surface.get("summary") or {})
    rows = list(surface.get("strategies") or [])

    def _count_tier(tier: str) -> int:
        return sum(1 for row in rows if isinstance(row, dict) and str(row.get("permission_tier") or "") == tier)

    def _count_path(path_type: str) -> int:
        return sum(1 for row in rows if isinstance(row, dict) and str(row.get("path_type") or "") == path_type)

    return {
        "production_eligible_count": int(summary.get("production_eligible_count") or _count_tier("PRODUCTION_ELIGIBLE")),
        "canary_only_count": int(summary.get("canary_only_count") or _count_tier("CANARY_ONLY")),
        "shadow_only_count": int(summary.get("shadow_only_count") or _count_tier("SHADOW_ONLY")),
        "disabled_until_contract_parity_count": int(
            summary.get("disabled_until_contract_parity_count") or _count_tier("DISABLED_UNTIL_CONTRACT_PARITY")
        ),
        "unknown_path_count": int(summary.get("unknown_path_count") or _count_path("UNKNOWN_PATH")),
    }


def _build_production_state(report: dict[str, Any]) -> dict[str, Any]:
    from services.production.scoped_tradability import build_production_state_scoped_contract

    failures = list(report.get("failures") or [])
    runtime_version = dict(report.get("runtime_version") or {})
    safety_failures, entry_failures = _split_production_failures(failures)
    strategy_tradability = report.get("strategy_tradability") or {}
    strategy_surface_summary = _strategy_surface_summary(report)
    buckets = _production_state_buckets(
        failures=failures,
        safety_failures=safety_failures,
        entry_failures=entry_failures,
        strategy_tradability=strategy_tradability,
    )
    safety_failure_count = len(buckets["safety_blockers"])
    entry_failure_count = len(buckets["entry_blockers"])
    artifact_stale_count = len(buckets["artifact_stale_blockers"])
    runtime_failure_count = len(buckets["runtime_blockers"])
    failure_count = safety_failure_count + entry_failure_count + artifact_stale_count + runtime_failure_count
    safety_go_no_go = "go" if safety_failure_count == 0 and runtime_failure_count == 0 else "no_go"
    entry_go_no_go = "go" if entry_failure_count == 0 and safety_go_no_go == "go" else "no_go"
    scoped_contract = build_production_state_scoped_contract(
        safety_go_no_go=safety_go_no_go,
        entry_go_no_go=entry_go_no_go,
        buckets=buckets,
        strategy_tradability=strategy_tradability,
        global_blockers=tuple(buckets["safety_blockers"] + buckets["runtime_blockers"]),
        entry_failures=tuple(entry_failures),
    )
    entry_go_no_go = str(scoped_contract.get("entry_go_no_go") or entry_go_no_go)
    summary = {
        "failure_count": failure_count,
        "safety_failure_count": safety_failure_count,
        "entry_failure_count": entry_failure_count,
        "artifact_stale_count": artifact_stale_count,
        "runtime_failure_count": runtime_failure_count,
        "idle_count": int(strategy_tradability.get("idle_count") or 0),
        "stale_heartbeat_count": int(strategy_tradability.get("stale_heartbeat_count") or 0),
        "tradable_entry_ready_count": int(strategy_tradability.get("tradable_entry_ready_count") or 0),
        "close_only_allowed_count": int(strategy_tradability.get("close_only_allowed_count") or 0),
        "strategy_surface": strategy_surface_summary,
    }
    return {
        "artifact_type": "production_state",
        "generated_at": report.get("generated_at"),
        "day": report.get("day"),
        "runtime_pid": runtime_version.get("process_pid"),
        "runtime_process_start_time": runtime_version.get("process_start_time"),
        "runtime_git_sha": runtime_version.get("git_head") or runtime_version.get("git_sha"),
        "runtime_config_hash": runtime_version.get("config_hash"),
        "runtime_lifecycle_generation": runtime_version.get("runtime_id") or runtime_version.get("restart_id"),
        "artifact_source_pid": os.getpid(),
        "artifact_source_component": "runtime_operations_watchdog",
        "go_no_go": safety_go_no_go,
        "global_halt": bool(scoped_contract.get("global_halt")),
        "safety_go_no_go": safety_go_no_go,
        "entry_go_no_go": entry_go_no_go,
        "blocked_scopes": list(scoped_contract.get("blocked_scopes") or []),
        "flow_overrides": list(scoped_contract.get("flow_overrides") or []),
        "tradability_authority": scoped_contract.get("authority"),
        "production_state_authorizes_execution": bool(scoped_contract.get("production_state_authorizes_execution")),
        "recommendation": report.get("recommendation"),
        "summary": summary,
        "buckets": buckets,
        "readiness": _readiness_summary(report, buckets),
        "readiness_details": _readiness_details(report, buckets),
        "failure_count": failure_count,
        "failures": failures,
        "safety_failure_count": safety_failure_count,
        "safety_failures": safety_failures,
        "entry_failure_count": entry_failure_count,
        "entry_failures": entry_failures,
        "artifact_stale_count": artifact_stale_count,
        "runtime_failure_count": runtime_failure_count,
        "idle_count": summary["idle_count"],
        "stale_heartbeat_count": summary["stale_heartbeat_count"],
        "tradable_entry_ready_count": summary["tradable_entry_ready_count"],
        "close_only_allowed_count": summary["close_only_allowed_count"],
        "strategy_surface_summary": strategy_surface_summary,
        "system_health": report.get("system_health") or {},
        "runtime_reconciliation": report.get("runtime_reconciliation") or {},
        "position_safety": report.get("position_safety") or {},
        "recovery_incidents": report.get("recovery_incidents") or {},
        "broker_truth_snapshot": report.get("broker_truth_snapshot") or {},
        "fill_attribution": report.get("fill_attribution") or {},
        "ledger_integrity": report.get("position_ledger_integrity") or {},
        "position_ledger_integrity": report.get("position_ledger_integrity") or {},
        "manual_intervention_locks": report.get("manual_intervention_locks") or {},
        "platform_eod": report.get("platform_eod") or {},
        "session_end": report.get("session_end") or {},
        "ib_restart_drill": report.get("ib_restart_drill") or {},
        "ib_gateway_operator_proof": report.get("ib_gateway_operator_proof") or {},
        "data_availability": report.get("data_availability") or {},
        "strategy_tradability": strategy_tradability,
        "strategy_manifest_gate": report.get("strategy_manifest_gate") or {},
        "contract_selection": report.get("contract_selection") or {},
        "option_trigger_ledger": report.get("option_trigger_ledger") or {},
        "qqq_liveness": report.get("qqq_liveness") or {},
        "run_system": report.get("run_system") or {},
        "telegram": report.get("telegram") or {},
        "disk_space": report.get("disk_space") or {},
        "market_calendar": report.get("market_calendar") or {},
        "runtime_version": runtime_version,
        "strategy_surface": report.get("strategy_surface") or report.get("live_strategy_surface") or {},
    }


def _write_production_state(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    state = _build_production_state(report)
    state_dir = root / "runtime/readiness/production_state"
    latest_path = state_dir / "production_state_latest.json"
    _write_json(latest_path, state)
    legacy_path = root / "runtime/readiness/production_state_latest.json"
    legacy_state = dict(state)
    legacy_state["deprecated_alias"] = True
    legacy_state["canonical_path"] = str(latest_path)
    _write_json(legacy_path, legacy_state)
    state["artifact_path"] = str(latest_path)
    state["legacy_artifact_path"] = str(legacy_path)
    return state


def build_runtime_operations_watchdog_report(
    *,
    repo_root: str | Path | None = None,
    day: str | None = None,
    now_et: datetime | None = None,
    qqq_stale_sec: float = 180.0,
    qqq_runtime_required_after: str = "09:35:00",
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    load_dotenv(root / ".env")
    current_et = now_et.astimezone(ET) if now_et is not None else datetime.now(ET)
    runtime_version = _runtime_version_status(root)
    ib_gateway_operator_proof = _ib_gateway_operator_proof_status(root=root, now_et=current_et)
    day_id = str(day or current_et.strftime("%Y%m%d"))
    target_date = datetime.strptime(day_id, "%Y%m%d").date()
    trading_day = bool(is_trading_day(target_date))
    config = _load_config(root / "config.yml")
    telegram_status = _telegram_status(config)
    disk_space = _disk_space_status(root=root, config=config)

    if not trading_day:
        return {
            "generated_at": current_et.isoformat(timespec="seconds"),
            "day": day_id,
            "repo_root": str(root),
            "market_calendar": {
                "trading_day": False,
                "status": "non_trading_day",
            },
            "config_loaded": bool(config),
            "telegram": telegram_status,
            "disk_space": disk_space,
            "runtime_version": runtime_version,
            "ib_gateway_operator_proof": ib_gateway_operator_proof,
            "run_system": {
                "lock_path": str(root / "runtime/locks/run_system.lock"),
                "lock_pid": None,
                "lock_pid_alive": False,
                "lock_matches_process": False,
                "stale_lock": False,
                "process_pids": [],
                "alive": False,
            },
            "opening_artifacts": {
                "checked_phases": [],
                "present_phases": [],
                "missing_phases": [],
                "invalid_phases": [],
                "artifacts": [],
            },
            "qqq_liveness": {},
            "qqq_observability": {},
            "qqq_diag": {},
            "qqq_runtime_audit": {
                "row_count": 0,
                "last_ts": None,
                "audit_dir": str(root / "runtime/audit" / f"{day_id[:4]}-{day_id[4:6]}-{day_id[6:8]}"),
            },
            "failures": [],
            "failure_count": 0,
            "recommendation": "PASS",
        }

    run_system_status = _run_system_status(root)

    audit_dir = root / "runtime/audit" / f"{day_id[:4]}-{day_id[4:6]}-{day_id[6:8]}"
    qqq_stock_strategy_ids = _qqq_stock_strategy_ids(config)
    qqq_primary_strategy_id = qqq_stock_strategy_ids[0] if qqq_stock_strategy_ids else "qqq_intraday_001"
    if qqq_primary_strategy_id == "qqq_intraday_001":
        obs_path = root / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json"
        obs = _read_json(obs_path)
        obs_ts = _parse_iso(obs.get("ts_now_et"))
        obs_age = _age_seconds(obs_ts, current_et)

        diag_path = root / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl"
        diag_last = _read_last_jsonl_object(diag_path)
        diag_ts = _parse_iso(diag_last.get("ts_now_et") or diag_last.get("ts"))
        diag_age = _age_seconds(diag_ts, current_et)
    else:
        obs, obs_age, diag_last, diag_age, obs_path, diag_path = _qqq_plugin_heartbeat_liveness_inputs(
            root=root,
            strategy_id=qqq_primary_strategy_id,
            now_et=current_et,
        )
        diag_ts = _parse_iso(diag_last.get("ts_now_et") or diag_last.get("ts"))
    qqq_audit = _strategy_audit_summary(
        audit_dir,
        (*qqq_stock_strategy_ids, "qqq_intraday_option_v1_001"),
    )
    opening_artifacts = _opening_artifact_status(root=root, day_id=day_id, now_et=current_et)
    platform_eod = _platform_eod_status(root=root, day_id=day_id, now_et=current_et)
    session_end = _session_end_status(root=root, config=config, now_et=current_et)
    market_session = time(9, 25) <= current_et.timetz().replace(tzinfo=None) <= time(16, 5)
    runtime_required_after_time = time.fromisoformat(qqq_runtime_required_after)
    qqq_liveness = _qqq_liveness_summary(
        obs=obs,
        obs_age=obs_age,
        diag_last=diag_last,
        diag_age=diag_age,
        qqq_audit=qqq_audit,
        stale_sec=qqq_stale_sec,
    )
    try:
        data_availability = configure_data_availability_service(
            durable_state_path=root / "runtime/state/data_availability_gates.json"
        ).snapshot(now=current_et)
    except Exception as exc:
        data_availability = {"ok": False, "active_gate_count": 0, "gates": [], "error": str(exc)}
    strategy_heartbeats = read_strategy_tradability_heartbeats(
        repo_root=root,
        now=current_et,
        max_age_sec=float(os.getenv("PRODUCTION_STRATEGY_TRADABILITY_HEARTBEAT_TTL_SEC", "90")),
    )
    data_availability = _reconcile_strategy_tradability_data_gates(
        data_availability,
        strategy_heartbeats=strategy_heartbeats,
    )
    strategy_tradability = build_strategy_tradability_snapshot(
        config=config,
        generated_at=current_et,
        data_availability=data_availability,
        qqq_liveness=qqq_liveness
        if market_session and current_et.timetz().replace(tzinfo=None) >= runtime_required_after_time
        else {},
        require_fresh_heartbeat=bool(os.getenv("PRODUCTION_REQUIRE_STRATEGY_TRADABILITY_HEARTBEAT", "1").strip().lower() not in {"0", "false", "no"}),
        strategy_heartbeats=strategy_heartbeats,
    )
    strategy_manifest_gate = _strategy_manifest_gate_status(root=root, config=config, generated_at=current_et)
    runtime_reconciliation = build_runtime_reconciliation_health(
        audit_root=root / "runtime/audit",
        day=day_id,
        now_et=current_et,
    )
    system_health = latest_system_health_transition(audit_dir=audit_dir, now_et=current_et)
    broker_truth_snapshot = _postgres_broker_truth_snapshot_status(
        config=config,
        repo_root=root,
        generated_at=current_et,
        runtime_version=runtime_version,
    )
    position_safety = _position_safety_status(root=root, now_et=current_et)
    fill_attribution = _postgres_fill_attribution_status(
        config=config,
        repo_root=root,
        generated_at=current_et,
        runtime_version=runtime_version,
    )
    position_ledger_integrity = _postgres_ledger_integrity_status(
        config=config,
        repo_root=root,
        generated_at=current_et,
    )
    recovery_incidents = _recovery_incidents_status(root=root, now_et=current_et)
    db_manual_locks = _annotate_manual_locks_with_recovery_incident_context(
        _postgres_manual_lock_status(config=config),
        recovery_incidents=recovery_incidents,
    )
    platform_eod = _reconcile_platform_eod_with_current_flat_truth(
        status=platform_eod,
        position_safety=position_safety,
        recovery_incidents=recovery_incidents,
        manual_locks=db_manual_locks,
        now_et=current_et,
    )
    ib_restart_drill = _ib_restart_drill_status(
        root=root,
        day_id=day_id,
        audit_dir=audit_dir,
        process_start_time=str(runtime_version.get("process_start_time") or ""),
    )
    required_option_strategy_ids = _option_contract_required_strategy_ids(config)
    contract_selection = _contract_selection_status(
        root=root,
        now_et=current_et,
        required_option_strategy_ids=required_option_strategy_ids,
    )
    option_trigger_ledger = _option_trigger_ledger_status(
        root=root,
        config=config,
        generated_at=current_et,
        required_strategy_ids=required_option_strategy_ids,
        runtime_version=runtime_version,
    )

    failures: list[dict[str, Any]] = []
    failures.extend(_disk_space_check(disk_space))
    if market_session:
        if not bool(telegram_status.get("available")):
            failures.append(
                {
                    "issue_key": "telegram_notifier_unavailable",
                    "severity": "ERROR",
                    "service": "TELEGRAM",
                    "error_type": "telegram_notifier_unavailable",
                    "detail": "missing_bot_token_or_chat_id_or_disabled",
                }
            )
        if not bool(run_system_status.get("alive")):
            failures.append(
                {
                    "issue_key": "run_system_down",
                    "severity": "ERROR",
                    "service": "RUN_SYSTEM",
                    "error_type": "run_system_down",
                    "detail": "process_missing",
                }
            )
        if obs_age is None or obs_age > qqq_stale_sec:
            failures.append(
                {
                    "issue_key": "qqq_strategy_stale:observability",
                    "severity": "ERROR",
                    "service": "QQQ_STRATEGY",
                    "error_type": "qqq_strategy_stale",
                    "detail": "observability",
                    "age_sec": obs_age,
                }
            )
        if diag_age is None or diag_age > qqq_stale_sec:
            failures.append(
                {
                    "issue_key": "qqq_strategy_stale:diag",
                    "severity": "ERROR",
                    "service": "QQQ_STRATEGY",
                    "error_type": "qqq_strategy_stale",
                    "detail": "diag",
                    "age_sec": diag_age,
                }
            )
        if current_et.timetz().replace(tzinfo=None) >= runtime_required_after_time and int(qqq_audit.get("row_count") or 0) == 0:
            failures.append(
                {
                    "issue_key": "qqq_runtime_audit_missing",
                    "severity": "ERROR",
                    "service": "QQQ_STRATEGY",
                    "error_type": "qqq_runtime_audit_missing",
                    "detail": "today_runtime_rows_missing",
                }
            )
        for blocker in list(runtime_reconciliation.get("blockers") or []):
            failures.append(
                {
                    "issue_key": _issue_key("runtime_reconciliation", blocker),
                    "severity": "ERROR",
                    "service": "RUNTIME_RECONCILIATION",
                    "error_type": "runtime_reconciliation_unhealthy",
                    "detail": blocker,
                }
            )

    failures.extend(_data_availability_check(data_availability))
    failures.extend(_strategy_tradability_check(strategy_tradability))
    failures.extend(_strategy_manifest_gate_check(strategy_manifest_gate))
    failures.extend(_system_health_check(system_health))
    failures.extend(_opening_artifact_check(root=root, day_id=day_id, now_et=current_et))
    failures.extend(_platform_eod_failures(platform_eod))
    failures.extend(_session_end_check(session_end))
    failures.extend(_postgres_manual_lock_check(db_manual_locks))
    failures.extend(_postgres_broker_truth_snapshot_check(broker_truth_snapshot))
    failures.extend(_postgres_fill_attribution_check(fill_attribution))
    failures.extend(_postgres_ledger_integrity_check(position_ledger_integrity))
    failures.extend(_position_safety_check(position_safety))
    failures.extend(_recovery_incidents_check(recovery_incidents))
    failures.extend(_ib_restart_drill_check(ib_restart_drill))
    failures.extend(_contract_selection_check(contract_selection))
    failures.extend(_option_trigger_ledger_check(option_trigger_ledger))
    failures.extend(_runtime_version_check(runtime_version))

    return {
        "generated_at": current_et.isoformat(timespec="seconds"),
        "day": day_id,
        "repo_root": str(root),
        "market_calendar": {
            "trading_day": True,
            "status": "trading_day",
        },
        "config_loaded": bool(config),
        "telegram": telegram_status,
        "disk_space": disk_space,
        "runtime_version": runtime_version,
        "ib_gateway_operator_proof": ib_gateway_operator_proof,
        "run_system": run_system_status,
        "opening_artifacts": opening_artifacts,
        "platform_eod": platform_eod,
        "session_end": session_end,
        "manual_intervention_locks": db_manual_locks,
        "broker_truth_snapshot": broker_truth_snapshot,
        "fill_attribution": fill_attribution,
        "position_ledger_integrity": position_ledger_integrity,
        "position_safety": position_safety,
        "recovery_incidents": recovery_incidents,
        "ib_restart_drill": ib_restart_drill,
        "contract_selection": contract_selection,
        "option_trigger_ledger": option_trigger_ledger,
        "data_availability": data_availability,
        "strategy_tradability": strategy_tradability,
        "strategy_manifest_gate": strategy_manifest_gate,
        "qqq_liveness": qqq_liveness,
        "qqq_observability": {
            "path": str(obs_path),
            "ts_now_et": obs.get("ts_now_et"),
            "age_sec": obs_age,
        },
        "qqq_diag": {
            "path": str(diag_path),
            "last_ts": diag_ts.isoformat() if diag_ts else None,
            "age_sec": diag_age,
            "last_event": str(diag_last.get("event") or "").strip(),
        },
        "qqq_runtime_audit": qqq_audit,
        "runtime_reconciliation": runtime_reconciliation,
        "system_health": system_health,
        "failures": failures,
        "failure_count": len(failures),
        "recommendation": "HOLD" if failures else "PASS",
    }


def _format_issue_message(issue: dict[str, Any], *, recovered: bool) -> str:
    status = "恢复" if recovered else "故障"
    return f"{issue.get('service')} {status}: {issue.get('error_type')} ({issue.get('detail')})"


async def _dispatch_transitions(
    *,
    report: dict[str, Any],
    state_path: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    previous = _read_json(state_path)
    previous_issues = dict(previous.get("issues") or {})
    current_failures = {str(item.get("issue_key") or "").strip(): item for item in report.get("failures") or [] if str(item.get("issue_key") or "").strip()}

    next_issues: dict[str, Any] = {}
    alerts_sent: list[dict[str, Any]] = []
    manager = build_notification_manager(config)

    async def _send(issue: dict[str, Any], *, status: str, recovered: bool) -> None:
        if manager is None:
            return
        event = NotificationEvent(
            event_type="system_alert",
            status=status,
            strategy_id="SYSTEM",
            symbol=str(issue.get("service") or "WATCHDOG"),
            side="N/A",
            qty=0.0,
            order_type="SYSTEM",
            decision_id="",
            ts=datetime.now(timezone.utc),
            reason=str(issue.get("issue_key") or ""),
            metadata={
                "service": str(issue.get("service") or "WATCHDOG"),
                "error_type": str(issue.get("error_type") or "watchdog_issue"),
                "detail": str(issue.get("detail") or ""),
                "issue_key": str(issue.get("issue_key") or ""),
                "recovered": recovered,
            },
        )
        ok = await manager.send_now(event)
        alerts_sent.append({"issue_key": issue.get("issue_key"), "status": status, "sent": bool(ok)})

    for issue_key, issue in current_failures.items():
        previous_issue = dict(previous_issues.get(issue_key) or {})
        alert_open_sent = bool(previous_issue.get("alert_open_sent"))
        send_open = previous_issue.get("status") != "open" or not alert_open_sent
        open_sent_now = alert_open_sent
        if send_open:
            await _send(issue, status="ERROR", recovered=False)
            open_sent_now = bool(alerts_sent and alerts_sent[-1].get("issue_key") == issue_key and alerts_sent[-1].get("status") == "ERROR" and alerts_sent[-1].get("sent"))
        next_issues[issue_key] = {
            **issue,
            "status": "open",
            "opened_at": previous_issue.get("opened_at") or report.get("generated_at"),
            "last_seen_at": report.get("generated_at"),
            "alert_open_sent": open_sent_now,
        }

    for issue_key, previous_issue in previous_issues.items():
        if issue_key in current_failures:
            continue
        if str(previous_issue.get("status") or "").strip() == "open":
            recovered_issue = dict(previous_issue)
            recovered_issue["issue_key"] = issue_key
            await _send(recovered_issue, status="INFO", recovered=True)
        next_issues[issue_key] = {
            **dict(previous_issue),
            "status": "closed",
            "resolved_at": report.get("generated_at"),
        }

    if manager is not None:
        await manager.shutdown()

    next_state = {
        "generated_at": report.get("generated_at"),
        "day": report.get("day"),
        "issues": next_issues,
        "alerts_sent": alerts_sent,
    }
    _write_json(state_path, next_state)
    return next_state


def run_runtime_operations_watchdog(
    *,
    repo_root: str | Path | None = None,
    day: str | None = None,
    output_path: str | Path | None = None,
    state_path: str | Path | None = None,
    now_et: datetime | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    report = build_runtime_operations_watchdog_report(repo_root=root, day=day, now_et=now_et)
    config = _load_config(root / "config.yml")
    final_output = Path(output_path) if output_path is not None else root / "runtime/state/runtime_operations_watchdog.json"
    if not final_output.is_absolute():
        final_output = root / final_output
    final_state = Path(state_path) if state_path is not None else root / "runtime/state/runtime_operations_watchdog_alert_state.json"
    if not final_state.is_absolute():
        final_state = root / final_state
    if not bool(((report.get("market_calendar") or {}).get("trading_day"))):
        transition_state = {
            "generated_at": report.get("generated_at"),
            "day": report.get("day"),
            "issues": {},
            "alerts_sent": [],
            "status": "non_trading_day_no_action",
        }
        _write_json(final_state, transition_state)
    else:
        transition_state = asyncio.run(_dispatch_transitions(report=report, state_path=final_state, config=config))
    production_state = _write_production_state(root, report)
    payload = {
        **report,
        "production_state": production_state,
        "transition_state": transition_state,
        "state_path": str(final_state),
    }
    production_paths = {
        root / "runtime/readiness/production_state/production_state_latest.json",
        root / "runtime/readiness/production_state_latest.json",
    }
    if final_output.resolve() in {path.resolve() for path in production_paths}:
        output_payload = production_state
        if final_output.resolve() == (root / "runtime/readiness/production_state_latest.json").resolve():
            output_payload = dict(production_state)
            output_payload["deprecated_alias"] = True
            output_payload["canonical_path"] = str(root / "runtime/readiness/production_state/production_state_latest.json")
        _write_json(final_output, output_payload)
    else:
        _write_json(final_output, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run runtime operations watchdog and emit Telegram alerts on transitions")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--day", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--state", default="")
    args = parser.parse_args(argv)
    payload = run_runtime_operations_watchdog(
        repo_root=args.repo_root or None,
        day=args.day or None,
        output_path=args.output or None,
        state_path=args.state or None,
    )
    print(
        json.dumps(
            {
                "day": payload["day"],
                "recommendation": payload["recommendation"],
                "failure_count": payload["failure_count"],
                "alerts_sent": len(list((payload.get("transition_state") or {}).get("alerts_sent") or [])),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if payload["recommendation"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
