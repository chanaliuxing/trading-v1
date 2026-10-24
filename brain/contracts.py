# brain/contracts.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BrainDatasetContext:
    """
    AI 大脑在做"本次训练/回测决策"时看到的上下文。

    This provides the AI brain with all the information it needs to decide
    whether to proceed with training, adjust HPO parameters, or abort.

    Attributes:
        symbol: Trading symbol
        table: Data table name
        freq: Data frequency (1m, 5m, etc.)
        goal: Training goal/objective
        horizon: Prediction horizon
        dataset_manifest: Complete dataset quality report
        hpo_grid: Proposed HPO parameter grid
        last_experiments: Optional list of previous experiment results
    """
    symbol: str
    table: str
    freq: str
    goal: str
    horizon: str

    dataset_manifest: Dict[str, Any]
    hpo_grid: Dict[str, Any]

    # 可选：上次/历史实验结果（方便大脑做迭代）
    last_experiments: Optional[List[Dict[str, Any]]] = None


@dataclass
class BrainTrainingDecision:
    """
    AI 大脑基于 DatasetContext 给出的训练决策。

    This is the AI brain's decision on whether and how to proceed with training.

    Attributes:
        action: Decision type (train/skip/adjust_hpo/abort)
        reasons: Human-readable explanation for the decision
        severity: Severity level (info/warning/error)
        adjusted_hpo_grid: Optional modified HPO grid if action is adjust_hpo
        notes: Additional metadata and flags
    """
    action: str                               # "train" | "skip" | "adjust_hpo" | "abort"
    reasons: str
    severity: str = "info"                    # "info" | "warning" | "error"
    adjusted_hpo_grid: Optional[Dict[str, Any]] = None
    notes: Dict[str, Any] = field(default_factory=dict)
