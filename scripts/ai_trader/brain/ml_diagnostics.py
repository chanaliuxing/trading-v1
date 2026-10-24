"""
ML Diagnostics Module for AI Brain

Provides intelligent diagnosis and recommendations based on ML health metrics.
"""

from typing import Dict, List, Any
import structlog

logger = structlog.get_logger()


class MLDiagnostics:
    """
    Diagnose ML system health and generate actionable recommendations.
    """

    def __init__(self, config: Dict[str, Any] = None):
        """
        Initialize diagnostics with configuration.

        Args:
            config: Optional config dict with thresholds:
                {
                    "model_layer": {
                        "ic_min": 0.20,
                        "ic_target": 0.30,
                        "ir_min": 0.30,
                        "coverage_min": 0.80
                    },
                    "strategy_layer": {
                        "sharpe_min": 1.0,
                        "sharpe_target": 1.5,
                        "mdd_max": -0.15
                    }
                }
        """
        self.config = config or self._default_config()

    def _default_config(self) -> Dict[str, Any]:
        """Default thresholds for diagnostics."""
        return {
            "model_layer": {
                "ic_min": 0.20,
                "ic_target": 0.30,
                "ir_min": 0.30,
                "coverage_min": 0.80
            },
            "strategy_layer": {
                "sharpe_min": 1.0,
                "sharpe_target": 1.5,
                "mdd_max": -0.15
            }
        }

    def diagnose(self, ml_health: Dict[str, Any]) -> Dict[str, Any]:
        """
        Diagnose ML system health and generate recommendations.

        Args:
            ml_health: Dict containing:
                {
                    "model_layer": {"ic": float, "ir": float, "coverage": float, "status": str},
                    "strategy_layer": {"sharpe": float, "mdd": float, "status": str},
                    "ready_for_trading": bool
                }

        Returns:
            {
                "diagnosis": str,  # "model_layer_weak" | "strategy_layer_suboptimal" | "healthy"
                "severity": str,   # "critical" | "warning" | "info"
                "next_actions": [
                    {
                        "action": str,
                        "params": dict,
                        "reasoning": str,
                        "priority": int  # 1=high, 2=medium, 3=low
                    }
                ],
                "summary": str
            }
        """
        model = ml_health.get("model_layer", {})
        strategy = ml_health.get("strategy_layer", {})

        ic = model.get("ic", 0.0)
        ir = model.get("ir", 0.0)
        coverage = model.get("coverage", 0.0)
        sharpe = strategy.get("sharpe", 0.0)
        mdd = strategy.get("mdd", 0.0)

        thresholds = self.config
        ic_min = thresholds["model_layer"]["ic_min"]
        ic_target = thresholds["model_layer"]["ic_target"]
        sharpe_min = thresholds["strategy_layer"]["sharpe_min"]

        actions = []
        diagnosis = "unknown"
        severity = "info"

        # Diagnosis logic
        if ic < ic_min:
            diagnosis = "model_layer_weak"
            severity = "critical" if ic < 0.10 else "warning"
            actions.extend(self._model_layer_actions(ic, ir, coverage, thresholds))

        elif ic >= ic_min and sharpe < sharpe_min:
            diagnosis = "strategy_layer_suboptimal"
            severity = "warning"
            actions.extend(self._strategy_layer_actions(ic, sharpe, mdd, thresholds))

        elif ic >= ic_target and sharpe >= thresholds["strategy_layer"]["sharpe_target"]:
            diagnosis = "healthy"
            severity = "info"
            actions.append({
                "action": "monitor",
                "params": {},
                "reasoning": f"IC={ic:.3f} and Sharpe={sharpe:.2f} are within target range. Continue monitoring.",
                "priority": 3
            })

        else:
            # IC is OK but not great, Sharpe is OK but not great
            diagnosis = "acceptable"
            severity = "info"
            actions.append({
                "action": "incremental_improve",
                "params": {"focus": "both"},
                "reasoning": f"IC={ic:.3f} and Sharpe={sharpe:.2f} are acceptable. Consider incremental improvements.",
                "priority": 2
            })

        # Sort actions by priority
        actions.sort(key=lambda x: x["priority"])

        # Generate summary
        summary = self._generate_summary(diagnosis, ic, sharpe, severity)

        return {
            "diagnosis": diagnosis,
            "severity": severity,
            "next_actions": actions,
            "summary": summary
        }

    def _model_layer_actions(
        self, ic: float, ir: float, coverage: float, thresholds: Dict
    ) -> List[Dict[str, Any]]:
        """Generate actions for Model Layer issues."""
        actions = []

        if ic < 0.10:
            # Critical: IC is near zero
            actions.append({
                "action": "feature_build",
                "params": {
                    "feature_types": ["microstructure", "cross_asset"],
                    "priority": "high",
                    "suggestions": [
                        "orderbook: bid_ask_spread, order_imbalance",
                        "cross_asset: SPY_ret_5m, VIX_change, QQQ_ret_5m"
                    ]
                },
                "reasoning": f"IC={ic:.3f} is critically low (<0.10). "
                             "Current technical indicators have no predictive power. "
                             "Need microstructure (orderbook) and cross-asset features.",
                "priority": 1
            })

            actions.append({
                "action": "change_prediction_task",
                "params": {
                    "horizon_minutes": 30,
                    "comment": "Longer horizon may be more predictable than 5min"
                },
                "reasoning": f"IC={ic:.3f} suggests 5-minute prediction is too noisy. "
                             "Try 30-minute or 1-hour prediction horizon.",
                "priority": 2
            })

        elif ic < 0.20:
            # Warning: IC below minimum threshold
            actions.append({
                "action": "run_hpo_cli",
                "params": {
                    "objective_metric": "ic",
                    "n_trials": 30,
                    "backend": "optuna-tpe",
                    "comment": "Optimize model-level parameters"
                },
                "reasoning": f"IC={ic:.3f} is below threshold {thresholds['model_layer']['ic_min']:.2f}. "
                             "Try optimizing model hyperparameters with IC objective.",
                "priority": 1
            })

            actions.append({
                "action": "feature_build",
                "params": {
                    "feature_types": ["cross_asset"],
                    "priority": "medium"
                },
                "reasoning": f"IC={ic:.3f} is low. Add SPY/VIX cross-asset features to improve model predictive power.",
                "priority": 2
            })

        # Coverage issue
        if coverage < thresholds["model_layer"]["coverage_min"]:
            actions.append({
                "action": "investigate_coverage",
                "params": {
                    "current_coverage": coverage,
                    "target_coverage": thresholds["model_layer"]["coverage_min"]
                },
                "reasoning": f"Coverage={coverage:.2%} is below target. "
                             "Many bars lack ML predictions. Check OOF data availability.",
                "priority": 2
            })

        return actions

    def _strategy_layer_actions(
        self, ic: float, sharpe: float, mdd: float, thresholds: Dict
    ) -> List[Dict[str, Any]]:
        """Generate actions for Strategy Layer issues."""
        actions = []

        if sharpe < thresholds["strategy_layer"]["sharpe_min"]:
            actions.append({
                "action": "alpha_loop",
                "params": {
                    "provider": "coop_optuna",
                    "gens": 2,
                    "n_trials_per_gen": 20,
                    "comment": "Optimize strategy parameters"
                },
                "reasoning": f"IC={ic:.3f} is adequate but Sharpe={sharpe:.2f} is below target. "
                             "Run alpha_loop to optimize position sizing and risk parameters.",
                "priority": 1
            })

            # If MDD is large, suggest tighter risk controls
            if abs(mdd) > abs(thresholds["strategy_layer"]["mdd_max"]):
                actions.append({
                    "action": "adjust_risk_controls",
                    "params": {
                        "stop_loss_pct": 0.02,  # Tighter stop loss
                        "conf_threshold": 0.7,  # Higher confidence threshold
                        "comment": "Reduce drawdown"
                    },
                    "reasoning": f"MaxDD={mdd:.2%} is excessive. Tighten stop loss and confidence threshold.",
                    "priority": 1
                })

        return actions

    def _generate_summary(self, diagnosis: str, ic: float, sharpe: float, severity: str) -> str:
        """Generate human-readable summary."""
        if diagnosis == "model_layer_weak":
            if ic < 0.10:
                return (f"🔴 CRITICAL: Model Layer has no predictive power (IC={ic:.3f}). "
                        "Features are ineffective. Immediate action required: add microstructure/cross-asset features.")
            else:
                return (f"⚠️  WARNING: Model Layer predictive power is low (IC={ic:.3f}, target=0.20). "
                        "Recommend feature engineering or model optimization.")

        elif diagnosis == "strategy_layer_suboptimal":
            return (f"⚠️  WARNING: Model predictions are OK (IC={ic:.3f}) but strategy performance is poor "
                    f"(Sharpe={sharpe:.2f}, target=1.0). Recommend optimizing strategy parameters.")

        elif diagnosis == "healthy":
            return f"✅ HEALTHY: IC={ic:.3f} and Sharpe={sharpe:.2f} are within target range. System performing well."

        elif diagnosis == "acceptable":
            return f"ℹ️  ACCEPTABLE: IC={ic:.3f} and Sharpe={sharpe:.2f} are acceptable but can be improved."

        else:
            return f"❓ UNKNOWN: Unable to diagnose system health."


def review_ml_health(ml_health: Dict[str, Any], config: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Convenience function for reviewing ML health.

    Args:
        ml_health: ML health metrics dict
        config: Optional config with thresholds

    Returns:
        Diagnosis result dict
    """
    diagnostics = MLDiagnostics(config)
    return diagnostics.diagnose(ml_health)
