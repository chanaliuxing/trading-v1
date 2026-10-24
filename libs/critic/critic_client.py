from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from libs.db.ch_pool import get_client
from libs.logger import get_logger

log = get_logger(__name__)


@dataclass
class CriticConfig:
    min_sample_size: int = 30
    block_sharpe_threshold: float = -0.2
    block_confidence_threshold: float = 0.8
    default_regime: str = "unknown"
    default_strategy: str = "generic"


def _load_config(cfg_path: str = "config/critic_policy.yaml") -> CriticConfig:
    try:
        import yaml

        cfg_raw = yaml.safe_load(Path(cfg_path).read_text())
        if not isinstance(cfg_raw, dict):
            raise ValueError("config not a mapping")
        return CriticConfig(
            min_sample_size=int(cfg_raw.get("min_sample_size", 30)),
            block_sharpe_threshold=float(cfg_raw.get("block_sharpe_threshold", -0.2)),
            block_confidence_threshold=float(cfg_raw.get("block_confidence_threshold", 0.8)),
            default_regime=str(cfg_raw.get("default_regime", "unknown")),
            default_strategy=str(cfg_raw.get("default_strategy", "generic")),
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("critic_config_load_failed", error=str(exc))
        return CriticConfig()


class CriticClient:
    """Evaluates plans using historical stats + heuristics from ClickHouse."""

    def __init__(self, cfg_path: str = "config/critic_policy.yaml") -> None:
        self.client = get_client()
        self.config = _load_config(cfg_path)

    def check_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        regime = str(plan.get("regime") or self.config.default_regime)
        strategy = str(plan.get("strategy") or self.config.default_strategy)

        stats = self._fetch_stats(regime=regime, strategy=strategy)
        heuristics = self._fetch_heuristics()

        approval_score = stats.get("sharpe")
        sample_size = stats.get("n", 0)
        # Block if negative sharpe + enough samples or high confidence heuristics say so
        should_block = False
        if approval_score is not None and sample_size >= self.config.min_sample_size:
            should_block = approval_score <= self.config.block_sharpe_threshold
        if heuristics:
            top_conf = max(h.get("confidence", 0.0) for h in heuristics)
            should_block = should_block or top_conf >= self.config.block_confidence_threshold

        return {
            "approval_score": approval_score,
            "sample_size": sample_size,
            "heuristics_hits": heuristics,
            "should_block": bool(should_block),
            "warnings": heuristics,
        }

    def _fetch_stats(self, regime: str, strategy: str) -> Dict[str, Any]:
        """Best-effort stats lookup; tolerates missing table/columns.
        
        Note: 'strategy' column is not currently in the schema, so we ignore it
        to prevent SQL errors. 'sharpe' is stored as 'oos_sharpe'.
        """
        q = """
        SELECT avg(oos_sharpe) AS sharpe, count() AS n
        FROM trading.ai_trader_memory_episodes
        WHERE regime = %(regime)s
        """
        try:
            # strategy argument ignored until schema supports it
            rows = self.client.query(q, {"regime": regime})
            if isinstance(rows, list) and rows:
                row = rows[0]
                return {
                    "sharpe": float(row.get("sharpe") or 0.0),
                    "n": int(row.get("n") or 0),
                }
        except Exception as exc:
            log.warning("critic_stats_query_failed", error=str(exc))
        return {"sharpe": None, "n": 0}

    def _fetch_heuristics(self) -> List[Dict[str, Any]]:
        q = """
        SELECT advice, confidence, condition, metrics
        FROM trading.knowledge_heuristics
        WHERE status = 'ACTIVE'
        """
        try:
            rows = self.client.query(q)
            hits: List[Dict[str, Any]] = []
            if isinstance(rows, list):
                for r in rows:
                    hit = {
                        "advice": r.get("advice"),
                        "confidence": float(r.get("confidence") or 0.0),
                        "condition": r.get("condition"),
                        "metrics": r.get("metrics") if isinstance(r.get("metrics"), dict) else {},
                    }
                    hits.append(hit)
            return hits
        except Exception as exc:
            log.warning("critic_heuristics_query_failed", error=str(exc))
            return []
