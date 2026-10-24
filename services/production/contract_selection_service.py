from __future__ import annotations

import json
import os
import hashlib
import subprocess
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from libs.options.internal_symbol import parse_internal_opt_symbol


ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PlatformContractSelectionResult:
    selection: Any | None
    status: str
    reason: str
    payload: dict[str, Any]
    identity_valid: bool
    missing_identity_fields: list[str]
    legacy_plugin_selection: bool = False


def select_option_contract_for_entry(
    *,
    runtime: Any,
    entry_plan: Any,
    plugin: Any,
    strategy_id: str,
    symbol: str,
    kernel_mode: str,
) -> PlatformContractSelectionResult:
    """Platform-owned contract-selection boundary for option entry plans.

    Plugins may provide preferences through ``build_contract_preferences_pure``.
    Until every plugin is migrated, the service can consume the legacy
    ``select_contract_pure`` hook, but validation and verdict payload ownership
    live here instead of in the kernel.
    """
    preferences = None
    preference_fn = getattr(plugin, "build_contract_preferences_pure", None)
    if callable(preference_fn):
        preferences = preference_fn(runtime, entry_plan)

    selection = None
    legacy = False
    platform_selector = getattr(plugin, "select_contract_from_preferences_pure", None)
    if callable(platform_selector):
        selection = platform_selector(runtime, entry_plan, preferences)
    else:
        legacy_selector = getattr(plugin, "select_contract_pure", None)
        if callable(legacy_selector):
            legacy = True
            if _legacy_plugin_selection_disabled():
                selection = None
            else:
                selection = legacy_selector(runtime, entry_plan)

    selection_payload = _json_safe(selection)
    identity = _selected_contract_identity({"payload": {"selection": selection_payload}})
    missing = _missing_contract_identity_fields(identity) if selection is not None else []
    identity_valid = _selected_contract_identity_valid(identity) if selection is not None else False
    strict_identity = _complete_contract_identity_required()
    effective_selection = None if (selection is not None and strict_identity and not identity_valid) else selection
    status = "selected" if effective_selection is not None else "rejected"
    if selection is None and legacy and _legacy_plugin_selection_disabled():
        reason = "legacy_plugin_contract_selection_disabled"
    elif selection is None:
        reason = "no_contract_selection"
    elif not identity_valid:
        reason = "contract_selected_identity_incomplete"
    else:
        reason = "contract_selected"
    payload = {
        "kernel_mode": str(kernel_mode or ""),
        "platform_service": "select_option_contract_for_entry",
        "plugin": str(getattr(plugin, "component_name", "option_plugin")),
        "legacy_plugin_selection": bool(legacy),
        "preferences": _json_safe(preferences),
        "entry_plan": _json_safe(entry_plan),
        "selection": selection_payload,
        "selected_contract": identity,
        "selected_contract_identity_valid": bool(identity_valid),
        "missing_contract_identity_fields": missing,
        "strategy_id": str(strategy_id or ""),
        "symbol": str(symbol or "").upper(),
    }
    return PlatformContractSelectionResult(
        selection=effective_selection,
        status=status,
        reason=reason,
        payload=payload,
        identity_valid=bool(identity_valid),
        missing_identity_fields=missing,
        legacy_plugin_selection=bool(legacy),
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _legacy_plugin_selection_disabled() -> bool:
    allow = str(os.getenv("ALLOW_LEGACY_OPTION_CONTRACT_SELECTION", "") or "").strip().lower()
    if allow in {"1", "true", "yes", "on"}:
        return False
    explicit = str(os.getenv("REQUIRE_PLATFORM_OPTION_CONTRACT_SELECTION", "") or "").strip().lower()
    return explicit in {"1", "true", "yes", "on"}


def _complete_contract_identity_required() -> bool:
    explicit = str(os.getenv("REQUIRE_COMPLETE_OPTION_CONTRACT_IDENTITY", "") or "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    return _legacy_plugin_selection_disabled()


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


def _process_start_time(root: Path) -> str:
    try:
        pid_text = (root / "runtime/locks/run_system.lock").read_text(encoding="utf-8").strip().split()[0]
        output = subprocess.check_output(["ps", "-p", pid_text, "-o", "lstart="], text=True, stderr=subprocess.DEVNULL).strip()
        if not output:
            return ""
        parsed = datetime.strptime(output, "%a %b %d %H:%M:%S %Y").replace(tzinfo=generated_tz())
        return parsed.isoformat(timespec="seconds")
    except Exception:
        return ""


def generated_tz():
    return datetime.now().astimezone().tzinfo


def _runtime_fingerprint(root: Path) -> dict[str, Any]:
    return {
        "process_start_time": os.getenv("PROCESS_START_TIME", "") or _process_start_time(root),
        "git_head": _git_head(root),
        "config_hash": _config_hash(root),
    }


def build_contract_selection_status(
    *,
    repo_root: str | Path,
    generated_at: datetime,
    required_option_strategy_ids: list[str] | None = None,
    max_age_sec: float = 300.0,
) -> dict[str, Any]:
    root = Path(repo_root)
    fingerprint = _runtime_fingerprint(root)
    required = [str(item or "").strip() for item in (required_option_strategy_ids or []) if str(item or "").strip()]
    path = root / "runtime/readiness/contract_selection/contract_selection_latest.json"
    handoff = _recent_option_handoff_status(
        root=root,
        now_et=generated_at,
        required_strategy_ids=required,
        max_age_sec=max_age_sec,
    )
    row = _read_json(path)
    if not row:
        ok = not bool(handoff.get("recent_required_event"))
        status = {
            "schema_version": 1,
            "artifact_type": "contract_selection",
            "generated_at": generated_at.isoformat(),
            "source_process_pid": os.getpid(),
            **fingerprint,
            "artifact_present": False,
            "path": str(path),
            "ok": ok,
            "go_no_go": "go" if ok else "no_go",
            "reason": "missing_contract_selection_verdict" if not ok else "missing",
            "required_option_strategy_ids": required,
            "recent_option_handoff": handoff,
            "blockers": [] if ok else ["missing_contract_selection_verdict"],
            "clear_conditions": [] if ok else ["write_current_contract_selection_verdict"],
        }
        write_contract_selection_artifact(repo_root=root, status=status)
        return status

    ts = _parse_iso(row.get("generated_at"))
    age_sec = _age_seconds(ts, generated_at)
    state = str(row.get("status") or "").strip().lower()
    reason = str(row.get("reason") or "").strip()
    stale = age_sec is None or age_sec > float(max_age_sec)
    stale_observability_only = stale and not bool(handoff.get("recent_required_event"))
    identity = _selected_contract_identity(row)
    selected_identity_valid = _selected_contract_identity_valid(identity) if state == "selected" else True
    ok = True
    blockers: list[str] = []
    if not stale and state in {"rejected", "no_go", "blocked", "failed"}:
        ok = False
        blockers.append("contract_selection_negative_verdict")
    if stale and bool(handoff.get("recent_required_event")):
        ok = False
        reason = reason or "stale_contract_selection_verdict"
        blockers.append("stale_contract_selection_verdict")
    enforce_selected_identity = state == "selected" and (not stale or bool(handoff.get("recent_required_event")))
    if enforce_selected_identity and not selected_identity_valid:
        ok = False
        reason = reason or "selected_contract_identity_invalid"
        blockers.append("selected_contract_identity_invalid")
    missing_identity_fields = (
        []
        if stale_observability_only
        else (_missing_contract_identity_fields(identity) if state == "selected" else [])
    )
    status = {
        "schema_version": 1,
        "artifact_type": "contract_selection",
        "generated_at": generated_at.isoformat(),
        "source_process_pid": os.getpid(),
        **fingerprint,
        "artifact_present": True,
        "path": str(path),
        "ok": bool(ok),
        "go_no_go": "go" if ok else "no_go",
        "status": state,
        "reason": reason,
        "strategy_id": str(row.get("strategy_id") or ""),
        "symbol": str(row.get("symbol") or ""),
        "verdict_generated_at": row.get("generated_at"),
        "age_sec": age_sec,
        "max_age_sec": float(max_age_sec),
        "stale": bool(stale),
        "stale_payload_ignored": bool(stale_observability_only),
        "selected_contract": {} if stale_observability_only else identity,
        "selected_contract_identity_valid": None if stale_observability_only else bool(selected_identity_valid),
        "missing_contract_identity_fields": missing_identity_fields,
        "payload": {}
        if stale_observability_only
        else (dict(row.get("payload") or {}) if isinstance(row.get("payload"), dict) else {}),
        "required_option_strategy_ids": required,
        "recent_option_handoff": handoff,
        "blockers": blockers,
        "clear_conditions": [] if ok else ["write_valid_selected_or_terminal_rejected_contract_selection_verdict"],
    }
    write_contract_selection_artifact(repo_root=root, status=status)
    return status


def write_contract_selection_artifact(*, repo_root: str | Path, status: dict[str, Any]) -> Path:
    path = Path(repo_root) / "runtime/readiness/contract_selection/contract_selection_status_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _selected_contract_identity(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    opt_symbol = str(selection.get("opt_symbol") or payload.get("opt_symbol") or "").strip().upper()
    identity = {
        "opt_symbol": opt_symbol,
        "expiry": str(selection.get("expiry") or payload.get("expiry") or ""),
        "strike": selection.get("strike", payload.get("strike")),
        "right": str(selection.get("right") or payload.get("right") or selection.get("side") or payload.get("side") or ""),
        "multiplier": selection.get("multiplier", payload.get("multiplier")),
        "exchange": str(selection.get("exchange") or payload.get("exchange") or ""),
        "tradingClass": str(
            selection.get("tradingClass")
            or selection.get("trading_class")
            or payload.get("tradingClass")
            or payload.get("trading_class")
            or ""
        ),
        "conId": selection.get("conId", selection.get("con_id", payload.get("conId", payload.get("con_id")))),
        "localSymbol": str(selection.get("localSymbol") or selection.get("local_symbol") or payload.get("localSymbol") or ""),
    }
    return _complete_internal_option_identity(identity)


def _complete_internal_option_identity(identity: dict[str, Any]) -> dict[str, Any]:
    opt_symbol = str(identity.get("opt_symbol") or "").strip().upper()
    if not opt_symbol:
        return identity
    try:
        parts = parse_internal_opt_symbol(opt_symbol)
    except Exception:
        return identity

    completed = dict(identity)
    completed["opt_symbol"] = opt_symbol
    if not str(completed.get("expiry") or "").strip():
        completed["expiry"] = f"{parts.expiry_ymd[:4]}-{parts.expiry_ymd[4:6]}-{parts.expiry_ymd[6:8]}"
    if completed.get("strike") is None or str(completed.get("strike") or "").strip() == "":
        completed["strike"] = parts.strike
    if not str(completed.get("right") or "").strip():
        completed["right"] = "CALL" if parts.right == "C" else "PUT"
    if not str(completed.get("multiplier") or "").strip():
        completed["multiplier"] = 100
    if not str(completed.get("exchange") or "").strip():
        completed["exchange"] = "SMART"
    if not str(completed.get("tradingClass") or "").strip():
        completed["tradingClass"] = parts.underlying
    if str(completed.get("conId") or "").strip() == "" and not str(completed.get("localSymbol") or "").strip():
        completed["localSymbol"] = _ib_local_symbol_from_internal_parts(parts)
    return completed


def _ib_local_symbol_from_internal_parts(parts: Any) -> str:
    strike_millis = int(round(float(parts.strike) * 1000.0))
    return f"{parts.underlying:<6}{parts.expiry_ymd[2:]}{parts.right}{strike_millis:08d}"


def _selected_contract_identity_valid(identity: dict[str, Any]) -> bool:
    opt_symbol = str(identity.get("opt_symbol") or "").strip().upper()
    if not opt_symbol:
        return False
    try:
        parts = parse_internal_opt_symbol(opt_symbol)
    except Exception:
        return False
    right = str(identity.get("right") or "").strip().upper()
    if right in {"CALL", "C"} and parts.right != "C":
        return False
    if right in {"PUT", "P"} and parts.right != "P":
        return False
    try:
        strike = identity.get("strike")
        if strike is not None and str(strike).strip() != "" and abs(float(strike) - float(parts.strike)) > 1e-9:
            return False
    except Exception:
        return False
    expiry = str(identity.get("expiry") or "").strip().replace("-", "")
    if expiry and expiry != parts.expiry_ymd:
        return False
    if _missing_contract_identity_fields(identity):
        return False
    return True


def _missing_contract_identity_fields(identity: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    multiplier = identity.get("multiplier")
    if str(multiplier or "").strip() == "":
        missing.append("multiplier")
    else:
        try:
            if abs(float(multiplier) - 100.0) > 1e-9:
                missing.append("multiplier")
        except Exception:
            missing.append("multiplier")
    if not str(identity.get("exchange") or "").strip():
        missing.append("exchange")
    if not str(identity.get("tradingClass") or "").strip():
        missing.append("tradingClass")
    if str(identity.get("conId") or "").strip() == "" and not str(identity.get("localSymbol") or "").strip():
        missing.append("broker_contract_identity")
    return missing


def _recent_option_handoff_status(
    *,
    root: Path,
    now_et: datetime,
    required_strategy_ids: list[str],
    max_age_sec: float,
) -> dict[str, Any]:
    path = root / "runtime/state/qqq_stock_option_handoff.jsonl"
    rows = _read_jsonl_objects(path)
    if not rows:
        return {"path": str(path), "recent_required_event": False, "reason": "missing"}
    required = set(required_strategy_ids or [])
    candidates: list[tuple[datetime, float, dict[str, Any]]] = []
    required_seen = 0
    for row in rows:
        consumer = str(row.get("consumer_strategy_id") or "").strip()
        if required and consumer not in required:
            continue
        required_seen += 1
        ts = _parse_iso(row.get("ts") or row.get("generated_at"))
        age_sec = _age_seconds(ts, now_et)
        recent = age_sec is not None and age_sec <= float(max_age_sec)
        terminal = bool(row.get("terminal")) or str(row.get("status") or "").strip().lower() in {
            "submitted",
            "filled",
            "rejected",
            "expired",
            "cancelled",
            "risk_blocked",
        }
        if recent and not terminal and ts is not None:
            candidates.append((ts, float(age_sec or 0.0), row))
    if not candidates:
        latest = rows[-1] if rows else {}
        reason = "no_recent_active_required_event" if required_seen else "not_required_strategy"
        return {
            "path": str(path),
            "recent_required_event": False,
            "reason": reason,
            "required_seen_count": required_seen,
            "latest": latest,
        }
    candidates.sort(key=lambda item: item[0])
    _, age_sec, latest = candidates[-1]
    consumer = str(latest.get("consumer_strategy_id") or "").strip()
    return {
        "path": str(path),
        "recent_required_event": True,
        "age_sec": age_sec,
        "max_age_sec": float(max_age_sec),
        "consumer_strategy_id": consumer,
        "trigger_key": str(latest.get("trigger_key") or latest.get("trigger_id") or ""),
        "trigger_id": str(latest.get("trigger_id") or ""),
        "status": str(latest.get("status") or ""),
        "reason": str(latest.get("reason") or ""),
        "row": latest,
        "active_required_event_count": len(candidates),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(ET)
    except Exception:
        return None


def _age_seconds(ts: datetime | None, now_et: datetime) -> float | None:
    if ts is None:
        return None
    now_norm = now_et.astimezone(ET) if now_et.tzinfo else now_et.replace(tzinfo=ET)
    return max(0.0, (now_norm - ts.astimezone(ET)).total_seconds())
