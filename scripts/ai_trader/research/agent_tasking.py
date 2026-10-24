from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence


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


def _task_id(cycle_id: str, role: str, task_type: str, patch: Mapping[str, Any]) -> str:
    blob = json.dumps({"c": cycle_id, "r": role, "t": task_type, "p": dict(patch)}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
    return f"task-{cycle_id}-{role}-{digest}"


def _first_symbol(spec: Mapping[str, Any]) -> str:
    universe = _as_dict(spec.get("universe"))
    symbols = _as_list(universe.get("symbols"))
    if symbols:
        return str(symbols[0]).upper()
    underlying = universe.get("underlying")
    if underlying:
        return str(underlying).upper()
    return ""


def _selector_matches_spec(selector: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    market = str(spec.get("market") or "").strip().lower()
    symbol = _first_symbol(spec)
    strategy_family = str(spec.get("strategy_family") or "")
    sel_market = str(selector.get("market") or "").strip().lower()
    sel_symbol = str(selector.get("symbol") or "").strip().upper()
    sel_family = str(selector.get("strategy_family") or "")
    if sel_market and sel_market != market:
        return False
    if sel_symbol and sel_symbol != symbol:
        return False
    if sel_family and sel_family != strategy_family:
        return False
    return bool(sel_market or sel_symbol or sel_family)


def _priority_by_intent(research_intent: str) -> int:
    intent = str(research_intent or "").strip().lower()
    mapping = {
        "promote_candidate": 100,
        "lineage_expand": 95,
        "retest_robustness": 90,
        "retest_data": 85,
        "seed": 70,
        "portfolio_diversify": 60,
    }
    return int(mapping.get(intent, 65))


def compile_agent_tasks(
    *,
    cycle_id: str,
    cycle_policy: Mapping[str, Any] | None,
    seed_specs: Sequence[Mapping[str, Any]] | None,
    input_refs: Sequence[str] | None = None,
) -> list[Dict[str, Any]]:
    policy = _as_dict(cycle_policy)
    seeds = [dict(x) for x in (seed_specs or []) if isinstance(x, Mapping)]
    task_patches = [_as_dict(x) for x in _as_list(policy.get("task_patches"))]
    carry_forward_rows = [_as_dict(x) for x in _as_list(policy.get("carry_forward_tasks"))]
    suppressed_targets = [_as_dict(x) for x in _as_list(policy.get("suppressed_targets"))]
    legacy_suppressions = _as_dict(policy.get("suppressions"))
    suppress_markets = {str(x).strip().lower() for x in _as_list(legacy_suppressions.get("markets")) if str(x).strip()}
    suppress_symbols = {str(x).strip().upper() for x in _as_list(legacy_suppressions.get("symbols")) if str(x).strip()}
    refs = [str(x) for x in (input_refs or [])]
    now = f"{cycle_id}T00:00:00Z"

    candidates: list[Dict[str, Any]] = []
    for row in carry_forward_rows:
        patch = _as_dict(row.get("task_patch"))
        if not patch:
            continue
        carry = dict(patch)
        carry["research_intent"] = row.get("intent") or carry.get("research_intent") or "seed"
        carry["_derived_source"] = {
            "source_task_id": row.get("source_task_id"),
            "source_experiment_id": row.get("source_experiment_id"),
            "source_outcome": row.get("source_outcome"),
            "mapped_action": row.get("action") or "carry_forward",
        }
        candidates.append(carry)
    candidates.extend(seeds)

    tasks: list[Dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    for spec in candidates:
        spec = dict(spec)
        market = str(spec.get("market") or "stocks").strip().lower()
        symbol = _first_symbol(spec)
        strategy_family = str(spec.get("strategy_family") or "")
        if market in suppress_markets or (symbol and f"{market}:{symbol}".upper() in suppress_symbols):
            spec["blocked_by"] = {
                "type": "policy_suppression",
                "reason": "suppressed_by_previous_cycle_policy",
                "market": market,
                "symbol": symbol,
                "next_action": "carry_forward_or_wait_for_data",
            }
            spec["research_intent"] = "retest_data"

        suppress_match = next((row for row in suppressed_targets if _selector_matches_spec(row, spec)), None)
        if suppress_match:
            if str(suppress_match.get("action") or "") == "suppress_retired_candidate":
                continue
            spec["blocked_by"] = {
                "type": "policy_suppression",
                "reason": "blocked_by_data",
                "market": market,
                "symbol": symbol,
                "next_action": "carry_forward_or_wait_for_data",
            }
            spec["research_intent"] = "retest_data"
            spec["_derived_source"] = {
                "source_task_id": suppress_match.get("source_task_id"),
                "source_experiment_id": suppress_match.get("source_experiment_id"),
                "source_outcome": suppress_match.get("source_outcome"),
                "mapped_action": suppress_match.get("action") or "suppress_symbol",
            }

        matched_patch = next((row for row in task_patches if _selector_matches_spec(row, spec)), None)
        if matched_patch:
            patch_payload = _as_dict(matched_patch.get("patch"))
            for key, value in patch_payload.items():
                if key == "lineage":
                    base_lineage = _as_dict(spec.get("lineage"))
                    base_lineage.update(_as_dict(value))
                    spec["lineage"] = base_lineage
                elif key == "blocked_by":
                    spec["blocked_by"] = _as_dict(value)
                else:
                    spec[key] = value
            if matched_patch.get("intent"):
                spec["research_intent"] = matched_patch.get("intent")
            spec["_derived_source"] = {
                "source_task_id": matched_patch.get("source_task_id"),
                "source_experiment_id": matched_patch.get("source_experiment_id"),
                "source_outcome": matched_patch.get("source_outcome"),
                "mapped_action": matched_patch.get("action") or "task_patch",
            }

        market = str(spec.get("market") or "stocks").strip().lower()
        role = "stocks_scalp" if market == "stocks" else "options_scalp"
        intent = str(spec.get("research_intent") or "seed")
        derived_source = _as_dict(spec.get("_derived_source"))
        derived_from = {
            "policy": policy.get("source_cycle_id"),
            "recommendation": policy.get("planner_actions", []),
            "policy_path": "next_cycle_policy.json",
            "source_task_id": derived_source.get("source_task_id"),
            "source_experiment_id": derived_source.get("source_experiment_id"),
            "source_outcome": derived_source.get("source_outcome"),
            "mapped_action": derived_source.get("mapped_action"),
        }
        blocked_by = _as_dict(spec.get("blocked_by"))
        patch = {
            "market": market,
            "strategy_family": spec.get("strategy_family"),
            "engine": spec.get("engine"),
            "universe": spec.get("universe"),
            "strategy_ref": spec.get("strategy_ref"),
            "module_selection": spec.get("module_selection"),
            "parameters": spec.get("parameters"),
            "data_window": spec.get("data_window"),
            "labels_and_ml": spec.get("labels_and_ml"),
            "robustness": spec.get("robustness"),
            "execution_reality": spec.get("execution_reality"),
            "sizing": spec.get("sizing"),
            "lineage": spec.get("lineage"),
            "acceptance": spec.get("acceptance"),
        }
        task_type = "strategy_experiment"
        tid = _task_id(cycle_id, role, task_type, {"task_patch": patch, "research_intent": intent})
        if tid in seen_task_ids:
            continue
        seen_task_ids.add(tid)
        priority = _priority_by_intent(intent)
        max_retries = 0 if intent == "retest_data" else 1
        retryable = intent != "retest_data"
        tasks.append(
            {
                "task_id": tid,
                "cycle_id": cycle_id,
                "agent_role": role,
                "task_type": task_type,
                "research_intent": intent,
                "priority": priority,
                "budget_cost": {"compute_hours": 1.5, "token_cost": 0},
                "max_runtime_minutes": 60,
                "max_variants": 1,
                "max_retries": max_retries,
                "retryable": retryable,
                "retry_count": 0,
                "failure_policy": "retry_then_defer" if retryable else "no_retry_data_block",
                "defer_reason": None,
                "input_refs": refs,
                "task_patch": patch,
                "derived_from": derived_from,
                "blocked_by": blocked_by or None,
                "created_at": now,
            }
        )

    # non-execution control tasks
    for role, task_type in (("ml_brain", "analysis"), ("evaluator", "analysis")):
        tid = _task_id(cycle_id, role, task_type, {"role": role})
        if tid in seen_task_ids:
            continue
        seen_task_ids.add(tid)
        tasks.append(
            {
                "task_id": tid,
                "cycle_id": cycle_id,
                "agent_role": role,
                "task_type": task_type,
                "research_intent": "portfolio_diversify",
                "priority": _priority_by_intent("portfolio_diversify"),
                "budget_cost": {"compute_hours": 0.2, "token_cost": 0},
                "max_runtime_minutes": 20,
                "max_variants": 1,
                "max_retries": 0,
                "retryable": False,
                "retry_count": 0,
                "failure_policy": "no_retry_analysis_only",
                "defer_reason": None,
                "input_refs": refs,
                "task_patch": {"role": role, "cycle_policy": policy},
                "derived_from": {
                    "policy": policy.get("source_cycle_id"),
                    "recommendation": policy.get("planner_actions", []),
                    "policy_path": "next_cycle_policy.json",
                },
                "blocked_by": None,
                "created_at": now,
            }
        )
    return tasks


def persist_agent_tasks(tasks: Sequence[Mapping[str, Any]], *, cycle_dir: str | Path) -> Dict[str, str]:
    out = Path(cycle_dir) / "agent_tasks"
    out.mkdir(parents=True, exist_ok=True)
    tasks_payload = [dict(t) for t in tasks]
    (out / "tasks.json").write_text(json.dumps(tasks_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    role_to_file = {
        "stocks_scalp": "stocks_scalp.json",
        "options_scalp": "options_scalp.json",
        "ml_brain": "ml_brain.json",
        "evaluator": "evaluator.json",
    }
    for role, name in role_to_file.items():
        role_tasks = [dict(t) for t in tasks_payload if str(t.get("agent_role") or "") == role]
        (out / name).write_text(json.dumps(role_tasks, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "agent_tasks_dir": str(out),
        "tasks_json": str(out / "tasks.json"),
        "stocks_scalp_json": str(out / "stocks_scalp.json"),
        "options_scalp_json": str(out / "options_scalp.json"),
        "ml_brain_json": str(out / "ml_brain.json"),
        "evaluator_json": str(out / "evaluator.json"),
    }


def load_agent_tasks(cycle_dir: str | Path) -> list[Dict[str, Any]]:
    path = Path(cycle_dir) / "agent_tasks" / "tasks.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [dict(x) for x in data if isinstance(x, Mapping)]
    except Exception:
        pass
    return []
