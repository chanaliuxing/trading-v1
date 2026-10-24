from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from libs.strategy import StrategyParams

from .templates import default_template_registry
from .types import KernelExecutionLane, KernelInstrumentBinding


def _normalize_gate_contract_inputs(*, extras: dict[str, Any], template) -> tuple[list[str], dict[str, Any]]:
    gate_pipeline = [
        str(item).strip()
        for item in list(extras.get("gate_pipeline") or (list(template.gate_pipeline) if template is not None else []))
        if str(item).strip()
    ]
    gate_overrides = {
        **dict(getattr(template, "gate_overrides", {}) or {}),
        **dict(extras.get("gate_overrides") or {}),
    }
    # Backward-compat adapter: map legacy multi_scope_gate extras into standard gate contracts.
    raw_multi_scope = dict(extras.get("multi_scope_gate") or {})
    raw_enabled = raw_multi_scope.get("enabled", None)
    if raw_enabled is not None and bool(raw_enabled):
        if "multi_scope_readiness" not in set(gate_pipeline):
            gate_pipeline.append("multi_scope_readiness")
    if raw_multi_scope:
        override = dict(gate_overrides.get("multi_scope_readiness") or {})
        params = dict(override.get("params") or {})
        for key in ("required_scopes", "execution_required", "confirm_required", "hedge_required", "allow_missing_runtime_context"):
            if key in raw_multi_scope and key not in params and key not in override:
                params[key] = raw_multi_scope.get(key)
        if "execution_required" not in params and "execution_required" not in override and "require_execution_ready" in raw_multi_scope:
            params["execution_required"] = bool(raw_multi_scope.get("require_execution_ready"))
        if "confirm_required" not in params and "confirm_required" not in override and "require_confirm_ready" in raw_multi_scope:
            params["confirm_required"] = bool(raw_multi_scope.get("require_confirm_ready"))
        if "hedge_required" not in params and "hedge_required" not in override and "require_hedge_ready" in raw_multi_scope:
            params["hedge_required"] = bool(raw_multi_scope.get("require_hedge_ready"))
        if params:
            override["params"] = params
        if "enabled" not in override and raw_enabled is not None:
            override["enabled"] = bool(raw_enabled)
        if override:
            gate_overrides["multi_scope_readiness"] = override
    return gate_pipeline, gate_overrides


def _build_lane(raw: dict[str, Any], *, default_role: str, default_idx: int) -> KernelExecutionLane | None:
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    return KernelExecutionLane(
        lane_id=str(raw.get("lane_id") or f"{default_role}_{default_idx}").strip() or f"{default_role}_{default_idx}",
        symbol=symbol,
        side=str(raw.get("side") or "").strip().upper() or None,
        role=str(raw.get("role") or default_role).strip().lower() or default_role,
        close_only_default=bool(raw.get("close_only_default", False)),
        ownership_scope=str(raw.get("ownership_scope") or "strategy").strip() or "strategy",
        flatten_authority=str(raw.get("flatten_authority") or "strategy").strip() or "strategy",
        protection_authority=str(raw.get("protection_authority") or "strategy").strip() or "strategy",
        adopt_authority=str(raw.get("adopt_authority") or "strategy").strip() or "strategy",
        execution_preference=dict(raw.get("execution_preference") or {}),
    )


def _parse_lanes(raw_lanes: list[Any], *, default_role: str) -> tuple[KernelExecutionLane, ...]:
    lanes: list[KernelExecutionLane] = []
    for idx, lane in enumerate(raw_lanes, start=1):
        if not isinstance(lane, dict):
            continue
        built = _build_lane(lane, default_role=default_role, default_idx=idx)
        if built is not None:
            lanes.append(built)
    return tuple(lanes)


@dataclass(frozen=True)
class KernelStrategyConfig:
    strategy_id: str
    order_namespace: str
    data_profile: str
    policy_id: str
    dry_run: bool = True
    startup_preflight_enabled: bool = True
    audit_root_dir: str = "runtime/audit"
    subscribe_bar_sec: int = 5
    gate_fail_mode: str = "fail_closed"
    gate_overrides: Dict[str, Any] = field(default_factory=dict)
    feature_params: Dict[str, Any] = field(default_factory=dict)
    policy_params: Dict[str, Any] = field(default_factory=dict)
    execution_profile: Dict[str, Any] = field(default_factory=dict)
    feature_set: List[str] = field(default_factory=list)
    gate_pipeline: List[str] = field(default_factory=list)
    instruments: List[KernelInstrumentBinding] = field(default_factory=list)
    raw_extras: Dict[str, Any] = field(default_factory=dict)


def load_kernel_config(strategy_id: str, params: StrategyParams) -> KernelStrategyConfig:
    extras = dict(params.extras or {})
    template_registry = default_template_registry()
    template_id = str(extras.get("strategy_template") or "").strip()
    template = template_registry.get(template_id) if template_id else None
    # Safety default: live instances should require exec-symbol snapshots so we don't trade
    # based on signal-leg features only (common source of wrong-symbol orders).
    dry_run = bool(extras.get("dry_run", True))
    if bool(extras.get("shadow_only", False)) and not dry_run:
        raise ValueError("shadow_only=true requires dry_run=true")
    startup_preflight_enabled = bool(extras.get("startup_preflight_enabled", True))
    extras.setdefault("require_exec_symbol_snapshot", (not dry_run) and startup_preflight_enabled)
    raw_universe = list(extras.get("universe") or [])
    if not raw_universe:
        direct_signal_symbol = str(extras.get("signal_symbol") or "").strip()
        direct_long_exec_symbol = str(extras.get("long_exec_symbol") or "").strip()
        direct_short_exec_symbol = str(extras.get("short_exec_symbol") or "").strip()
        if direct_signal_symbol and direct_long_exec_symbol and direct_short_exec_symbol:
            raw_universe = [
                {
                    "signal_symbol": direct_signal_symbol,
                    "long_exec_symbol": direct_long_exec_symbol,
                    "short_exec_symbol": direct_short_exec_symbol,
                    "confirm_symbol": extras.get("confirm_symbol"),
                    "volatility_symbol": extras.get("volatility_symbol"),
                }
            ]
    instruments: list[KernelInstrumentBinding] = []
    for item in raw_universe:
        if not isinstance(item, dict):
            continue
        signal_symbol = str(item.get("signal_symbol") or "").strip().upper()
        execution_lanes = _parse_lanes(list(item.get("execution_lanes") or []), default_role="execution")
        signal_lanes = _parse_lanes(list(item.get("signal_lanes") or []), default_role="signal")
        confirm_lanes = _parse_lanes(list(item.get("confirm_lanes") or []), default_role="confirm")
        protection_lanes = _parse_lanes(list(item.get("protection_lanes") or []), default_role="protection")
        hedge_lanes = _parse_lanes(list(item.get("hedge_lanes") or []), default_role="hedge")
        long_exec_symbol = str(item.get("long_exec_symbol") or signal_symbol).strip().upper()
        short_exec_symbol = str(item.get("short_exec_symbol") or long_exec_symbol).strip().upper()
        if execution_lanes:
            long_lane = next((lane for lane in execution_lanes if (lane.side or "").upper() == "LONG" and lane.role == "execution"), None)
            short_lane = next((lane for lane in execution_lanes if (lane.side or "").upper() == "SHORT" and lane.role == "execution"), None)
            if long_lane is not None:
                long_exec_symbol = long_lane.symbol
            if short_lane is not None:
                short_exec_symbol = short_lane.symbol
        if not signal_symbol or not long_exec_symbol or not short_exec_symbol:
            continue
        instruments.append(
            KernelInstrumentBinding(
                signal_symbol=signal_symbol,
                long_exec_symbol=long_exec_symbol,
                short_exec_symbol=short_exec_symbol,
                confirm_symbol=str(item.get("confirm_symbol") or signal_symbol).strip().upper() or None,
                volatility_symbol=str(item.get("volatility_symbol") or signal_symbol).strip().upper() or None,
                execution_lanes=execution_lanes,
                signal_lanes=signal_lanes,
                confirm_lanes=confirm_lanes,
                protection_lanes=protection_lanes,
                hedge_lanes=hedge_lanes,
                strategy_namespace=str(item.get("strategy_namespace") or strategy_id).strip() or str(strategy_id),
                ownership_scope=str(item.get("ownership_scope") or "strategy").strip() or "strategy",
            )
        )
    gate_pipeline, gate_overrides = _normalize_gate_contract_inputs(extras=extras, template=template)
    return KernelStrategyConfig(
        strategy_id=str(strategy_id),
        order_namespace=str(extras.get("order_namespace") or "stkv1").strip() or "stkv1",
        data_profile=str(extras.get("data_profile") or (template.data_profile if template is not None else "default")).strip() or "default",
        policy_id=str(extras.get("policy_id") or (template.policy_id if template is not None else "qqq_intraday_policy_v1")).strip() or "qqq_intraday_policy_v1",
        dry_run=dry_run,
        startup_preflight_enabled=startup_preflight_enabled,
        audit_root_dir=str(extras.get("audit_root_dir") or "runtime/audit").strip() or "runtime/audit",
        subscribe_bar_sec=int(extras.get("feature_bar_sec") or 5),
        gate_fail_mode=str(extras.get("gate_fail_mode") or "fail_closed").strip() or "fail_closed",
        gate_overrides=gate_overrides,
        feature_params={
            **dict(getattr(template, "feature_params", {}) or {}),
            **dict(extras.get("feature_params") or {}),
        },
        policy_params={
            **dict(getattr(template, "policy_params", {}) or {}),
            **dict(extras.get("policy_params") or {}),
        },
        execution_profile={
            **dict(getattr(template, "execution_profile", {}) or {}),
            **dict(extras.get("execution_profile") or {}),
        },
        feature_set=[
            str(item).strip()
            for item in list(extras.get("feature_set") or (list(template.feature_set) if template is not None else []))
            if str(item).strip()
        ],
        gate_pipeline=gate_pipeline,
        instruments=instruments,
        raw_extras=extras,
    )
