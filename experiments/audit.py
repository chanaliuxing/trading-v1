# experiments/audit.py

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TrialRecord:
    """
    单个 HPO trial 的记录。

    Attributes:
        trial_id: Trial identifier (integer index)
        params: Hyperparameters used in this trial
        metrics: Performance metrics from this trial
        status: Trial status (ok/failed/skipped)
        notes: Optional notes or error messages
    """
    trial_id: int
    params: Dict[str, Any]
    metrics: Dict[str, Any]
    status: str  # "ok" | "failed" | "skipped"
    notes: Optional[str] = None


@dataclass
class ExperimentRecord:
    """
    一次完整"AI 大脑 + PI + HPO"的实验记录。

    This is the complete audit trail for one experiment run, including:
    - What data was used (symbol, table, time range)
    - What label and features were used
    - What the AI brain decided
    - All HPO trials and their results

    Attributes:
        run_id: Unique experiment identifier (UUID)
        created_at: Creation timestamp (ISO format)
        symbol: Trading symbol
        table: Data table name
        freq: Data frequency
        goal: Training goal
        horizon: Prediction horizon
        label_id: Label identifier from registry
        label_manifest: Label definition and metadata
        feature_recipe_id: Optional feature recipe identifier
        feature_recipe: Feature recipe used
        dataset_manifest: Dataset quality report
        brain_decision: AI brain's decision
        hpo_grid: HPO parameter grid
        status: Experiment status (running/completed/aborted/failed)
        error: Optional error message
        summary_metrics: Best trial metrics
        trials: List of all trial records
    """
    run_id: str
    created_at: str

    symbol: str
    table: str
    freq: str
    goal: str
    horizon: str

    # 来自 LabelEngine / LabelRegistry
    label_id: str
    label_manifest: Dict[str, Any]

    # 特征配方 & 版本
    feature_recipe_id: Optional[str]
    feature_recipe: Dict[str, Any]

    # 数据体检整体报告
    dataset_manifest: Dict[str, Any]

    # 大脑的决策
    brain_decision: Dict[str, Any]

    # HPO 网格快照（进入 HPO 之前的）
    hpo_grid: Dict[str, Any]

    # 实验状态
    status: str = "running"  # "running" | "completed" | "aborted" | "failed"
    error: Optional[str] = None

    # 汇总指标（整个 experiment 的 best trial）
    summary_metrics: Dict[str, Any] = field(default_factory=dict)

    # 各个 trial 的详细记录
    trials: List[TrialRecord] = field(default_factory=list)


class ExperimentRunLogger:
    """
    负责把 ExperimentRecord 持久化到一个 JSONL 文件。

    Design:
    - Append-only: Only writes on end_run() to minimize I/O
    - One logger per experiment run
    - JSONL format for easy parsing and append operations

    Usage:
        logger = start_experiment_run(...)
        logger.log_trial(...)
        logger.set_summary_metrics(...)
        logger.end_run(status="completed")
    """

    def __init__(self, log_path: Path, record: ExperimentRecord) -> None:
        """
        Initialize logger with log path and experiment record.

        Args:
            log_path: Path to JSONL log file
            record: Experiment record to track
        """
        self._log_path = log_path
        self._record = record
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def run_id(self) -> str:
        """Return experiment run ID."""
        return self._record.run_id

    def log_trial(
        self,
        *,
        trial_id: int,
        params: Dict[str, Any],
        metrics: Dict[str, Any],
        status: str = "ok",
        notes: Optional[str] = None,
    ) -> None:
        """
        Log a single HPO trial.

        Args:
            trial_id: Trial identifier
            params: Hyperparameters used
            metrics: Performance metrics
            status: Trial status (ok/failed/skipped)
            notes: Optional notes or error message
        """
        rec = TrialRecord(
            trial_id=trial_id,
            params=params,
            metrics=metrics,
            status=status,
            notes=notes,
        )
        self._record.trials.append(rec)

    def set_summary_metrics(self, metrics: Dict[str, Any]) -> None:
        """
        Set summary metrics for the experiment (typically best trial metrics).

        Args:
            metrics: Summary metrics dict
        """
        self._record.summary_metrics = dict(metrics)

    def end_run(self, *, status: str, error: Optional[str] = None) -> None:
        """
        End the experiment run and write to log file.

        Args:
            status: Final status (completed/aborted/failed)
            error: Optional error message
        """
        self._record.status = status
        self._record.error = error

        payload = asdict(self._record)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def start_experiment_run(
    *,
    log_dir: str,
    symbol: str,
    table: str,
    freq: str,
    goal: str,
    horizon: str,
    label_id: str,
    label_manifest: Dict[str, Any],
    feature_recipe_id: Optional[str],
    feature_recipe: Dict[str, Any],
    dataset_manifest: Dict[str, Any],
    brain_decision: Dict[str, Any],
    hpo_grid: Dict[str, Any],
) -> ExperimentRunLogger:
    """
    对外唯一入口：新建一次实验记录，并返回一个 logger handle。

    Args:
        log_dir: Directory for experiment logs
        symbol: Trading symbol
        table: Data table name
        freq: Data frequency
        goal: Training goal
        horizon: Prediction horizon
        label_id: Label identifier
        label_manifest: Label definition
        feature_recipe_id: Optional feature recipe ID
        feature_recipe: Feature recipe
        dataset_manifest: Dataset quality report
        brain_decision: AI brain decision
        hpo_grid: HPO parameter grid

    Returns:
        ExperimentRunLogger instance for tracking this experiment
    """
    run_id = str(uuid.uuid4())
    record = ExperimentRecord(
        run_id=run_id,
        created_at=_now_iso(),
        symbol=symbol,
        table=table,
        freq=freq,
        goal=goal,
        horizon=horizon,
        label_id=label_id,
        label_manifest=label_manifest,
        feature_recipe_id=feature_recipe_id,
        feature_recipe=feature_recipe,
        dataset_manifest=dataset_manifest,
        brain_decision=brain_decision,
        hpo_grid=hpo_grid,
    )

    log_path = Path(log_dir) / "experiments.jsonl"
    return ExperimentRunLogger(log_path=log_path, record=record)
