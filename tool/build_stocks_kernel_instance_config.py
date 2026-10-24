from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import sys
from typing import TYPE_CHECKING, Any
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if TYPE_CHECKING:
    from strategies.stocks_kernel.plugin import BaseStockPlugin


def build_instance_config(
    *,
    strategy_id: str,
    strategy_template: str,
    signal_symbol: str,
    long_exec_symbol: str,
    short_exec_symbol: str,
    enabled: bool = False,
    dry_run: bool = True,
    capital_limit_usd: float = 100000.0,
    cadence_s: int = 5,
    ttl_ms: int = 2000,
    order_namespace: str = "stkv1",
    audit_root_dir: str = "runtime/audit",
    feature_bar_sec: int = 5,
    gate_fail_mode: str = "fail_closed",
    startup_preflight_enabled: bool = True,
    confirm_symbol: str | None = None,
    volatility_symbol: str | None = None,
    feature_params: dict[str, Any] | None = None,
    gate_overrides: dict[str, Any] | None = None,
    policy_params: dict[str, Any] | None = None,
    execution_profile: dict[str, Any] | None = None,
    extra_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from strategies.stocks_kernel.builder import StrategyInstanceSpec, build_strategy_instance_config

    return build_strategy_instance_config(
        StrategyInstanceSpec(
            strategy_id=strategy_id,
            strategy_template=strategy_template,
            signal_symbol=signal_symbol,
            long_exec_symbol=long_exec_symbol,
            short_exec_symbol=short_exec_symbol,
            enabled=enabled,
            dry_run=dry_run,
            capital_limit_usd=capital_limit_usd,
            cadence_s=cadence_s,
            ttl_ms=ttl_ms,
            order_namespace=order_namespace,
            audit_root_dir=audit_root_dir,
            feature_bar_sec=feature_bar_sec,
            gate_fail_mode=gate_fail_mode,
            startup_preflight_enabled=startup_preflight_enabled,
            confirm_symbol=confirm_symbol,
            volatility_symbol=volatility_symbol,
            feature_params=dict(feature_params or {}),
            gate_overrides=dict(gate_overrides or {}),
            policy_params=dict(policy_params or {}),
            execution_profile=dict(execution_profile or {}),
            extra_extras=dict(extra_extras or {}),
        )
    )


def load_stock_plugin(plugin_cls: str, *, plugin_params: dict[str, Any] | None = None) -> "BaseStockPlugin":
    from strategies.stocks_kernel.plugin import BaseStockPlugin

    module_name, _, attr_name = str(plugin_cls or "").strip().partition(":")
    if not module_name or not attr_name:
        raise ValueError("plugin class must be MODULE:CLASS")
    module = importlib.import_module(module_name)
    target: Any = module
    for part in attr_name.split("."):
        target = getattr(target, part)
    plugin = target(**dict(plugin_params or {}))
    if not isinstance(plugin, BaseStockPlugin):
        raise TypeError("plugin class must instantiate BaseStockPlugin")
    return plugin


def build_plugin_instance_config(
    *,
    plugin_cls: str,
    strategy_id: str,
    enabled: bool = False,
    dry_run: bool = False,
    capital_limit_usd: float = 250.0,
    cadence_s: int = 5,
    ttl_ms: int = 30000,
    qty: float = 1.0,
    trade_budget_usd: float | None = None,
    trade_interval_sec: int = 60,
    max_cycles_per_day: int = 240,
    plugin_params: dict[str, Any] | None = None,
    policy_params: dict[str, Any] | None = None,
    extra_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from strategies.stocks_kernel.builder import build_manifest_strategy_instance_config

    plugin = load_stock_plugin(plugin_cls, plugin_params=plugin_params)
    return build_manifest_strategy_instance_config(
        plugin=plugin,
        strategy_id=strategy_id,
        enabled=enabled,
        capital_limit_usd=capital_limit_usd,
        cadence_s=cadence_s,
        ttl_ms=ttl_ms,
        dry_run=dry_run,
        qty=qty,
        trade_budget_usd=trade_budget_usd,
        trade_interval_sec=trade_interval_sec,
        max_cycles_per_day=max_cycles_per_day,
        plugin_params=plugin_params,
        policy_params=policy_params,
        extra_extras=extra_extras,
    )


def build_admission_report(strategy_config: dict[str, Any]) -> dict[str, Any]:
    from services.production.strategy_manifest_gate_service import build_strategy_manifest_gate

    generated_at = datetime.now(tz=ZoneInfo("America/New_York"))
    gate = build_strategy_manifest_gate(
        config={"strategies": [strategy_config]},
        generated_at=generated_at,
    )
    return {
        "schema_version": 1,
        "artifact_type": "stock_strategy_onboarding_admission",
        "generated_at": generated_at.isoformat(),
        "go_no_go": gate.get("go_no_go"),
        "ok": bool(gate.get("ok")),
        "blockers": list(gate.get("blockers") or []),
        "strategy_config": strategy_config,
        "strategy_manifest_gate": gate,
    }


def _json_arg(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    return dict(json.loads(raw))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a minimal stocks_kernel strategy instance config block.")
    parser.add_argument("--strategy-id", required=False, default="stocks_kernel_smh_001")
    parser.add_argument("--template", required=False, default="trend_follow_equity")
    parser.add_argument("--signal-symbol", required=False, default="SMH")
    parser.add_argument("--long-exec-symbol", required=False, default="SOXL")
    parser.add_argument("--short-exec-symbol", required=False, default="SOXS")
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--live", action="store_true", help="Generate config with dry_run=false")
    parser.add_argument("--capital-limit-usd", type=float, default=100000.0)
    parser.add_argument("--cadence-s", type=int, default=5)
    parser.add_argument("--ttl-ms", type=int, default=2000)
    parser.add_argument("--order-namespace", default="stkv1")
    parser.add_argument("--audit-root-dir", default="runtime/audit")
    parser.add_argument("--feature-bar-sec", type=int, default=5)
    parser.add_argument("--gate-fail-mode", default="fail_closed")
    parser.add_argument("--disable-startup-preflight", action="store_true")
    parser.add_argument("--confirm-symbol", default=None)
    parser.add_argument("--volatility-symbol", default=None)
    parser.add_argument("--feature-params", default="")
    parser.add_argument("--gate-overrides", default="")
    parser.add_argument("--policy-params", default="")
    parser.add_argument("--execution-profile", default="")
    parser.add_argument("--extra-extras", default="")
    parser.add_argument("--plugin-cls", default="", help="Import path MODULE:CLASS for a BaseStockPlugin scaffold.")
    parser.add_argument("--plugin-params", default="")
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--trade-budget-usd", type=float, default=None)
    parser.add_argument("--trade-interval-sec", type=int, default=60)
    parser.add_argument("--max-cycles-per-day", type=int, default=240)
    parser.add_argument(
        "--validate-production",
        action="store_true",
        help="Emit onboarding admission report and exit nonzero if manifest/session gate fails.",
    )
    parser.add_argument("--list-templates", action="store_true")
    args = parser.parse_args()

    if args.list_templates:
        from strategies.stocks_kernel.builder import template_catalog

        print(yaml.safe_dump(template_catalog(), sort_keys=False, allow_unicode=False))
        return

    with contextlib.redirect_stdout(sys.stderr):
        if args.plugin_cls:
            payload = build_plugin_instance_config(
                plugin_cls=args.plugin_cls,
                strategy_id=args.strategy_id,
                enabled=bool(args.enabled),
                dry_run=not bool(args.live),
                capital_limit_usd=float(args.capital_limit_usd),
                cadence_s=int(args.cadence_s),
                ttl_ms=int(args.ttl_ms),
                qty=float(args.qty),
                trade_budget_usd=args.trade_budget_usd,
                trade_interval_sec=int(args.trade_interval_sec),
                max_cycles_per_day=int(args.max_cycles_per_day),
                plugin_params=_json_arg(args.plugin_params),
                policy_params=_json_arg(args.policy_params),
                extra_extras=_json_arg(args.extra_extras),
            )
        else:
            payload = build_instance_config(
                strategy_id=args.strategy_id,
                strategy_template=args.template,
                signal_symbol=args.signal_symbol,
                long_exec_symbol=args.long_exec_symbol,
                short_exec_symbol=args.short_exec_symbol,
                enabled=bool(args.enabled),
                dry_run=not bool(args.live),
                capital_limit_usd=float(args.capital_limit_usd),
                cadence_s=int(args.cadence_s),
                ttl_ms=int(args.ttl_ms),
                order_namespace=args.order_namespace,
                audit_root_dir=args.audit_root_dir,
                feature_bar_sec=int(args.feature_bar_sec),
                gate_fail_mode=args.gate_fail_mode,
                startup_preflight_enabled=not bool(args.disable_startup_preflight),
                confirm_symbol=args.confirm_symbol,
                volatility_symbol=args.volatility_symbol,
                feature_params=_json_arg(args.feature_params),
                gate_overrides=_json_arg(args.gate_overrides),
                policy_params=_json_arg(args.policy_params),
                execution_profile=_json_arg(args.execution_profile),
                extra_extras=_json_arg(args.extra_extras),
            )
        report = build_admission_report(payload) if args.validate_production else None
    if report is not None:
        print(yaml.safe_dump(report, sort_keys=False, allow_unicode=False))
        if not bool(report.get("ok")):
            raise SystemExit(2)
    else:
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False))


if __name__ == "__main__":
    main()
