#!/usr/bin/env python3
"""
Readiness checks for QQQ option v2 migration path.

Goals:
- Verify legacy strategy remains intact
- Verify v2 strategy is registered and correctly configured for the requested rollout mode
- Verify shared-cap-group config includes legacy, 0DTE, and v2 ids
- Verify parity snapshot/profile fields are configured before shadow enablement
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ID = "qqq_1dte_ma10_reject_0324_001"
V2_ID = "qqq_1dte_ma10_reject_0324_v2_001"
ZERO_DTE_ID = "qqq_0dte_opt_live_001"
V2_REGISTRY_NAME = "qqq_1dte_ma10_reject_live_v2"


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
    if V2_REGISTRY_NAME not in registry:
        errors.append(f"strategy_registry missing: {V2_REGISTRY_NAME}")
    else:
        infos.append(f"strategy_registry ok: {V2_REGISTRY_NAME}")
    return errors, infos


def _check_strategy_presence(cfg: Dict[str, Any], *, mode: str = "default") -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    legacy = _find_strategy(cfg, LEGACY_ID)
    v2 = _find_strategy(cfg, V2_ID)
    if legacy is None:
        errors.append(f"missing strategy: {LEGACY_ID}")
    else:
        infos.append(f"strategy present: {LEGACY_ID}")
        legacy_enabled = bool(legacy.get("enabled", False))
    if v2 is None:
        errors.append(f"missing strategy: {V2_ID}")
    else:
        infos.append(f"strategy present: {V2_ID}")
        enabled = bool(v2.get("enabled", False))
        shadow_mode = bool(v2.get("shadow_mode", False))
        if mode == "shadow":
            if not enabled:
                errors.append(f"v2 must be enabled in shadow mode: {V2_ID}")
            else:
                infos.append(f"v2 enabled for shadow mode: {V2_ID}")
            if not shadow_mode:
                errors.append(f"v2 shadow_mode must be true: {V2_ID}")
            else:
                infos.append(f"v2 shadow_mode enabled: {V2_ID}")
        elif mode == "live":
            if legacy is not None and legacy_enabled:
                errors.append(f"legacy must be disabled in v2 live mode: {LEGACY_ID}")
            else:
                infos.append(f"legacy disabled for v2 live mode: {LEGACY_ID}")
            if not enabled:
                errors.append(f"v2 must be enabled in live mode: {V2_ID}")
            else:
                infos.append(f"v2 enabled for live mode: {V2_ID}")
            if shadow_mode:
                errors.append(f"v2 shadow_mode must be false in live mode: {V2_ID}")
            else:
                infos.append(f"v2 shadow_mode disabled for live mode: {V2_ID}")
        else:
            if enabled:
                errors.append(f"v2 must remain disabled by default: {V2_ID}")
            else:
                infos.append(f"v2 disabled by default: {V2_ID}")
    return errors, infos


def _check_shared_cap_group(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    expected = {ZERO_DTE_ID, LEGACY_ID, V2_ID}
    v2 = _find_strategy(cfg, V2_ID) or {}
    extras = (((v2.get("params") or {}).get("extras") or {}))
    members = {str(x).strip() for x in (extras.get("shared_cap_group_strategy_ids") or []) if str(x).strip()}
    missing = sorted(expected - members)
    if missing:
        errors.append(f"v2 shared_cap_group_strategy_ids missing: {', '.join(missing)}")
    else:
        infos.append("v2 shared_cap_group_strategy_ids ok")
    if str(extras.get("shared_cap_group_id") or "").strip():
        infos.append(f"v2 shared_cap_group_id ok: {extras.get('shared_cap_group_id')}")
    else:
        errors.append("v2 shared_cap_group_id missing")
    return errors, infos


def _check_parity_extras(cfg: Dict[str, Any], *, mode: str = "default") -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    v2 = _find_strategy(cfg, V2_ID) or {}
    extras = (((v2.get("params") or {}).get("extras") or {}))
    profile = str(extras.get("live_parity_profile_id") or "").strip()
    snapshot_root = str(extras.get("live_parity_snapshot_root") or "").strip()
    if not profile:
        errors.append("v2 live_parity_profile_id missing")
    else:
        infos.append(f"v2 live_parity_profile_id ok: {profile}")
    if not snapshot_root:
        errors.append("v2 live_parity_snapshot_root missing")
    else:
        infos.append(f"v2 live_parity_snapshot_root ok: {snapshot_root}")
    if bool(extras.get("enable_live_parity_snapshot", False)):
        infos.append("v2 parity snapshot enabled")
    else:
        errors.append("v2 enable_live_parity_snapshot must be true")
    if mode == "shadow":
        if bool(extras.get("enable_live_shadow_mode", False)):
            infos.append("v2 live shadow mode enabled")
        else:
            errors.append("v2 enable_live_shadow_mode must be true in shadow mode")
    elif mode == "live":
        if bool(extras.get("enable_live_shadow_mode", False)):
            errors.append("v2 enable_live_shadow_mode must be false in live mode")
        else:
            infos.append("v2 live shadow mode disabled for live mode")
    return errors, infos


def run_checks(cfg: Dict[str, Any], *, mode: str = "default") -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    e, i = _check_registry(cfg)
    errors.extend(e)
    infos.extend(i)
    e, i = _check_strategy_presence(cfg, mode=mode)
    errors.extend(e)
    infos.extend(i)
    e, i = _check_shared_cap_group(cfg)
    errors.extend(e)
    infos.extend(i)
    e, i = _check_parity_extras(cfg, mode=mode)
    errors.extend(e)
    infos.extend(i)
    return errors, infos


def main() -> int:
    ap = argparse.ArgumentParser(description="Check QQQ option v2 readiness.")
    ap.add_argument("--config", default=str(ROOT / "config.yml"))
    ap.add_argument("--mode", default="default", choices=["default", "shadow", "live"])
    args = ap.parse_args()

    cfg = _load_cfg(Path(args.config))
    errors, infos = run_checks(cfg, mode=args.mode)
    print(f"QQQ option v2 readiness ({args.mode})")
    for info in infos:
        print(f"[info] {info}")
    for err in errors:
        print(f"[error] {err}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
