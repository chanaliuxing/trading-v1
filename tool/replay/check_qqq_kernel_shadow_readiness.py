#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
SHADOW_IDS = ("qqq_kernel_shadow_legacy_on_001", "qqq_kernel_shadow_legacy_off_001")
ALLOWED_SHARED_UNDERLYING_READ_MODES = {"plane_only", "prefer"}


def _load_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _instances(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id") or ""): item for item in list(config.get("strategies") or [])}


def _check(condition: bool, name: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "passed": bool(condition), "detail": dict(detail or {})}


def build_readiness_report(*, config_path: Path, require_disabled: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    config = _load_config(config_path)
    instances = _instances(config)

    live = instances.get("qqq_intraday_001")
    checks.append(_check(live is not None, "qqq_live_instance_present"))
    if live is not None:
        checks.append(
            _check(
                bool(live.get("enabled")) is True,
                "qqq_live_writer_enabled",
                {"enabled": bool(live.get("enabled"))},
            )
        )

    for strategy_id in SHADOW_IDS:
        item = instances.get(strategy_id)
        checks.append(_check(item is not None, f"{strategy_id}_present"))
        if item is None:
            continue
        extras = dict(((item.get("params") or {}).get("extras") or {}))
        policy_params = dict(extras.get("policy_params") or {})
        checks.extend(
            [
                _check(
                    (not bool(item.get("enabled"))) if require_disabled else True,
                    f"{strategy_id}_disabled_until_go",
                    {"enabled": bool(item.get("enabled")), "require_disabled": bool(require_disabled)},
                ),
                _check(bool(extras.get("dry_run")) is True, f"{strategy_id}_dry_run_true"),
                _check(bool(extras.get("shadow_only")) is True, f"{strategy_id}_shadow_only_true"),
                _check(
                    str(extras.get("policy_id") or "") == "qqq_intraday_policy_v1",
                    f"{strategy_id}_policy_id",
                    {"policy_id": extras.get("policy_id")},
                ),
                _check(
                    bool(extras.get("enable_shared_underlying_data_plane")) is True,
                    f"{strategy_id}_shared_plane_enabled",
                ),
                _check(
                    str(extras.get("shared_underlying_read_mode") or "") in ALLOWED_SHARED_UNDERLYING_READ_MODES,
                    f"{strategy_id}_shared_plane_mode_allowed",
                    {
                        "shared_underlying_read_mode": extras.get("shared_underlying_read_mode"),
                        "allowed_modes": sorted(ALLOWED_SHARED_UNDERLYING_READ_MODES),
                    },
                ),
                _check(
                    str(extras.get("shadow_origin_strategy_id") or "") == "qqq_intraday_001",
                    f"{strategy_id}_origin_strategy",
                    {"shadow_origin_strategy_id": extras.get("shadow_origin_strategy_id")},
                ),
                _check(
                    str(extras.get("signal_symbol") or "") == "QQQ"
                    and str(extras.get("long_exec_symbol") or "") == "TQQQ"
                    and str(extras.get("short_exec_symbol") or "") == "SQQQ",
                    f"{strategy_id}_symbol_binding",
                    {
                        "signal_symbol": extras.get("signal_symbol"),
                        "long_exec_symbol": extras.get("long_exec_symbol"),
                        "short_exec_symbol": extras.get("short_exec_symbol"),
                    },
                ),
                _check(
                    ("legacy_trader_parity_mode" in policy_params),
                    f"{strategy_id}_legacy_mode_explicit",
                    {"legacy_trader_parity_mode": policy_params.get("legacy_trader_parity_mode")},
                ),
            ]
        )

    on_params = dict(
        (((instances.get("qqq_kernel_shadow_legacy_on_001") or {}).get("params") or {}).get("extras") or {}).get(
            "policy_params"
        )
        or {}
    )
    off_params = dict(
        (((instances.get("qqq_kernel_shadow_legacy_off_001") or {}).get("params") or {}).get("extras") or {}).get(
            "policy_params"
        )
        or {}
    )
    checks.append(
        _check(
            on_params.get("legacy_trader_parity_mode") is True
            and off_params.get("legacy_trader_parity_mode") is False,
            "legacy_on_off_modes_distinct",
            {
                "legacy_on": on_params.get("legacy_trader_parity_mode"),
                "legacy_off": off_params.get("legacy_trader_parity_mode"),
            },
        )
    )

    migration_path = ROOT / "db" / "pg" / "migration_20260420_shadow_orders_origin_fields.ddl"
    migration_text = migration_path.read_text(encoding="utf-8") if migration_path.exists() else ""
    checks.extend(
        [
            _check(migration_path.exists(), "shadow_orders_origin_migration_exists", {"path": str(migration_path)}),
            _check("origin_strategy_id" in migration_text, "shadow_orders_origin_strategy_id_field"),
            _check("legacy_mode" in migration_text, "shadow_orders_legacy_mode_field"),
            _check("shadow_profile_id" in migration_text, "shadow_orders_shadow_profile_id_field"),
        ]
    )

    parity_runner = ROOT / "tool" / "replay" / "run_qqq_kernel_shadow_parity.py"
    output_root = ROOT / "reports" / "qqq_kernel_shadow_parity"
    checks.extend(
        [
            _check(parity_runner.exists(), "parity_runner_exists", {"path": str(parity_runner)}),
            _check(output_root.exists(), "parity_output_root_exists", {"path": str(output_root)}),
        ]
    )

    failed = [item for item in checks if not bool(item.get("passed"))]
    return {
        "ok": not failed,
        "recommendation": "GO_FOR_READINESS_ONLY" if not failed else "HOLD",
        "checks": checks,
        "failed_checks": [str(item.get("name")) for item in failed],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check QQQ kernel shadow readiness before enabling dry-run shadow.")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--output", default="")
    parser.add_argument("--allow-enabled", action="store_true", help="Do not require shadow instances to remain disabled.")
    args = parser.parse_args(argv)
    report = build_readiness_report(config_path=Path(args.config), require_disabled=not bool(args.allow_enabled))
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "recommendation": report["recommendation"], "failed_checks": report["failed_checks"]}, ensure_ascii=False, sort_keys=True))
    return 0 if bool(report["ok"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
