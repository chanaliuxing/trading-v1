# quality/dataset_sanity.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class DatasetSanityIssue:
    """
    Single issue found during dataset sanity check.

    Attributes:
        code: Issue code (e.g., "high_missing", "zero_variance")
        message: Human-readable message
        severity: Issue severity level
        details: Optional additional details
    """

    code: str
    message: str
    severity: str  # "info" | "warning" | "error"
    details: Optional[Dict[str, Any]] = None


@dataclass
class DatasetSanityResult:
    """
    Structured result from dataset sanity check.

    Provides both detailed report and compressed summary for LLM consumption.

    Attributes:
        ok: Overall pass/fail status
        issues: List of detected issues
        report: Detailed report (column stats, correlations, etc.)
        summary: Brief conclusion dict
    """

    ok: bool
    issues: List[DatasetSanityIssue] = field(default_factory=list)
    report: Dict[str, Any] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)

    def add_issue(
        self,
        code: str,
        message: str,
        severity: str = "warning",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add an issue to the result."""
        self.issues.append(
            DatasetSanityIssue(
                code=code,
                message=message,
                severity=severity,
                details=details,
            )
        )
        if severity == "error":
            self.ok = False

    def brief_summary(self, *, label_col: Optional[str] = None) -> Dict[str, Any]:
        """
        Compress sanity results for LLM consumption.

        Returns a compact summary suitable for AI Brain context,
        avoiding large statistical tables.

        Args:
            label_col: Optional label column name for label-specific summary

        Returns:
            Dict with keys:
                - ok: bool
                - severity: "ok" | "warning" | "error"
                - n_rows: int
                - n_cols: int
                - issue_counts: {"info": int, "warning": int, "error": int}
                - top_issues: List of top 5 issues
                - label_info: Optional label statistics
        """
        n_rows = int(self.report.get("basic", {}).get("n_rows", 0))
        n_cols = int(self.report.get("basic", {}).get("n_cols", 0))

        # Count issues by severity
        severities = {"info": 0, "warning": 0, "error": 0}
        for issue in self.issues:
            if issue.severity in severities:
                severities[issue.severity] += 1

        # Extract top 5 issues
        top_issues = []
        for issue in self.issues[:5]:
            top_issues.append(
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                }
            )

        # Label info (if available)
        label_info = None
        if "labels" in self.report:
            labels_data = self.report["labels"]
            if labels_data:  # Only if there are labels
                if label_col and label_col in labels_data:
                    # Use specified label
                    label_info = {
                        "label_col": label_col,
                        **labels_data[label_col],
                    }
                elif not label_col:
                    # No label specified - use first label
                    first_label = next(iter(labels_data.keys()))
                    label_info = {
                        "label_col": first_label,
                        **labels_data[first_label],
                    }

        # Determine overall severity level
        severity_level = "ok"
        if severities["error"] > 0:
            severity_level = "error"
        elif severities["warning"] > 0:
            severity_level = "warning"

        return {
            "ok": self.ok,
            "severity": severity_level,
            "n_rows": n_rows,
            "n_cols": n_cols,
            "issue_counts": severities,
            "top_issues": top_issues,
            "label_info": label_info,
        }


def run_dataset_sanity(
    df: pd.DataFrame,
    label_cols: Optional[List[str]] = None,
    max_rows: int = 50_000,
    high_missing_threshold: float = 0.30,
    high_corr_threshold: float = 0.98,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    对训练前的 dataset_df 做一次"体检"。

    返回:
      - report: 详细报告（列级统计、共线性、label 分布等）
      - summary: 简要结论（overall_status/ issues / basic 统计）
    """
    report: Dict[str, Any] = {}
    summary: Dict[str, Any] = {}
    n_rows, n_cols = df.shape

    if n_rows == 0:
        summary = {
            "overall_status": "fail",
            "reason": "empty_dataframe",
            "details": "DataFrame has 0 rows.",
        }
        report["basic"] = {"n_rows": 0, "n_cols": n_cols}
        return report, summary

    # 采样，避免在超大 DataFrame 上全量算统计
    if n_rows > max_rows:
        df_sample = df.sample(n=max_rows, random_state=42)
    else:
        df_sample = df

    numeric_cols = df_sample.select_dtypes(include=[np.number]).columns.tolist()
    issues: List[str] = []
    col_stats: Dict[str, Any] = {}

    # === 数值列统计 ===
    for col in numeric_cols:
        s = df_sample[col]
        null_ratio = float(s.isna().mean())
        finite_ratio = float(np.isfinite(s.to_numpy(dtype=float)).mean())
        variance = float(np.nanvar(s.to_numpy(dtype=float)))

        desc = s.describe(percentiles=[0.01, 0.5, 0.99])

        col_stats[col] = {
            "null_ratio": null_ratio,
            "finite_ratio": finite_ratio,
            "variance": variance,
            "min": float(desc["min"]) if "min" in desc.index else 0.0,
            "p01": float(desc.get("1%", desc.get("min", 0.0))),
            "median": float(desc["50%"]) if "50%" in desc.index else 0.0,
            "p99": float(desc.get("99%", desc.get("max", 0.0))),
            "max": float(desc["max"]) if "max" in desc.index else 0.0,
        }

        if null_ratio > high_missing_threshold:
            issues.append(f"high_missing:{col}:{null_ratio:.2%}")
        if variance == 0.0:
            issues.append(f"zero_variance:{col}")
        if finite_ratio < 0.99:
            issues.append(f"non_finite_values:{col}:{1 - finite_ratio:.2%}")

    report["basic"] = {
        "n_rows": int(n_rows),
        "n_cols": int(n_cols),
        "numeric_cols": numeric_cols,
    }
    report["columns"] = col_stats

    # === 高相关性检查（多重共线性） ===
    corr_notes: List[str] = []
    if 1 < len(numeric_cols) <= 60:  # 列太多就先不算，避免 O(n^2) 爆炸
        corr = df_sample[numeric_cols].corr()
        high_pairs = []
        for i, c1 in enumerate(numeric_cols):
            for c2 in numeric_cols[i + 1 :]:
                v = corr.loc[c1, c2]
                if abs(v) >= high_corr_threshold:
                    high_pairs.append((c1, c2, float(v)))

        if high_pairs:
            corr_notes.append(
                f"{len(high_pairs)} highly correlated pairs (|r| >= {high_corr_threshold})"
            )
            report["high_corr_pairs"] = [
                {"col1": c1, "col2": c2, "corr": v} for c1, c2, v in high_pairs
            ]
            issues.append("high_multicollinearity")

    # === Label 相关检查 ===
    label_info: Dict[str, Any] = {}
    label_issues: List[str] = []

    if label_cols:
        for lc in label_cols:
            if lc not in df_sample.columns:
                label_issues.append(f"label_missing:{lc}")
                continue

            s = df_sample[lc].dropna()
            if s.empty:
                label_issues.append(f"label_all_nan:{lc}")
                continue

            uniq = s.nunique()
            if uniq <= 10:
                # 认为是分类任务，看类别分布是否极度不平衡
                value_counts = s.value_counts(normalize=True).sort_values(ascending=False)
                label_info[lc] = {
                    "task": "cls",
                    "n_unique": int(uniq),
                    "top_values": value_counts.head(5).to_dict(),
                }
                top_ratio = float(value_counts.iloc[0])
                if top_ratio > 0.95:
                    label_issues.append(f"label_heavy_imbalance:{lc}:{top_ratio:.2%}")
            else:
                # 认为是回归/连续 label
                label_info[lc] = {
                    "task": "reg",
                    "n_unique": int(uniq),
                    "mean": float(s.mean()),
                    "std": float(s.std()),
                }

    report["labels"] = label_info
    if label_issues:
        issues.extend(label_issues)

    # === 汇总 ===
    if any(i.startswith("label_missing") or i.startswith("label_all_nan") for i in issues):
        overall = "fail"
    elif any(
        i.startswith("high_missing")
        or i.startswith("high_multicollinearity")
        or i.startswith("label_heavy_imbalance")
        or i.startswith("non_finite_values")
        for i in issues
    ):
        overall = "warn"
    else:
        overall = "ok"

    summary = {
        "overall_status": overall,
        "issues": issues,
        "basic": {"n_rows": int(n_rows), "n_cols": int(n_cols)},
        "labels": label_info,
        "notes": corr_notes,
    }

    return report, summary


def run_dataset_sanity_v2(
    df: pd.DataFrame,
    label_cols: Optional[List[str]] = None,
    max_rows: int = 50_000,
    high_missing_threshold: float = 0.30,
    high_corr_threshold: float = 0.98,
) -> DatasetSanityResult:
    """
    对训练前的 dataset_df 做一次"体检"（新版本,返回 DatasetSanityResult）。

    This is the new interface that returns structured DatasetSanityResult
    with brief_summary() method for LLM consumption.

    Args:
        df: DataFrame to check
        label_cols: Optional list of label column names
        max_rows: Maximum rows to sample for statistics
        high_missing_threshold: Threshold for high missing ratio
        high_corr_threshold: Threshold for high correlation

    Returns:
        DatasetSanityResult with structured issues and brief_summary() method
    """
    # Call existing function to get report and summary
    report, summary = run_dataset_sanity(
        df=df,
        label_cols=label_cols,
        max_rows=max_rows,
        high_missing_threshold=high_missing_threshold,
        high_corr_threshold=high_corr_threshold,
    )

    # Convert old format to new DatasetSanityResult
    overall_status = summary.get("overall_status", "ok")
    ok = overall_status != "fail"

    result = DatasetSanityResult(
        ok=ok,
        issues=[],
        report=report,
        summary=summary,
    )

    # Convert issue strings to DatasetSanityIssue objects
    for issue_str in summary.get("issues", []):
        # Parse issue string (format: "code:details" or "code")
        parts = issue_str.split(":", 1)
        code = parts[0]
        message = issue_str  # Full string as message

        # Determine severity based on overall_status and code
        if code.startswith("label_missing") or code.startswith("label_all_nan"):
            severity = "error"
        elif code.startswith("high_missing") or code.startswith("high_multicollinearity") or code.startswith("label_heavy_imbalance") or code.startswith("non_finite_values") or code.startswith("zero_variance"):
            severity = "warning"
        else:
            severity = "info"

        result.add_issue(
            code=code,
            message=message,
            severity=severity,
        )

    return result
