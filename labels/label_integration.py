# labels/label_integration.py
"""
Integration adapter between existing wf_hpo.py label system and new Label Engine.

This module provides a bridge between:
1. Existing: build_labels_from_profile() in backtest_ml_engine.py
2. New: LabelEngine with standardized label computation

Usage in wf_hpo.py:
    from labels.label_integration import build_labels_with_sanity

    # Replace:
    # df_is = build_labels_from_profile(df_is, label_cfg, target_col)

    # With:
    # df_is, label_sanity = build_labels_with_sanity(df_is, label_cfg, target_col)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pandas as pd

from labels.label_engine import LabelEngine, LabelRegistry
from labels.label_sanity import (
    LabelSanityConfig,
    LabelSanityReport,
    run_label_sanity_checks,
    summarize_label_for_llm,
)


def build_labels_with_sanity(
    df: pd.DataFrame,
    label_cfg: Dict[str, Any],
    target_col: str = "y",
    *,
    enable_sanity: bool = True,
    sanity_cfg: Optional[LabelSanityConfig] = None,
) -> Tuple[pd.DataFrame, Optional[LabelSanityReport]]:
    """
    Enhanced label building with Label Engine + Sanity checks.

    This function acts as a drop-in replacement for build_labels_from_profile()
    with added quality validation.

    Args:
        df: DataFrame with at least ['symbol', 'ts', 'close'] columns
        label_cfg: Label profile configuration from label_profiles.yml OR
                   new format: {"label_id": "ret_10m", "params": {...}}
        target_col: Target column name to create (default: "y")
        enable_sanity: Whether to run label sanity checks (default: True)
        sanity_cfg: Optional sanity configuration (uses defaults if None)

    Returns:
        Tuple of:
            - df with target_col added
            - LabelSanityReport (if enable_sanity=True) or None

    Design:
        - Supports both old label_profiles.yml format and new label_id format
        - Uses LabelEngine if label_id is provided
        - Falls back to build_labels_from_profile() for legacy profiles
        - Adds quality checks regardless of method used

    Example (new format):
        label_cfg = {
            "label_id": "ret_10m",
            "params": {},  # Optional label-specific parameters
            "task": "reg",
        }
        df, sanity = build_labels_with_sanity(df, label_cfg, "y")

    Example (legacy format):
        label_cfg = {
            "task": "reg",
            "horizon_bars": 5,
            "base": {"transform": "log_return"},
        }
        df, sanity = build_labels_with_sanity(df, label_cfg, "y")
    """
    # Determine if this is new label_id format or legacy profile format
    label_id = label_cfg.get("label_id")
    task_type = label_cfg.get("task", "reg")

    if label_id:
        # New path: Use Label Engine
        df_labeled = _build_with_label_engine(df, label_id, target_col, label_cfg)
    else:
        # Legacy path: Use existing build_labels_from_profile()
        df_labeled = _build_with_legacy_profile(df, label_cfg, target_col)

    # Run sanity checks
    sanity_report: Optional[LabelSanityReport] = None
    if enable_sanity:
        sanity_report = _run_sanity_checks(
            df_labeled,
            label_id=label_id or "legacy_profile",
            value_col=target_col,
            task_type=task_type,
            cfg=sanity_cfg,
        )

    return df_labeled, sanity_report


def _build_with_label_engine(
    df: pd.DataFrame,
    label_id: str,
    target_col: str,
    label_cfg: Dict[str, Any],
) -> pd.DataFrame:
    """
    Build labels using Label Engine.

    Args:
        df: Input DataFrame
        label_id: Label identifier (e.g., "ret_10m", "tb_10m_atr1")
        target_col: Target column name to create
        label_cfg: Label configuration with optional params

    Returns:
        DataFrame with target_col added

    Design:
        - Initialize LabelEngine with registry
        - Attach label using label_id
        - Rename output to target_col if different
    """
    # Get registry path from config or use default
    registry_path = label_cfg.get("registry_path", "config/label_registry.json")

    # Initialize Label Engine
    label_registry = LabelRegistry(registry_path)
    label_engine = LabelEngine(label_registry)

    # Get label parameters from config
    label_params = label_cfg.get("params", {})

    # Attach label
    df_labeled = label_engine.attach_label(
        df.copy(),
        label_id=label_id,
        **label_params,
    )

    # The label engine adds a column with name = label_id
    # If target_col is different, rename it
    if label_id != target_col and label_id in df_labeled.columns:
        df_labeled = df_labeled.rename(columns={label_id: target_col})

    return df_labeled


def _build_with_legacy_profile(
    df: pd.DataFrame,
    label_cfg: Dict[str, Any],
    target_col: str,
) -> pd.DataFrame:
    """
    Build labels using legacy build_labels_from_profile() function.

    Args:
        df: Input DataFrame
        label_cfg: Legacy label profile configuration
        target_col: Target column name to create

    Returns:
        DataFrame with target_col added

    Design:
        - Import build_labels_from_profile() dynamically
        - Call with original parameters
        - This maintains backward compatibility
    """
    try:
        from backtest_ml_engine import build_labels_from_profile

        df_labeled = build_labels_from_profile(df, label_cfg, target_col)
        return df_labeled
    except ImportError as exc:
        raise ImportError(
            "backtest_ml_engine module not found. "
            "Cannot use legacy label profiles without this module."
        ) from exc


def _run_sanity_checks(
    df: pd.DataFrame,
    label_id: str,
    value_col: str,
    task_type: str,
    cfg: Optional[LabelSanityConfig],
) -> LabelSanityReport:
    """
    Run label sanity checks on the created labels.

    Args:
        df: DataFrame with label column
        label_id: Label identifier for reporting
        value_col: Name of label value column
        task_type: Task type ("reg" or "cls")
        cfg: Optional sanity configuration

    Returns:
        LabelSanityReport with quality metrics

    Design:
        - For classification tasks, try to infer side column
        - Use defaults if sanity_cfg not provided
        - Return comprehensive quality report
    """
    cfg = cfg or LabelSanityConfig(
        min_samples=1000,  # Lower threshold for small datasets
        enable_debug=False,
    )

    # For classification tasks, try to find or create side column
    side_col: Optional[str] = None
    if task_type in ("cls", "classification"):
        # Look for existing side column
        if f"{value_col}_side" in df.columns:
            side_col = f"{value_col}_side"
        elif "y_side" in df.columns and value_col == "y":
            side_col = "y_side"
        # Otherwise, run checks without side column
        # (side distribution checks will be skipped)

    report = run_label_sanity_checks(
        df,
        label_id=label_id,
        value_col=value_col,
        side_col=side_col,
        task_type=task_type,
        cfg=cfg,
    )

    return report


def integrate_sanity_into_dataset_manifest(
    dataset_manifest: Dict[str, Any],
    label_sanity: Optional[LabelSanityReport],
) -> Dict[str, Any]:
    """
    Integrate label sanity report into dataset manifest for AI brain.

    Args:
        dataset_manifest: Existing dataset manifest from wf_hpo.py
        label_sanity: Label sanity report (or None)

    Returns:
        Updated dataset manifest with label_sanity section

    Design:
        - Adds label_sanity as a top-level key in manifest
        - Converts report to dict for JSON serialization
        - If report is None, adds placeholder
        - Preserves existing manifest structure

    Example:
        manifest = {
            "dataset_sanity": {...},
            "feature_sanity": {...},
        }
        manifest = integrate_sanity_into_dataset_manifest(manifest, label_sanity)
        # Now manifest["label_sanity"] contains quality metrics
    """
    manifest = dict(dataset_manifest)  # Copy to avoid mutation

    if label_sanity:
        manifest["label_sanity"] = label_sanity.to_dict()
    else:
        manifest["label_sanity"] = {
            "is_ok": False,
            "issues": {"no_sanity_check": "Label sanity check was not performed"},
        }

    return manifest


def summarize_label_sanity_for_logging(
    label_sanity: Optional[LabelSanityReport],
) -> str:
    """
    Create a concise summary of label sanity for logging.

    Args:
        label_sanity: Label sanity report (or None)

    Returns:
        Human-readable summary string

    Design:
        - Uses summarize_label_for_llm() for consistency
        - Provides clear status for logging/debugging

    Example output:
        Label [ret_10m] summary:
        - task_type=reg, n_samples=9500, nan_ratio=0.050
        - value_stats: mean=0.00100, std=0.02000, p1=-0.04000, p50=0.00000, p99=0.04000
        - abs_mean_z=0.050
        - issues: none
    """
    if not label_sanity:
        return "Label sanity: not performed"

    summary = summarize_label_for_llm(label_sanity.to_dict())
    return summary


# Convenience function for quick integration
def build_and_check_labels(
    df: pd.DataFrame,
    label_id: str,
    target_col: str = "y",
    task_type: str = "reg",
) -> Tuple[pd.DataFrame, LabelSanityReport]:
    """
    Simplified interface: build labels with Label Engine and check quality.

    This is a convenience function for the most common use case:
    - Use Label Engine with label_id
    - Run sanity checks
    - Return both results

    Args:
        df: Input DataFrame
        label_id: Label identifier (e.g., "ret_10m")
        target_col: Target column name (default: "y")
        task_type: Task type (default: "reg")

    Returns:
        Tuple of (df_with_labels, sanity_report)

    Example:
        df, sanity = build_and_check_labels(df, "ret_10m")
        if not sanity.is_ok:
            print(f"Label quality issues: {sanity.issues}")
    """
    label_cfg = {
        "label_id": label_id,
        "task": task_type,
        "params": {},
    }

    df_labeled, sanity_report = build_labels_with_sanity(
        df,
        label_cfg,
        target_col,
        enable_sanity=True,
    )

    if sanity_report is None:
        raise RuntimeError("Sanity check was expected but not performed")

    return df_labeled, sanity_report
