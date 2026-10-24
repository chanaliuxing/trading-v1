from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional

DEFAULT_POLICY_PATH = os.environ.get(
    "RESEARCH_POLICY_PATH",
    "configs/research_policy.json",
)


@dataclass
class DomainObjective:
    weight: float
    risk_aversion: float


@dataclass
class ExperimentBudget:
    daily_compute_hours: float
    max_parallel_experiments: int
    exploration_ratio: float  # 0 ~ 1


@dataclass
class StrategyFocusRules:
    min_data_points_for_eval: int
    min_oos_sharpe_for_scaleup: float


@dataclass
class SafetyLimits:
    max_unreviewed_patches_per_day: int
    forbidden_targets: list


@dataclass
class AutoEvolutionRules:
    allow_kinds: list                 # e.g., ["tool", "data_pipeline", "module"]
    require_human_review_for: list    # e.g., ["data_pipeline", "module:ml_train_core"]
    max_auto_patches_per_day: int
    max_files_touched: int


@dataclass
class ResearchPolicy:
    version: str
    objectives: Dict[str, DomainObjective]
    experiment_budget: ExperimentBudget
    strategy_focus_rules: StrategyFocusRules
    safety_limits: SafetyLimits
    auto_evolution: AutoEvolutionRules
    governance: Dict[str, Any] = field(default_factory=dict)
    other_fields: Dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json_dict(data: Dict[str, Any]) -> "ResearchPolicy":
        objectives = {
            k: DomainObjective(**v)
            for k, v in data.get("objectives", {}).items()
        }
        exp_bdg = ExperimentBudget(**data.get("experiment_budget", {
            "daily_compute_hours": 12.0,
            "max_parallel_experiments": 5,
            "exploration_ratio": 0.2,
        }))
        focus_rules = StrategyFocusRules(**data.get("strategy_focus_rules", {
            "min_data_points_for_eval": 50,
            "min_oos_sharpe_for_scaleup": 0.7,
        }))
        safety = SafetyLimits(**data.get("safety_limits", {
            "max_unreviewed_patches_per_day": 3,
            "forbidden_targets": ["kernel:", "execution:", "risk:"],
        }))
        auto_ev = AutoEvolutionRules(**data.get("auto_evolution", {
            "allow_kinds": ["tool", "data_pipeline", "module"],
            "require_human_review_for": ["data_pipeline", "module:ml_train_core"],
            "max_auto_patches_per_day": 2,
            "max_files_touched": 5,
        }))
        return ResearchPolicy(
            version=data.get("version", "3.0.0"),
            objectives=objectives,
            experiment_budget=exp_bdg,
            strategy_focus_rules=focus_rules,
            safety_limits=safety,
            auto_evolution=auto_ev,
            governance=data.get("governance", {}),
            other_fields=data.get("other_fields", {}),
        )


def load_research_policy(path: str = DEFAULT_POLICY_PATH) -> ResearchPolicy:
    if not os.path.exists(path):
        default = ResearchPolicy(
            version="3.0.0",
            objectives={
                "trading": DomainObjective(weight=0.6, risk_aversion=0.8),
                "desktop": DomainObjective(weight=0.3, risk_aversion=0.3),
                "legal":   DomainObjective(weight=0.1, risk_aversion=0.9),
            },
            experiment_budget=ExperimentBudget(
                daily_compute_hours=12.0,
                max_parallel_experiments=5,
                exploration_ratio=0.2,
            ),
            strategy_focus_rules=StrategyFocusRules(
                min_data_points_for_eval=50,
                min_oos_sharpe_for_scaleup=0.7,
            ),
            safety_limits=SafetyLimits(
                max_unreviewed_patches_per_day=3,
                forbidden_targets=["kernel:", "execution:", "risk:"],
            ),
            auto_evolution=AutoEvolutionRules(
                allow_kinds=["tool", "data_pipeline", "module"],
                require_human_review_for=["data_pipeline", "module:ml_train_core"],
                max_auto_patches_per_day=2,
                max_files_touched=5,
            ),
        )
        save_research_policy(default, path)
        return default

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return ResearchPolicy.from_json_dict(data)


def save_research_policy(policy: ResearchPolicy, path: str = DEFAULT_POLICY_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(policy.to_json_dict(), f, indent=2, ensure_ascii=False)
