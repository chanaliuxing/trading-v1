#!/usr/bin/env python3
from __future__ import annotations

"""
Collect basic mutable_targets health from ClickHouse/logs (best effort) and write to runs/memory/mutable_targets.json.
Currently captures:
- features_1m_active staleness/errors as proxy for data pipeline health
- counts of recent code_patch_experiment failures from experiments log
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any

try:
    from libs.db.ch_pool import get_client
except Exception:
    get_client = None  # type: ignore


def _load_experiment_failures(days: int = 1) -> Dict[str, int]:
    fp = Path("runs/ai_trader/evolution_experiments.jsonl")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    fail_counts: Dict[str, int] = {}
    if not fp.exists():
        return fail_counts
    with fp.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            # no timestamp in schema; skip cutoff filtering
            if row.get("test_result") == "FAIL":
                tgt = row.get("target_skill") or "unknown"
                fail_counts[tgt] = fail_counts.get(tgt, 0) + 1
    return fail_counts


def _score_health(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """
    Derive a coarse 0-10 health score based on staleness/errors/immune cases/failures.
    """
    score = 10.0
    lag = metrics.get("lag_hours")
    if lag is not None:
        if lag > 24:
            score -= 2.0
        if lag > 72:
            score -= 2.0
        if lag > 168:
            score -= 2.0
    err_rate = metrics.get("error_rate")
    if err_rate is not None:
        score -= min(5.0, err_rate * 10.0)
    immune = metrics.get("immune_cases_7d")
    if immune is not None:
        score -= min(3.0, immune * 0.5)
    failures = metrics.get("failures_24h")
    try:
        failures_val = failures if isinstance(failures, int) else sum(failures.values()) if isinstance(failures, dict) else 0
    except Exception:
        failures_val = 0
    if failures_val:
        score -= min(2.0, failures_val * 0.5)
    score = max(0.0, score)
    metrics["health_score"] = round(score, 2)
    metrics["recommendation"] = "use" if score >= 7.0 else "sync_or_fallback"
    return metrics


def _table_health(table: str, symbols: list[str]) -> Dict[str, Any]:
    if not get_client:
        return {}
    try:
        ch = get_client()
        res = ch.query(
            f"""
            SELECT
                max(ts) AS max_ts,
                count() AS rows
            FROM {table}
            WHERE symbol IN %(syms)s
            """,
            {"syms": symbols},
            settings={"use_query_cache": False},
        )
        if not res:
            return {}
        max_ts = res[0].get("max_ts")
        rows = int(res[0].get("rows", 0) or 0)
        lag_hours = None
        if max_ts:
            lag_hours = (datetime.now(timezone.utc) - max_ts).total_seconds() / 3600
        return {"rows": rows, "max_ts": str(max_ts) if max_ts else None, "lag_hours": lag_hours}
    except Exception:
        return {}


def _error_rate(table: str, symbols: list[str]) -> Dict[str, Any]:
    if not get_client:
        return {}
    try:
        ch = get_client()
        res = ch.query(
            f"""
            SELECT
                countIf(error != '') AS error_rows,
                count() AS total_rows
            FROM {table}
            WHERE symbol IN %(syms)s
        """,
            {"syms": symbols},
            settings={"use_query_cache": False},
        )
        if not res:
            return {}
        total = int(res[0].get("total_rows", 0) or 0)
        err = int(res[0].get("error_rows", 0) or 0)
        err_rate = err / total if total else None
        return {"error_rows": err, "total_rows": total, "error_rate": err_rate}
    except Exception:
        return {}


def _agg_immune_cases(days: int = 7) -> Dict[str, int]:
    fp = Path("runs/memory/immune_cases.jsonl")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    counts: Dict[str, int] = {}
    if not fp.exists():
        return counts
    with fp.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("ts")
            try:
                if ts and datetime.fromisoformat(ts) < cutoff:
                    continue
            except Exception:
                pass
            target = row.get("component") or row.get("type") or "unknown"
            counts[target] = counts.get(target, 0) + 1
    return counts


def main() -> None:
    mutable: Dict[str, Any] = {}
    try:
        fail_counts = _load_experiment_failures()
        if fail_counts:
            mutable["tool:code_evolver"] = {
                "id": "tool:code_evolver",
                "kind": "tool",
                "domain": "research",
                "risk_level": "medium",
                "mutable": True,
                "metrics": _score_health({"failures_24h": sum(fail_counts.values())}),
            }
    except Exception:
        pass

    # Data/feature table health (best effort)
    symbols = ["TSLA", "NVDA"]
    try:
        feat_active = _table_health("trading.features_1m_active", symbols)
        if feat_active:
            feat_active.update(_error_rate("trading.features_1m_active", symbols))
            feat_active["failures_24h"] = fail_counts.get("data:features_1m_active", 0) if 'fail_counts' in locals() else 0
            feat_active = _score_health(feat_active)
            mutable["data:features_1m_active"] = {
                "id": "data:features_1m_active",
                "kind": "data_pipeline",
                "domain": "data",
                "risk_level": "medium",
                "mutable": True,
                "metrics": feat_active,
            }
        feat_base = _table_health("trading.features_1m", symbols)
        if feat_base:
            feat_base.update(_error_rate("trading.features_1m", symbols))
            feat_base["failures_24h"] = fail_counts.get("data:features_1m", 0) if 'fail_counts' in locals() else 0
            feat_base = _score_health(feat_base)
            mutable["data:features_1m"] = {
                "id": "data:features_1m",
                "kind": "data_pipeline",
                "domain": "data",
                "risk_level": "medium",
                "mutable": True,
                "metrics": feat_base,
            }
        labels = _table_health("trading.features_1m_with_labels", symbols)
        if labels:
            labels.update(_error_rate("trading.features_1m_with_labels", symbols))
            labels["failures_24h"] = fail_counts.get("data:features_1m_with_labels", 0) if 'fail_counts' in locals() else 0
            labels = _score_health(labels)
            mutable["data:features_1m_with_labels"] = {
                "id": "data:features_1m_with_labels",
                "kind": "data_pipeline",
                "domain": "data",
                "risk_level": "medium",
                "mutable": True,
                "metrics": labels,
            }
    except Exception:
        pass

    # Merge immune case counts as part of mutable metrics
    try:
        immune_counts = _agg_immune_cases()
        if immune_counts:
            for k, v in mutable.items():
                metrics = v.get("metrics", {})
                metrics["immune_cases_7d"] = immune_counts.get(k, 0)
                v["metrics"] = metrics
    except Exception:
        pass

    out = Path("runs/memory/mutable_targets.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(mutable, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
