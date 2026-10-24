#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any

import yaml
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tool.ops.check_qqq_shadow_trace_coverage import build_trace_coverage_report
from tool.ops.qqq_live_strategy_aliases import matched_qqq_live_strategy_ids
from tool.ops.system_health_artifact import latest_system_health_transition
from libs.market_calendar import is_trading_day

ET = ZoneInfo("America/New_York")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


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


def _file_age_seconds(path: Path, now: datetime) -> float | None:
    if not path.exists():
        return None
    return max(0.0, now.timestamp() - path.stat().st_mtime)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _find_run_system_pids(repo_root: Path) -> list[int]:
    matches: list[int] = []
    for pid_dir in Path("/proc").iterdir() if Path("/proc").exists() else []:
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "run_system.py" in cmdline and "python" in cmdline:
            matches.append(int(pid_dir.name))
    if matches:
        return sorted(matches)
    # macOS fallback
    try:
        import subprocess

        proc = subprocess.run(
            ["pgrep", "-f", "[Pp]ython.*run_system.py"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        for line in proc.stdout.splitlines():
            text = line.strip()
            if text.isdigit():
                matches.append(int(text))
    except Exception:
        pass
    return sorted(set(matches))


def _state_requires_heartbeat(payload: dict[str, Any]) -> bool:
    semantics = str(payload.get("updated_at_semantics") or "").strip().lower()
    return semantics not in {"event_driven_state_snapshot"}


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


def _production_state_status(*, root: Path, now: datetime, max_age_sec: float = 90.0) -> dict[str, Any]:
    path = root / "runtime/readiness/production_state/production_state_latest.json"
    payload = _read_json(path)
    generated_at = _parse_iso(payload.get("generated_at"))
    age_sec = _age_seconds(generated_at, now)
    try:
        max_age_value = float(max_age_sec)
    except Exception:
        max_age_value = 90.0
    stale = bool(payload) and max_age_value > 0 and (age_sec is None or age_sec > max_age_value)
    failures = list(payload.get("failures") or []) if isinstance(payload.get("failures"), list) else []
    go_no_go = str(payload.get("go_no_go") or "").strip().lower()
    return {
        "path": str(path),
        "present": bool(payload),
        "generated_at": payload.get("generated_at"),
        "age_sec": age_sec,
        "max_age_sec": max_age_value,
        "stale": stale,
        "go_no_go": go_no_go or None,
        "recommendation": payload.get("recommendation"),
        "failure_count": int(payload.get("failure_count") or len(failures) or 0) if payload else 0,
        "failures": failures,
        "system_health": payload.get("system_health") or {},
        "position_safety": payload.get("position_safety") or {},
        "platform_eod": payload.get("platform_eod") or {},
        "raw": payload,
    }


def _strategy_runtime_audit_summary(audit_dir: Path, strategy_ids: str | list[str] | tuple[str, ...]) -> dict[str, Any]:
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


def _load_config(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


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
    return ids or ["qqq_intraday_001"]


def _plugin_heartbeat_opening_inputs(
    *,
    root: Path,
    strategy_id: str,
    now: datetime,
) -> tuple[dict[str, Any], float | None, float | None, dict[str, Any], float | None, float | None, Path, dict[str, Any], float | None, float | None, bool, Path, Path]:
    heartbeat_path = root / "runtime/readiness/strategy_tradability/heartbeats" / f"{strategy_id}.json"
    heartbeat = _read_json(heartbeat_path)
    details = heartbeat.get("details") if isinstance(heartbeat.get("details"), dict) else {}
    generated_at = _parse_iso(heartbeat.get("generated_at"))
    heartbeat_age = _age_seconds(generated_at, now)
    file_age = _file_age_seconds(heartbeat_path, now)
    data_age = details.get("data_age_sec")
    obs = {
        "ready": bool(heartbeat.get("tradable")) or bool(details.get("readiness_ready")),
        "missing_required": [],
        "ts_now_et": heartbeat.get("generated_at"),
        "required_values": {
            "QQQ": {"last": True, "vwap": True, "age_sec": data_age},
            "TQQQ": {"last": True, "age_sec": data_age},
            "SQQQ": {"last": True, "age_sec": data_age},
        },
    }
    state = {
        "updated_at": heartbeat.get("generated_at"),
        "updated_at_semantics": "event_driven_state_snapshot",
    }
    diag_last = {
        "ts": heartbeat.get("generated_at"),
        "event": "strategy_tradability_heartbeat",
    }
    return (
        obs,
        heartbeat_age,
        file_age,
        state,
        heartbeat_age,
        file_age,
        heartbeat_path,
        diag_last,
        heartbeat_age,
        file_age,
        False,
        heartbeat_path,
        heartbeat_path,
    )


def build_opening_health_report(
    *,
    repo_root: str | Path | None = None,
    phase: str = "open",
    day: str | None = None,
    heartbeat_stale_sec: float = 120.0,
    data_stale_sec: float = 90.0,
    require_ready_after_sec: float = 120.0,
) -> dict[str, Any]:
    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    now = datetime.now(ET)
    day_id = str(day or now.strftime("%Y%m%d"))
    phase_norm = str(phase or "open").strip().lower()
    target_date = datetime.strptime(day_id, "%Y%m%d").date()
    trading_day = bool(is_trading_day(target_date))
    config = _load_config(root / "config.yml")

    if not trading_day:
        audit_dir = root / "runtime/audit" / f"{day_id[:4]}-{day_id[4:6]}-{day_id[6:8]}"
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "phase": phase_norm,
            "day": day_id,
            "market_calendar": {
                "trading_day": False,
                "status": "non_trading_day",
            },
            "recommendation": "PASS",
            "blockers": [],
            "remediation_actions": [],
            "run_system": {
                "lock_path": str(root / "runtime/locks/run_system.lock"),
                "lock_pid": None,
                "lock_pid_alive": False,
                "process_pids": [],
                "process_alive": False,
            },
            "qqq_observability": {},
            "qqq_state": {},
            "qqq_diag": {},
            "market_data": {},
            "qqq_runtime_audit": {
                "row_count": 0,
                "last_ts": None,
                "audit_dir": str(audit_dir),
            },
            "trace_coverage": {},
        }

    lock_path = root / "runtime/locks/run_system.lock"
    lock_pid_text = lock_path.read_text(encoding="utf-8").strip() if lock_path.exists() else ""
    lock_pid = int(lock_pid_text) if lock_pid_text.isdigit() else None
    lock_pid_alive = bool(lock_pid and _pid_alive(lock_pid))
    run_system_pids = _find_run_system_pids(root)

    audit_dir = root / "runtime/audit" / f"{day_id[:4]}-{day_id[4:6]}-{day_id[6:8]}"
    qqq_stock_strategy_ids = _qqq_stock_strategy_ids(config)
    qqq_primary_strategy_id = qqq_stock_strategy_ids[0] if qqq_stock_strategy_ids else "qqq_intraday_001"
    if qqq_primary_strategy_id == "qqq_intraday_001":
        obs_path = root / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json"
        obs = _read_json(obs_path)
        obs_ts = _parse_iso(obs.get("ts_now_et"))
        obs_age = _age_seconds(obs_ts, now)
        obs_file_age = _file_age_seconds(obs_path, now)

        state_path = root / "runtime/state/qqq_intraday_qqq_intraday_001.json"
        state = _read_json(state_path)
        state_ts = _parse_iso(state.get("updated_at"))
        state_age = _age_seconds(state_ts, now)
        state_file_age = _file_age_seconds(state_path, now)
        state_requires_heartbeat = _state_requires_heartbeat(state)

        diag_path = root / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl"
        diag_last = _read_last_jsonl_object(diag_path)
        diag_ts = _parse_iso(diag_last.get("ts_now_et") or diag_last.get("ts"))
        diag_age = _age_seconds(diag_ts, now)
        diag_file_age = _file_age_seconds(diag_path, now)
    else:
        (
            obs,
            obs_age,
            obs_file_age,
            state,
            state_age,
            state_file_age,
            state_path,
            diag_last,
            diag_age,
            diag_file_age,
            state_requires_heartbeat,
            obs_path,
            diag_path,
        ) = _plugin_heartbeat_opening_inputs(root=root, strategy_id=qqq_primary_strategy_id, now=now)
        diag_ts = _parse_iso(diag_last.get("ts_now_et") or diag_last.get("ts"))
    system_health = latest_system_health_transition(audit_dir=audit_dir, now_et=now)
    production_state = _production_state_status(root=root, now=now)
    qqq_runtime_audit = _strategy_runtime_audit_summary(
        audit_dir,
        (*qqq_stock_strategy_ids, "qqq_intraday_option_v1_001"),
    )

    required_values = dict(obs.get("required_values") or {})
    qqq = dict(required_values.get("QQQ") or {})
    tqqq = dict(required_values.get("TQQQ") or {})
    sqqq = dict(required_values.get("SQQQ") or {})

    market_data = {
        "qqq_last_present": qqq.get("last") is not None,
        "qqq_vwap_present": bool(qqq.get("vwap")),
        "tqqq_last_present": tqqq.get("last") is not None,
        "sqqq_last_present": sqqq.get("last") is not None,
        "qqq_age_sec": qqq.get("age_sec"),
        "tqqq_age_sec": tqqq.get("age_sec"),
        "sqqq_age_sec": sqqq.get("age_sec"),
    }

    trace_coverage = build_trace_coverage_report(day=day_id, trace_root=root / "reports/qqq_signal_snapshots")
    seconds_since_open = max(
        0.0,
        (
            now
            - datetime.combine(now.date(), time(hour=9, minute=30), tzinfo=ET)
        ).total_seconds(),
    )

    blockers: list[str] = []
    remediation_actions: list[str] = []
    if not bool(system_health.get("present")):
        blockers.append("system_health_transition_missing")
    elif bool(system_health.get("unhealthy")):
        if bool(system_health.get("stale")):
            blockers.append("system_health:stale")
        else:
            health_state = str(system_health.get("state") or "unknown")
            blockers.append(f"system_health:{health_state}")
        for item in system_health.get("recovery_readiness_blockers") or []:
            blockers.append(f"recovery_readiness:{item}")
    if phase_norm in {"open", "acceptance"} and seconds_since_open >= require_ready_after_sec:
        if not bool(production_state.get("present")):
            blockers.append("production_state_missing")
        elif bool(production_state.get("stale")):
            blockers.append("production_state_stale")
        elif str(production_state.get("go_no_go") or "").lower() != "go":
            blockers.append("production_state_no_go")
        if int(production_state.get("failure_count") or 0) > 0:
            blockers.append("production_state_failures")
    if not run_system_pids and not lock_pid_alive:
        blockers.append("run_system_down")
        remediation_actions.append("start_run_system")
    if phase_norm in {"open", "acceptance"}:
        if obs_age is None or obs_age > heartbeat_stale_sec:
            blockers.append("qqq_observability_stale")
            remediation_actions.append("restart_run_system")
        if diag_age is None or diag_age > heartbeat_stale_sec:
            blockers.append("qqq_diag_stale")
            remediation_actions.append("restart_run_system")
        if state_requires_heartbeat and (state_age is None or state_age > heartbeat_stale_sec):
            blockers.append("qqq_state_stale")
            remediation_actions.append("restart_run_system")
    if phase_norm in {"open", "acceptance"} and seconds_since_open >= require_ready_after_sec:
        if not bool(obs.get("ready")):
            blockers.append("qqq_observability_not_ready")
        if market_data["qqq_last_present"] is False or market_data["qqq_vwap_present"] is False:
            blockers.append("qqq_market_data_missing")
        for key in ("qqq_age_sec", "tqqq_age_sec", "sqqq_age_sec"):
            value = market_data.get(key)
            try:
                age_value = float(value)
            except Exception:
                continue
            if age_value > data_stale_sec:
                blockers.append(f"market_data_stale:{key}")
        if int(qqq_runtime_audit.get("row_count") or 0) == 0:
            blockers.append("qqq_runtime_audit_missing")
    if phase_norm == "acceptance":
        if trace_coverage.get("missing_strategy_ids"):
            blockers.extend(f"trace_coverage:{item}" for item in trace_coverage.get("missing_strategy_ids") or [])
        if not bool(trace_coverage.get("live_writer_present", trace_coverage.get("legacy_writer_present"))):
            blockers.append("legacy_trace_writer_missing")

    blockers = sorted(set(blockers))
    remediation_actions = sorted(set(remediation_actions))
    if not blockers:
        recommendation = "PASS"
    elif any(item in {"start_run_system", "restart_run_system"} for item in remediation_actions):
        recommendation = "AUTO_REMEDIATE"
    else:
        recommendation = "HOLD"

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "phase": phase_norm,
        "day": day_id,
        "market_calendar": {
            "trading_day": True,
            "status": "trading_day",
        },
        "recommendation": recommendation,
        "blockers": blockers,
        "remediation_actions": remediation_actions,
        "run_system": {
            "lock_path": str(lock_path),
            "lock_pid": lock_pid,
            "lock_pid_alive": lock_pid_alive,
            "process_pids": run_system_pids,
            "process_alive": bool(run_system_pids or lock_pid_alive),
        },
        "qqq_observability": {
            "path": str(obs_path),
            "exists": obs_path.exists(),
            "ready": bool(obs.get("ready")),
            "missing_required": list(obs.get("missing_required") or []),
            "ts_now_et": obs.get("ts_now_et"),
            "age_sec": obs_age,
            "file_age_sec": obs_file_age,
        },
        "qqq_state": {
            "path": str(state_path),
            "exists": state_path.exists(),
            "updated_at": state.get("updated_at"),
            "age_sec": state_age,
            "file_age_sec": state_file_age,
            "updated_at_semantics": state.get("updated_at_semantics"),
            "requires_heartbeat": state_requires_heartbeat,
        },
        "qqq_diag": {
            "path": str(diag_path),
            "exists": diag_path.exists(),
            "last_ts": diag_ts.isoformat() if diag_ts else None,
            "age_sec": diag_age,
            "file_age_sec": diag_file_age,
            "last_event": str(diag_last.get("event") or "").strip(),
        },
        "market_data": market_data,
        "system_health": system_health,
        "production_state": production_state,
        "qqq_runtime_audit": qqq_runtime_audit,
        "trace_coverage": trace_coverage,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build opening-time system + strategy health report")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--phase", default="open", choices=("preopen", "open", "acceptance"))
    parser.add_argument("--day", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--heartbeat-stale-sec", type=float, default=120.0)
    parser.add_argument("--data-stale-sec", type=float, default=90.0)
    parser.add_argument("--require-ready-after-sec", type=float, default=120.0)
    args = parser.parse_args(argv)
    payload = build_opening_health_report(
        repo_root=args.repo_root or None,
        phase=args.phase,
        day=args.day or None,
        heartbeat_stale_sec=args.heartbeat_stale_sec,
        data_stale_sec=args.data_stale_sec,
        require_ready_after_sec=args.require_ready_after_sec,
    )
    output = Path(args.output) if str(args.output or "").strip() else REPO_ROOT / "runtime/state/opening_day" / f"{payload['day']}_{payload['phase']}_health.json"
    if not output.is_absolute():
        output = REPO_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"day": payload["day"], "phase": payload["phase"], "recommendation": payload["recommendation"], "blockers": payload["blockers"]}, ensure_ascii=False, sort_keys=True))
    return 0 if payload["recommendation"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
