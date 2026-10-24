# experiments/summary.py

from __future__ import annotations

from typing import Any, Dict, List, Optional

import math

from experiments.registry import (
    load_experiment_records,
    top_n_by_metric,
    DEFAULT_INDEX_PATH,
)
from experiments.types import ExperimentRecord


def _metric_range(records: List[ExperimentRecord], field: str) -> Optional[Dict[str, float]]:
    vals = []
    for r in records:
        v = getattr(r.metrics, field, None)
        if v is not None and not math.isnan(float(v)):
            vals.append(float(v))
    if not vals:
        return None
    return {"min": min(vals), "max": max(vals)}


def build_experiment_summary_for_llm(
    *,
    symbol: str,
    freq: str,
    horizon: str,
    label_id: str,
    index_path=DEFAULT_INDEX_PATH,
    top_k: int = 3,
) -> Dict[str, Any]:
    """
    读取实验 index，抽取针对某个 (symbol, freq, horizon, label_id) 的核心摘要，
    直接可以塞给 brain.context_builder.build_brain_prompts(..., experiment_summary=这里的返回值).
    """
    records = load_experiment_records(
        index_path=index_path,
        symbol=symbol,
        freq=freq,
        horizon=horizon,
        label_id=label_id,
    )

    if not records:
        return {
            "status": "no_history",
            "message": f"No experiments found for {symbol} @ {freq} / {horizon} / label={label_id}",
        }

    # 按 Sharpe 拿 top-k
    top_by_sharpe = top_n_by_metric(records, "sharpe", n=top_k)
    top_by_ic = top_n_by_metric(records, "ic", n=top_k)

    sharpe_range = _metric_range(records, "sharpe")
    ic_range = _metric_range(records, "ic")
    mdd_range = _metric_range(records, "max_drawdown")
    hit_range = _metric_range(records, "hit_rate")

    def _compact(rec: ExperimentRecord) -> Dict[str, Any]:
        return {
            "run_id": rec.run_id,
            "run_ts": rec.run_ts,
            "model_type": rec.model_type,
            "label_id": rec.label_id,
            "sharpe": rec.metrics.sharpe,
            "ic": rec.metrics.ic,
            "hit_rate": rec.metrics.hit_rate,
            "max_drawdown": rec.metrics.max_drawdown,
            "trades": rec.metrics.trades,
            "pnl": rec.metrics.pnl,
            "tags": rec.tags,
        }

    summary = {
        "status": "ok",
        "symbol": symbol,
        "freq": freq,
        "horizon": horizon,
        "label_id": label_id,
        "num_experiments": len(records),
        "sharpe_range": sharpe_range,
        "ic_range": ic_range,
        "max_drawdown_range": mdd_range,
        "hit_rate_range": hit_range,
        "top_by_sharpe": [_compact(r) for r in top_by_sharpe],
        "top_by_ic": [_compact(r) for r in top_by_ic],
    }
    return summary
