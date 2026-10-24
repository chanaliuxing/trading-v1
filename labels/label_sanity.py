# labels/label_sanity.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


@dataclass
class LabelSanityConfig:
    """
    针对标签分布的体检阈值。
    可根据经验/回测慢慢调优。

    Attributes:
        min_samples: Minimum valid samples (non-NaN labels)
        max_abs_mean_z_reg: Max allowed |z-score| of mean for regression
        max_majority_ratio_cls: Max allowed majority class ratio for classification
        extreme_tail_ratio_reg: Max allowed ratio of extreme values
        enable_debug: Enable debug printing
    """
    # 最小有效样本数（非 NaN 的 label）
    min_samples: int = 5_000

    # 对回归标签: 允许的最大 |z-score|（均值偏移）
    max_abs_mean_z_reg: float = 1.5

    # 对分类标签: 允许的最大 majority 比例
    max_majority_ratio_cls: float = 0.99  # >0.99 认为极度不平衡

    # 连续标签中极端值的比例上限 (如 >99% 分位 |y| > limit)
    extreme_tail_ratio_reg: float = 0.02  # >2% 极端尾部

    # 打印/记录用
    enable_debug: bool = False


@dataclass
class LabelSanityReport:
    """
    标签健康报告：既能给 Gate 用，也能塞进 dataset_manifest 给 AI 大脑看。

    This report provides comprehensive label quality metrics for both
    regression and classification tasks.

    Attributes:
        label_id: Label identifier
        value_col: Value column name
        side_col: Optional side/class column name
        task_type: Task type (reg or cls)
        n_samples: Number of valid samples
        nan_ratio: Ratio of NaN values
        mean: Mean value (regression)
        std: Standard deviation (regression)
        min: Minimum value (regression)
        max: Maximum value (regression)
        p1: 1st percentile (regression)
        p50: Median (regression)
        p99: 99th percentile (regression)
        abs_mean_z: Absolute z-score of mean (regression)
        extreme_tail_ratio: Ratio of extreme tail values (regression)
        n_pos: Number of positive samples (classification)
        n_neg: Number of negative samples (classification)
        n_zero: Number of zero/neutral samples (classification)
        majority_ratio: Majority class ratio (classification)
        is_ok: Overall health status
        issues: Dictionary of detected issues
    """
    label_id: str
    value_col: str
    side_col: Optional[str]

    task_type: str  # "reg" | "cls"

    n_samples: int
    nan_ratio: float

    # 回归统计
    mean: Optional[float] = None
    std: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    p1: Optional[float] = None
    p50: Optional[float] = None
    p99: Optional[float] = None
    abs_mean_z: Optional[float] = None
    extreme_tail_ratio: Optional[float] = None

    # 分类统计 (side)
    n_pos: Optional[int] = None
    n_neg: Optional[int] = None
    n_zero: Optional[int] = None
    majority_ratio: Optional[float] = None

    # 体检结论
    is_ok: bool = True
    issues: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert report to dictionary."""
        return asdict(self)


def run_label_sanity_checks(
    df: pd.DataFrame,
    *,
    label_id: str,
    value_col: str,
    side_col: Optional[str] = None,
    task_type: str = "reg",
    cfg: Optional[LabelSanityConfig] = None,
) -> LabelSanityReport:
    """
    核心入口：对标签进行体检。

    This function performs comprehensive quality checks on label columns,
    detecting issues like:
    - Insufficient samples
    - Extreme distribution bias
    - Heavy tails
    - Class imbalance

    Args:
        df: DataFrame with label columns
        label_id: Label identifier
        value_col: Name of value column
        side_col: Optional name of side/class column
        task_type: Task type ("reg" or "cls")
        cfg: Optional configuration

    Returns:
        LabelSanityReport with health metrics and issues

    Raises:
        KeyError: If value_col not in DataFrame
    """
    cfg = cfg or LabelSanityConfig()

    issues: Dict[str, Any] = {}

    if value_col not in df.columns:
        raise KeyError(f"Label value_col={value_col!r} not in DataFrame columns")

    y = df[value_col].to_numpy()
    is_finite = np.isfinite(y)
    n_samples = int(is_finite.sum())
    nan_ratio = float((~is_finite).mean())

    # 初始化 report
    report = LabelSanityReport(
        label_id=label_id,
        value_col=value_col,
        side_col=side_col,
        task_type=task_type,
        n_samples=n_samples,
        nan_ratio=nan_ratio,
        issues={},
    )

    if n_samples < cfg.min_samples:
        issues["too_few_samples"] = {
            "n_samples": n_samples,
            "min_required": cfg.min_samples,
        }

    # === 回归任务：检查分布 ===
    if task_type == "reg":
        y_valid = y[is_finite]
        if y_valid.size > 0:
            mean = float(np.nanmean(y_valid))
            std = float(np.nanstd(y_valid))
            report.mean = mean
            report.std = std
            report.min = float(np.nanmin(y_valid))
            report.max = float(np.nanmax(y_valid))
            report.p1 = float(np.nanpercentile(y_valid, 1))
            report.p50 = float(np.nanpercentile(y_valid, 50))
            report.p99 = float(np.nanpercentile(y_valid, 99))

            if std > 0:
                abs_mean_z = abs(mean / std)
                report.abs_mean_z = abs_mean_z
                if abs_mean_z > cfg.max_abs_mean_z_reg:
                    issues["mean_bias"] = {
                        "abs_mean_z": abs_mean_z,
                        "max_allowed": cfg.max_abs_mean_z_reg,
                    }

            # 极端尾部比例：|y| > p99 的比例
            tail_thresh = np.nanpercentile(np.abs(y_valid), 99)
            extreme_mask = np.abs(y_valid) > tail_thresh
            extreme_ratio = float(extreme_mask.mean())
            report.extreme_tail_ratio = extreme_ratio
            if extreme_ratio > cfg.extreme_tail_ratio_reg:
                issues["too_heavy_tails"] = {
                    "extreme_tail_ratio": extreme_ratio,
                    "max_allowed": cfg.extreme_tail_ratio_reg,
                }

    # === 分类任务：检查标签不平衡 ===
    if side_col and side_col in df.columns:
        side = df[side_col].to_numpy()
        n_pos = int((side == 1).sum())
        n_neg = int((side == -1).sum())
        n_zero = int((side == 0).sum())

        report.n_pos = n_pos
        report.n_neg = n_neg
        report.n_zero = n_zero

        total = n_pos + n_neg + n_zero
        if total > 0:
            majority = max(n_pos, n_neg, n_zero)
            majority_ratio = majority / total
            report.majority_ratio = float(majority_ratio)
            if task_type in ("cls", "classification") and majority_ratio > cfg.max_majority_ratio_cls:
                issues["label_imbalance"] = {
                    "majority_ratio": majority_ratio,
                    "max_allowed": cfg.max_majority_ratio_cls,
                    "n_pos": n_pos,
                    "n_neg": n_neg,
                    "n_zero": n_zero,
                }

    report.issues = issues or None
    report.is_ok = not bool(issues)

    if cfg.enable_debug:
        print("[LabelSanity] report =", report.to_dict())

    return report


def summarize_label_for_llm(label_sanity: Dict[str, Any]) -> str:
    """
    把 label_sanity dict 转成一段短文，给 LLM 看。

    This creates a human-readable summary of label quality for the AI brain.

    Args:
        label_sanity: Label sanity report dictionary

    Returns:
        Human-readable summary string
    """
    if not label_sanity:
        return "Label sanity report is missing."

    lid = label_sanity.get("label_id", "?")
    t = label_sanity.get("task_type", "reg")
    n = label_sanity.get("n_samples", 0)
    nan_ratio = label_sanity.get("nan_ratio", 0.0)

    issues = label_sanity.get("issues") or {}

    lines = [f"Label [{lid}] summary:"]
    lines.append(f"- task_type={t}, n_samples={n}, nan_ratio={nan_ratio:.3f}")

    if t == "reg":
        mean = label_sanity.get("mean")
        std = label_sanity.get("std")
        p1 = label_sanity.get("p1")
        p50 = label_sanity.get("p50")
        p99 = label_sanity.get("p99")
        abs_mean_z = label_sanity.get("abs_mean_z")
        if mean is not None and std is not None:
            # Format stats, handling potential None values
            stats_parts = [f"mean={mean:.5f}", f"std={std:.5f}"]
            if p1 is not None:
                stats_parts.append(f"p1={p1:.5f}")
            if p50 is not None:
                stats_parts.append(f"p50={p50:.5f}")
            if p99 is not None:
                stats_parts.append(f"p99={p99:.5f}")
            lines.append(f"- value_stats: {', '.join(stats_parts)}")
            if abs_mean_z is not None:
                lines.append(f"- abs_mean_z={abs_mean_z:.3f}")

    if label_sanity.get("majority_ratio") is not None:
        lines.append(
            f"- side distribution: pos={label_sanity.get('n_pos')}, "
            f"neg={label_sanity.get('n_neg')}, zero={label_sanity.get('n_zero')}, "
            f"majority_ratio={label_sanity.get('majority_ratio'):.3f}"
        )

    if issues:
        lines.append(f"- issues: {issues}")
    else:
        lines.append("- issues: none")

    return "\n".join(lines)
