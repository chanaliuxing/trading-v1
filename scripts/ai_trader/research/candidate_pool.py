from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, ensure_ascii=False), encoding="utf-8")


def update_candidate_pool_from_evaluation(
    *,
    cycle_id: str,
    evaluation_bundle: Mapping[str, Any],
    state_dir: str | Path = "runs/ai_trader/research/state",
) -> Dict[str, str]:
    state_root = Path(state_dir)
    candidate_path = state_root / "candidate_pool.json"
    lineage_path = state_root / "lineage_index.json"

    candidate_pool = _load_json(candidate_path) or {"updated_at": None, "candidates": []}
    lineage_index = _load_json(lineage_path) or {"updated_at": None, "records": []}
    candidates = _as_list(_as_dict(candidate_pool).get("candidates"))
    records = _as_list(_as_dict(lineage_index).get("records"))

    bundle = _as_dict(evaluation_bundle)
    decision = _as_dict(bundle.get("promotion_decision"))
    leaderboard = _as_list(bundle.get("leaderboard"))
    leaderboard_map = {
        str(_as_dict(row).get("experiment_id") or ""): _as_dict(row) for row in leaderboard
    }

    def _upsert_candidate(exp_id: str, payload: Dict[str, Any]) -> None:
        for item in candidates:
            row = _as_dict(item)
            if str(row.get("experiment_id") or "") == exp_id:
                row.update(payload)
                return
        candidates.append(payload)

    def _upsert_lineage(exp_id: str, payload: Dict[str, Any]) -> None:
        for item in records:
            row = _as_dict(item)
            if str(row.get("experiment_id") or "") == exp_id:
                row.update(payload)
                return
        records.append(payload)

    for winner in _as_list(decision.get("winners")):
        row = _as_dict(winner)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        lb = _as_dict(leaderboard_map.get(exp_id))
        result = _as_dict(lb.get("result"))
        lineage = _as_dict(result.get("lineage"))
        _upsert_candidate(
            exp_id,
            {
                "experiment_id": exp_id,
                "status": "promoted",
                "tier": row.get("tier") or "candidate",
                "score": row.get("score"),
                "market": result.get("market"),
                "strategy_family": result.get("strategy_family"),
                "promoted_at_cycle": cycle_id,
                "updated_at": datetime.utcnow().isoformat(),
            },
        )
        _upsert_lineage(
            exp_id,
            {
                "family_id": lineage.get("family_id") or result.get("strategy_family") or "unknown",
                "experiment_id": exp_id,
                "parent_experiment_id": lineage.get("parent_experiment_id"),
                "mutation_type": lineage.get("mutation_type") or "seed",
                "status": "promoted",
                "promoted_at_cycle": cycle_id,
                "retest_reason": None,
                "rejected_reason": None,
            },
        )

    for retest in _as_list(decision.get("retest_queue")):
        row = _as_dict(retest)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        lb = _as_dict(leaderboard_map.get(exp_id))
        result = _as_dict(lb.get("result"))
        lineage = _as_dict(result.get("lineage"))
        _upsert_candidate(
            exp_id,
            {
                "experiment_id": exp_id,
                "status": "retest",
                "score": row.get("score"),
                "market": result.get("market"),
                "strategy_family": result.get("strategy_family"),
                "retest_reason": row.get("reason"),
                "updated_at": datetime.utcnow().isoformat(),
            },
        )
        _upsert_lineage(
            exp_id,
            {
                "family_id": lineage.get("family_id") or result.get("strategy_family") or "unknown",
                "experiment_id": exp_id,
                "parent_experiment_id": lineage.get("parent_experiment_id"),
                "mutation_type": lineage.get("mutation_type") or "retest",
                "status": "retest",
                "promoted_at_cycle": None,
                "retest_reason": row.get("reason"),
                "rejected_reason": None,
            },
        )

    for rejected in _as_list(decision.get("rejected")):
        row = _as_dict(rejected)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        lb = _as_dict(leaderboard_map.get(exp_id))
        result = _as_dict(lb.get("result"))
        lineage = _as_dict(result.get("lineage"))
        _upsert_candidate(
            exp_id,
            {
                "experiment_id": exp_id,
                "status": "rejected",
                "score": row.get("score"),
                "market": result.get("market"),
                "strategy_family": result.get("strategy_family"),
                "rejected_reason": row.get("reason"),
                "updated_at": datetime.utcnow().isoformat(),
            },
        )
        _upsert_lineage(
            exp_id,
            {
                "family_id": lineage.get("family_id") or result.get("strategy_family") or "unknown",
                "experiment_id": exp_id,
                "parent_experiment_id": lineage.get("parent_experiment_id"),
                "mutation_type": lineage.get("mutation_type") or "seed",
                "status": "rejected",
                "promoted_at_cycle": None,
                "retest_reason": None,
                "rejected_reason": row.get("reason"),
            },
        )

    candidate_pool["updated_at"] = datetime.utcnow().isoformat()
    dedup_candidates: list[Dict[str, Any]] = []
    seen_exp: set[str] = set()
    for item in candidates:
        row = _as_dict(item)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id or exp_id in seen_exp:
            continue
        seen_exp.add(exp_id)
        dedup_candidates.append(row)
    candidate_pool["candidates"] = dedup_candidates
    lineage_index["updated_at"] = datetime.utcnow().isoformat()
    dedup_records: list[Dict[str, Any]] = []
    seen_lineage_exp: set[str] = set()
    for item in records:
        row = _as_dict(item)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id or exp_id in seen_lineage_exp:
            continue
        seen_lineage_exp.add(exp_id)
        dedup_records.append(row)
    lineage_index["records"] = dedup_records

    _write_json(candidate_path, candidate_pool)
    _write_json(lineage_path, lineage_index)
    return {
        "candidate_pool_json": str(candidate_path),
        "lineage_index_json": str(lineage_path),
    }
