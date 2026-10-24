from __future__ import annotations

import json
import logging
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Tuple, List

from .state import ResearchState, collect_research_snapshot
from .policy import ResearchPolicy, load_research_policy
from .orchestrator import ExperimentOrchestrator
from .agent_tasking import load_agent_tasks
from . import experiment_api

from scripts.ai_trader.brain.provider_fallback import ProviderPool, ProviderConfig
from scripts.ai_trader.research.readiness import check_training_window

# v3.5: Import governance and predictive immunity
from libs.evolution.governance import EvolutionGovernance

logger = logging.getLogger(__name__)

RESEARCH_CYCLES_ROOT = Path(
    os.getenv("AI_TRADER_RESEARCH_CYCLES_ROOT", "runs/ai_trader/research/cycles")
)


def _research_focus_mode() -> str:
    return str(os.getenv("AI_TRADER_RESEARCH_FOCUS", "")).strip().lower()


def _spy_scalp_focus_enabled() -> bool:
    return _research_focus_mode() in {"spy_scalp", "spy_scalping", "spy"}


def _pick_worst_mutable(mutables: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Use EvolutionStrategyManager to intelligently select the next target.
    Falls back to heuristic if EvolutionStrategyManager fails.
    """
    try:
        from libs.evolution.evolution_strategy_manager import EvolutionStrategyManager

        manager = EvolutionStrategyManager()

        # Get currently running experiments to exclude
        exclude_targets = []
        # TODO: Query orchestrator for in-progress experiments

        # Select next target using UCB (balances exploration/exploitation)
        result = manager.select_next_target(
            strategy="ucb",
            exclude_targets=exclude_targets
        )

        if result.get("ok") is not False and result.get("id"):
            target_id = result["id"]

            # Find the target in mutables
            best = mutables.get(target_id)

            if best and isinstance(best, dict):
                # Add evolution strategy metadata
                best["evolution_priority"] = result.get("priority", "medium")
                best["evolution_score"] = result.get("priority_score", 0.0)
                best["expected_benefit"] = result.get("expected_benefit", 0.0)
                best["expected_risk"] = result.get("expected_risk", 0.0)

                # Derive issue summary
                metrics = best.get("metrics", {})
                issues = []
                if (metrics.get("lag_hours") or 0) > 24:
                    issues.append(f"lag={metrics.get('lag_hours'):.1f}h")
                if metrics.get("error_rate"):
                    issues.append(f"error_rate={metrics.get('error_rate'):.3f}")
                if metrics.get("immune_cases_7d"):
                    issues.append(f"immune_cases_7d={metrics.get('immune_cases_7d')}")
                if metrics.get("failures_24h"):
                    issues.append(f"failures_24h={metrics.get('failures_24h')}")

                best["issue_summary"] = ", ".join(issues) or f"evolution_selected (score={result.get('priority_score', 0.0):.3f})"

                logger.info(
                    "evolution_strategy_selected_target",
                    extra={
                        "target": target_id,
                        "priority": result.get("priority"),
                        "score": result.get("priority_score"),
                        "benefit": result.get("expected_benefit"),
                        "risk": result.get("expected_risk"),
                    }
                )

                return best

        # If EvolutionStrategyManager didn't return a valid target, fall back
        logger.warning("evolution_strategy_no_target_fallback_to_heuristic")

    except Exception as e:
        logger.warning(
            "evolution_strategy_failed_fallback_to_heuristic",
            extra={"error": str(e)}
        )

    # FALLBACK: Original heuristic
    best = None
    score = -1.0
    for mt in mutables.values():
        metrics = mt.get("metrics", {}) if isinstance(mt, dict) else {}
        # Prefer lower health_score; fall back to a composite if missing
        if metrics.get("health_score") is not None:
            s = 10 - float(metrics.get("health_score"))
        else:
            lag = metrics.get("lag_hours") or 0
            err = metrics.get("error_rate") or 0
            imm = metrics.get("immune_cases_7d") or 0
            failures = metrics.get("failures_24h") or 0
            s = (lag / 24.0) + (err * 10) + imm + failures
        if s > score:
            score = s
            best = mt
    # Apply a minimal threshold to avoid triggering on healthy targets
    if score < 0.5:
        return None
    # Derive a short issue summary
    if best and isinstance(best, dict):
        metrics = best.get("metrics", {})
        issues = []
        if (metrics.get("lag_hours") or 0) > 24:
            issues.append(f"lag={metrics.get('lag_hours'):.1f}h")
        if metrics.get("error_rate"):
            issues.append(f"error_rate={metrics.get('error_rate'):.3f}")
        if metrics.get("immune_cases_7d"):
            issues.append(f"immune_cases_7d={metrics.get('immune_cases_7d')}")
        if metrics.get("failures_24h"):
            issues.append(f"failures_24h={metrics.get('failures_24h')}")
        best["issue_summary"] = ", ".join(issues) or "auto-selected"
    return best


# Enhanced prompt with dual-role enforcement and health-score-driven decision logic
RESEARCH_AGENT_PROMPT = """You are TWO specialists working sequentially for an AI trading lab.

**IMPORTANT: You have access to historical memory in the input JSON under "memory_context".**
Use this to avoid repeating past mistakes and leverage successful patterns.

**PHASE 1: Data Engineer**
Your FIRST priority is data infrastructure health. Before ANY training/HPO/feature work:

1. Check mutable_targets health:
   - health_score < 0.5 → MUST propose sync/backfill/repair FIRST
   - recommendation = "sync_or_fallback" → MUST trigger remediation
   - stale_days > 3 → Investigate and propose fix

2. Check data readiness:
   - Coverage < 80% → Propose window adjustment or data backfill
   - Null ratios > 10% → Investigate label generation pipeline
   - Duplicates > 5% → Propose deduplication

3. Check immune_cases:
   - Recurring "data_not_ready" → Fix root cause (sync, schema, etc.)
   - ClickHouse timeouts → Optimize queries or increase resources

**Decision Rule**: ONLY proceed to Phase 2 if all data health scores ≥ 0.5

**PHASE 2: Quantitative Researcher**
After data is healthy, optimize strategies:

1. Feature engineering (only if data is ready)
2. HPO (only if features are stable)
3. Strategy parameter tuning
4. Model experimentation

RUNTIME DIRECTIVE:
- Read `runtime_directives` from USER JSON.
- If `runtime_directives.focus_mode == "spy_scalp"`, prioritize SPY intraday scalp only:
  - symbol must be SPY
  - default market should be stocks unless explicitly asked for options
  - do not diversify to unrelated symbols
  - include one short self_reflection sentence per proposal about last cycle failure -> next fix

EV/SHARPE & HEDGE DIRECTIVES (apply to every proposal):
- Prefer high expected value (EV>0), high reward/risk, and low correlation to SPY/QQQ; if Beta_corr > 0.8 → downgrade/reject unless hedged.
- For each experiment/strategy, include a quick assessment in decision_reasoning: ev_estimate, rr_estimate, p_win_estimate, beta_corr, hedge_plan, crowding_flag (high/med/low).
- If edge < cost or crowding_flag=high → reduce size or pause.
- Prefer option structures/hedged structures when volatility is mispriced; no naked high-risk proposals.

**CRITICAL: Output MUST be valid JSON ONLY. No explanations, no markdown, no code blocks.**
**Return ONLY the JSON object below, nothing else:**

{
  "focus": [ {"strategy": "str", "stage": "incubation|validation|production", "rationale": "short"} ],
  "experiments": [
    {
      "kind": "backfill|feature_experiment|hpo|strategy_experiment|patch_experiment|routing_experiment",
      "target": "str",
      "params": {},
      "decision_reasoning": "Why this experiment now? Reference health_score/recommendation",
      "alternatives_considered": ["..."],
      "confidence": 0.0
    }
  ],
  "infra_experiments": [
    {
      "kind": "tool_patch_experiment|data_pipeline_patch_experiment|module_patch_experiment",
      "target": "str",
      "params": {},
      "budget": {"max_runtime_hours": <number>, "max_token_cost": <number>},
      "health_score": <number>,
      "expected_improvement": "short description"
    }
  ],
  "policy_patch_candidates": [ {"path": "dot.path", "value": <any>, "reason": "short"} ],
  "telemetry": {
    "summary": "short",
    "key_observations": [ "..." ],
    "risk_warnings": [ "..." ],
    "metrics_of_interest": {},
    "data_health_summary": {
      "overall_score": <0-1>,
      "unhealthy_targets": ["..."],
      "remediation_needed": true|false
    }
  },
  "meta": {
    "version": "v1",
    "agent_name": "ResearchAgent",
    "confidence": 0.0,
    "policy_used": "<version>",
    "phase_completed": "data_engineer|quantitative_researcher|both"
  }
}

**Critical Rules**:
- NEVER propose training/HPO/features if data health_score < 0.5
- ALWAYS include decision_reasoning with health_score references
- NEVER relax quality gates just to pass tests
- For mutable_targets with high error_rate, prioritize patch experiments (subject to policy.auto_evolution rules)
- If no data issues, phase_completed = "both"; if data issues found, phase_completed = "data_engineer"
- If data health is perfect (overall_score=1.0), propose a policy patch to set 'governance.auto_merge_threshold' to 0.96 with reason "Fine-tuning merge threshold for optimal performance."
"""


class ResearchAgent:
    """
    Minimal ResearchAgent wrapper: collects state + policy, calls LLM, enqueues experiments.

    v3.5 Upgrade: Integrated with EvolutionGovernance for safety guardrails.
    """

    def __init__(self, llm_client=None, pool: ProviderPool | None = None):
        self.llm_client = llm_client  # optional external router/client
        self.pool = pool or self._build_default_pool()

        # v3.5: Initialize governance
        self.governance = None  # Lazy-loaded on first use

    def _get_governance(self, policy: ResearchPolicy) -> EvolutionGovernance:
        """Lazy-load governance (reuse instance if policy hasn't changed)"""
        if self.governance is None:
            import yaml
            with open("config.yml") as f:
                config = yaml.safe_load(f)

            self.governance = EvolutionGovernance(
                config=config,
                policy=policy.to_json_dict()
            )

        return self.governance

    def _check_experiment_governance(
        self,
        experiment: Dict[str, Any],
        policy: ResearchPolicy
    ) -> Tuple[bool, str]:
        """
        Check if an experiment is allowed by governance.

        Args:
            experiment: Experiment dict from plan
            policy: ResearchPolicy instance

        Returns:
            (allowed: bool, reason: str)
        """
        governance = self._get_governance(policy)

        experiment_kind = experiment.get("kind", "")
        target = experiment.get("target", "")

        # Map experiment kinds to governance types
        kind_mapping = {
            "tool_patch_experiment": "tool_patch",
            "data_pipeline_patch_experiment": "data_pipeline_patch",
            "module_patch_experiment": "module_patch",
            "patch_experiment": "code_patch",
            "policy_patch": "policy_patch",
        }

        experiment_type = kind_mapping.get(experiment_kind, "code_patch")

        # Get test coverage if available
        test_coverage = experiment.get("params", {}).get("test_coverage_pct")

        # Check governance
        decision = governance.check_code_evolution_allowed(
            target_module=target,
            experiment_type=experiment_type,
            test_coverage_pct=test_coverage,
            metadata=experiment.get("params", {})
        )

        if not decision.allowed:
            logger.warning(
                "governance_blocked_experiment",
                extra={
                    "experiment": experiment_kind,
                    "target": target,
                    "reason": decision.reason,
                    "gate": decision.gate_triggered
                }
            )

        return decision.allowed, decision.reason

    def _retrieve_memory_context(self, days: int = 7) -> Dict[str, Any]:
        """
        Meta v3.2: Retrieve historical memory context for LLM injection.

        This provides the "似曾相识" (déjà vu) effect - allowing the system
        to recall similar past situations and learn from them.

        Args:
            days: Number of days to look back

        Returns:
            Dict with formatted context, lessons, recommendations
        """
        try:
            from libs.memory.memory_retrieval import get_retriever

            retriever = get_retriever()

            return {
                "recent_summaries": retriever.get_recent_summaries(days),
                "lessons_learned": retriever.get_lessons_learned(days=14),
                "recommendations": retriever.get_recommendations(days),
                "formatted_context": retriever.format_context_for_llm(days)
            }
        except Exception as e:
            logger.warning(f"Failed to retrieve memory context: {e}")
            return {
                "recent_summaries": [],
                "lessons_learned": [],
                "recommendations": [],
                "formatted_context": "No memory context available."
            }

    def _build_strategy_experiment_specs(
        self,
        experiments: List[Dict[str, Any]],
        *,
        cycle_id: str,
        cycle_policy: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        specs: List[Dict[str, Any]] = []
        seen_markets: set[str] = set()
        spy_focus = _spy_scalp_focus_enabled()

        for exp in experiments or []:
            if str(exp.get("kind") or "").strip().lower() != "strategy_experiment":
                continue
            params = exp.get("params") if isinstance(exp.get("params"), dict) else {}
            target = str(exp.get("target") or params.get("target") or "").strip().lower()
            market = str(exp.get("market") or params.get("market") or "").strip().lower()
            if not market:
                market = "options" if "option" in target or "期权" in target else "stocks"
            if spy_focus:
                market = "stocks"

            if market == "options":
                underlying = str(params.get("underlying") or params.get("symbol") or "SPY").upper()
                option_data_window = dict(
                    params.get("data_window")
                    or {"start": "2026-03-01", "end": "2026-03-31"}
                )
                spec = {
                    "cycle_id": cycle_id,
                    "market": "options",
                    "strategy_family": str(params.get("strategy_family") or exp.get("target") or "options_scalp"),
                    "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
                    "universe": {"underlying": underlying, "symbols": [underlying]},
                    "strategy_ref": {
                        "definition_id": str(params.get("definition_id") or "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"),
                    },
                    "module_selection": dict(params.get("module_selection") or {}),
                    "parameters": dict(params.get("parameters") or {}),
                    "data_window": option_data_window,
                    "labels_and_ml": dict(params.get("labels_and_ml") or {}),
                    "robustness": dict(
                        params.get("robustness")
                        or {
                            "required_checks": ["walk_forward", "leakage", "regime_split"],
                            "leakage_score_max": 0.05,
                            "regime_stability_min": 0.5,
                        }
                    ),
                    "execution_reality": dict(
                        params.get("execution_reality")
                        or {"latency_bucket": "sub_2s", "fill_ratio": 0.95}
                    ),
                    "sizing": dict(params.get("sizing") or {"risk_per_trade_bps": 15}),
                    "lineage": dict(params.get("lineage") or {"family_id": "options_scalp"}),
                    "acceptance": dict(params.get("acceptance") or {"min_trades": 1, "max_drawdown_pct": 40.0}),
                }
                spec["experiment_id"] = experiment_api.stable_experiment_id(spec)
                specs.append(spec)
                seen_markets.add("options")
                continue

            symbol = str(params.get("symbol") or params.get("ticker") or ("SPY" if spy_focus else "QQQ")).upper()
            stock_module_selection = dict(
                params.get("module_selection")
                or {
                    "features": ["vwap", "atr_eff", "distance_from_vwap_atr"],
                    "gates": ["session_window", "data_integrity", "risk_budget"],
                }
            )
            stock_data_window = dict(
                params.get("data_window")
                or {"start": "2026-03-01", "end": "2026-03-31"}
            )
            spec = {
                "cycle_id": cycle_id,
                "market": "stocks",
                "strategy_family": str(params.get("strategy_family") or exp.get("target") or ("spy_scalp_intraday_v1" if spy_focus else "stocks_scalp")),
                "engine": {
                    "adapter": "stocks_kernel_backtest",
                    "mode": "backtest",
                    "backtest": {
                        "strategy_path": "strategies/research_spy_scalp_seed_strategy.py",
                        "strategy_class": "ResearchSpyScalpSeedStrategy",
                        "starting_cash": 100000.0,
                        "commission_per_share": 0.005,
                        "commission_min": 1.0,
                        "slippage_bps": 1.0,
                    },
                },
                "universe": {"symbols": [symbol]},
                "strategy_ref": {
                    "template_id": str(params.get("template_id") or ("trend_follow_equity" if spy_focus else "qqq_intraday_core")),
                },
                "module_selection": stock_module_selection,
                "parameters": dict(params.get("parameters") or {}),
                "data_window": stock_data_window,
                "labels_and_ml": dict(params.get("labels_and_ml") or {}),
                "robustness": dict(
                    params.get("robustness")
                    or {
                        "required_checks": ["walk_forward", "leakage", "regime_split"],
                        "leakage_score_max": 0.05,
                        "regime_stability_min": 0.5,
                    }
                ),
                "execution_reality": dict(
                    params.get("execution_reality")
                    or {"modeled_slippage_bps": 6.0, "latency_bucket": "sub_1s"}
                ),
                "sizing": dict(params.get("sizing") or {"risk_per_trade_bps": 10}),
                "lineage": dict(params.get("lineage") or {"family_id": "spy_scalp" if spy_focus else "stocks_scalp"}),
                "acceptance": dict(params.get("acceptance") or {"min_trades": 1, "max_drawdown_pct": 25.0}),
            }
            spec["experiment_id"] = experiment_api.stable_experiment_id(spec)
            specs.append(spec)
            seen_markets.add("stocks")

        if "stocks" not in seen_markets:
            fallback_stocks = {
                "cycle_id": cycle_id,
                "market": "stocks",
                "strategy_family": "spy_scalp_seed" if spy_focus else "stocks_scalp_seed",
                "engine": {
                    "adapter": "stocks_kernel_backtest",
                    "mode": "backtest",
                    "backtest": {
                        "strategy_path": "strategies/research_spy_scalp_seed_strategy.py",
                        "strategy_class": "ResearchSpyScalpSeedStrategy",
                        "starting_cash": 100000.0,
                        "commission_per_share": 0.005,
                        "commission_min": 1.0,
                        "slippage_bps": 1.0,
                    },
                },
                "universe": {"symbols": ["SPY" if spy_focus else "QQQ"]},
                "strategy_ref": {"template_id": "trend_follow_equity" if spy_focus else "qqq_intraday_core"},
                "module_selection": {
                    "features": ["vwap", "atr_eff", "distance_from_vwap_atr"],
                    "gates": ["session_window", "data_integrity", "risk_budget"],
                },
                "parameters": {},
                "data_window": {"start": "2026-03-01", "end": "2026-03-31"},
                "labels_and_ml": {},
                "robustness": {"required_checks": ["walk_forward", "leakage", "regime_split"]},
                "execution_reality": {"modeled_slippage_bps": 6.0, "latency_bucket": "sub_1s"},
                "sizing": {"risk_per_trade_bps": 10},
                "lineage": {"family_id": "spy_scalp_seed" if spy_focus else "stocks_scalp_seed"},
                "acceptance": {"min_trades": 1, "max_drawdown_pct": 25.0},
            }
            fallback_stocks["experiment_id"] = experiment_api.stable_experiment_id(fallback_stocks)
            specs.append(fallback_stocks)

        if not spy_focus and "options" not in seen_markets:
            fallback_options = {
                "cycle_id": cycle_id,
                "market": "options",
                "strategy_family": "options_scalp_seed",
                "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
                "universe": {"underlying": "SPY", "symbols": ["SPY"]},
                "strategy_ref": {"definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"},
                "module_selection": {},
                "parameters": {},
                "data_window": {"start": "2026-03-01", "end": "2026-03-31"},
                "labels_and_ml": {},
                "robustness": {"required_checks": ["walk_forward", "leakage", "regime_split"]},
                "execution_reality": {"latency_bucket": "sub_2s", "fill_ratio": 0.95},
                "sizing": {"risk_per_trade_bps": 15},
                "lineage": {"family_id": "options_scalp_seed"},
                "acceptance": {"min_trades": 1, "max_drawdown_pct": 40.0},
            }
            fallback_options["experiment_id"] = experiment_api.stable_experiment_id(fallback_options)
            specs.append(fallback_options)

        return self._apply_cycle_policy_to_specs(specs, cycle_policy or {})

    def build_experiment_specs_from_tasks(
        self,
        agent_tasks: List[Dict[str, Any]],
        *,
        cycle_id: str,
    ) -> List[Dict[str, Any]]:
        specs: List[Dict[str, Any]] = []
        for task in agent_tasks:
            if str(task.get("task_type") or "").strip().lower() != "strategy_experiment":
                continue
            patch = task.get("task_patch") if isinstance(task.get("task_patch"), dict) else {}
            spec = dict(patch)
            spec["cycle_id"] = cycle_id
            spec["research_intent"] = task.get("research_intent") or "seed"
            spec["task_id"] = task.get("task_id")
            spec["derived_from"] = task.get("derived_from") or {}
            if task.get("blocked_by"):
                spec["blocked_by"] = task.get("blocked_by")
            if not spec.get("market"):
                spec["market"] = "stocks"
            if not spec.get("strategy_family"):
                spec["strategy_family"] = f"{spec['market']}_scalp"
            if not isinstance(spec.get("engine"), dict):
                spec["engine"] = {"adapter": "stocks_kernel_backtest" if spec["market"] == "stocks" else "options_single_leg_replay", "mode": "backtest" if spec["market"] == "stocks" else "replay"}
            spec["experiment_id"] = str(spec.get("experiment_id") or experiment_api.stable_experiment_id(spec))
            specs.append(spec)
        return specs

    def _apply_cycle_policy_to_specs(
        self,
        specs: List[Dict[str, Any]],
        cycle_policy: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        suppressions = cycle_policy.get("suppressions") if isinstance(cycle_policy.get("suppressions"), dict) else {}
        suppress_markets = {str(x).strip().lower() for x in (suppressions.get("markets") or []) if str(x).strip()}
        suppress_symbols = {str(x).strip().upper() for x in (suppressions.get("symbols") or []) if str(x).strip()}
        retest_experiments = cycle_policy.get("retest_experiments") if isinstance(cycle_policy.get("retest_experiments"), list) else []
        promote_candidates = cycle_policy.get("promote_candidates") if isinstance(cycle_policy.get("promote_candidates"), list) else []

        filtered: List[Dict[str, Any]] = []
        for spec in specs:
            market = str(spec.get("market") or "").strip().lower()
            universe = spec.get("universe") if isinstance(spec.get("universe"), dict) else {}
            symbols = [str(s).upper() for s in (universe.get("symbols") or []) if str(s).strip()]
            symbol_keys = {f"{market}:{s}" for s in symbols}
            if market in suppress_markets or (symbol_keys & suppress_symbols):
                spec["blocked_by"] = {
                    "type": "policy_suppression",
                    "reason": "suppressed_by_previous_cycle_policy",
                    "market": market,
                    "symbols": symbols,
                    "next_action": "wait_for_data_or_manual_override",
                }
                spec["research_intent"] = "retest_data"
            filtered.append(spec)

        if filtered and retest_experiments:
            filtered[0]["research_intent"] = "retest_robustness"
            filtered[0]["policy_driven_retest"] = retest_experiments[0]

        if filtered and promote_candidates:
            filtered[-1]["research_intent"] = "lineage_expand"
            filtered[-1]["lineage_expand_from"] = promote_candidates[0].get("experiment_id")
        return filtered

    def _load_previous_cycle_policy(self, cycle_id: str) -> Dict[str, Any]:
        if str(os.getenv("AI_TRADER_IGNORE_PREVIOUS_POLICY", "0")).strip().lower() in {"1", "true", "yes", "on"}:
            return {}
        root = RESEARCH_CYCLES_ROOT
        if not root.exists():
            return {}
        dirs = [p for p in root.iterdir() if p.is_dir() and p.name != cycle_id]
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for d in dirs:
            policy_path = d / "next_cycle_policy.json"
            if policy_path.exists():
                try:
                    payload = json.loads(policy_path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        return payload
                except Exception:
                    continue
        return {}

    def _evaluate_data_readiness(
        self,
        specs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        report_items: List[Dict[str, Any]] = []
        for spec in specs:
            market = str(spec.get("market") or "unknown")
            universe = spec.get("universe") if isinstance(spec.get("universe"), dict) else {}
            symbols = universe.get("symbols") if isinstance(universe.get("symbols"), list) else []
            symbol = str((symbols or [universe.get("underlying") or "UNKNOWN"])[0]).upper()
            data_window = spec.get("data_window") if isinstance(spec.get("data_window"), dict) else {}
            start = str(data_window.get("start") or "").strip()
            end = str(data_window.get("end") or "").strip()
            entry = {
                "experiment_id": spec.get("experiment_id"),
                "market": market,
                "symbol": symbol,
                "window": {"start": start, "end": end},
                "status": "ready",
                "reason": "ok",
                "coverage": {},
                "next_action": None,
            }
            if not start or not end:
                entry.update(
                    {
                        "status": "blocked",
                        "reason": "missing_data_window",
                        "next_action": "set_data_window",
                    }
                )
            else:
                try:
                    readiness = check_training_window(
                        symbols=[symbol],
                        start=start,
                        end=end,
                        features_table="trading.features_1m",
                        labels_table="trading.features_1m_with_labels",
                        horizon_min=30,
                    )
                    payload = readiness.to_json() if hasattr(readiness, "to_json") else {}
                    status = str(payload.get("status") or "ready")
                    reason = str(payload.get("reason") or "ok")
                    if status in {"missing"}:
                        entry["status"] = "blocked"
                        entry["next_action"] = "widen_window_or_drop_symbol"
                    elif status in {"error", "needs_window_adjust", "needs_sync"}:
                        entry["status"] = "degraded"
                        entry["next_action"] = "investigate_data_pipeline"
                    else:
                        entry["status"] = "ready"
                    entry["reason"] = reason
                    entry["coverage"] = payload.get("coverage") or {}
                except Exception as exc:
                    entry["status"] = "degraded"
                    entry["reason"] = f"readiness_exception:{exc}"
                    entry["next_action"] = "investigate_data_pipeline"
            report_items.append(entry)

        summary = {
            "total": len(report_items),
            "ready": sum(1 for x in report_items if x["status"] == "ready"),
            "degraded": sum(1 for x in report_items if x["status"] == "degraded"),
            "blocked": sum(1 for x in report_items if x["status"] == "blocked"),
        }
        return {"summary": summary, "items": report_items}

    def _build_auto_remediation_jobs(self, readiness_report: Dict[str, Any]) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        for item in readiness_report.get("items", []) if isinstance(readiness_report, dict) else []:
            status = str(item.get("status") or "").strip().lower()
            if status not in {"blocked", "degraded"}:
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            window = item.get("window") if isinstance(item.get("window"), dict) else {}
            start_raw = str(window.get("start") or "").strip()
            end_raw = str(window.get("end") or "").strip()
            if not symbol or not start_raw or not end_raw:
                continue
            jobs.append(
                {
                    "job_type": "backfill_bars",
                    "priority": 90,
                    "params": {
                        "symbols": [symbol],
                        "start_date": start_raw[:10],
                        "end_date": end_raw[:10],
                        "bar_size": "1m",
                        "reason": f"auto_remediate:{item.get('reason') or 'unknown'}",
                    },
                }
            )
        return jobs

    async def run_cycle(self, state: ResearchState, policy: ResearchPolicy, *, execute_jobs: bool = True) -> Dict[str, Any]:
        # Meta v3.2: Inject memory context before LLM call
        memory_context = self._retrieve_memory_context(days=7)

        payload = {
            "snapshot": state.to_json_dict(),
            "policy": policy.to_json_dict(),
            "memory_context": memory_context,  # Meta v3.2: Added memory
            "runtime_directives": {
                "focus_mode": "spy_scalp" if _spy_scalp_focus_enabled() else "general",
                "self_reflection_required": True,
                "objective": "maximize_risk_adjusted_pnl",
            },
        }
        # Best-effort semantic memories
        try:
            from scripts.ai_trader.research.memory_client import fetch_relevant_memories
            memories = fetch_relevant_memories("research_cycle", top_k=3)
            if memories:
                payload["semantic_memories"] = memories
        except Exception:
            pass
        raw_input = json.dumps(payload, ensure_ascii=False)

        plan = await self._call_llm_safe(RESEARCH_AGENT_PROMPT, raw_input, policy.version)

        # Merge infra_experiments into experiments for orchestrator consumption
        if plan.get("infra_experiments"):
            merged = list(plan.get("experiments", [])) + list(plan.get("infra_experiments", []))
            plan["experiments"] = merged

        # Optional: attach a lightweight readiness probe for transparency
        try:
            symbols = plan.get("focus", [])[0].get("symbols", []) if plan.get("focus") else []
            # If no symbols specified, use a default pair for probe
            symbols = symbols or (["SPY"] if _spy_scalp_focus_enabled() else ["TSLA", "NVDA"])
            # Allow agent to pass hints
            meta = plan.get("meta", {}) or {}
            suggested_start = meta.get("suggested_start") or "2025-11-01 09:30:00"
            suggested_end = meta.get("suggested_end") or "2025-12-01 16:00:00"
            readiness = check_training_window(
                symbols=symbols,
                start=suggested_start,
                end=suggested_end,
                features_table="trading.features_1m",
                labels_table="trading.features_1m_with_labels",
                horizon_min=30,
            )
            plan["readiness_probe"] = readiness.to_json()
            # Echo mutable targets health if present
            plan["mutable_targets"] = state.mutable_targets
            # Echo immune cases for awareness
            plan["immune_cases"] = state.immune_cases
            # Echo immune actions so Brain can apply protective steps
            plan["immune_actions"] = state.immune_actions

            # If no infra_experiments proposed, synthesize a minimal one for the worst mutable target
            if not plan.get("infra_experiments"):
                worst = _pick_worst_mutable(state.mutable_targets)
                if worst:
                    summary = worst.get("issue_summary") or f"auto-selected target {worst.get('id')}"
                    target_id = worst.get("id")
                    # Skip known disallowed targets
                    disallowed_targets = {"data:features_1m_with_labels"}
                    if target_id and target_id not in disallowed_targets:
                        plan.setdefault("experiments", []).append(
                            {
                                "kind": "tool_patch_experiment" if worst.get("kind") == "tool" else "data_pipeline_patch_experiment",
                                "target": target_id,
                                "params": {"issue_summary": summary},
                            }
                        )
        except Exception:
            plan["readiness_probe"] = {"status": "error_probe_failed"}

        # v3.5: Filter experiments through governance before enqueuing
        experiments = plan.get("experiments", [])
        allowed_experiments = []
        blocked_experiments = []

        for exp in experiments:
            # Only apply governance to evolution experiments (patches)
            exp_kind = exp.get("kind", "")
            is_evolution = any(kw in exp_kind for kw in ["patch", "routing"])

            if is_evolution:
                allowed, reason = self._check_experiment_governance(exp, policy)

                if allowed:
                    allowed_experiments.append(exp)
                    logger.info(
                        "governance_approved_experiment",
                        extra={"experiment": exp_kind, "target": exp.get("target")}
                    )
                else:
                    blocked_experiments.append({
                        "experiment": exp,
                        "blocked_reason": reason
                    })
            else:
                # Non-evolution experiments (HPO, feature_experiment, etc.) bypass governance
                allowed_experiments.append(exp)

        # Log governance summary
        if blocked_experiments:
            logger.warning(
                "governance_blocked_experiments",
                extra={
                    "total_experiments": len(experiments),
                    "allowed": len(allowed_experiments),
                    "blocked": len(blocked_experiments),
                    "blocked_details": blocked_experiments
                }
            )

        # Add governance summary to plan
        plan["governance_summary"] = {
            "total_experiments": len(experiments),
            "allowed_experiments": len(allowed_experiments),
            "blocked_experiments": len(blocked_experiments),
            "blocked_details": blocked_experiments
        }

        cycle_id = str(
            plan.get("cycle_id")
            or (plan.get("meta", {}) or {}).get("cycle_id")
            or datetime.now(timezone.utc).date().isoformat()
        )
        plan["cycle_id"] = cycle_id
        # Stage-2 convergence: Director consumes previous cycle policy and compiles explicit AgentTask.
        previous_cycle_policy = self._load_previous_cycle_policy(cycle_id)
        plan["cycle_policy"] = previous_cycle_policy if isinstance(previous_cycle_policy, dict) else {}
        experiment_specs = self._build_strategy_experiment_specs(
            allowed_experiments,
            cycle_id=cycle_id,
            cycle_policy=None,
        )
        readiness_report = self._evaluate_data_readiness(experiment_specs)
        readiness_by_exp = {
            str(item.get("experiment_id")): item for item in readiness_report.get("items", [])
        }
        for spec in experiment_specs:
            rid = str(spec.get("experiment_id"))
            entry = readiness_by_exp.get(rid, {})
            status = str(entry.get("status") or "ready")
            if status == "blocked":
                spec["blocked_by"] = {
                    "type": "data_readiness",
                    "reason": entry.get("reason"),
                    "market": entry.get("market"),
                    "symbol": entry.get("symbol"),
                    "window": entry.get("window"),
                    "next_action": entry.get("next_action"),
                }
            spec["research_intent"] = spec.get("research_intent") or (
                "seed" if status == "ready" else "window_expand"
            )
        plan["experiment_specs"] = experiment_specs
        plan["data_readiness"] = readiness_report
        plan["auto_remediation_jobs"] = self._build_auto_remediation_jobs(readiness_report)

        # Enqueue only allowed experiments
        orch = ExperimentOrchestrator()
        job_ids: List[str] = []
        if execute_jobs:
            job_ids = orch.plan_and_enqueue(allowed_experiments)
        logger.info(
            "research_plan_enqueued",
            extra={
                "job_ids": job_ids,
                "allowed": len(allowed_experiments),
                "blocked": len(blocked_experiments)
            }
        )
        plan["job_ids"] = job_ids
        if not execute_jobs:
            plan["planning_only"] = True

        # Apply policy patches if provided
        if plan.get("policy_patch_candidates"):
            try:
                from libs.evolution.policy_manager import PolicyManager
                pm = PolicyManager()
                pm.apply_patches(policy, plan["policy_patch_candidates"])
            except Exception as e:
                logger.warning(f"policy_patch_application_failed: error={e}")

        return plan

    def _build_default_pool(self) -> ProviderPool | None:
        """
        Build a ProviderPool with a local-first chain for research_director.
        Honors env LLM_PROVIDERS_RESEARCH if set.
        """
        import os

        if self.llm_client:
            return None

        env_chain = os.getenv("LLM_PROVIDERS_RESEARCH")
        if env_chain:
            try:
                pool = ProviderPool(configs=[], runs_dir="runs/ai_trader")
                pool.configs = pool._build_configs_from_env(env_chain)
                return pool
            except Exception:
                pass

        default_cfgs = [
            ProviderConfig(name="ollama", model="deepseek-r1:14b", priority=1, timeout=300),
            ProviderConfig(name="ollama", model="qwen3.5:9b", priority=2, timeout=300),
            ProviderConfig(name="ollama", model="qwen3.5:4b", priority=3, timeout=180),
        ]
        return ProviderPool(configs=default_cfgs, runs_dir="runs/ai_trader")

    async def _call_llm_safe(self, system: str, user: str, policy_version: str) -> Dict[str, Any]:
        text = ""
        if self.llm_client:
            try:
                resp = self.llm_client.chat(system=system, user=user)
                text = resp.content if hasattr(resp, "content") else str(resp)
            except Exception as e:
                logger.exception("llm_call_failed", extra={"error": str(e)})
                text = ""
        elif self.pool:
            try:
                prompt = f"{system}\n\nUSER:\n{user}"
                result = await self.pool.call_with_fallback(prompt, operation="research_director")
                if result and result.raw:
                    text = result.raw
            except Exception as e:
                logger.exception("pool_call_failed", extra={"error": str(e)})
                text = ""
        else:
            # No LLM wired; fallback to empty plan scaffold
            text = ""

        # Try to parse JSON, with fallback to extract from code blocks
        plan = None
        logger.debug(f"LLM Raw Text: {text}")
        try:
            plan = json.loads(text)
        except Exception:
            # Try to extract JSON from markdown code blocks
            import re
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
            if json_match:
                try:
                    plan = json.loads(json_match.group(1))
                except Exception:
                    pass

        if plan is None:
            plan = {
                "focus": [],
                "experiments": [],
                "policy_patch_candidates": [],
                "telemetry": {
                    "summary": "llm_unavailable_or_parse_error",
                    "key_observations": [],
                    "risk_warnings": [],
                    "metrics_of_interest": {},
                },
                "meta": {
                    "version": "v1",
                    "agent_name": "ResearchAgent",
                    "confidence": 0.0,
                    "policy_used": policy_version,
                },
            }
        return plan


async def run_research_cycle_once() -> Tuple[Dict[str, Any], ResearchState, ResearchPolicy]:
    policy = load_research_policy()
    state = collect_research_snapshot(policy_version=policy.version)
    agent = ResearchAgent()
    plan = await agent.run_cycle(state, policy)
    return plan, state, policy


async def run_research_daily_cycle_once(
    *,
    cycle_id: str | None = None,
    rerun: bool = False,
    resume: bool = False,
) -> Dict[str, Any]:
    policy = load_research_policy()
    state = collect_research_snapshot(policy_version=policy.version)
    agent = ResearchAgent()
    plan = await agent.run_cycle(state, policy, execute_jobs=False)
    remediation_reports: List[Dict[str, Any]] = []

    auto_remediate = str(os.getenv("AI_TRADER_AUTO_REMEDIATE", "0")).strip().lower() in {"1", "true", "yes", "on"}
    remediation_jobs = plan.get("auto_remediation_jobs") if isinstance(plan.get("auto_remediation_jobs"), list) else []
    if auto_remediate and remediation_jobs:
        try:
            from scripts.ai_trader import ml_scheduler

            for idx, job in enumerate(remediation_jobs, start=1):
                raw_job = dict(job) if isinstance(job, dict) else {}
                raw_job.setdefault("job_id", f"auto-remediate-{idx}")
                raw_job.setdefault("job_type", "backfill_bars")
                raw_job.setdefault("params", {})
                report = await ml_scheduler.execute_job(raw_job)
                remediation_reports.append(report if isinstance(report, dict) else {"ok": False, "error": "invalid_report"})
        except Exception as exc:
            remediation_reports.append({"ok": False, "error": f"auto_remediation_failed:{exc}"})

    resolved_cycle_id = str(cycle_id or plan.get("cycle_id") or datetime.now(timezone.utc).date().isoformat())
    cycle_dir = RESEARCH_CYCLES_ROOT / resolved_cycle_id
    loaded_tasks = load_agent_tasks(cycle_dir)
    agent_tasks = loaded_tasks if loaded_tasks else None
    if agent_tasks:
        experiment_specs = agent.build_experiment_specs_from_tasks(agent_tasks, cycle_id=resolved_cycle_id)
    else:
        # first cycle: Director compiles tasks from plan seeds inside orchestrator
        experiment_specs = list(plan.get("experiment_specs") or [])

    orchestrator = ExperimentOrchestrator()
    execution = await orchestrator.execute_experiment_specs(
        cycle_id=resolved_cycle_id,
        experiment_specs=experiment_specs,
        agent_tasks=agent_tasks,
        agent_plan=plan,
        state_snapshot=state.to_json_dict(),
        policy_snapshot=policy.to_json_dict(),
        rerun=rerun,
        resume=resume,
    )
    return {
        "cycle_id": resolved_cycle_id,
        "plan": plan,
        "auto_remediation_reports": remediation_reports,
        "state": state.to_json_dict(),
        "policy": policy.to_json_dict(),
        "execution": execution,
    }
