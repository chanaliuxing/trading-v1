from __future__ import annotations

"""Structured contracts for research experiment persistence."""

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


def _copy_mapping(value: Mapping[str, Any] | None) -> Dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: Optional[str] = None
    cycle_id: Optional[str] = None
    market: Optional[str] = None
    strategy_family: Optional[str] = None
    engine: Dict[str, Any] = field(default_factory=dict)
    universe: Dict[str, Any] = field(default_factory=dict)
    strategy_ref: Dict[str, Any] = field(default_factory=dict)
    module_selection: Dict[str, Any] = field(default_factory=dict)
    parameters: Dict[str, Any] = field(default_factory=dict)
    data_window: Dict[str, Any] = field(default_factory=dict)
    labels_and_ml: Dict[str, Any] = field(default_factory=dict)
    robustness: Dict[str, Any] = field(default_factory=dict)
    execution_reality: Dict[str, Any] = field(default_factory=dict)
    sizing: Dict[str, Any] = field(default_factory=dict)
    lineage: Dict[str, Any] = field(default_factory=dict)
    acceptance: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ExperimentSpec":
        payload = dict(data or {})
        known = {
            "experiment_id",
            "cycle_id",
            "market",
            "strategy_family",
            "engine",
            "universe",
            "strategy_ref",
            "module_selection",
            "parameters",
            "data_window",
            "labels_and_ml",
            "robustness",
            "execution_reality",
            "sizing",
            "lineage",
            "acceptance",
        }
        metadata = {k: payload[k] for k in payload.keys() - known}
        return cls(
            experiment_id=payload.get("experiment_id"),
            cycle_id=payload.get("cycle_id"),
            market=payload.get("market"),
            strategy_family=payload.get("strategy_family"),
            engine=_copy_mapping(payload.get("engine")),
            universe=_copy_mapping(payload.get("universe")),
            strategy_ref=_copy_mapping(payload.get("strategy_ref")),
            module_selection=_copy_mapping(payload.get("module_selection")),
            parameters=_copy_mapping(payload.get("parameters")),
            data_window=_copy_mapping(payload.get("data_window")),
            labels_and_ml=_copy_mapping(payload.get("labels_and_ml")),
            robustness=_copy_mapping(payload.get("robustness")),
            execution_reality=_copy_mapping(payload.get("execution_reality")),
            sizing=_copy_mapping(payload.get("sizing")),
            lineage=_copy_mapping(payload.get("lineage")),
            acceptance=_copy_mapping(payload.get("acceptance")),
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["metadata"]:
            data.pop("metadata", None)
        return data


@dataclass(frozen=True)
class ExperimentResult:
    experiment_id: str
    cycle_id: Optional[str] = None
    status: str = "created"
    market: Optional[str] = None
    strategy_family: Optional[str] = None
    adapter: Optional[str] = None
    summary_metrics: Dict[str, Any] = field(default_factory=dict)
    quality_flags: Dict[str, Any] = field(default_factory=dict)
    robustness: Dict[str, Any] = field(default_factory=dict)
    execution_reality: Dict[str, Any] = field(default_factory=dict)
    sizing: Dict[str, Any] = field(default_factory=dict)
    lineage: Dict[str, Any] = field(default_factory=dict)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ExperimentResult":
        payload = dict(data or {})
        known = {
            "experiment_id",
            "cycle_id",
            "status",
            "market",
            "strategy_family",
            "adapter",
            "summary_metrics",
            "quality_flags",
            "robustness",
            "execution_reality",
            "sizing",
            "lineage",
            "artifacts",
            "notes",
            "errors",
        }
        metadata = {k: payload[k] for k in payload.keys() - known}
        return cls(
            experiment_id=str(payload.get("experiment_id") or ""),
            cycle_id=payload.get("cycle_id"),
            status=str(payload.get("status") or "created"),
            market=payload.get("market"),
            strategy_family=payload.get("strategy_family"),
            adapter=payload.get("adapter"),
            summary_metrics=_copy_mapping(payload.get("summary_metrics")),
            quality_flags=_copy_mapping(payload.get("quality_flags")),
            robustness=_copy_mapping(payload.get("robustness")),
            execution_reality=_copy_mapping(payload.get("execution_reality")),
            sizing=_copy_mapping(payload.get("sizing")),
            lineage=_copy_mapping(payload.get("lineage")),
            artifacts=_copy_mapping(payload.get("artifacts")),
            notes=list(payload.get("notes") or []),
            errors=list(payload.get("errors") or []),
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["metadata"]:
            data.pop("metadata", None)
        return data


@dataclass(frozen=True)
class LineageRecord:
    family_id: str
    experiment_id: str
    parent_experiment_id: Optional[str] = None
    mutation_type: str = "seed"
    promoted_from: Optional[str] = None
    rejected_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "LineageRecord":
        payload = dict(data or {})
        known = {
            "family_id",
            "experiment_id",
            "parent_experiment_id",
            "mutation_type",
            "promoted_from",
            "rejected_reason",
        }
        metadata = {k: payload[k] for k in payload.keys() - known}
        return cls(
            family_id=str(payload.get("family_id") or ""),
            experiment_id=str(payload.get("experiment_id") or ""),
            parent_experiment_id=payload.get("parent_experiment_id"),
            mutation_type=str(payload.get("mutation_type") or "seed"),
            promoted_from=payload.get("promoted_from"),
            rejected_reason=payload.get("rejected_reason"),
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["metadata"]:
            data.pop("metadata", None)
        return data


@dataclass(frozen=True)
class ExperimentPaths:
    experiment_dir: Path
    spec_path: Path
    state_path: Path
    result_path: Path
    events_path: Path
    lineage_path: Path
    artifacts_dir: Path
    artifact_paths: Dict[str, Path]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "experiment_dir": str(self.experiment_dir),
            "spec_path": str(self.spec_path),
            "state_path": str(self.state_path),
            "result_path": str(self.result_path),
            "events_path": str(self.events_path),
            "lineage_path": str(self.lineage_path),
            "artifacts_dir": str(self.artifacts_dir),
            "artifact_paths": {k: str(v) for k, v in self.artifact_paths.items()},
        }
