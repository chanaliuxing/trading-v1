from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_execution_feedback(
    *,
    cycle_id: str,
    shadow_runs_dir: str | Path,
) -> Dict[str, Any]:
    root = Path(shadow_runs_dir)
    rows: list[Dict[str, Any]] = []
    for run_dir in sorted(root.iterdir()) if root.exists() else []:
        if not run_dir.is_dir():
            continue
        summary_path = run_dir / "run_summary.json"
        if not summary_path.exists():
            continue
        summary = _as_dict(json.loads(summary_path.read_text(encoding="utf-8")))
        candidate_id = str(summary.get("candidate_id") or run_dir.name)
        experiment_id = str(summary.get("experiment_id") or "")
        observed = _as_dict(summary.get("observed"))
        expected = _as_dict(summary.get("expected"))
        allocation = _as_dict(summary.get("allocation"))
        result_ref = _as_dict(summary.get("result_ref"))
        result = _as_dict(result_ref.get("result"))
        market = str(result.get("market") or "").strip().lower()
        universe = _as_dict(result.get("universe"))
        symbols = universe.get("symbols") if isinstance(universe.get("symbols"), list) else []
        symbol = str(symbols[0] if symbols else universe.get("underlying") or "").upper()
        strategy_family = str(result.get("strategy_family") or "")
        source_task_id = str(result.get("task_id") or "")

        observed_fill = float(observed.get("fill_ratio") or 0.0)
        expected_fill = float(expected.get("fill_ratio") or 0.0)
        fill_quality = round(observed_fill - expected_fill, 4)
        observed_slippage = float(observed.get("modeled_slippage_bps") or 0.0)
        expected_slippage = float(expected.get("modeled_slippage_bps") or 0.0)
        slippage_delta = round(observed_slippage - expected_slippage, 4)
        observed_latency = float(observed.get("latency_ms_p50") or 0.0)

        if slippage_delta > 1.5:
            feedback_status = "slippage_bad"
            next_action = "tighten_execution_constraints"
        elif fill_quality < -0.05:
            feedback_status = "fill_quality_bad"
            next_action = "downgrade_or_retest"
        else:
            feedback_status = "execution_ok"
            next_action = "keep_candidate"

        rows.append(
            {
                "candidate_id": candidate_id,
                "experiment_id": experiment_id,
                "source_task_id": source_task_id,
                "run_mode": str(summary.get("run_mode") or "shadow"),
                "market": market,
                "symbol": symbol,
                "strategy_family": strategy_family,
                "expected_vs_realized": {
                    "fill_ratio_expected": expected_fill,
                    "fill_ratio_observed": observed_fill,
                    "slippage_bps_expected": expected_slippage,
                    "slippage_bps_observed": observed_slippage,
                },
                "fill_quality": fill_quality,
                "slippage_delta": slippage_delta,
                "latency_observed": observed_latency,
                "feedback_status": feedback_status,
                "next_action": next_action,
                "allocation": allocation,
            }
        )

    summary = {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": rows,
        "stats": {
            "execution_ok": sum(1 for x in rows if x.get("feedback_status") == "execution_ok"),
            "slippage_bad": sum(1 for x in rows if x.get("feedback_status") == "slippage_bad"),
            "fill_quality_bad": sum(1 for x in rows if x.get("feedback_status") == "fill_quality_bad"),
        },
    }
    return summary


def persist_execution_feedback(
    *,
    feedback: Mapping[str, Any],
    output_dir: str | Path,
    state_root: str | Path = "runs/ai_trader/research/state",
) -> Dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    feedback_path = out / "execution_feedback.json"
    feedback_path.write_text(json.dumps(dict(feedback), indent=2, ensure_ascii=False), encoding="utf-8")

    state_dir = Path(state_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    index_path = state_dir / "live_feedback_index.json"
    index = {"updated_at": None, "items": []}
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                index = loaded
        except Exception:
            index = {"updated_at": None, "items": []}
    items = index.get("items")
    if not isinstance(items, list):
        items = []
    for row in feedback.get("items", []) if isinstance(feedback, Mapping) else []:
        if isinstance(row, Mapping):
            items.append(dict(row))
    dedup: Dict[str, Dict[str, Any]] = {}
    for row in items:
        exp_id = str(_as_dict(row).get("experiment_id") or "")
        cid = str(_as_dict(row).get("candidate_id") or "")
        key = f"{exp_id}|{cid}"
        if not key.strip("|"):
            continue
        dedup[key] = dict(row)
    index_payload = {
        "updated_at": _iso_now(),
        "items": list(dedup.values()),
    }
    index_path.write_text(json.dumps(index_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "execution_feedback_json": str(feedback_path),
        "live_feedback_index_json": str(index_path),
    }
