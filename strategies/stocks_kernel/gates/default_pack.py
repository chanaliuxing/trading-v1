from __future__ import annotations

from .data_integrity import DataIntegrityGate
from .direction import DirectionMatchGate
from .foreign_position_isolation import ForeignPositionIsolationGate
from .freshness import FreshnessGate
from .multi_scope_readiness import MultiScopeReadinessGate
from .open_orders_isolation import OpenOrdersIsolationGate
from .registry import GateRegistry, GateSpec
from .risk_budget import RiskBudgetGate
from .seed_ready import SeedReadyGate
from .session_window import SessionWindowGate


def register_builtin_gate_pack(registry: GateRegistry) -> GateRegistry:
    registry.register(
        GateSpec(
            gate_id="session_window",
            default_config={"allowed_sessions": ["RTH", "PRE", "POST", "OVERNIGHT"]},
            factory=lambda cfg: SessionWindowGate(allowed_sessions=list(cfg.get("allowed_sessions") or [])),
        )
    )
    registry.register(GateSpec(gate_id="freshness", factory=lambda cfg: FreshnessGate(), default_config={}))
    registry.register(
        GateSpec(
            gate_id="seed_ready",
            default_config={"required_features": ["vwap", "atr_eff", "v3m"]},
            factory=lambda cfg: SeedReadyGate(required_features=list(cfg.get("required_features") or [])),
        )
    )
    registry.register(
        GateSpec(
            gate_id="direction_match",
            default_config={"feature_id": "v3m", "min_abs_value": 0.0},
            factory=lambda cfg: DirectionMatchGate(
                feature_id=str(cfg.get("feature_id") or "v3m"),
                min_abs_value=float(cfg.get("min_abs_value") or 0.0),
            ),
        )
    )
    registry.register(GateSpec(gate_id="data_integrity", factory=lambda cfg: DataIntegrityGate(), default_config={}))
    registry.register(
        GateSpec(
            gate_id="risk_budget",
            default_config={"max_notional": 1_000_000.0},
            factory=lambda cfg: RiskBudgetGate(max_notional=float(cfg.get("max_notional") or 1_000_000.0)),
        )
    )
    registry.register(GateSpec(gate_id="open_orders_isolation", factory=lambda cfg: OpenOrdersIsolationGate(), default_config={}))
    registry.register(GateSpec(gate_id="foreign_position_isolation", factory=lambda cfg: ForeignPositionIsolationGate(), default_config={}))
    registry.register(
        GateSpec(
            gate_id="multi_scope_readiness",
            default_config={
                "required_scopes": [],
                "execution_required": False,
                "confirm_required": False,
                "hedge_required": False,
                "allow_missing_runtime_context": True,
            },
            factory=lambda cfg: MultiScopeReadinessGate(
                required_scopes=list(cfg.get("required_scopes") or []),
                execution_required=bool(cfg.get("execution_required", False)),
                confirm_required=bool(cfg.get("confirm_required", False)),
                hedge_required=bool(cfg.get("hedge_required", False)),
                allow_missing_runtime_context=bool(cfg.get("allow_missing_runtime_context", True)),
            ),
        )
    )
    return registry


def default_gate_registry() -> GateRegistry:
    return register_builtin_gate_pack(GateRegistry())
