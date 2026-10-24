from __future__ import annotations

import json
import os
import hashlib
import subprocess
from datetime import datetime, time
from pathlib import Path
from typing import Any

from services.production.strategy_data_contract import (
    build_strategy_data_contracts,
    validate_strategy_production_manifests,
)


def _git_head(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _config_hash(root: Path) -> str:
    digest = hashlib.sha256()
    paths = [path for pattern in ("config.yml", "config.yaml", "configs/**/*.yml", "configs/**/*.yaml", "configs/**/*.json") for path in root.glob(pattern) if path.is_file()]
    for path in sorted(paths):
        try:
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        except Exception:
            digest.update(str(path).encode("utf-8"))
            digest.update(b"\0unreadable")
    return digest.hexdigest()


def _runtime_process_start_time(root: Path) -> str:
    try:
        pid_text = (root / "runtime/locks/run_system.lock").read_text(encoding="utf-8").strip().split()[0]
        pid = int(pid_text)
        output = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if not output:
            return ""
        parsed = datetime.strptime(output, "%a %b %d %H:%M:%S %Y").replace(tzinfo=generated_tz())
        return parsed.isoformat(timespec="seconds")
    except Exception:
        return ""


def generated_tz():
    return datetime.now().astimezone().tzinfo


def build_strategy_manifest_gate(
    *,
    config: dict[str, Any],
    generated_at: datetime,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    mandatory_ids = [
        str(contract.strategy_id or "").strip()
        for contract in build_strategy_data_contracts(config)
        if bool(getattr(contract, "production_blocking", True)) and str(contract.strategy_id or "").strip()
    ]
    validation = validate_strategy_production_manifests(config, mandatory_strategy_ids=mandatory_ids)
    mandatory_set = set(mandatory_ids)
    session_records, session_blockers = _session_policy_records(config=config, mandatory_ids=mandatory_set)
    option_records, option_blockers = _option_policy_records(config=config, mandatory_ids=mandatory_set)
    blockers = list(validation.blockers) + session_blockers + option_blockers
    root = Path(repo_root).resolve() if repo_root is not None else None
    status = {
        "schema_version": 1,
        "artifact_type": "strategy_manifest_gate",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        "process_start_time": os.getenv("PROCESS_START_TIME", "") or (_runtime_process_start_time(root) if root is not None else ""),
        "git_head": _git_head(root) if root is not None else "",
        "config_hash": _config_hash(root) if root is not None else "",
        "go_no_go": "go" if not blockers else "no_go",
        "ok": not blockers,
        "mandatory_strategy_ids": mandatory_ids,
        "manifest_validation": validation.to_dict(),
        "session_policy_records": session_records,
        "session_policy_count": len(session_records),
        "option_policy_records": option_records,
        "option_policy_count": len(option_records),
        "blockers": blockers,
        "clear_conditions": [] if not blockers else ["fix_strategy_production_manifest_or_session_policy"],
    }
    if repo_root is not None:
        write_strategy_manifest_gate_artifact(repo_root=repo_root, status=status)
    return status


def write_strategy_manifest_gate_artifact(*, repo_root: str | Path, status: dict[str, Any]) -> Path:
    path = Path(repo_root) / "runtime/readiness/strategy_manifest_gate/strategy_manifest_gate_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _session_policy_records(*, config: dict[str, Any], mandatory_ids: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    blockers: list[str] = []
    strategies = config.get("strategies") if isinstance(config, dict) else []
    if not isinstance(strategies, list):
        return records, blockers
    for spec in strategies:
        if not isinstance(spec, dict) or not bool(spec.get("enabled")):
            continue
        strategy_id = str(spec.get("id") or spec.get("strategy_id") or "").strip()
        if not strategy_id or strategy_id not in mandatory_ids:
            continue
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
        manifest = extras.get("production_manifest") if isinstance(extras.get("production_manifest"), dict) else {}
        eod = manifest.get("eod_contract") if isinstance(manifest.get("eod_contract"), dict) else {}
        session = manifest.get("session_policy") if isinstance(manifest.get("session_policy"), dict) else {}
        force_flatten = _first_value(
            session,
            extras,
            params,
            keys=("force_flatten_deadline", "force_flatten_time", "flatten_before_close_et"),
        )
        no_new_entry_after = _first_value(
            session,
            extras,
            params,
            keys=("no_new_entry_after", "entry_cutoff_time", "no_new_after_et", "a_latest_entry_time"),
        )
        entry_start = _first_value(session, extras, params, keys=("entry_start", "ab_time_start", "session_start"))
        entry_end = _first_value(session, extras, params, keys=("entry_end", "ab_time_end", "session_end"))
        allowed_overnight = _bool_value(
            _first_value(session, extras, params, keys=("allowed_overnight",)),
            default=not bool(eod.get("no_overnight")),
        )
        afterhours_allowed = _bool_value(
            _first_value(session, extras, params, keys=("afterhours_allowed",)),
            default=False,
        )
        record_blockers: list[str] = []
        if bool(eod.get("no_overnight")) and allowed_overnight:
            record_blockers.append("allowed_overnight_conflicts_with_no_overnight")
        if not _valid_hms(force_flatten):
            record_blockers.append("force_flatten_deadline_missing_or_invalid")
        if not _valid_hms(no_new_entry_after):
            record_blockers.append("entry_cutoff_missing_or_invalid")
        if entry_start and not _valid_hms(entry_start):
            record_blockers.append("entry_start_invalid")
        if entry_end and not _valid_hms(entry_end):
            record_blockers.append("entry_end_invalid")
        if _valid_hms(no_new_entry_after) and _valid_hms(force_flatten):
            cutoff = _parse_hms(no_new_entry_after)
            flatten = _parse_hms(force_flatten)
            if cutoff is not None and flatten is not None and cutoff > flatten:
                record_blockers.append("entry_cutoff_after_force_flatten")
        if afterhours_allowed and not session:
            record_blockers.append("afterhours_requires_explicit_session_policy")
        for blocker in record_blockers:
            blockers.append(f"{strategy_id}:{blocker}")
        records.append(
            {
                "strategy_id": strategy_id,
                "session_policy_source": "production_manifest.session_policy" if session else "legacy_extras",
                "force_flatten_deadline": str(force_flatten or ""),
                "entry_cutoff": str(no_new_entry_after or ""),
                "entry_start": str(entry_start or ""),
                "entry_end": str(entry_end or ""),
                "allowed_overnight": bool(allowed_overnight),
                "afterhours_allowed": bool(afterhours_allowed),
                "blockers": record_blockers,
            }
        )
    return records, blockers


def _first_value(*dicts: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for data in dicts:
        if not isinstance(data, dict):
            continue
        for key in keys:
            if key not in data:
                continue
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, str) and value == "":
                continue
            return value
    return None


def _option_policy_records(*, config: dict[str, Any], mandatory_ids: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    blockers: list[str] = []
    strategies = config.get("strategies") if isinstance(config, dict) else []
    if not isinstance(strategies, list):
        return records, blockers
    for spec in strategies:
        if not isinstance(spec, dict) or not bool(spec.get("enabled")):
            continue
        strategy_id = str(spec.get("id") or spec.get("strategy_id") or "").strip()
        if not strategy_id or strategy_id not in mandatory_ids:
            continue
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
        manifest = extras.get("production_manifest") if isinstance(extras.get("production_manifest"), dict) else {}
        option_contract = manifest.get("option_contract") if isinstance(manifest.get("option_contract"), dict) else {}
        if not _bool_value(option_contract.get("required"), default=False):
            continue
        underlying = str(_first_value(extras, params, spec, keys=("underlying", "symbol")) or "").strip().upper()
        risk_symbol = str(_first_value(extras, params, spec, keys=("risk_symbol",)) or "").strip().upper()
        trigger_sources = _string_list(_first_value(extras, params, spec, keys=("trigger_source_ids", "trigger_strategy_ids")))
        definition_id = str(_first_value(extras, params, spec, keys=("definition_id",)) or "").strip()
        replay_strategy_id = str(_first_value(extras, params, spec, keys=("replay_strategy_id",)) or "").strip()
        paper_smoke_only = _bool_value(_first_value(extras, params, spec, keys=("paper_smoke_only",)), default=False)
        contracts = _int_value(_first_value(extras, params, spec, keys=("contracts", "max_contracts")), default=0)
        spread = _float_value(_first_value(extras, params, spec, keys=("max_spread_pct", "spread_max_pct")), default=0.0)
        dte = _first_value(extras, params, spec, keys=("dte_preferred", "dte", "dte_min"))
        record_blockers: list[str] = []
        if not underlying:
            record_blockers.append("option_underlying_missing")
        if not risk_symbol.startswith("OPT:"):
            record_blockers.append("option_risk_symbol_missing_or_invalid")
        elif underlying and not risk_symbol.startswith(f"OPT:{underlying}:"):
            record_blockers.append("option_risk_symbol_underlying_mismatch")
        if contracts <= 0:
            record_blockers.append("option_contracts_missing_or_invalid")
        if not (trigger_sources or definition_id or replay_strategy_id or paper_smoke_only):
            record_blockers.append("option_trigger_lineage_missing")
        if spread <= 0.0 and not paper_smoke_only:
            record_blockers.append("option_max_spread_missing_or_invalid")
        if dte is None and "DTE" not in risk_symbol and not paper_smoke_only:
            record_blockers.append("option_expiry_policy_missing")
        for blocker in record_blockers:
            blockers.append(f"{strategy_id}:{blocker}")
        records.append(
            {
                "strategy_id": strategy_id,
                "underlying": underlying,
                "risk_symbol": risk_symbol,
                "trigger_sources": trigger_sources,
                "definition_id": definition_id,
                "replay_strategy_id": replay_strategy_id,
                "paper_smoke_only": bool(paper_smoke_only),
                "contracts": contracts,
                "max_spread_pct": spread,
                "dte_policy": str(dte if dte is not None else ""),
                "blockers": record_blockers,
            }
        )
    return records, blockers


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple | set):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _bool_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _float_value(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_hms(value: Any) -> time | None:
    try:
        parts = [int(part) for part in str(value or "").strip().strip("'\"").split(":")]
        if len(parts) == 2:
            return time(parts[0], parts[1])
        if len(parts) == 3:
            return time(parts[0], parts[1], parts[2])
    except Exception:
        return None
    return None


def _valid_hms(value: Any) -> bool:
    return _parse_hms(value) is not None
