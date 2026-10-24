"""Experiment persistence helpers for the Research Layer."""
from __future__ import annotations

import json
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Mapping

from .contracts import ExperimentPaths, ExperimentResult, ExperimentSpec, LineageRecord

EXPERIMENTS_ROOT = Path(os.getenv("AI_TRADER_RESEARCH_EXPERIMENTS_ROOT", "runs/ai_trader/research/experiments"))
EXP_LOG = Path(os.getenv("AI_TRADER_RESEARCH_EXPERIMENTS_LOG", "runs/ai_trader/experiments.jsonl"))

_VOLATILE_KEYS = {
    "ts",
    "timestamp",
    "created_at",
    "started_at",
    "completed_at",
    "failed_at",
    "updated_at",
    "status",
    "metrics",
    "summary_metrics",
    "artifacts",
    "result",
    "error",
    "errors",
}

_DEFAULT_ARTIFACT_FILES = {
    "trade_log": "trade_log.jsonl",
    "equity_curve": "equity_curve.csv",
    "report": "report.json",
    "robustness": "robustness.json",
    "execution_reality": "execution_reality.json",
    "lineage": "lineage.json",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: Any, max_len: int = 32) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    return raw[:max_len] or "na"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(_jsonable(v) for v in value)
    return value


def _canonical(value: Any) -> Any:
    value = _jsonable(value)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items(), key=lambda item: item[0]) if v is not None}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def _experiment_key_components(spec: ExperimentSpec) -> Dict[str, Any]:
    lineage = spec.lineage or {}
    symbols = spec.universe.get("symbols") if isinstance(spec.universe, dict) else None
    return {
        "cycle_id": spec.cycle_id,
        "market": spec.market,
        "strategy_family": spec.strategy_family,
        "template_id": (spec.strategy_ref or {}).get("template_id"),
        "definition_id": (spec.strategy_ref or {}).get("definition_id"),
        "adapter": (spec.engine or {}).get("adapter"),
        "symbols": list(symbols or []),
        "family_id": lineage.get("family_id"),
        "parent_experiment_id": lineage.get("parent_experiment_id"),
        "mutation_type": lineage.get("mutation_type"),
        "parameters": spec.parameters,
        "module_selection": spec.module_selection,
        "data_window": spec.data_window,
        "labels_and_ml": spec.labels_and_ml,
    }


def _hash_payload(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


def _ensure_spec(config: Mapping[str, Any] | ExperimentSpec) -> ExperimentSpec:
    if isinstance(config, ExperimentSpec):
        return config
    return ExperimentSpec.from_mapping(config)


def stable_experiment_id(config: Mapping[str, Any] | ExperimentSpec) -> str:
    spec = _ensure_spec(config)
    if spec.experiment_id:
        return str(spec.experiment_id)
    payload = _experiment_key_components(spec)
    digest = _hash_payload(payload)
    cycle = _slugify(spec.cycle_id or "cycle")
    market = _slugify(spec.market or "market")
    family = _slugify(spec.strategy_family or (spec.lineage or {}).get("family_id") or "exp")
    symbols = spec.universe.get("symbols") if isinstance(spec.universe, dict) else None
    symbol_slug = _slugify((symbols or ["na"])[0]) if symbols else "na"
    return f"exp-{cycle}-{market}-{family}-{symbol_slug}-{digest}"


def _experiment_dir(exp_id: str, cycle_id: str | None = None) -> Path:
    cycle_slug = _slugify(cycle_id or "uncategorized")
    return EXPERIMENTS_ROOT / cycle_slug / exp_id


def _resolve_experiment_dir(exp_id: str, cycle_id: str | None = None) -> Path:
    if cycle_id:
        return _experiment_dir(exp_id, cycle_id)
    if EXPERIMENTS_ROOT.exists():
        matches = sorted(EXPERIMENTS_ROOT.glob(f"*/{exp_id}"))
        if matches:
            return matches[0]
    return _experiment_dir(exp_id, cycle_id)


def _paths_for(exp_dir: Path) -> ExperimentPaths:
    artifacts_dir = exp_dir / "artifacts"
    artifact_paths = {name: artifacts_dir / filename for name, filename in _DEFAULT_ARTIFACT_FILES.items()}
    return ExperimentPaths(
        experiment_dir=exp_dir,
        spec_path=exp_dir / "spec.json",
        state_path=exp_dir / "state.json",
        result_path=exp_dir / "result.json",
        events_path=exp_dir / "events.jsonl",
        lineage_path=exp_dir / "lineage.json",
        artifacts_dir=artifacts_dir,
        artifact_paths=artifact_paths,
    )


def _append(row: Dict[str, Any]) -> None:
    EXP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EXP_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")


def _append_event(paths: ExperimentPaths, row: Dict[str, Any]) -> None:
    paths.events_path.parent.mkdir(parents=True, exist_ok=True)
    with paths.events_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _load_state(paths: ExperimentPaths) -> Dict[str, Any]:
    if not paths.state_path.exists():
        return {}
    try:
        return json.loads(paths.state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _default_artifact_paths(exp_id: str, cycle_id: str | None = None) -> Dict[str, str]:
    paths = _paths_for(_resolve_experiment_dir(exp_id, cycle_id))
    return {name: str(path) for name, path in paths.artifact_paths.items()}


def artifact_paths(exp_id: str, cycle_id: str | None = None) -> Dict[str, str]:
    """Return canonical artifact paths for the experiment."""
    return _default_artifact_paths(exp_id, cycle_id)


def persist_spec(config: Mapping[str, Any] | ExperimentSpec) -> ExperimentSpec:
    spec = _ensure_spec(config)
    exp_id = stable_experiment_id(spec)
    if spec.experiment_id != exp_id:
        spec = ExperimentSpec.from_mapping({**spec.to_dict(), "experiment_id": exp_id})
    exp_dir = _experiment_dir(exp_id, spec.cycle_id)
    paths = _paths_for(exp_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts_dir.mkdir(parents=True, exist_ok=True)

    lineage = LineageRecord.from_mapping({
        **(spec.lineage or {}),
        "experiment_id": exp_id,
        "family_id": (spec.lineage or {}).get("family_id") or spec.strategy_family or spec.market or "unknown",
    })
    _write_json(paths.spec_path, spec.to_dict())
    _write_json(paths.lineage_path, lineage.to_dict())

    state = {
        "experiment_id": exp_id,
        "cycle_id": spec.cycle_id,
        "status": "created",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "spec_path": str(paths.spec_path),
        "state_path": str(paths.state_path),
        "result_path": str(paths.result_path),
        "events_path": str(paths.events_path),
        "lineage_path": str(paths.lineage_path),
        "artifacts_dir": str(paths.artifacts_dir),
        "artifact_paths": {k: str(v) for k, v in paths.artifact_paths.items()},
        "spec": spec.to_dict(),
        "lineage": lineage.to_dict(),
    }
    _write_json(paths.state_path, state)
    _append_event(paths, {
        "event": "experiment_created",
        "experiment_id": exp_id,
        "cycle_id": spec.cycle_id,
        "ts": _now_iso(),
        "spec_path": str(paths.spec_path),
        "lineage_path": str(paths.lineage_path),
    })
    _append({
        "event": "experiment_created",
        "experiment_id": exp_id,
        "cycle_id": spec.cycle_id,
        "ts": _now_iso(),
        "spec_path": str(paths.spec_path),
    })
    return spec


def create_experiment(config: Mapping[str, Any] | ExperimentSpec) -> str:
    spec = persist_spec(config)
    return str(spec.experiment_id or stable_experiment_id(spec))


def start_experiment(exp_id: str, params: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    params = dict(params or {})
    spec = ExperimentSpec.from_mapping(params.get("spec") or {})
    cycle_id = params.get("cycle_id") or spec.cycle_id
    paths = _paths_for(_resolve_experiment_dir(exp_id, cycle_id))
    state = _load_state(paths)
    if not state:
        state = {
            "experiment_id": exp_id,
            "cycle_id": cycle_id,
            "spec": spec.to_dict(),
        }
    state.update({
        "experiment_id": exp_id,
        "cycle_id": cycle_id,
        "status": "running",
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "completed_at": None,
        "failed_at": None,
        "blocked_at": None,
        "error": None,
    })
    _write_json(paths.state_path, state)
    _append_event(paths, {
        "event": "experiment_started",
        "experiment_id": exp_id,
        "cycle_id": cycle_id,
        "params": _jsonable(params),
        "ts": _now_iso(),
    })
    _append({
        "event": "experiment_started",
        "experiment_id": exp_id,
        "cycle_id": cycle_id,
        "ts": _now_iso(),
    })
    return state


def run_experiment(exp_id: str, params: Mapping[str, Any]) -> None:
    start_experiment(exp_id, params)


def _terminal_event_name(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "blocked":
        return "experiment_blocked"
    if normalized in {"failed", "aborted", "error"}:
        return "experiment_failed"
    return "experiment_completed"


def _finalize_experiment(
    exp_id: str,
    *,
    status: str,
    metrics: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
    error: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    params = dict(params or {})
    spec = ExperimentSpec.from_mapping(params.get("spec") or {})
    cycle_id = params.get("cycle_id") or spec.cycle_id
    paths = _paths_for(_resolve_experiment_dir(exp_id, cycle_id))
    state = _load_state(paths)
    if not state:
        state = {
            "experiment_id": exp_id,
            "cycle_id": cycle_id,
            "spec": spec.to_dict(),
        }
    normalized_status = str(status or "failed").strip().lower() or "failed"
    event_name = _terminal_event_name(normalized_status)
    terminal_at = _now_iso()

    result = ExperimentResult.from_mapping({
        "experiment_id": exp_id,
        "cycle_id": cycle_id,
        "status": normalized_status,
        "market": spec.market,
        "strategy_family": spec.strategy_family,
        "summary_metrics": dict(metrics or {}),
        "quality_flags": params.get("quality_flags") or {},
        "robustness": params.get("robustness") or spec.robustness,
        "execution_reality": params.get("execution_reality") or spec.execution_reality,
        "sizing": params.get("sizing") or spec.sizing,
        "lineage": params.get("lineage") or spec.lineage,
        "artifacts": dict(artifacts or {}),
        "notes": params.get("notes") or ([] if not error else [error]),
        "errors": [] if not error else [error],
    })

    state.update({
        "experiment_id": exp_id,
        "cycle_id": cycle_id,
        "status": normalized_status,
        "updated_at": _now_iso(),
        "completed_at": terminal_at if normalized_status == "completed" else None,
        "failed_at": terminal_at if normalized_status in {"failed", "aborted", "error"} else None,
        "blocked_at": terminal_at if normalized_status == "blocked" else None,
        "error": error,
        "summary_metrics": result.summary_metrics,
        "quality_flags": result.quality_flags,
        "robustness": result.robustness,
        "execution_reality": result.execution_reality,
        "sizing": result.sizing,
        "lineage": result.lineage,
        "artifacts": result.artifacts,
        "result": result.to_dict(),
    })
    _write_json(paths.result_path, result.to_dict())
    _write_json(paths.state_path, state)
    _append_event(paths, {
        "event": event_name,
        "experiment_id": exp_id,
        "cycle_id": cycle_id,
        "status": normalized_status,
        "metrics": _jsonable(metrics or {}),
        "artifacts": _jsonable(artifacts or {}),
        "error": error,
        "ts": terminal_at,
    })
    _append({
        "event": event_name,
        "experiment_id": exp_id,
        "cycle_id": cycle_id,
        "status": normalized_status,
        "ts": terminal_at,
    })
    return state


def complete_experiment(
    exp_id: str,
    status: str,
    metrics: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    return _finalize_experiment(
        exp_id,
        status=status,
        metrics=metrics,
        artifacts=artifacts,
        params=params,
    )


def block_experiment(
    exp_id: str,
    error: str,
    metrics: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    return _finalize_experiment(
        exp_id,
        status="blocked",
        metrics=metrics,
        artifacts=artifacts,
        error=error,
        params=params,
    )


def fail_experiment(
    exp_id: str,
    error: str,
    metrics: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    return _finalize_experiment(
        exp_id,
        status="failed",
        metrics=metrics,
        artifacts=artifacts,
        error=error,
        params=params,
    )


def compare_variants(exp_id: str, a: Dict[str, Any], b: Dict[str, Any]) -> None:
    row = {
        "event": "experiment_compare",
        "experiment_id": exp_id,
        "variant_a": a,
        "variant_b": b,
        "ts": _now_iso(),
    }
    _append(row)


def load_experiment(exp_id: str, cycle_id: str | None = None) -> Dict[str, Any]:
    paths = _paths_for(_resolve_experiment_dir(exp_id, cycle_id))
    state = _load_state(paths)
    if not state and not paths.spec_path.exists():
        return {}
    spec = {}
    lineage = {}
    result = {}
    if paths.spec_path.exists():
        try:
            spec = json.loads(paths.spec_path.read_text(encoding="utf-8"))
        except Exception:
            spec = {}
    if paths.lineage_path.exists():
        try:
            lineage = json.loads(paths.lineage_path.read_text(encoding="utf-8"))
        except Exception:
            lineage = {}
    if paths.result_path.exists():
        try:
            result = json.loads(paths.result_path.read_text(encoding="utf-8"))
        except Exception:
            result = {}
    return {
        "state": state,
        "spec": spec,
        "lineage": lineage,
        "result": result,
        "paths": paths.as_dict(),
    }


def artifact_path(exp_id: str, name: str, cycle_id: str | None = None) -> str:
    paths = _paths_for(_resolve_experiment_dir(exp_id, cycle_id))
    if name in paths.artifact_paths:
        return str(paths.artifact_paths[name])
    return str(paths.artifacts_dir / name)
