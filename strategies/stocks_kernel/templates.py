from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StrategyTemplate:
    template_id: str
    data_profile: str
    policy_id: str
    category: str = "equity"
    description: str = ""
    feature_set: tuple[str, ...] = ()
    gate_pipeline: tuple[str, ...] = ()
    feature_params: dict[str, Any] = field(default_factory=dict)
    gate_overrides: dict[str, Any] = field(default_factory=dict)
    policy_params: dict[str, Any] = field(default_factory=dict)
    execution_profile: dict[str, Any] = field(default_factory=dict)


class StrategyTemplateRegistry:
    def __init__(self) -> None:
        self._templates: dict[str, StrategyTemplate] = {}

    def register(self, template: StrategyTemplate) -> None:
        template_id = str(template.template_id or "").strip()
        if not template_id:
            raise ValueError("template_id is required")
        self._templates[template_id] = template

    def get(self, template_id: str) -> StrategyTemplate:
        key = str(template_id or "").strip()
        if key not in self._templates:
            raise KeyError(f"unknown strategy template: {key}")
        return self._templates[key]

    def known_template_ids(self) -> list[str]:
        return sorted(self._templates.keys())

    def catalog(self) -> list[dict[str, object]]:
        return [
            {
                "template_id": template.template_id,
                "category": template.category,
                "description": template.description,
                "data_profile": template.data_profile,
                "policy_id": template.policy_id,
                "feature_set": list(template.feature_set),
                "gate_pipeline": list(template.gate_pipeline),
            }
            for template in sorted(self._templates.values(), key=lambda item: item.template_id)
        ]


def default_template_registry() -> StrategyTemplateRegistry:
    registry = StrategyTemplateRegistry()
    registry.register(
        StrategyTemplate(
            template_id="qqq_intraday_core",
            data_profile="equity_rth_standard",
            policy_id="qqq_intraday_policy_v1",
            category="intraday",
            description="RTH intraday trend-following template for leveraged long/short ETF execution.",
            feature_set=("vwap", "atr_eff", "v3m"),
            policy_params={"rule_engine_parity": True, "entry_model": "baseline", "qqew_decoupled": True},
            gate_pipeline=(
                "session_window",
                "freshness",
                "seed_ready",
                "direction_match",
                "data_integrity",
                "open_orders_isolation",
                "foreign_position_isolation",
            ),
        )
    )
    registry.register(
        StrategyTemplate(
            template_id="spy_intraday_core",
            data_profile="equity_rth_standard",
            policy_id="spy_intraday_policy_v1",
            category="intraday",
            description="SPY intraday parity template cloned from the QQQ stock logic with symbol-specific binding only.",
            feature_set=("vwap", "atr_eff", "v3m"),
            policy_params={
                "rule_engine_parity": True,
                "entry_model": "baseline",
                "qqew_decoupled": False,
                "confirm_decoupled": False,
                "confirm_feature_prefix": "rsp",
            },
            gate_pipeline=(
                "session_window",
                "freshness",
                "seed_ready",
                "direction_match",
                "data_integrity",
                "open_orders_isolation",
                "foreign_position_isolation",
            ),
        )
    )
    registry.register(
        StrategyTemplate(
            template_id="trend_follow_equity",
            data_profile="default",
            policy_id="qqq_intraday_policy_v1",
            category="intraday",
            description="Generic intraday trend-following equity template with VWAP distance gating.",
            feature_set=("vwap", "atr_eff", "v3m", "distance_from_vwap_atr"),
            gate_pipeline=(
                "session_window",
                "freshness",
                "seed_ready",
                "direction_match",
                "data_integrity",
                "open_orders_isolation",
                "foreign_position_isolation",
                "risk_budget",
            ),
            feature_params={"distance_from_vwap_atr": {"window": 14}},
        )
    )
    registry.register(
        StrategyTemplate(
            template_id="offhours_equity",
            data_profile="equity_offhours_standard",
            policy_id="qqq_intraday_policy_v1",
            category="offhours",
            description="Off-hours equity template for premarket, post-market, and overnight sessions.",
            feature_set=("vwap", "atr_eff", "v3m"),
            gate_pipeline=(
                "session_window",
                "freshness",
                "seed_ready",
                "direction_match",
                "data_integrity",
                "open_orders_isolation",
                "foreign_position_isolation",
            ),
            gate_overrides={"session_window": {"params": {"allowed_sessions": ["PRE", "POST", "OVERNIGHT"]}}},
        )
    )
    registry.register(
        StrategyTemplate(
            template_id="breakout_equity",
            data_profile="equity_rth_standard",
            policy_id="qqq_intraday_policy_v1",
            category="intraday",
            description="Breakout-oriented equity template with volatility and distance filters.",
            feature_set=("vwap", "atr_eff", "v3m", "vol_surge", "distance_from_vwap_atr"),
            policy_params={
                "rule_engine_parity": True,
                "entry_model": "baseline",
                "qqew_decoupled": True,
                "entry_check_on_new_minute_only": True,
            },
            gate_pipeline=(
                "session_window",
                "freshness",
                "seed_ready",
                "direction_match",
                "data_integrity",
                "open_orders_isolation",
                "foreign_position_isolation",
                "risk_budget",
            ),
            feature_params={"distance_from_vwap_atr": {"window": 20}, "vol_surge": {"window": 8}},
        )
    )
    registry.register(
        StrategyTemplate(
            template_id="mean_revert_equity",
            data_profile="equity_rth_standard",
            policy_id="qqq_intraday_policy_v1",
            category="intraday",
            description="Mean-reversion equity template with VWAP distance and position-in-day context.",
            feature_set=("vwap", "atr_eff", "distance_from_vwap_atr", "pos_day"),
            gate_pipeline=(
                "session_window",
                "freshness",
                "seed_ready",
                "data_integrity",
                "open_orders_isolation",
                "foreign_position_isolation",
                "risk_budget",
            ),
            feature_params={"distance_from_vwap_atr": {"window": 10}},
        )
    )
    return registry
