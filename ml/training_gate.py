# ml/training_gate.py

"""
Training Gate: Pre-training data quality validation.

This module provides a hard gate before HPO/training to prevent:
- Training on insufficient data
- Training on data with excessive NaN values
- Training on data with mostly constant features
- Training on severely imbalanced labels

Based on upgradeFeatures12 Part 2 and upgradeFeatures14 specifications.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class TrainingGateConfig:
    """
    Configuration for training gate validation thresholds.

    These are conservative defaults that can be adjusted based on experience.
    All thresholds are hard limits - exceeding any threshold blocks training.
    """

    # Dataset size constraints
    # NOTE: Defaults tuned for intraday options ML. Override for other regimes.
    min_rows: int = 3_000  # Minimum dataset size for reliable training

    # Data quality constraints
    max_null_frac_per_col: float = 0.2  # Max 20% NaN per column
    max_constant_col_frac: float = 0.5  # Max 50% constant columns

    # Label quality constraints
    min_label_nonnull_frac: float = 0.9  # Min 90% non-null labels
    min_label_pos_frac: float = 0.02  # Min 2% positive samples (label > 0)
    min_label_neg_frac: float = 0.02  # Min 2% negative samples (label < 0)


@dataclass
class TrainingGateResult:
    """
    Result of training gate validation.

    Attributes:
        ok: True if all checks pass, False otherwise
        reasons: List of failure reasons (empty if ok=True)
        metrics: Dictionary of measured metrics for debugging/logging
    """

    ok: bool
    reasons: List[str]
    metrics: Dict[str, Any]


def run_training_gate(
    df: pd.DataFrame,
    *,
    label_col: str,
    cfg: Optional[TrainingGateConfig] = None,
) -> TrainingGateResult:
    """
    Run training gate validation on dataset before HPO/training.

    This is a hard gate with simple, interpretable checks:
    - Dataset has enough rows
    - No excessive NaN values per column
    - Not too many constant columns
    - Label has sufficient non-null values
    - Label has balanced positive/negative samples

    Args:
        df: DataFrame with features and label column
        label_col: Name of the label column
        cfg: Configuration for thresholds (uses defaults if None)

    Returns:
        TrainingGateResult with ok=True if all checks pass, ok=False otherwise

    Example:
        >>> gate_result = run_training_gate(df, label_col="label")
        >>> if not gate_result.ok:
        ...     raise ValueError(f"Training blocked: {gate_result.reasons}")
    """
    if cfg is None:
        cfg = TrainingGateConfig()

    reasons: List[str] = []
    metrics: Dict[str, Any] = {}

    n_rows = int(len(df))
    metrics["n_rows"] = n_rows

    # ---- Check 1: Minimum row count ----
    if n_rows < cfg.min_rows:
        reasons.append(f"rows < min_rows: {n_rows} < {cfg.min_rows}")

    # ---- Check 2: NaN fraction per column ----
    null_fracs = df.isna().mean()
    max_null_frac = float(null_fracs.max() if len(null_fracs) else 0.0)
    metrics["max_null_frac_per_col"] = max_null_frac

    if max_null_frac > cfg.max_null_frac_per_col:
        worst_col = null_fracs.idxmax() if len(null_fracs) else "unknown"
        reasons.append(
            f"max_null_frac_per_col={max_null_frac:.3f} > "
            f"{cfg.max_null_frac_per_col:.3f} (worst column: {worst_col})"
        )

    # ---- Check 3: Constant column fraction ----
    constant_cols: List[str] = []
    for col in df.columns:
        # A column is "constant" if it has ≤1 unique value (ignoring NaN)
        nunique = df[col].nunique(dropna=True)
        if nunique <= 1:
            constant_cols.append(col)

    const_frac = float(len(constant_cols) / max(len(df.columns), 1))
    metrics["constant_col_frac"] = const_frac
    metrics["constant_cols"] = constant_cols

    if const_frac > cfg.max_constant_col_frac:
        reasons.append(
            f"constant_col_frac={const_frac:.3f} > {cfg.max_constant_col_frac:.3f} "
            f"({len(constant_cols)} constant columns: {constant_cols[:5]}...)"
        )

    # ---- Check 4: Label column exists ----
    if label_col not in df.columns:
        reasons.append(f"label_col={label_col!r} not in DataFrame columns")
        # Early return since we can't do label checks
        return TrainingGateResult(ok=False, reasons=reasons, metrics=metrics)

    # ---- Check 5: Label non-null fraction ----
    label_series = df[label_col]
    label_notna = label_series.dropna()
    n_label = int(len(label_notna))
    label_nonnull_frac = float(n_label / max(n_rows, 1))
    metrics["label_nonnull_frac"] = label_nonnull_frac

    if label_nonnull_frac < cfg.min_label_nonnull_frac:
        reasons.append(
            f"label_nonnull_frac={label_nonnull_frac:.3f} < "
            f"{cfg.min_label_nonnull_frac:.3f}"
        )

    # ---- Check 6: Label balance (pos/neg samples) ----
    if n_label > 0:
        # Assumption: label is numeric, >0 = positive sample, <0 = negative sample
        # This works for forward returns, triple barrier, etc.
        arr = label_notna.to_numpy(dtype=float)
        pos_frac = float((arr > 0).mean())
        neg_frac = float((arr < 0).mean())
        zero_frac = float((arr == 0).mean())

        metrics["label_pos_frac"] = pos_frac
        metrics["label_neg_frac"] = neg_frac
        metrics["label_zero_frac"] = zero_frac

        if pos_frac < cfg.min_label_pos_frac:
            reasons.append(
                f"label_pos_frac={pos_frac:.3f} < {cfg.min_label_pos_frac:.3f} "
                f"(too few positive samples)"
            )
        if neg_frac < cfg.min_label_neg_frac:
            reasons.append(
                f"label_neg_frac={neg_frac:.3f} < {cfg.min_label_neg_frac:.3f} "
                f"(too few negative samples)"
            )
    else:
        # All labels are NaN
        reasons.append("All labels are NaN (no valid samples)")

    # ---- Final decision ----
    ok = len(reasons) == 0
    return TrainingGateResult(ok=ok, reasons=reasons, metrics=metrics)
