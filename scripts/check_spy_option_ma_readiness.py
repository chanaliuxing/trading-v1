#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ID = "spy_1dte_ma10_reject_0324_001"
PURE_ID = "spy_1dte_ma10_reject_0324_pure_001"
REGISTRY_NAME = "spy_1dte_ma10_reject_live_v2"
EXPECTED_DEFINITION_ID = "SPY_MA_CROSS_STACK_CONT_V2_DUAL"


def _load_cfg(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _find_strategy(cfg: Dict[str, Any], strategy_id: str) -> Dict[str, Any] | None:
    for spec in (cfg.get("strategies") or []):
        if str((spec or {}).get("id") or "").strip() == str(strategy_id):
            return spec
    return None


def _check_registry(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    registry = dict(cfg.get("strategy_registry") or {})
    if REGISTRY_NAME not in registry:
        errors.append(f"strategy_registry missing: {REGISTRY_NAME}")
    else:
        infos.append(f"strategy_registry ok: {REGISTRY_NAME}")
    return errors, infos


def _check_active_strategy(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    active = _find_strategy(cfg, ACTIVE_ID)
    if active is None:
        errors.append(f"missing strategy: {ACTIVE_ID}")
        return errors, infos
    infos.append(f"strategy present: {ACTIVE_ID}")
    if not bool(active.get("enabled", False)):
        errors.append(f"active strategy must be enabled: {ACTIVE_ID}")
    else:
        infos.append(f"active strategy enabled: {ACTIVE_ID}")
    if bool(active.get("shadow_mode", False)):
        errors.append(f"active strategy shadow_mode must be false: {ACTIVE_ID}")
    else:
        infos.append(f"active strategy shadow_mode disabled: {ACTIVE_ID}")
    return errors, infos


def _check_extras(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    active = _find_strategy(cfg, ACTIVE_ID) or {}
    extras = (((active.get("params") or {}).get("extras")) or {})
    if str(extras.get("definition_id") or "").strip() != EXPECTED_DEFINITION_ID:
        errors.append(f"definition_id mismatch: expected {EXPECTED_DEFINITION_ID}")
    else:
        infos.append(f"definition_id ok: {EXPECTED_DEFINITION_ID}")
    if str(extras.get("kernel_mode") or "").strip() != "pure_plugin":
        errors.append("kernel_mode must be pure_plugin")
    else:
        infos.append("kernel_mode ok: pure_plugin")
    if not bool(extras.get("strict_scope_validation", False)):
        errors.append("strict_scope_validation must be true")
    else:
        infos.append("strict_scope_validation enabled")
    profile = str(extras.get("live_parity_profile_id") or "").strip()
    if not profile:
        errors.append("live_parity_profile_id missing")
    else:
        infos.append(f"live_parity_profile_id ok: {profile}")
    snapshot_root = str(extras.get("live_parity_snapshot_root") or "").strip()
    if not snapshot_root:
        errors.append("live_parity_snapshot_root missing")
    else:
        infos.append(f"live_parity_snapshot_root ok: {snapshot_root}")
    if not bool(extras.get("enable_live_parity_snapshot", False)):
        errors.append("enable_live_parity_snapshot must be true")
    else:
        infos.append("live parity snapshot enabled")
    if "enable_live_shadow_mode" not in extras:
        errors.append("enable_live_shadow_mode missing")
    elif bool(extras.get("enable_live_shadow_mode", False)):
        infos.append("live shadow mode currently enabled")
    else:
        infos.append("live shadow mode currently disabled")
    members = {str(x).strip() for x in (extras.get("shared_cap_group_strategy_ids") or []) if str(x).strip()}
    expected = {ACTIVE_ID, PURE_ID}
    missing = sorted(expected - members)
    if missing:
        errors.append(f"shared_cap_group_strategy_ids missing: {', '.join(missing)}")
    else:
        infos.append("shared_cap_group_strategy_ids ok")
    return errors, infos


def run_checks(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    for fn in (_check_registry, _check_active_strategy, _check_extras):
        e, i = fn(cfg)
        errors.extend(e)
        infos.extend(i)
    return errors, infos


def main() -> int:
    ap = argparse.ArgumentParser(description="Check SPY option MA readiness.")
    ap.add_argument("--config", default=str(ROOT / "config.yml"))
    args = ap.parse_args()

    cfg = _load_cfg(Path(args.config))
    errors, infos = run_checks(cfg)
    print("SPY option MA readiness")
    for info in infos:
        print(f"[info] {info}")
    for err in errors:
        print(f"[error] {err}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
