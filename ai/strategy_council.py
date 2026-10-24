from __future__ import annotations

"""
Strategy Council (multi-LLM, hedge-aware) scaffolding.

This module defines the data shapes and orchestration stubs for:
- Generating strategy proposals from multiple LLMs
- Cross-reviewing proposals (risk/execution/hedge feedback)
- Synthesizing a final plan with hedge/risk limits
- Running paper validation

It intentionally keeps business logic minimal so it can be extended by
Brain/Orchestrator without hard-coding provider details. All returned
objects are simple dataclasses convertible to dict for logging.
"""

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid
import json
from datetime import date
import logging

logger = logging.getLogger(__name__)

RISK_CONSTRAINT_TEMPLATE = {
    "max_dd_pct": 5.0,
    "pos_limit_pct": 2.0,
    "slippage_bps": 10.0,
    "low_win_rate_size": "size<=0.5x with hard stop",
}

HEDGE_RULE_TEMPLATE = {
    "enable_if_confidence_below": 0.6,
    "vol_trigger": "realized_vol>threshold",
    "pair_or_min_var_if_high_corr": True,
    "target_beta_or_vega_neutral_if_market_exposed": True,
}


# ---- Data Models ----


@dataclass
class HedgePlan:
    hedge_type: str  # pair|min_var|beta|vega
    instrument: str  # ETF|FUT|OPT|FX
    ratio_or_target_beta: float
    trigger_rules: Dict[str, Any]
    fallback: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyProposal:
    proposal_id: str
    symbols: List[str]
    side: str  # long|short|mixed
    weights: Dict[str, float]
    entry_rule: str
    exit_rule: str
    hold_horizon_min: int
    expected_ev: float
    confidence: float
    vol_target: Optional[float] = None
    max_drawdown: Optional[float] = None
    win_rate_est: Optional[float] = None
    payoff_ratio_est: Optional[float] = None
    hedge_plan: Optional[HedgePlan] = None
    risk_limits: Optional[Dict[str, Any]] = None
    liquidity_note: Optional[str] = None
    rationale: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.hedge_plan:
            data["hedge_plan"] = self.hedge_plan.to_dict()
        return data


@dataclass
class ReviewFeedback:
    target_proposal_id: str
    concerns: List[str]
    improvements: List[str]
    feasibility_score: float
    hedge_score: Optional[float] = None
    risk_score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FinalPlan:
    base_proposal_id: str
    merged_changes: Dict[str, Any]
    hedge_plan: Optional[HedgePlan]
    risk_limits: Dict[str, Any]
    open_risks: List[str]
    approvals: List[str]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.hedge_plan:
            data["hedge_plan"] = self.hedge_plan.to_dict()
        return data


@dataclass
class PaperResult:
    ok: bool
    pnl: float
    ev_realized: float
    mdd: float
    sharpe: Optional[float]
    hit_rate: Optional[float]
    payoff_ratio: Optional[float]
    hedge_pnl: Optional[float] = None
    hedge_var_reduction: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---- Helpers ----


def _gen_id() -> str:
    return uuid.uuid4().hex[:8]


def _default_risk_limits() -> Dict[str, Any]:
    base = {k: v for k, v in RISK_CONSTRAINT_TEMPLATE.items() if isinstance(v, (int, float))}
    return {
        "max_dd_pct": base.get("max_dd_pct", 5.0),
        "pos_limit_pct": base.get("pos_limit_pct", 2.0),
        "slippage_bps": base.get("slippage_bps", 10.0),
    }


# ---- Core Orchestration (scaffolding) ----


def generate_proposals(ctx: Dict[str, Any]) -> List[StrategyProposal]:
    """
    Generate proposals from multiple LLMs (best-effort).
    Falls back to empty if providers are unavailable.
    """
    if isinstance(ctx, dict) and ctx.get("disable_llm"):
        pool = None
    else:
        pool = _build_pool()

    universe = ctx.get("universe") if isinstance(ctx, dict) else None
    risk = ctx.get("risk_limits") if isinstance(ctx, dict) else _default_risk_limits()
    prompt = _proposal_prompt(universe=universe, risk_limits=risk)

    proposals: List[StrategyProposal] = []
    raw: List[Dict[str, Any]] = []
    if pool:
        raw = _call_llm(pool, prompt, operation="strategy_proposals")
    if not raw:
        # Heuristic fallback: simple long + hedged pair
        symbols = universe or ["AAPL", "TSLA", "SPY"]
        primary = symbols[0]
        hedge_sym = symbols[2] if len(symbols) >= 3 else "SPY"
        proposals.append(
            StrategyProposal(
                proposal_id=_gen_id(),
                symbols=[primary],
                side="long",
                weights={primary: 1.0},
                entry_rule="breakout_20",
                exit_rule="atr_stop_2",
                hold_horizon_min=60,
                expected_ev=0.2,
                confidence=0.55,
                vol_target=0.2,
                max_drawdown=4.0,
                hedge_plan=HedgePlan(
                    hedge_type="beta",
                    instrument="ETF",
                    ratio_or_target_beta=0.5,
                    trigger_rules={"if_confidence_below": 0.6},
                    fallback="reduce_size",
                ),
                risk_limits=risk,
                rationale="fallback_heuristic",
            )
        )
        if hedge_sym != primary:
            proposals.append(
                StrategyProposal(
                    proposal_id=_gen_id(),
                    symbols=[primary, hedge_sym],
                    side="long",
                    weights={primary: 1.0, hedge_sym: -0.5},
                    entry_rule="mean_revert_5",
                    exit_rule="stop_loss_2atr",
                    hold_horizon_min=120,
                    expected_ev=0.15,
                    confidence=0.5,
                    vol_target=0.25,
                    max_drawdown=5.0,
                    hedge_plan=None,
                    risk_limits=risk,
                    rationale="fallback_pair_hedge",
                )
            )
    else:
        for item in raw:
            try:
                proposals.append(_proposal_from_dict(item))
            except Exception as exc:
                logger.warning("proposal_parse_failed: %s", exc)
    return proposals


def cross_review(proposals: List[StrategyProposal], ctx: Dict[str, Any]) -> List[ReviewFeedback]:
    """
    Cross-review proposals via LLM. Falls back to placeholder reviews.
    """
    if isinstance(ctx, dict) and ctx.get("disable_llm"):
        pool = None
    else:
        pool = _build_pool()
    if pool is None:
        return [
            ReviewFeedback(
                target_proposal_id=p.proposal_id,
                concerns=["no_review_pool_available"],
                improvements=[],
                feasibility_score=0.0,
                hedge_score=None,
                risk_score=None,
            )
            for p in proposals
        ]

    prompt = _review_prompt(proposals)
    raw = _call_llm(pool, prompt, operation="strategy_reviews")
    feedback: List[ReviewFeedback] = []
    for item in raw:
        try:
            target = item.get("target_proposal_id") or item.get("proposal_id")
            feedback.append(
                ReviewFeedback(
                    target_proposal_id=str(target),
                    concerns=item.get("concerns") or [],
                    improvements=item.get("improvements") or [],
                    feasibility_score=float(item.get("feasibility_score", 0.0) or 0.0),
                    hedge_score=_safe_float(item.get("hedge_score")),
                    risk_score=_safe_float(item.get("risk_score")),
                )
            )
        except Exception as exc:
            logger.warning("review_parse_failed", err=str(exc))
    return feedback


def synthesize_plan(
    proposals: List[StrategyProposal],
    feedbacks: List[ReviewFeedback],
    ctx: Dict[str, Any],
) -> Optional[FinalPlan]:
    """
    Basic synthesizer:
    - filter proposals by EV>0 and risk limits if provided
    - pick highest expected_ev proposal
    - attach hedge plan when confidence is low
    """
    _ = ctx  # placeholder
    if not proposals:
        return None

    base_limits = _default_risk_limits()
    risk_limits = ctx.get("risk_limits") if isinstance(ctx, dict) else None
    targets = ctx.get("targets") if isinstance(ctx, dict) else {}
    max_dd_target = targets.get("max_drawdown_target")
    min_conf = float(ctx.get("min_confidence", 0.5)) if isinstance(ctx, dict) else 0.5
    def _passes_risk(p: StrategyProposal) -> bool:
        lim = {**base_limits, **(risk_limits or {})}
        if max_dd_target is not None:
            lim["max_dd_pct"] = min(lim.get("max_dd_pct", 5.0), float(max_dd_target))
        if p.max_drawdown is not None and p.max_drawdown > lim.get("max_dd_pct", 5.0):
            return False
        return p.expected_ev > 0

    candidates = [p for p in proposals if _passes_risk(p)]
    if not candidates:
        return None
    chosen = sorted(candidates, key=lambda p: p.expected_ev, reverse=True)[0]

    hedge_plan = chosen.hedge_plan
    if hedge_plan is None and chosen.confidence < HEDGE_RULE_TEMPLATE["enable_if_confidence_below"]:
        hedge_plan = HedgePlan(
            hedge_type="beta",
            instrument="ETF",
            ratio_or_target_beta=0.5,
            trigger_rules={"if_confidence_below": HEDGE_RULE_TEMPLATE["enable_if_confidence_below"]},
            fallback="reduce_size",
        )

    open_risks = []
    for fb in feedbacks:
        if fb.target_proposal_id == chosen.proposal_id:
            open_risks.extend(fb.concerns or [])

    merged_limits = {**base_limits, **(chosen.risk_limits or {})}

    if chosen.confidence < min_conf:
        open_risks.append(f"low_confidence<{min_conf}")
        # Degrade to non-trading corrective action placeholder
        merged_changes = {"action": "data_or_feature_fix", "reason": "confidence_low"}
    else:
        merged_changes = {}

    return FinalPlan(
        base_proposal_id=chosen.proposal_id,
        merged_changes=merged_changes,
        hedge_plan=hedge_plan,
        risk_limits=merged_limits,
        open_risks=open_risks,
        approvals=["synthetic_placeholder"],
    )


def run_paper_plan(plan: FinalPlan, ctx: Dict[str, Any]) -> PaperResult:
    """
    Run paper/simulated execution for the plan.
    If ctx["paper_runner"] is provided (callable returning metrics dict),
    use it; otherwise return a neutral placeholder.
    """
    metrics_path = ctx.get("paper_metrics_path") if isinstance(ctx, dict) else None
    if metrics_path:
        try:
            raw = Path(metrics_path).read_text(encoding="utf-8")
            loaded = json.loads(raw)
            return _paper_result_from_metrics(loaded)
        except Exception as exc:
            logger.warning("paper_metrics_path_failed: %s", exc)

    runner = ctx.get("paper_runner") if isinstance(ctx, dict) else None
    # Allow passing a precomputed metrics dict (from external backtest)
    precomputed = ctx.get("paper_metrics") if isinstance(ctx, dict) else None
    if isinstance(precomputed, dict):
        return _paper_result_from_metrics(precomputed)
    if callable(runner):
        try:
            metrics = runner(plan, ctx)
            if isinstance(metrics, dict):
                return PaperResult(
                    ok=bool(metrics.get("ok", True)),
                    pnl=float(metrics.get("pnl", 0.0) or 0.0),
                    ev_realized=float(metrics.get("ev_realized", metrics.get("ev", 0.0)) or 0.0),
                    mdd=float(metrics.get("mdd", 0.0) or 0.0),
                    sharpe=_safe_float(metrics.get("sharpe")),
                    hit_rate=_safe_float(metrics.get("hit_rate")),
                    payoff_ratio=_safe_float(metrics.get("payoff_ratio")),
                    hedge_pnl=_safe_float(metrics.get("hedge_pnl")),
                    hedge_var_reduction=_safe_float(metrics.get("hedge_var_reduction")),
                    meta={k: v for k, v in metrics.items() if k not in {"pnl", "ev_realized", "ev", "mdd", "sharpe", "hit_rate", "payoff_ratio", "hedge_pnl", "hedge_var_reduction", "ok"}},
                )
        except Exception as exc:
            logger.warning("paper_runner_failed: %s", exc)

    return PaperResult(
        ok=True,
        pnl=0.0,
        ev_realized=0.0,
        mdd=0.0,
        sharpe=None,
        hit_rate=None,
        payoff_ratio=None,
        hedge_pnl=None,
        hedge_var_reduction=None,
        meta={"status": "not_implemented"},
    )


def _paper_result_from_metrics(metrics: Dict[str, Any]) -> PaperResult:
    return PaperResult(
        ok=bool(metrics.get("ok", True)),
        pnl=float(metrics.get("pnl", 0.0) or 0.0),
        ev_realized=float(metrics.get("ev_realized", metrics.get("ev", 0.0)) or 0.0),
        mdd=float(metrics.get("mdd", 0.0) or 0.0),
        sharpe=_safe_float(metrics.get("sharpe")),
        hit_rate=_safe_float(metrics.get("hit_rate")),
        payoff_ratio=_safe_float(metrics.get("payoff_ratio")),
        hedge_pnl=_safe_float(metrics.get("hedge_pnl")),
        hedge_var_reduction=_safe_float(metrics.get("hedge_var_reduction")),
        meta={k: v for k, v in metrics.items() if k not in {"pnl", "ev_realized", "ev", "mdd", "sharpe", "hit_rate", "payoff_ratio", "hedge_pnl", "hedge_var_reduction", "ok"}},
    )


def run_council(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    End-to-end helper:
    - generate proposals
    - cross-review
    - synthesize final plan
    - run paper (placeholder)
    - record meeting
    Returns a dict with all outputs.
    """
    proposals = generate_proposals(ctx)
    reviews = cross_review(proposals, ctx)
    final_plan = synthesize_plan(proposals, reviews, ctx)
    paper_result = run_paper_plan(final_plan, ctx) if final_plan else None
    record_meeting(
        proposals,
        reviews,
        final_plan,
        paper_result,
        runs_dir=ctx.get("runs_dir", "runs/ai_trader"),
    )
    outcome_record = None
    if final_plan and paper_result:
        # ev_plan is not known until a real backtest; if user passes ev_plan
        # in ctx we respect it, otherwise keep placeholder in logger.
        outcome_record = log_outcome_from_paper(
            final_plan,
            paper_result,
            ev_plan_override=ctx.get("ev_plan"),
        )
        _notify_and_stage_live(final_plan, paper_result, ctx)
    return {
        "proposals": proposals,
        "reviews": reviews,
        "final_plan": final_plan,
        "paper_result": paper_result,
        "plan_vs_outcome": outcome_record,
    }


def record_meeting(
    proposals: List[StrategyProposal],
    feedbacks: List[ReviewFeedback],
    final_plan: Optional[FinalPlan],
    paper_result: Optional[PaperResult],
    runs_dir: str = "runs/ai_trader",
) -> Optional[Path]:
    """
    Persist a council meeting snapshot for auditing and memory ingestion.
    """
    try:
        dstr = date.today().isoformat()
        out_dir = Path(runs_dir) / dstr
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "strategy_council.jsonl"
        record = {
            "proposals": [p.to_dict() for p in proposals],
            "feedbacks": [f.to_dict() for f in feedbacks],
            "final_plan": final_plan.to_dict() if final_plan else None,
            "paper_result": paper_result.to_dict() if paper_result else None,
        }
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # Best-effort memory ingestion for retrieval/lessons
        try:
            from scripts.ai_trader.memory.memory_fabric import write_memory  # type: ignore

            summary = "strategy_council_meeting"
            lessons = []
            if final_plan and final_plan.open_risks:
                lessons = [f"open_risk:{r}" for r in final_plan.open_risks]
            write_memory(
                "STS",
                {
                    "type": "strategy_council",
                    "summary": summary,
                    "final_plan": final_plan.to_dict() if final_plan else None,
                    "open_risks": final_plan.open_risks if final_plan else [],
                    "paper_result": paper_result.to_dict() if paper_result else None,
                    "lessons": lessons,
                    "timestamp": date.today().isoformat(),
                },
            )
        except Exception:
            pass
        return out_path
    except Exception:
        return None


def demo_stub() -> None:
    """
    Minimal self-test: build two dummy proposals, synthesize, and record.
    """
    p1 = StrategyProposal(
        proposal_id=_gen_id(),
        symbols=["AAPL"],
        side="long",
        weights={"AAPL": 1.0},
        entry_rule="breakout_20",
        exit_rule="atr_stop_2",
        hold_horizon_min=60,
        expected_ev=0.8,
        confidence=0.55,
        vol_target=0.2,
        max_drawdown=4.0,
        hedge_plan=None,
        risk_limits={"max_dd_pct": 5.0},
    )
    p2 = StrategyProposal(
        proposal_id=_gen_id(),
        symbols=["TSLA"],
        side="long",
        weights={"TSLA": 1.0},
        entry_rule="pullback_5",
        exit_rule="trail_stop",
        hold_horizon_min=120,
        expected_ev=0.4,
        confidence=0.7,
        vol_target=0.25,
        max_drawdown=6.0,
    )
    feedbacks = cross_review([p1, p2], ctx={})
    plan = synthesize_plan([p1, p2], feedbacks, ctx={})
    paper = run_paper_plan(plan, ctx={}) if plan else None
    record_meeting([p1, p2], feedbacks, plan, paper)
    if plan and paper:
        log_outcome_from_paper(plan, paper)


def _safe_float(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:
        return None


def _proposal_from_dict(d: Dict[str, Any]) -> StrategyProposal:
    hedge = d.get("hedge_plan")
    hedge_plan = None
    if isinstance(hedge, dict):
        hedge_plan = HedgePlan(
            hedge_type=str(hedge.get("hedge_type") or hedge.get("type") or "beta"),
            instrument=str(hedge.get("instrument") or "ETF"),
            ratio_or_target_beta=float(hedge.get("ratio_or_target_beta") or hedge.get("ratio") or 0.5),
            trigger_rules=hedge.get("trigger_rules") or {},
            fallback=hedge.get("fallback"),
        )
    return StrategyProposal(
        proposal_id=str(d.get("proposal_id") or d.get("id") or _gen_id()),
        symbols=[str(s) for s in (d.get("symbols") or [])],
        side=str(d.get("side") or "long"),
        weights={str(k): float(v) for k, v in (d.get("weights") or {}).items()},
        entry_rule=str(d.get("entry_rule") or ""),
        exit_rule=str(d.get("exit_rule") or ""),
        hold_horizon_min=int(d.get("hold_horizon_min") or d.get("hold_minutes") or 0),
        expected_ev=float(d.get("expected_ev") or 0.0),
        confidence=float(d.get("confidence") or 0.0),
        vol_target=_safe_float(d.get("vol_target")),
        max_drawdown=_safe_float(d.get("max_drawdown")),
        win_rate_est=_safe_float(d.get("win_rate_est")),
        payoff_ratio_est=_safe_float(d.get("payoff_ratio_est")),
        hedge_plan=hedge_plan,
        risk_limits=d.get("risk_limits"),
        liquidity_note=d.get("liquidity_note"),
        rationale=d.get("rationale"),
    )


def log_outcome_from_paper(plan: FinalPlan, paper: PaperResult, ev_plan_override: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Create a plan_vs_outcome record using plan + paper_result and persist it.
    """
    try:
        from ai.plan_outcome_logger import log_plan_vs_outcome  # type: ignore
    except Exception:
        return None

    hedge_effect = {
        "hedge_pnl": paper.hedge_pnl,
        "hedge_var_reduction": paper.hedge_var_reduction,
    }
    issues = []
    try:
        if paper.mdd is not None and plan.risk_limits:
            limit = plan.risk_limits.get("max_dd_pct", 5.0)
            if limit is not None and paper.mdd > limit:
                issues.append("mdd_above_limit")
    except Exception:
        pass

    record = log_plan_vs_outcome(
        plan_id=plan.base_proposal_id,
        summary="strategy_council_paper",
        ev_plan=float(ev_plan_override) if ev_plan_override is not None else 0.0,
        ev_realized=paper.ev_realized,
        mdd=paper.mdd,
        hedge_effect=hedge_effect,
        issues=issues,
        lessons=[],
        extra={"paper_meta": paper.to_dict(), "risk_limits": plan.risk_limits},
    )
    return record


def _notify_and_stage_live(plan: FinalPlan, paper: PaperResult, ctx: Dict[str, Any]) -> None:
    """
    If paper results look acceptable, send Slack notification and stage for live.
    Criteria (best-effort): paper.ok and ev_realized>=0 and mdd <= risk limit.
    """
    try:
        ok = bool(paper.ok) and float(paper.ev_realized) >= 0.0
        mdd_ok = True
        try:
            max_dd = (plan.risk_limits or {}).get("max_dd_pct")
            if max_dd is not None:
                mdd_ok = paper.mdd <= max_dd
        except Exception:
            pass
        if not (ok and mdd_ok):
            return

        # Slack notify
        webhook = ctx.get("notify_slack") if isinstance(ctx, dict) else None
        if webhook:
            import urllib.request
            payload = {
                "text": f"[StrategyCouncil] Paper plan ready: proposal={plan.base_proposal_id}, ev_realized={paper.ev_realized}, mdd={paper.mdd}, hedge_pnl={paper.hedge_pnl}"
            }
            try:
                req = urllib.request.Request(
                    webhook,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=2)
            except Exception:
                pass

        # Stage live switch intent
        whitelist = ctx.get("live_whitelist") if isinstance(ctx, dict) else []
        can_auto = whitelist and plan.base_proposal_id in whitelist
        stage_dir = Path(ctx.get("runs_dir", "runs/ai_trader")) / "live_staging"
        stage_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "proposal_id": plan.base_proposal_id,
            "ev_realized": paper.ev_realized,
            "mdd": paper.mdd,
            "hedge_pnl": paper.hedge_pnl,
            "hedge_var_reduction": paper.hedge_var_reduction,
            "ok": ok,
            "mdd_ok": mdd_ok,
            "timestamp": date.today().isoformat(),
            "auto_switch": bool(can_auto),
            "reason": "paper_passed",
        }
        fp = stage_dir / f"{plan.base_proposal_id}.json"
        fp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception:
        return


def _build_pool():
    """
    Build ProviderPool if available (prefers env LLM_PROVIDERS_RESEARCH).
    Returns None if unavailable to avoid hard dependency.
    """
    try:
        from scripts.ai_trader.brain.provider_fallback import ProviderPool, ProviderConfig  # type: ignore
    except Exception:
        return None

    import os

    env_chain = os.getenv("LLM_PROVIDERS_RESEARCH")
    if env_chain:
        try:
            pool = ProviderPool(configs=[], runs_dir="runs/ai_trader")
            pool.configs = pool._build_configs_from_env(env_chain)
            return pool
        except Exception as exc:
            logger.warning("provider_pool_env_failed", err=str(exc))

    # Default local models
    try:
        default_cfgs = [
            ProviderConfig(name="ollama", model="qwen2.5:7b-instruct", priority=1, timeout=120),
            ProviderConfig(name="ollama", model="deepseek-r1:1.5b", priority=2, timeout=120),
        ]
        return ProviderPool(configs=default_cfgs, runs_dir="runs/ai_trader")
    except Exception as exc:
        logger.warning("provider_pool_default_failed", err=str(exc))
        return None


def _call_llm(pool, prompt: str, operation: str) -> List[Dict[str, Any]]:
    """
    Call LLM and parse JSON list; fallback to empty on errors.
    """
    try:
        result = pool.call_with_fallback(prompt, operation=operation)
        # If provider returns coroutine, run it synchronously
        import inspect, asyncio
        if inspect.iscoroutine(result):
            result = asyncio.run(result)
        elif hasattr(result, "__await__"):
            result = asyncio.run(result)
        if not result or not result.raw:
            return []
        text = result.raw.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("items") or data.get("proposals") or data.get("feedbacks") or []
        if not isinstance(data, list):
            return []
        return data
    except Exception as exc:
        logger.warning("llm_call_failed %s: %s", operation, exc)
        return []


def _proposal_prompt(universe: Optional[List[str]], risk_limits: Dict[str, Any]) -> str:
    uni_txt = ", ".join(universe or ["AAPL", "TSLA", "NVDA", "SPY"])
    targets = {
        "daily_return_target": risk_limits.get("daily_return_target", ">=0"),
        "weekly_return_target": risk_limits.get("weekly_return_target", ">=0"),
        "max_drawdown_target": risk_limits.get("max_dd_pct", 5.0),
    }
    return f"""
You are a 3-member strategy council. Each role outputs ONE proposal (strict JSON, list of 3 objects).
Roles:
- Strategy Officer: trend/structure edge with clear entry/exit and EV>0.
- Quant Researcher: statistical or factor blend with explicit horizon and risk limits.
- Hedge Specialist: pair/min_var/beta/vega hedge with triggers; include fallback.
Universe: [{uni_txt}]
Risk limits (template): {json.dumps(risk_limits)}
Risk policy template: {json.dumps(RISK_CONSTRAINT_TEMPLATE)}
Hedge rules: {json.dumps(HEDGE_RULE_TEMPLATE)}
Targets: {json.dumps(targets)}
Each proposal MUST include: proposal_id, symbols, side, weights, entry_rule, exit_rule, hold_horizon_min,
expected_ev, confidence, vol_target, max_drawdown, win_rate_est, payoff_ratio_est,
hedge_plan (hedge_type/instrument/ratio_or_target_beta/trigger_rules/fallback), risk_limits, liquidity_note, rationale.
Filters: discard EV<=0 or max_drawdown>risk_limits.max_dd_pct. If confidence<0.6, include hedge_plan.
If you believe EV cannot meet targets or confidence <0.5, propose a data/feature/hedge-fix action instead of a trading plan.
Return ONLY a JSON list of 3 proposals (no prose, no markdown).
"""


def _review_prompt(proposals: List[StrategyProposal]) -> str:
    props = [p.to_dict() for p in proposals]
    return f"""
You are Risk Officer, Execution Officer, Compliance Officer. Review each proposal with role-specific checks.
Execution Officer must flag liquidity/IBKR tradability gaps; Compliance flags position limits; Risk checks drawdown/hedge.
Given proposals: {json.dumps(props)}
Return ONLY JSON list of feedback objects:
  - target_proposal_id
  - concerns (list)
  - improvements (list)
  - feasibility_score (0-1)
  - hedge_score (0-1)
  - risk_score (0-1)
Use the templates:
  risk_limits: {json.dumps(RISK_CONSTRAINT_TEMPLATE)}
  hedge_rules: {json.dumps(HEDGE_RULE_TEMPLATE)}
Do not add prose.
"""


__all__ = [
    "HedgePlan",
    "StrategyProposal",
    "ReviewFeedback",
    "FinalPlan",
    "PaperResult",
    "generate_proposals",
    "cross_review",
    "synthesize_plan",
    "run_paper_plan",
    "run_council",
    "record_meeting",
    "demo_stub",
]
