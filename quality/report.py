# quality/report.py
"""
Unified Sanity Report (UF 21 Part 1)

Aggregates ClickHouse pre-checks + DatasetSanity + Label checks
into a single structured report for:
- ExperimentRecord storage
- AI Brain context (compressed)
- Human-readable summaries

功能:
- SanityReport: 统一的体检报告 dataclass
- build_sanity_report: 聚合所有体检结果
- sanity_report_to_dict: 序列化为 dict/JSON
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

from .dataset_sanity import DatasetSanityIssue
from .clickhouse_stats import TableStats


@dataclass
class SanityReport:
    """
    Unified sanity report aggregating all quality checks.

    Attributes:
        level: Overall health level ("ok" | "warn" | "error")
        summary: One-line human-readable summary
        label_health: Label quality level
        feature_health: Feature quality level
        label_notes: Label-specific issues
        feature_notes: Feature-specific issues
        risk_flags: Risk flags for AI Brain decision-making
        stats: Key statistics (n_rows, n_cols, etc.)
        raw_issues: All issues for debugging
    """
    # 聚合后的整体等级：ok / warn / error
    level: str

    # 一句话 summary，给人和 LLM 看
    summary: str

    # 标签健康状况：ok / warn / error
    label_health: str

    # 特征整体状况，用于 AI 大脑判断是否「谨慎」
    feature_health: str

    # Notes and flags (all have defaults, must come after required fields)
    label_notes: List[str] = field(default_factory=list)
    feature_notes: List[str] = field(default_factory=list)

    # 风险标签，给大脑用来触发不同行为
    # 例如：["few_rows", "label_imbalance", "many_feature_nulls"]
    risk_flags: List[str] = field(default_factory=list)

    # 汇总的一些关键统计（行数、特征数等）
    stats: Dict[str, Any] = field(default_factory=dict)

    # 原始 issue 列表（方便 debug / 日志）
    raw_issues: List[Dict[str, Any]] = field(default_factory=list)


def build_sanity_report(
    *,
    issues: List[DatasetSanityIssue],
    ch_issues: List[Dict[str, Any]],
    table_stats: Optional[TableStats],
    label_col: str = "y",
) -> SanityReport:
    """
    把 DatasetSanity（含 label）+ ClickHouse 预体检结果
    汇总成一个 SanityReport。

    Args:
        issues: DatasetSanity issues
        ch_issues: ClickHouse pre-check issues
        table_stats: ClickHouse table statistics (optional)
        label_col: Label column name

    Returns:
        Unified SanityReport
    """
    # 1) 合并所有 issues
    all_issues: List[Dict[str, Any]] = []

    for i in issues:
        all_issues.append({
            "severity": i.severity,
            "code": i.code,
            "message": i.message,
            "column": getattr(i, "column", None),
            "extra": getattr(i, "extra", None) or {},
        })
    all_issues.extend(ch_issues or [])

    # 2) 计算整体 level
    severities = {i["severity"] for i in all_issues}
    if "error" in severities:
        level = "error"
    elif "warn" in severities or "warning" in severities:
        level = "warn"
    else:
        level = "ok"

    # 3) 抽取 label / feature 相关的 issue
    label_notes: List[str] = []
    feature_notes: List[str] = []
    risk_flags: List[str] = []

    for i in all_issues:
        code = i.get("code", "")
        msg = i.get("message", "")
        col = i.get("column")

        # label 相关
        if col == label_col or code.startswith("label_"):
            label_notes.append(msg)
            if code in {
                "label_too_few_non_null",
                "label_all_nan",
                "label_zero_variance",
                "label_too_many_nan",
                "label_class_imbalance",
                "label_missing",
            }:
                risk_flags.append(code)

        # 特征/整体数据相关
        else:
            feature_notes.append(msg)
            if code in {
                "ch_zero_rows",
                "ch_too_few_rows",
                "ch_high_null_ratio",
                "ch_near_zero_var",
                "feature_all_nan",
                "feature_zero_variance",
                "high_missing",
                "zero_variance",
                "non_finite_values",
            }:
                risk_flags.append(code)

    # 去重 risk_flags
    risk_flags = sorted(set(risk_flags))

    # 4) label_health / feature_health
    # Determine health based on the actual severity of issues in each category
    label_health = "ok"
    if label_notes:
        # Check severities of label-related issues
        label_severities = [
            i["severity"] for i in all_issues
            if (i.get("column") == label_col or i.get("code", "").startswith("label_"))
        ]
        if "error" in label_severities:
            label_health = "error"
        elif "warning" in label_severities or "warn" in label_severities:
            label_health = "warn"

    feature_health = "ok"
    if feature_notes:
        # Check severities of feature-related issues
        feature_severities = [
            i["severity"] for i in all_issues
            if not (i.get("column") == label_col or i.get("code", "").startswith("label_"))
        ]
        if "error" in feature_severities:
            feature_health = "error"
        elif "warning" in feature_severities or "warn" in feature_severities:
            feature_health = "warn"

    # 5) stats 汇总
    stats: Dict[str, Any] = {}
    if table_stats is not None:
        stats["n_rows"] = table_stats.n_rows
        stats["n_cols"] = len(table_stats.cols)

        # 简单统计几个典型列的 null_ratio 情况
        high_null_cols = sorted(
            [
                (name, cs.null_ratio)
                for name, cs in table_stats.cols.items()
                if cs.null_ratio > 0.5
            ],
            key=lambda x: x[1],
            reverse=True,
        )
        if high_null_cols:
            stats["high_null_cols"] = [
                {"name": name, "null_ratio": ratio}
                for name, ratio in high_null_cols[:10]
            ]

    # 6) 一句话 summary
    parts = []
    if "n_rows" in stats:
        parts.append(f"rows={stats['n_rows']}")
    if "n_cols" in stats:
        parts.append(f"features={stats['n_cols']}")

    parts.append(f"label={label_health}")
    parts.append(f"features_health={feature_health}")
    if risk_flags:
        parts.append("risks=" + ",".join(risk_flags[:3]))  # Top 3 risks
    else:
        parts.append("risks=none")

    summary = " | ".join(parts)

    return SanityReport(
        level=level,
        summary=summary,
        label_health=label_health,
        label_notes=label_notes,
        feature_health=feature_health,
        feature_notes=feature_notes,
        risk_flags=risk_flags,
        stats=stats,
        raw_issues=all_issues,
    )


def sanity_report_to_dict(report: SanityReport) -> Dict[str, Any]:
    """
    方便直接放进 ExperimentRecord / JSONL 的 helper。

    Args:
        report: SanityReport instance

    Returns:
        Dict representation suitable for JSON serialization
    """
    return asdict(report)
