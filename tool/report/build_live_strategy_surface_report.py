#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

PATH_OPTIONS_KERNEL_PURE_PLUGIN = "OPTIONS_KERNEL_PURE_PLUGIN"
PATH_STOCKS_KERNEL_EXECUTION_PATH = "STOCKS_KERNEL_EXECUTION_PATH"
PATH_DIRECT_STRATEGY_EXECUTION_PATH = "DIRECT_STRATEGY_EXECUTION_PATH"
PATH_LEGACY_STRATEGY_EXECUTION_PATH = "LEGACY_STRATEGY_EXECUTION_PATH"
PATH_OPTION_PRIVATE_DATA_PATH = "OPTION_PRIVATE_DATA_PATH"
PATH_UNKNOWN = "UNKNOWN_PATH"

TIER_PRODUCTION_ELIGIBLE = "PRODUCTION_ELIGIBLE"
TIER_CANARY_ONLY = "CANARY_ONLY"
TIER_SHADOW_ONLY = "SHADOW_ONLY"
TIER_DISABLED_UNTIL_CONTRACT_PARITY = "DISABLED_UNTIL_CONTRACT_PARITY"


def _load_yaml(path: str | Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"config root must be a mapping: {path}")
    return payload


def _norm_list(values: Any) -> list[str]:
    if isinstance(values, str):
        return [values.strip()] if values.strip() else []
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        if isinstance(value, str):
            item = value.strip()
            if item:
                out.append(item)
    return out


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _extract_symbols(extras: dict[str, Any]) -> list[str]:
    values: list[str] = []
    values.extend(_norm_list(extras.get("symbols")))
    for key in (
        "symbol",
        "underlying",
        "signal_symbol",
        "long_exec_symbol",
        "short_exec_symbol",
        "confirm_symbol",
        "risk_symbol",
    ):
        value = extras.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    universe = extras.get("universe")
    if isinstance(universe, list):
        for row in universe:
            if not isinstance(row, dict):
                continue
            for key in ("signal_symbol", "long_exec_symbol", "short_exec_symbol", "confirm_symbol"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
    return _unique(values)


def _extract_strategy_refs(extras: dict[str, Any]) -> list[str]:
    values: list[str] = []
    values.extend(_norm_list(extras.get("shared_cap_group_strategy_ids")))
    value = extras.get("shadow_origin_strategy_id")
    if isinstance(value, str) and value.strip():
        values.append(value.strip())
    value = extras.get("signal_source_source_strategy_id")
    if isinstance(value, str) and value.strip():
        values.append(value.strip())
    return _unique(values)


def _strategy_mode(strategy: dict[str, Any], extras: dict[str, Any]) -> str:
    if bool(strategy.get("shadow_mode")) or bool(extras.get("shadow_only")) or bool(extras.get("shadow_mode")):
        return "shadow"
    if bool(extras.get("dry_run")):
        return "dry_run"
    return "live"


def _asset_family(module_name: str, extras: dict[str, Any]) -> str:
    module_lower = module_name.lower()
    if "options" in module_lower or "option" in module_lower:
        return "options"
    if str(extras.get("risk_symbol") or "").strip().upper().startswith("OPT:"):
        return "options"
    return "equity"


def _module_path(module_name: str, *, repo_root: Path) -> Path:
    return repo_root / (module_name.replace(".", "/") + ".py")


def _scan_source(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "data_bypass_patterns": [],
            "execution_bypass_patterns": [],
            "legacy_risk_patterns": [],
        }
    text = path.read_text(encoding="utf-8")
    checks = {
        "data_bypass_patterns": {
            "direct_ctx_get_price": "ctx.get_price(",
            "direct_ctx_history": "ctx.history(",
            "redis_client": "RedisClient",
            "clickhouse_client": "clickhouse",
            "sfs_reader": "SFS1sReader",
            "option_quote_store": "options_quote_store",
        },
        "execution_bypass_patterns": {
            "execution_service_import": "ExecutionService",
            "ib_adapter_import": "IBAdapter",
            "submit_intent": "submit_intent",
            "protective_stop_flag": "protective_stop",
            "flatten_action_flag": "flatten_action",
        },
        "runtime_path_signals": {
            "subscribe_features": "subscribe_features",
            "on_features": "on_features",
            "option_kernel_strategy": "OptionKernelStrategy",
            "stocks_kernel_strategy": "StocksKernelStrategy",
            "plugin_cls": "plugin_cls",
            "pure_plugin_cycle": "_run_pure_plugin_cycle",
            "build_signal_pure": "build_signal_pure",
        },
        "legacy_risk_patterns": {
            "legacy_trader_parity_mode": "legacy_trader_parity_mode",
            "enforce_stock_execution_blockers": "enforce_stock_execution_blockers",
        },
    }
    out: dict[str, Any] = {"exists": True}
    for field, patterns in checks.items():
        hits = [name for name, needle in patterns.items() if needle in text]
        out[field] = hits
    return out


def _data_plane_mode(module_name: str, extras: dict[str, Any], source_scan: dict[str, Any]) -> str:
    if bool(extras.get("enable_shared_underlying_data_plane")):
        read_mode = str(extras.get("shared_underlying_read_mode") or "").strip() or "enabled"
        return f"underlying_shared_plane:{read_mode}"
    if "direct_ctx_get_price" in source_scan.get("data_bypass_patterns", []):
        return "direct_ctx_market_reads"
    if "sfs_reader" in source_scan.get("data_bypass_patterns", []):
        return "private_sfs_reader"
    runtime_signals = set(source_scan.get("runtime_path_signals", []))
    if (
        module_name == "strategies.stocks_kernel_strategy"
        or "stocks_kernel_strategy" in runtime_signals
        or "subscribe_features" in runtime_signals
        or "on_features" in runtime_signals
    ):
        return "engine_managed_features"
    if "option_kernel_strategy" in runtime_signals:
        return "engine_managed_features"
    if "option" in module_name.lower():
        return "option_private_data_path"
    return "unknown"


def _execution_path(module_name: str, extras: dict[str, Any], source_scan: dict[str, Any]) -> str:
    if "stocks_kernel" in module_name.lower():
        return "stocks_kernel_execution_path"
    runtime_signals = set(source_scan.get("runtime_path_signals", []))
    kernel_mode = str(extras.get("kernel_mode") or "").strip().lower()
    if (
        kernel_mode == "pure_plugin"
        and "option_kernel_strategy" in runtime_signals
        and "plugin_cls" in runtime_signals
    ):
        return "options_kernel_pure_plugin"
    if kernel_mode and "option_kernel_strategy" in runtime_signals:
        return f"options_kernel_configured:{kernel_mode}"
    if source_scan.get("execution_bypass_patterns"):
        return "direct_strategy_execution_path"
    return "legacy_strategy_execution_path"


def _path_type(data_plane_mode: str, execution_path: str) -> str:
    if data_plane_mode == "option_private_data_path":
        return PATH_OPTION_PRIVATE_DATA_PATH
    mapping = {
        "options_kernel_pure_plugin": PATH_OPTIONS_KERNEL_PURE_PLUGIN,
        "stocks_kernel_execution_path": PATH_STOCKS_KERNEL_EXECUTION_PATH,
        "direct_strategy_execution_path": PATH_DIRECT_STRATEGY_EXECUTION_PATH,
        "legacy_strategy_execution_path": PATH_LEGACY_STRATEGY_EXECUTION_PATH,
    }
    return mapping.get(execution_path, PATH_UNKNOWN)


def _evidence_flags(
    *,
    path_type: str,
    data_plane_mode: str,
    execution_path: str,
    extras: dict[str, Any],
) -> dict[str, bool]:
    return {
        "direct_strategy_execution_path": execution_path == "direct_strategy_execution_path",
        "legacy_strategy_execution_path": execution_path == "legacy_strategy_execution_path",
        "option_private_data_path": data_plane_mode == "option_private_data_path" or path_type == PATH_OPTION_PRIVATE_DATA_PATH,
        "options_kernel_pure_plugin": execution_path == "options_kernel_pure_plugin",
        "stocks_kernel_execution_path": execution_path == "stocks_kernel_execution_path",
        "stock_execution_blockers_disabled": extras.get("enforce_stock_execution_blockers") is False,
    }


def _contract_readiness_and_tier(
    *,
    path_type: str,
    data_plane_mode: str,
    execution_path: str,
    evidence_flags: dict[str, bool],
    risk_flags: list[str],
    source_scan: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    blockers: list[str] = []
    warnings: list[str] = []
    data_contract_ready = data_plane_mode not in {"unknown", "direct_ctx_market_reads", "private_sfs_reader", "option_private_data_path"}
    session_contract_ready = True
    risk_contract_ready = "legacy_parity_mode_enabled" not in risk_flags
    execution_contract_ready = execution_path not in {"legacy_strategy_execution_path", "direct_strategy_execution_path"}
    fill_attribution_ready = path_type in {PATH_OPTIONS_KERNEL_PURE_PLUGIN, PATH_STOCKS_KERNEL_EXECUTION_PATH}

    if evidence_flags.get("legacy_strategy_execution_path"):
        blockers.append("legacy_strategy_execution_path")
    if evidence_flags.get("direct_strategy_execution_path"):
        blockers.append("direct_strategy_execution_path")
    if evidence_flags.get("option_private_data_path"):
        blockers.append("option_private_data_path")
    if evidence_flags.get("stock_execution_blockers_disabled"):
        blockers.append("stock_execution_blockers_disabled")
    if path_type == PATH_UNKNOWN:
        blockers.append("unknown_path_type")
    if not data_contract_ready:
        blockers.append(f"data_contract_not_ready:{data_plane_mode}")
    if not risk_contract_ready:
        blockers.append("risk_contract_parity_not_ready")
    if not fill_attribution_ready:
        warnings.append("fill_attribution_parity_missing")

    if evidence_flags.get("legacy_strategy_execution_path") or path_type in {PATH_OPTION_PRIVATE_DATA_PATH, PATH_UNKNOWN}:
        tier = TIER_DISABLED_UNTIL_CONTRACT_PARITY
    elif evidence_flags.get("direct_strategy_execution_path") or evidence_flags.get("stock_execution_blockers_disabled"):
        tier = TIER_SHADOW_ONLY
    elif path_type in {PATH_OPTIONS_KERNEL_PURE_PLUGIN, PATH_STOCKS_KERNEL_EXECUTION_PATH}:
        tier = TIER_CANARY_ONLY
        warnings.append("production_parity_proof_required")
    else:
        tier = TIER_DISABLED_UNTIL_CONTRACT_PARITY
        blockers.append("unknown_path_type")

    production_contract_ready = False
    return (
        {
            "data_contract_ready": bool(data_contract_ready),
            "session_contract_ready": bool(session_contract_ready),
            "risk_contract_ready": bool(risk_contract_ready),
            "execution_contract_ready": bool(execution_contract_ready),
            "fill_attribution_ready": bool(fill_attribution_ready),
            "production_contract_ready": production_contract_ready,
            "blockers": _unique(blockers),
            "warnings": _unique(warnings),
            "source_runtime_signals": list(source_scan.get("runtime_path_signals") or []),
        },
        tier,
    )


def _risk_flags(
    strategy: dict[str, Any],
    extras: dict[str, Any],
    mode: str,
    data_plane_mode: str,
    execution_path: str,
    source_scan: dict[str, Any],
) -> list[str]:
    flags: list[str] = []
    if mode == "live":
        flags.append("live_strategy")
    if "shadow" in mode:
        flags.append("shadow_mode_forbidden")
    for key, value in extras.items():
        key_s = str(key or "").strip().lower()
        value_s = str(value or "").strip().lower()
        if key_s in {"shadow_only", "shadow_mode"} and bool(value):
            flags.append(f"shadow_config_forbidden:{key_s}")
        if "shadow" in key_s and value not in (False, None, "", [], {}):
            flags.append(f"shadow_config_forbidden:{key_s}")
        if isinstance(value, str) and "shadow" in value_s:
            flags.append(f"shadow_config_forbidden:{key_s}")
    if bool(extras.get("legacy_trader_parity_mode")):
        flags.append("legacy_parity_mode_enabled")
    if extras.get("enforce_stock_execution_blockers") is False:
        flags.append("stock_execution_blockers_disabled")
    if data_plane_mode in {"unknown", "direct_ctx_market_reads", "private_sfs_reader", "option_private_data_path"}:
        flags.append(f"data_path:{data_plane_mode}")
    if execution_path in {"legacy_strategy_execution_path", "direct_strategy_execution_path"}:
        flags.append(f"execution_path:{execution_path}")
    strategy_id = str(strategy.get("id") or "").strip().lower()
    if "shadow" in strategy_id:
        flags.append("shadow_named_strategy_forbidden")
    for hit in source_scan.get("data_bypass_patterns", []):
        flags.append(f"source_data_signal:{hit}")
    for hit in source_scan.get("execution_bypass_patterns", []):
        flags.append(f"source_execution_signal:{hit}")
    if not bool(strategy.get("enabled")):
        flags.append("disabled_strategy")
    return _unique(flags)


def build_live_strategy_surface_report(
    config_path: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    config_file = Path(config_path)
    root = Path(repo_root) if repo_root is not None else config_file.resolve().parent
    payload = _load_yaml(config_file)
    strategies = payload.get("strategies") or []
    if not isinstance(strategies, list):
        raise ValueError("config strategies must be a list")

    rows: list[dict[str, Any]] = []
    active_ids: list[str] = []
    live_ids: list[str] = []
    shadow_ids: list[str] = []
    dry_run_ids: list[str] = []
    high_risk_ids: list[str] = []

    for strategy in strategies:
        if not isinstance(strategy, dict):
            continue
        if not bool(strategy.get("enabled")):
            continue
        strategy_id = str(strategy.get("id") or "").strip()
        params = strategy.get("params") or {}
        extras = (params.get("extras") or {}) if isinstance(params, dict) else {}
        module_name = str(strategy.get("module") or "").strip()
        mode = _strategy_mode(strategy, extras)
        source_path = _module_path(module_name, repo_root=root)
        source_scan = _scan_source(source_path)
        data_plane_mode = _data_plane_mode(module_name, extras, source_scan)
        execution_path = _execution_path(module_name, extras, source_scan)
        family = _asset_family(module_name, extras)
        symbols = _extract_symbols(extras)
        strategy_refs = _extract_strategy_refs(extras)
        path_type = _path_type(data_plane_mode, execution_path)
        risk_flags = _risk_flags(
            strategy,
            extras,
            mode,
            data_plane_mode,
            execution_path,
            source_scan,
        )
        evidence_flags = _evidence_flags(
            path_type=path_type,
            data_plane_mode=data_plane_mode,
            execution_path=execution_path,
            extras=extras,
        )
        contract_readiness, permission_tier = _contract_readiness_and_tier(
            path_type=path_type,
            data_plane_mode=data_plane_mode,
            execution_path=execution_path,
            evidence_flags=evidence_flags,
            risk_flags=risk_flags,
            source_scan=source_scan,
        )
        row = {
            "strategy_id": strategy_id,
            "enabled": True,
            "mode": mode,
            "asset_family": family,
            "registry_name": str(strategy.get("registry_name") or "").strip(),
            "module": module_name,
            "class": str(strategy.get("class") or "").strip(),
            "source_file": str(source_path),
            "symbols": symbols,
            "strategy_refs": strategy_refs,
            "data_plane_mode": data_plane_mode,
            "execution_path": execution_path,
            "path_type": path_type,
            "permission_tier": permission_tier,
            "contract_readiness": contract_readiness,
            "evidence_flags": evidence_flags,
            "shadow_origin_strategy_id": str(extras.get("shadow_origin_strategy_id") or "").strip(),
            "health_subscription_required": True,
            "risk_flags": risk_flags,
            "source_scan": source_scan,
        }
        rows.append(row)
        active_ids.append(strategy_id)
        if mode == "live":
            live_ids.append(strategy_id)
        elif mode == "shadow":
            shadow_ids.append(strategy_id)
        else:
            dry_run_ids.append(strategy_id)
        if any(
            flag.startswith("data_path:")
            or flag.startswith("execution_path:")
            or flag in {"legacy_parity_mode_enabled", "stock_execution_blockers_disabled", "shadow_named_but_live_config"}
            for flag in row["risk_flags"]
        ):
            high_risk_ids.append(strategy_id)

    rows.sort(key=lambda item: item["strategy_id"])
    active_ids.sort()
    live_ids.sort()
    shadow_ids.sort()
    dry_run_ids.sort()
    high_risk_ids = sorted(set(high_risk_ids))
    tier_counts = {
        "production_eligible_count": sum(1 for row in rows if row.get("permission_tier") == TIER_PRODUCTION_ELIGIBLE),
        "canary_only_count": sum(1 for row in rows if row.get("permission_tier") == TIER_CANARY_ONLY),
        "shadow_only_count": sum(1 for row in rows if row.get("permission_tier") == TIER_SHADOW_ONLY),
        "disabled_until_contract_parity_count": sum(
            1 for row in rows if row.get("permission_tier") == TIER_DISABLED_UNTIL_CONTRACT_PARITY
        ),
        "unknown_path_count": sum(1 for row in rows if row.get("path_type") == PATH_UNKNOWN),
    }

    return {
        "config_path": str(config_file),
        "repo_root": str(root),
        "summary": {
            "active_strategy_count": len(active_ids),
            "live_strategy_count": len(live_ids),
            "shadow_strategy_count": len(shadow_ids),
            "dry_run_strategy_count": len(dry_run_ids),
            "high_risk_strategy_count": len(high_risk_ids),
            **tier_counts,
        },
        "strategy_surface_summary": tier_counts,
        "active_strategy_ids": active_ids,
        "live_strategy_ids": live_ids,
        "shadow_strategy_ids": shadow_ids,
        "dry_run_strategy_ids": dry_run_ids,
        "high_risk_strategy_ids": high_risk_ids,
        "strategies": rows,
    }


def _row_by_strategy_id(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = list(report.get("strategies") or [])
    return {
        str(row.get("strategy_id") or "").strip(): dict(row)
        for row in rows
        if str(row.get("strategy_id") or "").strip()
    }


def evaluate_phase0_freeze_gate(
    report: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    require_baseline: bool = False,
    allow_shadow_named_live: bool = False,
    allow_shadow_promotion: bool = False,
) -> dict[str, Any]:
    del allow_shadow_named_live, allow_shadow_promotion
    rows = list(report.get("strategies") or [])
    blocking_strategy_ids: list[str] = []
    blocking_reasons: list[str] = []
    for row in rows:
        strategy_id = str(row.get("strategy_id") or "").strip()
        risk_flags = {str(item).strip() for item in (row.get("risk_flags") or []) if str(item).strip()}
        for flag in sorted(risk_flags):
            if flag.startswith("shadow_"):
                blocking_strategy_ids.append(strategy_id)
                blocking_reasons.append(f"{strategy_id}:{flag}")
    baseline_live_ids: list[str] = []
    current_live_ids = sorted(str(item).strip() for item in (report.get("live_strategy_ids") or []) if str(item).strip())
    current_shadow_ids = sorted(str(item).strip() for item in (report.get("shadow_strategy_ids") or []) if str(item).strip())
    if baseline is None:
        if require_baseline:
            blocking_reasons.append("phase0_freeze_baseline_missing")
    else:
        baseline_live_ids = sorted(str(item).strip() for item in (baseline.get("live_strategy_ids") or []) if str(item).strip())
        baseline_shadow_ids = sorted(str(item).strip() for item in (baseline.get("shadow_strategy_ids") or []) if str(item).strip())
        current_live = set(current_live_ids)
        baseline_live = set(baseline_live_ids)
        baseline_shadow = set(baseline_shadow_ids)
        baseline_rows = _row_by_strategy_id(baseline)
        for strategy_id in sorted(current_live - baseline_live):
            if strategy_id in baseline_shadow:
                blocking_strategy_ids.append(strategy_id)
                blocking_reasons.append(f"{strategy_id}:shadow_promoted_to_live")
            else:
                blocking_strategy_ids.append(strategy_id)
                blocking_reasons.append(f"{strategy_id}:new_live_strategy_not_in_phase0_baseline")
        for strategy_id in sorted(current_live & baseline_live):
            baseline_mode = str((baseline_rows.get(strategy_id) or {}).get("mode") or "").strip()
            current_mode = str((_row_by_strategy_id(report).get(strategy_id) or {}).get("mode") or "").strip()
            if baseline_mode and current_mode and baseline_mode != current_mode and current_mode == "live":
                blocking_strategy_ids.append(strategy_id)
                blocking_reasons.append(f"{strategy_id}:mode_changed_to_live_from_{baseline_mode}")
    return {
        "ok": len(blocking_strategy_ids) == 0 and len(blocking_reasons) == 0,
        "blocking_strategy_ids": sorted(blocking_strategy_ids),
        "blocking_reasons": sorted(blocking_reasons),
        "summary": dict(report.get("summary") or {}),
        "baseline_summary": dict((baseline or {}).get("summary") or {}),
        "baseline_live_strategy_ids": baseline_live_ids,
        "current_live_strategy_ids": current_live_ids,
        "current_forbidden_strategy_ids": current_shadow_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Phase 0 live/shadow strategy surface report from config.yml")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    repo_root = args.repo_root or None
    report = build_live_strategy_surface_report(args.config, repo_root=repo_root)
    payload = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
        return 0
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
