#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.index_super_win_manifest import ReplicaManifest


@dataclass(frozen=True)
class Finding:
    level: str
    message: str
    details: Dict[str, Any]


def _load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _is_writable_path(path: Path) -> bool:
    target_dir = path if path.is_dir() else path.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        probe = target_dir / ".preflight_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _normalize_broker_positions(payload: Any) -> Dict[str, float]:
    if isinstance(payload, dict):
        return {
            str(symbol or "").strip().upper(): float(qty)
            for symbol, qty in payload.items()
            if str(symbol or "").strip()
        }
    if isinstance(payload, list):
        normalized: Dict[str, float] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            qty = _optional_float(item.get("qty"))
            if symbol and qty is not None:
                normalized[symbol] = qty
        return normalized
    return {}


def _default_manifest_path() -> Path:
    return ROOT / "configs" / "index_super_win" / "qqq_replica_manifest.example.yml"


def _default_baseline_path() -> Path:
    return ROOT / "docs" / "baselines" / "qqq_intraday_001_replica_baseline.json"


def _default_diag_snapshot_path() -> Path:
    return ROOT / "runtime" / "state" / "index_super_win_diag_snapshot_index_super_win_001.json"


def _default_audit_root() -> Path:
    return ROOT / "runtime" / "audit"


def build_preflight_report(
    *,
    manifest_path: Path,
    baseline_path: Path,
    runtime_snapshot_path: Optional[Path],
    broker_positions_path: Optional[Path],
    diag_snapshot_path: Path,
    audit_root: Path,
) -> Dict[str, Any]:
    findings: list[Finding] = []

    manifest_cfg = _load_yaml(manifest_path)
    manifest = ReplicaManifest.from_params(manifest_cfg)
    enabled = [item for item in manifest.symbols if item.enabled]

    if not enabled:
        findings.append(Finding("FAIL", "No enabled replica manifests found", {"path": str(manifest_path)}))
    else:
        findings.append(
            Finding(
                "PASS",
                "Loaded enabled replica manifests",
                {"path": str(manifest_path), "enabled_symbols": [item.signal_symbol for item in enabled]},
            )
        )

    runtime_scope = str(manifest_cfg.get("runtime_scope") or "").strip()
    order_namespace = str(manifest_cfg.get("order_namespace") or "").strip()
    strategy_id = str(manifest_cfg.get("strategy_id") or "").strip()
    if runtime_scope:
        if runtime_scope.startswith("index_super_win_replica"):
            findings.append(Finding("PASS", "runtime_scope configured", {"runtime_scope": runtime_scope}))
        else:
            findings.append(
                Finding(
                    "FAIL",
                    "runtime_scope must start with index_super_win_replica",
                    {"runtime_scope": runtime_scope},
                )
            )
    else:
        findings.append(Finding("FAIL", "runtime_scope missing", {}))
    if order_namespace == "iswv2":
        findings.append(Finding("PASS", "order_namespace matches isolation contract", {"order_namespace": order_namespace}))
    else:
        findings.append(Finding("FAIL", "order_namespace must be iswv2", {"order_namespace": order_namespace or None}))
    if strategy_id:
        if strategy_id == "qqq_intraday_001":
            findings.append(
                Finding(
                    "FAIL",
                    "strategy_id collides with qqq strategy_id",
                    {"strategy_id": strategy_id},
                )
            )
        else:
            findings.append(Finding("PASS", "strategy_id configured", {"strategy_id": strategy_id}))
    else:
        findings.append(Finding("WARN", "strategy_id missing in manifest config", {}))

    local_audit_bridge = manifest_cfg.get("enable_local_audit_bridge", True)
    if bool(local_audit_bridge):
        findings.append(Finding("PASS", "local audit bridge enabled", {"enable_local_audit_bridge": bool(local_audit_bridge)}))
    else:
        findings.append(Finding("FAIL", "local audit bridge must be enabled", {"enable_local_audit_bridge": bool(local_audit_bridge)}))

    stop_dedupe_sec = _optional_float(manifest_cfg.get("stop_intent_dedupe_sec"))
    if stop_dedupe_sec is None or stop_dedupe_sec > 0.0:
        findings.append(
            Finding(
                "PASS",
                "stop intent dedupe seconds valid",
                {"stop_intent_dedupe_sec": float(stop_dedupe_sec) if stop_dedupe_sec is not None else 3.0},
            )
        )
    else:
        findings.append(Finding("FAIL", "stop_intent_dedupe_sec must be > 0", {"stop_intent_dedupe_sec": stop_dedupe_sec}))

    zero_confirm = manifest_cfg.get("zero_position_flat_confirmations", 1)
    try:
        zero_confirm_int = int(zero_confirm)
    except (TypeError, ValueError):
        zero_confirm_int = 0
    if zero_confirm_int >= 1:
        findings.append(Finding("PASS", "zero_position_flat_confirmations valid", {"zero_position_flat_confirmations": zero_confirm_int}))
    else:
        findings.append(Finding("FAIL", "zero_position_flat_confirmations must be >= 1", {"zero_position_flat_confirmations": zero_confirm}))

    if bool(manifest_cfg.get("kill_switch_on_unprotected_live", True)):
        findings.append(
            Finding(
                "PASS",
                "kill switch on unprotected live enabled",
                {"kill_switch_on_unprotected_live": bool(manifest_cfg.get("kill_switch_on_unprotected_live", True))},
            )
        )
    else:
        findings.append(
            Finding(
                "FAIL",
                "kill_switch_on_unprotected_live must be enabled",
                {"kill_switch_on_unprotected_live": bool(manifest_cfg.get("kill_switch_on_unprotected_live", False))},
            )
        )

    seen_signal: set[str] = set()
    seen_exec: set[str] = set()
    for item in enabled:
        binding = item.to_binding_dict()
        signal = binding["signal_symbol"]
        long_exec = binding["long_exec_symbol"]
        short_exec = binding["short_exec_symbol"]
        if signal in seen_signal:
            findings.append(Finding("FAIL", "Duplicate signal symbol in manifest", {"signal_symbol": signal}))
        else:
            seen_signal.add(signal)
        for exec_symbol in {long_exec, short_exec}:
            key = f"{signal}:{exec_symbol}"
            if key not in seen_exec:
                seen_exec.add(key)
        if not binding.get("confirm_symbol"):
            findings.append(Finding("FAIL", "confirm_symbol missing", {"signal_symbol": signal}))
        if not binding.get("volatility_symbol"):
            findings.append(Finding("FAIL", "volatility_symbol missing", {"signal_symbol": signal}))

    if baseline_path.exists():
        findings.append(Finding("PASS", "baseline artifact exists", {"path": str(baseline_path)}))
    else:
        findings.append(Finding("FAIL", "baseline artifact missing", {"path": str(baseline_path)}))

    replay_tools = [
        ROOT / "tool" / "replay" / "qqq_index_replica_replay.py",
        ROOT / "tool" / "replay" / "export_audit_trace.py",
    ]
    for tool_path in replay_tools:
        if tool_path.exists():
            findings.append(Finding("PASS", "replay artifact exists", {"path": str(tool_path)}))
        else:
            findings.append(Finding("FAIL", "replay artifact missing", {"path": str(tool_path)}))

    if _is_writable_path(diag_snapshot_path):
        findings.append(Finding("PASS", "diag snapshot path writable", {"path": str(diag_snapshot_path)}))
    else:
        findings.append(Finding("FAIL", "diag snapshot path not writable", {"path": str(diag_snapshot_path)}))

    if _is_writable_path(audit_root):
        findings.append(Finding("PASS", "audit root writable", {"path": str(audit_root)}))
    else:
        findings.append(Finding("FAIL", "audit root not writable", {"path": str(audit_root)}))

    broker_positions: Dict[str, float] = {}
    if broker_positions_path is None:
        findings.append(Finding("FAIL", "broker positions snapshot argument missing", {}))
    elif broker_positions_path.exists():
        broker_positions = _normalize_broker_positions(_load_json(broker_positions_path))
        findings.append(
            Finding("PASS", "loaded broker positions snapshot", {"path": str(broker_positions_path), "symbols": sorted(broker_positions)})
        )
    else:
        findings.append(Finding("FAIL", "broker positions snapshot missing", {"path": str(broker_positions_path)}))

    runtime_payload: Dict[str, Any] = {}
    if runtime_snapshot_path is None:
        findings.append(Finding("FAIL", "runtime snapshot argument missing", {}))
    elif runtime_snapshot_path.exists():
        raw_runtime = _load_json(runtime_snapshot_path)
        if isinstance(raw_runtime, dict):
            runtime_payload = dict(raw_runtime.get("replica_runtime") or raw_runtime)
            findings.append(
                Finding(
                    "PASS",
                    "loaded runtime snapshot",
                    {"path": str(runtime_snapshot_path), "symbols": sorted(runtime_payload)},
                )
            )
        else:
            findings.append(Finding("FAIL", "runtime snapshot is not a dict payload", {"path": str(runtime_snapshot_path)}))
    else:
        findings.append(Finding("FAIL", "runtime snapshot missing", {"path": str(runtime_snapshot_path)}))

    for item in enabled:
        signal = item.signal_symbol
        signal_runtime = dict(runtime_payload.get(signal) or {})
        if not signal_runtime:
            findings.append(Finding("WARN", "runtime snapshot missing symbol", {"signal_symbol": signal}))
            continue
        entry_px = _optional_float(signal_runtime.get("entry_px"))
        stop_px = _optional_float(signal_runtime.get("stop_px"))
        is_traded = bool(signal_runtime.get("is_traded", False))
        extras = signal_runtime.get("extras") or {}
        extras = extras if isinstance(extras, dict) else {}
        live_qty = int(max(0.0, _optional_float(extras.get("live_qty")) or signal_runtime.get("expected_entry_qty") or 0))
        protection_active = bool(extras.get("protection_active", False))
        entry_time = _coerce_datetime(extras.get("entry_time_et"))

        if is_traded and live_qty <= 0:
            findings.append(Finding("FAIL", "live runtime has zero qty", {"signal_symbol": signal}))
        if is_traded and entry_px is None:
            findings.append(Finding("FAIL", "live runtime missing entry_px", {"signal_symbol": signal}))
        if is_traded and stop_px is None:
            findings.append(Finding("FAIL", "live runtime missing stop_px", {"signal_symbol": signal}))
        if is_traded and entry_time is None:
            findings.append(Finding("WARN", "live runtime missing entry_time_et", {"signal_symbol": signal}))
        if is_traded and stop_px is not None and not protection_active:
            findings.append(
                Finding(
                    "FAIL",
                    "live runtime has stop_px but protection_active=false",
                    {"signal_symbol": signal, "stop_px": stop_px},
                )
            )
        if broker_positions:
            long_exec = str(item.long_exec_symbol or item.signal_symbol).strip().upper()
            short_exec = str(item.short_exec_symbol or item.long_exec_symbol or item.signal_symbol).strip().upper()
            broker_qty = 0.0
            if long_exec == short_exec:
                broker_qty = broker_positions.get(long_exec, 0.0)
            else:
                broker_qty = broker_positions.get(long_exec, 0.0) - broker_positions.get(short_exec, 0.0)
            if is_traded and abs(broker_qty) < 1e-6:
                findings.append(Finding("FAIL", "runtime marked live but broker snapshot flat", {"signal_symbol": signal}))
            if (not is_traded) and abs(broker_qty) > 1e-6:
                findings.append(Finding("FAIL", "runtime flat but broker snapshot non-zero", {"signal_symbol": signal, "broker_qty": broker_qty}))
            if is_traded and live_qty > 0 and abs(abs(broker_qty) - live_qty) > 1e-6:
                findings.append(
                    Finding(
                        "FAIL",
                        "runtime live_qty mismatches broker snapshot",
                        {"signal_symbol": signal, "runtime_live_qty": live_qty, "broker_qty": broker_qty},
                    )
                )

    ready = not any(item.level == "FAIL" for item in findings)
    return {
        "manifest_path": str(manifest_path),
        "baseline_path": str(baseline_path),
        "runtime_snapshot_path": str(runtime_snapshot_path) if runtime_snapshot_path else None,
        "broker_positions_path": str(broker_positions_path) if broker_positions_path else None,
        "diag_snapshot_path": str(diag_snapshot_path),
        "audit_root": str(audit_root),
        "ready_for_enablement": ready,
        "findings": [{"level": item.level, "message": item.message, "details": item.details} for item in findings],
    }


def _print_report(payload: Dict[str, Any]) -> None:
    print(f"ready_for_enablement={payload['ready_for_enablement']}")
    for item in payload["findings"]:
        suffix = ""
        if item["details"]:
            suffix = " " + json.dumps(item["details"], ensure_ascii=False, separators=(",", ":"), default=str)
        print(f"[{item['level']}] {item['message']}{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight readiness check for index_super_win replica enablement.")
    parser.add_argument("--manifest", default=str(_default_manifest_path()))
    parser.add_argument("--baseline", default=str(_default_baseline_path()))
    parser.add_argument("--runtime-snapshot", default=None)
    parser.add_argument("--broker-positions", default=None)
    parser.add_argument("--diag-snapshot", default=str(_default_diag_snapshot_path()))
    parser.add_argument("--audit-root", default=str(_default_audit_root()))
    parser.add_argument("--output-path", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build_preflight_report(
        manifest_path=Path(args.manifest),
        baseline_path=Path(args.baseline),
        runtime_snapshot_path=Path(args.runtime_snapshot) if args.runtime_snapshot else None,
        broker_positions_path=Path(args.broker_positions) if args.broker_positions else None,
        diag_snapshot_path=Path(args.diag_snapshot),
        audit_root=Path(args.audit_root),
    )
    if args.output_path:
        output_path = Path(args.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(payload)
    return 0 if payload["ready_for_enablement"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
