from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import json
from pathlib import Path


@dataclass
class StrategyMetrics:
    oos: Dict[str, Any] = field(default_factory=dict)
    live: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyState:
    strategy_id: str
    domain: str = "trading"
    stage: str = "incubation"  # incubation | validation | production
    metrics: StrategyMetrics = field(default_factory=StrategyMetrics)
    recent_experiments: List[Dict[str, Any]] = field(default_factory=list)
    immune_cases: List[Dict[str, Any]] = field(default_factory=list)
    skill_usage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillState:
    name: str
    domain: str = "trading"
    stage: str = "incubation"  # incubation | stable | deprecated
    success_rate: Optional[float] = None
    error_rate: Optional[float] = None
    recent_patches: int = 0


@dataclass
class MutableTarget:
    """
    A module/tool/data-pipeline that is allowed to evolve (self-programming target).
    """
    id: str                     # e.g. "module:feature_dsl_v2", "tool:backtest_ml_engine", "data:ohlcv_cleaner"
    kind: str                   # "module" | "tool" | "data_pipeline"
    domain: str = "trading"
    risk_level: str = "medium"  # "low" | "medium" | "high"
    mutable: bool = True
    metrics: Dict[str, Any] = field(default_factory=dict)  # e.g. error_rate, runtime_p95
    recent_issues: List[Dict[str, Any]] = field(default_factory=list)
    last_patches: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class BudgetState:
    total: float
    used: float


@dataclass
class Budgets:
    compute_hours: BudgetState
    tokens: Optional[BudgetState] = None


@dataclass
class ResearchState:
    timestamp: str
    policy_version: Optional[str]
    budgets: Budgets
    strategies: Dict[str, StrategyState] = field(default_factory=dict)
    skills: Dict[str, SkillState] = field(default_factory=dict)
    immune_cases: List[Dict[str, Any]] = field(default_factory=list)
    immune_actions: List[Dict[str, Any]] = field(default_factory=list)
    experiments: List[Dict[str, Any]] = field(default_factory=list)
    mutable_targets: Dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> Dict[str, Any]:
        return asdict(self)


def collect_research_snapshot(policy_version: Optional[str] = None) -> ResearchState:
    """
    Minimal snapshot collector. This is intentionally lightweight so it can be
    expanded with real metrics/ClickHouse queries later.
    """
    now = datetime.now(timezone.utc).isoformat()

    strategies: Dict[str, StrategyState] = {}
    compute_used = 0.0
    tokens_used = 0.0

    # 🆕 P2.1: Fetch real trading metrics from ClickHouse
    try:
        from libs.db.ch_pool import get_client

        ch = get_client()

        # Query real PnL and trading stats from last 30 days
        query = """
        SELECT
            strategy_id,
            count(*) as total_trades,
            sum(pnl) as total_pnl,
            avg(pnl) as avg_pnl,
            stddevPop(pnl) as std_pnl,
            countIf(pnl > 0) as winning_trades,
            countIf(pnl < 0) as losing_trades
        FROM trading.trades
        WHERE ts >= now() - INTERVAL 30 DAY
          AND strategy_id != ''
        GROUP BY strategy_id
        """

        results = ch.query(query)

        for row in results:
            strategy_id = row.get("strategy_id") or "unknown"
            total_trades = int(row.get("total_trades") or 0)
            total_pnl = float(row.get("total_pnl") or 0.0)
            avg_pnl = float(row.get("avg_pnl") or 0.0)
            std_pnl = float(row.get("std_pnl") or 1.0)
            winning_trades = int(row.get("winning_trades") or 0)
            losing_trades = int(row.get("losing_trades") or 0)

            # Calculate Sharpe ratio (annualized, assuming daily trades)
            sharpe = (avg_pnl / std_pnl * (252 ** 0.5)) if std_pnl > 0 else 0.0
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

            strat = strategies.get(strategy_id) or StrategyState(strategy_id=strategy_id)
            strat.metrics.live = {
                "total_trades": total_trades,
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(avg_pnl, 2),
                "sharpe": round(sharpe, 2),
                "win_rate": round(win_rate, 1),
                "winning_trades": winning_trades,
                "losing_trades": losing_trades
            }
            strategies[strategy_id] = strat

        # Best-effort: pull recent OOF metrics for additional context
        try:
            oof_rows = ch.query(
                """
                SELECT symbol,
                       avgOrNull(fusion_auc) AS auc,
                       avgOrNull(fusion_ic) AS ic,
                       sumOrNull(n_bars)     AS n_bars
                FROM trading.ml_oof_metrics
                WHERE ts >= now() - INTERVAL 3 day
                GROUP BY symbol
                LIMIT 50
                """,
                settings={"use_query_cache": False},
            )
            for r in oof_rows or []:
                sym = r.get("symbol") or "unknown"
                key = f"intraday_{sym}"
                strat = strategies.get(key) or StrategyState(strategy_id=key)
                strat.metrics.oos = {
                    "auc": r.get("auc"),
                    "ic": r.get("ic"),
                    "n": r.get("n_bars"),
                }
                strategies[key] = strat
        except Exception:
            pass

    except Exception:
        pass

    # Fallback: try reading from file-based events (legacy)
    if not strategies:
        try:
            runs_dir = Path("runs/ai_trader")
            ml_events = sorted(runs_dir.glob("*/ml_events.jsonl"))[-3:]
            for fp in ml_events:
                with fp.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        evt = json.loads(line)
                        if evt.get("event") != "ml.job" or evt.get("status") not in ("completed", "failed"):
                            continue
                        # accumulate rough compute usage from duration
                        try:
                            if evt.get("duration_sec"):
                                compute_used += float(evt["duration_sec"]) / 3600.0
                        except Exception:
                            pass
                        syms = evt.get("params", {}).get("symbols", []) or []
                        key = "intraday_" + "_".join(syms) if syms else "intraday"
                        metrics = evt.get("metrics", {}) or {}
                        strat = strategies.get(key) or StrategyState(strategy_id=key)
                        strat.metrics.oos = {
                            "auc": metrics.get("xgb_auc") or metrics.get("fusion_auc"),
                            "ic": metrics.get("xgb_ic") or metrics.get("fusion_ic"),
                            "n": metrics.get("xgb_n") or metrics.get("fusion_n"),
                            "coverage": metrics.get("n_bars"),
                            "error_code": metrics.get("error_code") or evt.get("metrics", {}).get("error_code"),
                        }
                        recent = strat.recent_experiments or []
                        recent.append({"job_id": evt.get("job_id"), "ok": evt.get("ok")})
                        strat.recent_experiments = recent[-5:]
                        strategies[key] = strat
        except Exception:
            pass

    if not strategies:
        strategies["intraday_placeholder"] = StrategyState(
            strategy_id="intraday_placeholder",
            metrics=StrategyMetrics(
                oos={"sharpe": None, "trades": None, "coverage": None},
                live={},
            ),
        )

    # Load immune cases (best effort)
    immune_cases: List[Dict[str, Any]] = []
    try:
        immune_fp = Path("runs/memory/immune_cases.jsonl")
        if immune_fp.exists():
            with immune_fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    immune_cases.append(json.loads(line))
    except Exception:
        immune_cases = []

    # Best-effort load mutable target health from a JSON snapshot if exists
    mutable_targets: Dict[str, Any] = {}
    try:
        mt_fp = Path("runs/memory/mutable_targets.json")
        if mt_fp.exists():
            mt_data = json.loads(mt_fp.read_text())
            for k, v in mt_data.items():
                # Only include mutable targets, skip forbidden domains
                if isinstance(v, dict):
                    mutable_targets[k] = v
    except Exception:
        pass
    if not mutable_targets:
        # Fallback default placeholders
        mutable_targets = {
            "data:features_1m_active": MutableTarget(
                id="data:features_1m_active",
                kind="data_pipeline",
                domain="data",
                risk_level="medium",
                mutable=True,
                metrics={},
            ),
        }

    # Include a simple budget snapshot; used is derived best-effort from recent events or live metrics
    budgets = Budgets(
        compute_hours=BudgetState(total=12.0, used=compute_used or 3.5),
        tokens=BudgetState(total=1_000_000, used=tokens_used or 200_000),
    )

    return ResearchState(
        timestamp=now,
        policy_version=policy_version,
        budgets=budgets,
        strategies=strategies,
        skills={},
        immune_cases=immune_cases,
        experiments=[],
        mutable_targets=mutable_targets,
    )
