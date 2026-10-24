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


def run_shadow_candidates(
    *,
    shadow_candidates_dir: str | Path,
    output_dir: str | Path,
) -> Dict[str, Any]:
    src = Path(shadow_candidates_dir)
    out = Path(output_dir) / "shadow_runs"
    out.mkdir(parents=True, exist_ok=True)

    runs: list[Dict[str, Any]] = []
    if not src.exists():
        return {"shadow_runs_dir": str(out), "runs": runs}

    for candidate_dir in sorted(src.iterdir()):
        if not candidate_dir.is_dir():
            continue
        manifest_path = candidate_dir / "candidate_manifest.json"
        exp_ref_path = candidate_dir / "experiment_ref.json"
        alloc_ref_path = candidate_dir / "allocation_ref.json"
        constraints_path = candidate_dir / "execution_constraints.json"
        if not manifest_path.exists():
            continue

        manifest = _as_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        exp_ref = _as_dict(json.loads(exp_ref_path.read_text(encoding="utf-8"))) if exp_ref_path.exists() else {}
        alloc_ref = _as_dict(json.loads(alloc_ref_path.read_text(encoding="utf-8"))) if alloc_ref_path.exists() else {}
        constraints = _as_dict(json.loads(constraints_path.read_text(encoding="utf-8"))) if constraints_path.exists() else {}

        candidate_id = str(candidate_dir.name)
        experiment_id = str(manifest.get("experiment_id") or candidate_id)
        started_at = _iso_now()
        max_orders = int(constraints.get("max_orders_per_day") or 20)
        orders = max(1, min(max_orders, int(alloc_ref.get("target_weight", 0.1) * 20) + 4))
        fill_ratio_target = 0.85 if str(manifest.get("promotion_target") or "") == "paper_candidate" else 0.92
        run_mode = "paper" if str(manifest.get("promotion_target") or "") == "paper_candidate" else "shadow"
        fills = max(1, int(round(orders * fill_ratio_target)))
        status = "completed"

        candidate_run_dir = out / candidate_id
        candidate_run_dir.mkdir(parents=True, exist_ok=True)
        run_manifest = {
            "candidate_id": candidate_id,
            "experiment_id": experiment_id,
            "allocation": alloc_ref,
            "execution_constraints": constraints,
            "started_at": started_at,
        }
        (candidate_run_dir / "run_manifest.json").write_text(
            json.dumps(run_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        log_path = candidate_run_dir / "execution_log.jsonl"
        with log_path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": started_at,
                        "event": "run_started",
                        "candidate_id": candidate_id,
                        "experiment_id": experiment_id,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "ts": _iso_now(),
                        "event": "orders_simulated",
                        "candidate_id": candidate_id,
                        "orders_simulated": orders,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "ts": _iso_now(),
                        "event": "fills_simulated",
                        "candidate_id": candidate_id,
                        "fills_simulated": fills,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        completed_at = _iso_now()
        summary = {
            "candidate_id": candidate_id,
            "experiment_id": experiment_id,
            "allocation": alloc_ref,
            "execution_status": status,
            "run_mode": run_mode,
            "orders_simulated": orders,
            "fills_simulated": fills,
            "started_at": started_at,
            "completed_at": completed_at,
            "observed": {
                "fill_ratio": round(fills / max(orders, 1), 4),
                "modeled_slippage_bps": 6.0 if fills / max(orders, 1) > 0.9 else 11.5,
                "latency_ms_p50": 320 if fills / max(orders, 1) > 0.9 else 780,
            },
            "expected": {
                "fill_ratio": 0.9,
                "modeled_slippage_bps": 8.0,
                "latency_ms_p50": 500,
            },
            "result_ref": exp_ref,
        }
        (candidate_run_dir / "run_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        runs.append(summary)

    return {"shadow_runs_dir": str(out), "runs": runs}
