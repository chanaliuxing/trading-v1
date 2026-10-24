#!/usr/bin/env python3
"""
Evolution Governance Module (v3.5 Autonomy with Safety Guardrails)

This module implements governance controls to prevent autonomous evolution
from becoming an "uncontrolled monster". It enforces:

1. Policy-defined permissions (allowed_modules, forbidden_modules)
2. Daily rate limits (max_patches_per_day)
3. Quality gates (test coverage requirements)
4. Predictive immunity integration (freeze when risk is high)

Design Philosophy:
- Deny by default (must explicitly allow)
- Layer multiple safety checks (defense in depth)
- Transparent logging (all decisions audited)
- Graceful degradation (fallback to safe mode on errors)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Literal
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


@dataclass
class GovernanceDecision:
    """Result of a governance check"""
    allowed: bool
    reason: str
    gate_triggered: Optional[str] = None  # Which gate blocked the operation
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class EvolutionGovernance:
    """
    Governance layer for autonomous evolution.

    Checks:
    1. Config switch (features.v3_autonomy.enable_policy_self_application)
    2. Policy permissions (governance.allow_code_evolution)
    3. Module whitelist/blacklist
    4. Daily rate limits
    5. Test coverage requirements
    6. Predictive immunity (system health risk)
    """

    def __init__(self, config: Optional[Dict] = None, policy: Optional[Dict] = None):
        """
        Initialize governance.

        Args:
            config: config.yml loaded as dict (or load from file if None)
            policy: research_policy.json loaded as dict (or load from file if None)
        """
        provided_config = config is not None
        provided_policy = policy is not None
        self.config = config or self._load_config()
        self.policy = policy or self._load_policy()

        # Load feature flags
        self.v3_autonomy = self.config.get("features", {}).get("v3_autonomy", {})

        # Load governance rules from policy
        raw_governance = self.policy.get("governance", {})
        if isinstance(raw_governance, dict):
            self.governance = raw_governance
        else:
            # Backward compatibility: some legacy policy files stored governance as string.
            logger.warning(
                "invalid_governance_config_type",
                extra={"type": type(raw_governance).__name__}
            )
            self.governance = {}

        # Load rate limit tracking
        # Use isolated rate-limit file when caller injects config/policy (typical in unit tests)
        # to avoid leaking persisted daily counts across independent runs.
        if provided_config or provided_policy:
            self.rate_limit_file = Path(f"/tmp/governance_daily_limits_{id(self)}.jsonl")
        else:
            self.rate_limit_file = Path("runs/evolution/governance_daily_limits.jsonl")
        self.rate_limit_file.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "governance_initialized",
            extra={
                "policy_updates_allowed": self.governance.get("allow_policy_updates"),
                "code_evolution_allowed": self.governance.get("allow_code_evolution"),
                "max_patches_per_day": self.governance.get("max_patches_per_day"),
            }
        )

    def _load_config(self) -> Dict:
        """Load config.yml"""
        import yaml
        config_path = Path("config.yml")

        if config_path.exists():
            with open(config_path) as f:
                return yaml.safe_load(f)

        logger.warning("config.yml not found, using empty config")
        return {}

    def _load_policy(self) -> Dict:
        """Load research_policy.json"""
        policy_path = Path("configs/research_policy.json")

        if policy_path.exists():
            with open(policy_path) as f:
                return json.load(f)

        logger.warning("research_policy.json not found, using empty policy")
        return {}

    def check_code_evolution_allowed(
        self,
        target_module: str,
        experiment_type: Literal[
            "tool_patch", "data_pipeline_patch", "module_patch",
            "code_patch", "policy_patch"
        ],
        test_coverage_pct: Optional[float] = None,
        metadata: Optional[Dict] = None
    ) -> GovernanceDecision:
        """
        Check if code evolution is allowed for a given target.

        Args:
            target_module: Module path (e.g., "libs/skills/generated/", "tools/data_quality")
            experiment_type: Type of evolution experiment
            test_coverage_pct: Optional test coverage percentage (0-100)
            metadata: Optional metadata about the change

        Returns:
            GovernanceDecision (allowed=True/False with reason)
        """
        metadata = metadata or {}

        # Gate 1: Global kill switch
        if not self.v3_autonomy.get("enable_policy_self_application", False):
            return GovernanceDecision(
                allowed=False,
                reason="🔒 Autonomy disabled by config (features.v3_autonomy.enable_policy_self_application=false)",
                gate_triggered="global_kill_switch",
                metadata={"config_key": "features.v3_autonomy.enable_policy_self_application"}
            )

        # Gate 2: Policy-level code evolution permission
        if not self.governance.get("allow_code_evolution", False):
            return GovernanceDecision(
                allowed=False,
                reason="🔒 Code evolution disabled by policy (governance.allow_code_evolution=false)",
                gate_triggered="policy_permission",
                metadata={"policy_key": "governance.allow_code_evolution"}
            )

        # Gate 3: Policy updates (special case)
        if experiment_type == "policy_patch":
            if not self.governance.get("allow_policy_updates", False):
                return GovernanceDecision(
                    allowed=False,
                    reason="🔒 Policy updates disabled by governance (governance.allow_policy_updates=false)",
                    gate_triggered="policy_updates_disabled",
                    metadata={"policy_key": "governance.allow_policy_updates"}
                )

        # Gate 4: Module whitelist/blacklist
        allowed_modules = self.governance.get("allowed_modules", [])
        forbidden_modules = self.governance.get("forbidden_modules", [])

        # Check forbidden list (deny takes precedence)
        for forbidden in forbidden_modules:
            if target_module.startswith(forbidden):
                return GovernanceDecision(
                    allowed=False,
                    reason=f"🚫 Module '{target_module}' is in forbidden list (matches '{forbidden}')",
                    gate_triggered="forbidden_module",
                    metadata={"matched_pattern": forbidden}
                )

        # Check allowed list (if non-empty, must match)
        if allowed_modules:
            is_allowed = any(target_module.startswith(allowed) for allowed in allowed_modules)
            if not is_allowed:
                return GovernanceDecision(
                    allowed=False,
                    reason=f"⚠️ Module '{target_module}' not in allowed list",
                    gate_triggered="not_in_whitelist",
                    metadata={"allowed_modules": allowed_modules}
                )

        # Gate 5: Test coverage gate
        min_coverage = self.v3_autonomy.get("auto_merge_min_test_coverage_pct", 85)
        if test_coverage_pct is not None and test_coverage_pct < min_coverage:
            return GovernanceDecision(
                allowed=False,
                reason=f"📊 Test coverage too low: {test_coverage_pct:.1f}% < {min_coverage}% (auto_merge_min_test_coverage_pct)",
                gate_triggered="test_coverage_gate",
                metadata={
                    "actual_coverage": test_coverage_pct,
                    "required_coverage": min_coverage
                }
            )

        # Gate 6: Daily rate limits
        max_patches = self.governance.get("max_patches_per_day", 3)
        daily_count = self._get_daily_patch_count()

        if daily_count >= max_patches:
            return GovernanceDecision(
                allowed=False,
                reason=f"⏱️ Daily patch limit reached: {daily_count}/{max_patches} (governance.max_patches_per_day)",
                gate_triggered="rate_limit",
                metadata={
                    "daily_count": daily_count,
                    "max_allowed": max_patches
                }
            )

        # Gate 7: Predictive immunity (system health risk)
        risk_check = self._check_system_risk()
        if not risk_check.allowed:
            return risk_check

        # All gates passed
        return GovernanceDecision(
            allowed=True,
            reason=f"✅ All governance checks passed for '{target_module}' ({experiment_type})",
            metadata={
                "target_module": target_module,
                "experiment_type": experiment_type,
                "test_coverage_pct": test_coverage_pct,
                "daily_patches_used": daily_count,
                "max_patches_per_day": max_patches
            }
        )

    def _get_daily_patch_count(self) -> int:
        """Count patches submitted today"""
        today = datetime.now(timezone.utc).date()

        if not self.rate_limit_file.exists():
            return 0

        count = 0
        with open(self.rate_limit_file, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    entry_date = datetime.fromisoformat(entry["timestamp"]).date()

                    if entry_date == today and entry.get("action") == "patch_submitted":
                        count += 1
                except Exception:
                    continue

        return count

    def record_patch_submission(self, metadata: Dict[str, Any]):
        """Record a patch submission for rate limiting"""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "patch_submitted",
            "metadata": metadata
        }

        with open(self.rate_limit_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        logger.info(
            "governance_patch_recorded",
            extra={
                "daily_count": self._get_daily_patch_count(),
                "metadata": metadata
            }
        )

    def _check_system_risk(self) -> GovernanceDecision:
        """
        Check predictive immunity system.

        Returns:
            GovernanceDecision (allowed=False if risk is too high)
        """
        if not self.config.get("immunity", {}).get("enable_predictive_immunity", True):
            # Predictive immunity disabled, allow by default
            return GovernanceDecision(
                allowed=True,
                reason="Predictive immunity disabled, skipping risk check"
            )

        try:
            from libs.immune.risk_predictor import get_risk_predictor_from_config

            predictor = get_risk_predictor_from_config(self.config)

            # Check if evolution should be frozen
            if predictor.should_freeze_evolution():
                risk = predictor.predict_risk_score()
                return GovernanceDecision(
                    allowed=False,
                    reason=f"🚫 System risk too high: {risk.overall_score:.2f} >= freeze_evolution threshold. Recommendations: {', '.join(risk.recommendations)}",
                    gate_triggered="predictive_immunity",
                    metadata={
                        "risk_score": risk.overall_score,
                        "risk_level": risk.level,
                        "risk_components": risk.components,
                        "recommendations": risk.recommendations
                    }
                )

            # Check if ALL operations should be frozen (emergency)
            if predictor.should_freeze_all():
                risk = predictor.predict_risk_score()
                return GovernanceDecision(
                    allowed=False,
                    reason=f"🛑 EMERGENCY FREEZE: System risk critical ({risk.overall_score:.2f}). Manual intervention required.",
                    gate_triggered="emergency_freeze",
                    metadata={
                        "risk_score": risk.overall_score,
                        "risk_level": risk.level,
                        "risk_components": risk.components,
                        "recommendations": risk.recommendations
                    }
                )

            # Risk acceptable
            risk = predictor.predict_risk_score()
            return GovernanceDecision(
                allowed=True,
                reason=f"✅ System risk acceptable: {risk.overall_score:.2f}",
                metadata={"risk_score": risk.overall_score, "risk_level": risk.level}
            )

        except Exception as e:
            logger.error(f"Predictive immunity check failed: {e}", exc_info=True)
            # Fail-safe: deny on error
            return GovernanceDecision(
                allowed=False,
                reason=f"⚠️ Predictive immunity check failed: {e}. Denying for safety.",
                gate_triggered="immunity_check_error"
            )

    def get_governance_summary(self) -> Dict[str, Any]:
        """Get current governance status summary"""
        return {
            "autonomy_enabled": self.v3_autonomy.get("enable_policy_self_application", False),
            "code_evolution_allowed": self.governance.get("allow_code_evolution", False),
            "policy_updates_allowed": self.governance.get("allow_policy_updates", False),
            "rate_limits": {
                "max_patches_per_day": self.governance.get("max_patches_per_day", 3),
                "current_daily_count": self._get_daily_patch_count()
            },
            "quality_gates": {
                "min_test_coverage_pct": self.v3_autonomy.get("auto_merge_min_test_coverage_pct", 85)
            },
            "allowed_modules": self.governance.get("allowed_modules", []),
            "forbidden_modules": self.governance.get("forbidden_modules", []),
            "predictive_immunity_enabled": self.config.get("immunity", {}).get("enable_predictive_immunity", True)
        }


def check_evolution_governance(
    target_module: str,
    experiment_type: str,
    test_coverage_pct: Optional[float] = None,
    config: Optional[Dict] = None,
    policy: Optional[Dict] = None
) -> GovernanceDecision:
    """
    Convenience function to check governance.

    Example:
        >>> decision = check_evolution_governance(
        ...     target_module="libs/skills/generated/risk/",
        ...     experiment_type="tool_patch",
        ...     test_coverage_pct=92.5
        ... )
        >>> if decision.allowed:
        ...     # Proceed with evolution
        ...     pass
        >>> else:
        ...     print(decision.reason)
    """
    governance = EvolutionGovernance(config=config, policy=policy)
    return governance.check_code_evolution_allowed(
        target_module=target_module,
        experiment_type=experiment_type,
        test_coverage_pct=test_coverage_pct
    )
