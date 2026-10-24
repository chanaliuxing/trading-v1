#!/usr/bin/env python3
"""
AI大脑诊断OOF训练失败的根因工具 (Phase 3 Standardized)

当OOF训练结果差 (AUC < 0.55 或 IC < 0.1) 时，自动诊断根本原因并返回标准Diagnosis对象：
1. 数据利用率过低（数据被过度过滤）
2. Label列不存在（配置与Schema不匹配）
3. Label质量差（噪声太大，无预测能力）

Phase 3 Upgrade:
- Returns standard Diagnosis object (from libs.health.issue)
- Issues + Root Causes + Recommended Actions
- Consumable by DataHealer v2 and AI Brain

Usage:
    venv/bin/python tools/diagnose_oof_failure.py \
        --job-id 20251126152732-jknb2v \
        --auc 0.5 \
        --ic 0.0 \
        --n-train 1614 \
        --horizon-min 5 \
        --symbol TSLA \
        --start "2025-09-27 16:00:00" \
        --end "2025-11-26 16:00:00" \
        --output /tmp/oof_diagnosis.json
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Dict, List, Any, Optional

import clickhouse_connect

# Phase 3: Import standard protocols
from libs.health.issue import (
    Diagnosis,
    Issue,
    Action,
    RootCause,
    IssueType,
    Severity,
    RootCauseType,
    make_diagnosis_id,
    make_issue_id,
    make_action_id
)


def _generate_diagnosis_summary(
    issues: List[Issue],
    primary_root_cause: Optional[RootCause],
    recommended_actions: List[Action]
) -> str:
    """Generate human-readable diagnosis summary"""
    if not issues:
        return "No significant issues found. Poor performance may be due to feature quality or model parameters."

    summary_parts = []
    summary_parts.append(f"Found {len(issues)} issue(s).")

    if primary_root_cause:
        summary_parts.append(f"Primary root cause: {primary_root_cause.description}")

    if recommended_actions:
        auto_actions = [a for a in recommended_actions if a.is_safe_auto_execute]
        if auto_actions:
            summary_parts.append(f"{len(auto_actions)} auto-fixable action(s) available.")

    return " ".join(summary_parts)


def _calculate_diagnosis_confidence(root_causes: List[RootCause]) -> float:
    """Calculate overall diagnosis confidence from root causes"""
    if not root_causes:
        return 0.0

    # Weighted average: primary root cause gets double weight
    if len(root_causes) == 1:
        return root_causes[0].confidence

    # Average of all confidences
    return sum(rc.confidence for rc in root_causes) / len(root_causes)


def count_expected_bars(
    cli,
    symbol: str,
    start: str,
    end: str,
    market_hours_only: bool = True,
    features_table: str = "trading.features_1m_with_labels"
) -> int:
    """
    计算期望的数据量（基于时间范围）
    """

    query = f"""
    SELECT count() as cnt
    FROM {features_table}
    WHERE symbol = %(symbol)s
        AND ts >= toDateTime(%(start)s)
        AND ts <= toDateTime(%(end)s)
    """

    if market_hours_only:
        query += " AND toHour(ts) >= 9 AND toHour(ts) < 16"

    result = cli.query(query, parameters={'symbol': symbol, 'start': start, 'end': end})
    return result.result_rows[0][0]


def get_label_columns(cli, table: str = "trading.features_1m_with_labels") -> List[str]:
    """
    获取数据库中所有label列
    """

    table_name = table.split('.')[-1]

    query = f"""
    SELECT name
    FROM system.columns
    WHERE database = 'trading'
        AND table = '{table_name}'
        AND name LIKE 'label%'
    ORDER BY name
    """

    result = cli.query(query)
    return [row[0] for row in result.result_rows]


def get_label_stats(
    cli,
    symbol: str,
    start: str,
    end: str,
    label_col: str,
    threshold_bps: float = 10.0,
    table: str = "trading.features_1m_with_labels"
) -> Dict[str, Any]:
    """
    分析label质量统计
    """

    threshold = threshold_bps / 10000.0

    query = f"""
    SELECT
        count() as total,
        countIf({label_col} IS NULL) as null_count,
        countIf(abs({label_col}) < {threshold}) as zero_count,
        countIf({label_col} > 0) as positive_count,
        countIf({label_col} < 0) as negative_count,
        avg(abs({label_col})) as mean_magnitude,
        stddevPop({label_col}) as std_label,
        min({label_col}) as min_label,
        max({label_col}) as max_label
    FROM {table}
    WHERE symbol = %(symbol)s
        AND ts >= toDateTime(%(start)s)
        AND ts <= toDateTime(%(end)s)
        AND toHour(ts) >= 9
        AND toHour(ts) < 16
    """

    result = cli.query(query, parameters={'symbol': symbol, 'start': start, 'end': end})
    row = result.result_rows[0]

    total = row[0]
    if total == 0:
        return {"error": "No data found"}

    return {
        "total": total,
        "null_count": row[1],
        "null_rate": row[1] / total,
        "zero_count": row[2],
        "zero_rate": row[2] / total,
        "positive_count": row[3],
        "positive_rate": row[3] / total,
        "negative_count": row[4],
        "negative_rate": row[4] / total,
        "mean_magnitude": float(row[5]) if row[5] else 0,
        "std_bps": float(row[6]) * 10000 if row[6] else 0,
        "min": float(row[7]) if row[7] else 0,
        "max": float(row[8]) if row[8] else 0
    }


def diagnose_oof_failure(
    job_id: str,
    oof_result: Dict[str, Any],
    symbol: str,
    start: str,
    end: str,
    features_table: str = "trading.features_1m_with_labels",
    output_file: str = None
) -> Diagnosis:
    """
    诊断OOF训练失败的根因 (Phase 3: Returns standard Diagnosis)

    Args:
        job_id: OOF任务ID
        oof_result: OOF结果 {"xgb_auc": 0.5, "xgb_ic": 0.0, "xgb_n": 1614, "horizon_min": 5}
        symbol: 股票代码
        start: 开始时间
        end: 结束时间
        output_file: 输出文件路径

    Returns:
        Diagnosis object (standard protocol from Phase 1)
    """

    if not features_table:
        features_table = "trading.features_1m_with_labels"

    cli = clickhouse_connect.get_client(host='localhost', port=8123, database='trading')

    # Phase 3: Initialize with standard objects
    issues = []
    root_causes = []
    recommended_actions = []
    primary_root_cause = None

    # Legacy report for printing (will be deprecated)
    report = {
        "timestamp": datetime.now().isoformat(),
        "job_id": job_id,
        "symbol": symbol,
        "time_range": {"start": start, "end": end},
        "symptoms": {
            "auc": oof_result.get("xgb_auc", 0.0),
            "ic": oof_result.get("xgb_ic", 0.0),
            "n_train": oof_result.get("xgb_n", 0),
            "horizon_min": oof_result.get("horizon_min", 5)
        },
        "diagnosis": [],
        "root_cause": None,
        "recommended_actions": []
    }
    if oof_result.get('ev') is not None:
        report['symptoms']['ev'] = oof_result['ev']
    if oof_result.get('beta_corr') is not None:
        report['symptoms']['beta_corr'] = oof_result['beta_corr']

    horizon_min = oof_result.get("horizon_min", 5)
    # Recompute actual_n from the selected features table to avoid mismatches
    actual_n = 0
    try:
        q_actual = f"""
        SELECT count() AS cnt
        FROM {features_table}
        WHERE symbol = %(symbol)s
          AND ts >= %(start)s
          AND ts <= %(end)s
          AND {label_col} IS NOT NULL
        """
        res_actual = cli.query_df(q_actual, parameters={"symbol": symbol, "start": start, "end": end})
        if not res_actual.empty:
            actual_n = int(res_actual["cnt"][0])
    except Exception:
        actual_n = oof_result.get("xgb_n", 0)
    label_col = f"label_h{horizon_min}"

    print("=" * 80)
    print(f"AI BRAIN OOF FAILURE DIAGNOSIS")
    print("=" * 80)
    print(f"Job ID:       {job_id}")
    print(f"Symbol:       {symbol}")
    print(f"Time range:   {start} to {end}")
    print(f"Horizon:      {horizon_min} minutes")
    print(f"Label column: {label_col}")
    print()

    print("Symptoms:")
    print(f"  AUC:        {report['symptoms']['auc']:.4f}")
    print(f"  IC:         {report['symptoms']['ic']:.4f}")
    print(f"  N_train:    {actual_n:,}")
    if report["symptoms"].get("ev") is not None:
        try:
            print(f"  EV:         {float(report['symptoms']['ev']):.4f}")
        except Exception:
            pass
    if report["symptoms"].get("beta_corr") is not None:
        try:
            print(f"  Beta Corr:  {float(report['symptoms']['beta_corr']):.4f}")
        except Exception:
            pass
    print()

    # ========================================================================
    # 检查1: 数据利用率
    # ========================================================================

    print("[Check 1] Data Utilization Rate")
    print("-" * 80)

    # Use the chosen features_table to count actual rows
    try:
        q_rows = f"""
        SELECT count() AS cnt
        FROM {features_table}
        WHERE symbol = %(symbol)s AND ts >= %(start)s AND ts <= %(end)s
          AND toHour(ts) >= 9 AND toHour(ts) < 16
        """
        actual_rows_df = cli.query_df(q_rows, parameters={"symbol": symbol, "start": start, "end": end})
        if not actual_rows_df.empty:
            actual_n = int(actual_rows_df['cnt'][0])
    except Exception:
        pass

    expected_n = count_expected_bars(cli, symbol, start, end, market_hours_only=True, features_table=features_table)
    utilization_rate = actual_n / expected_n if expected_n > 0 else 0

    print(f"  Expected bars:  {expected_n:,}")
    print(f"  Actual bars:    {actual_n:,}")
    print(f"  Utilization:    {utilization_rate:.1%}")

    if utilization_rate < 0.5:
        severity_level = Severity.CRITICAL if utilization_rate < 0.3 else Severity.HIGH

        # Phase 3: Create standard Issue object
        issue = Issue(
            issue_id=make_issue_id("training", IssueType.DATA_QUALITY_POOR),
            timestamp=datetime.now().isoformat(),
            component="training",
            issue_type=IssueType.DATA_QUALITY_POOR,
            severity=severity_level,
            title="Low data utilization in OOF training",
            description=f"Only {utilization_rate:.1%} of available data was used for training ({actual_n:,} / {expected_n:,})",
            affected_symbols=[symbol],
            affected_tables=[features_table],
            metrics={
                "expected_n": expected_n,
                "actual_n": actual_n,
                "utilization_rate": utilization_rate,
                "horizon_min": horizon_min
            }
        )
        issues.append(issue)

        # Phase 3: Create RootCause
        rc = RootCause(
            type=RootCauseType.TRAINING_DATA_INSUFFICIENT,
            confidence=0.85,
            evidence={
                "expected_bars": expected_n,
                "actual_bars": actual_n,
                "utilization_rate": utilization_rate,
                "time_range": {"start": start, "end": end}
            },
            description=f"Training data utilization is only {utilization_rate:.1%}, indicating excessive filtering or missing data"
        )
        root_causes.append(rc)

        # Set as primary if this is the first critical issue
        if not primary_root_cause and severity_level == Severity.CRITICAL:
            primary_root_cause = rc

        # Legacy format for printing
        report["diagnosis"].append({
            "issue": "low_data_utilization",
            "severity": severity_level.value,
            "expected_n": expected_n,
            "actual_n": actual_n,
            "utilization_rate": utilization_rate,
            "description": f"Only {utilization_rate:.1%} of available data was used for training"
        })
        print(f"  ⚠️  WARNING: Low data utilization ({utilization_rate:.1%})")
    else:
        print(f"  ✅ OK: Data utilization is acceptable")

    # ========================================================================
    # 检查1b: EV / Beta（如果提供）
    # ========================================================================
    ev_val = report["symptoms"].get("ev")
    beta_val = report["symptoms"].get("beta_corr")
    if ev_val is not None:
        try:
            ev_f = float(ev_val)
            if ev_f <= 0.0:
                issues.append(Issue(
                    issue_id=make_issue_id("negative_ev", IssueType.PERFORMANCE_DEGRADED),
                    timestamp=datetime.now().isoformat(),
                    component="model",
                    issue_type=IssueType.PERFORMANCE_DEGRADED,
                    severity=Severity.HIGH,
                    title="Model expected value is non-positive",
                    description=f"EV={ev_f:.4f} <= 0",
                    affected_symbols=[symbol],
                    metrics={"ev": ev_f}
                ))
                rc = RootCause(
                    type=RootCauseType.MODEL_UNDERFITTING,
                    confidence=0.6,
                    evidence={"ev": ev_f},
                    description="Model expected value is non-positive"
                )
                root_causes.append(rc)
                print(f"  ⚠️  WARNING: EV non-positive (EV={ev_f:.4f})")
        except Exception:
            pass
    if beta_val is not None:
        try:
            beta_f = float(beta_val)
            if beta_f > 0.8:
                issues.append(Issue(
                    issue_id=make_issue_id("high_beta_corr", IssueType.PERFORMANCE_DEGRADED),
                    timestamp=datetime.now().isoformat(),
                    component="model",
                    issue_type=IssueType.PERFORMANCE_DEGRADED,
                    severity=Severity.MEDIUM,
                    title="Model too correlated to market (beta drift)",
                    description=f"beta_corr={beta_f:.4f} > 0.8",
                    affected_symbols=[symbol],
                    metrics={"beta_corr": beta_f}
                ))
                rc = RootCause(
                    type=RootCauseType.MODEL_UNDERFITTING,
                    confidence=0.5,
                    evidence={"beta_corr": beta_f},
                    description="Model too correlated to market (beta drift)"
                )
                root_causes.append(rc)
                print(f"  ⚠️  WARNING: High beta correlation (beta_corr={beta_f:.4f})")
        except Exception:
            pass

    print()

    # ========================================================================
    # 检查2: Label列存在性
    # ========================================================================

    print("[Check 2] Label Column Existence")
    print("-" * 80)

    existing_labels = get_label_columns(cli, table=features_table)
    print(f"  Expected label: {label_col}")
    print(f"  Existing labels: {existing_labels}")

    if label_col not in existing_labels:
        # Phase 3: Create standard Issue
        issue = Issue(
            issue_id=make_issue_id("labels", IssueType.SCHEMA_MISMATCH),
            timestamp=datetime.now().isoformat(),
            component="labels",
            issue_type=IssueType.SCHEMA_MISMATCH,
            severity=Severity.CRITICAL,
            title=f"Label column {label_col} missing",
            description=f"Config requires {label_col}, but it doesn't exist in database. Available: {existing_labels}",
            affected_symbols=[symbol],
            affected_tables=[features_table],
            metrics={
                "expected_label": label_col,
                "existing_labels": existing_labels,
                "horizon_min": horizon_min
            }
        )
        issues.append(issue)

        # Phase 3: Create RootCause
        rc = RootCause(
            type=RootCauseType.LABEL_COLUMN_MISSING,
            confidence=1.0,
            evidence={
                "expected_label": label_col,
                "existing_labels": existing_labels,
                "schema_query": "SELECT name FROM system.columns WHERE table='features_1m_with_labels' AND name LIKE 'label%'"
            },
            description=f"Label column {label_col} does not exist in database schema"
        )
        root_causes.append(rc)

        # Set as primary root cause (highest priority)
        primary_root_cause = rc

        # Phase 3: Create recommended Action 1 - Generate label
        action1 = Action(
            action_id=make_action_id("generate_label"),
            action_type="generate_label",
            priority=1,
            title=f"Generate {label_col} column",
            description=f"Create missing {label_col} column with {horizon_min}-minute forward returns",
            command=f"venv/bin/python tools/generate_labels.py --horizon {horizon_min} --symbols {symbol}",
            params={
                "horizon_min": horizon_min,
                "symbols": [symbol],
                "start": start,
                "end": end,
                "label_col": label_col
            },
            estimated_duration_sec=300,
            is_safe_auto_execute=True,
            requires_approval=False
        )
        recommended_actions.append(action1)

        # Phase 3: Create recommended Action 2 - Switch to existing label
        if existing_labels:
            best_label = "label_h120" if "label_h120" in existing_labels else existing_labels[-1]
            best_horizon = int(best_label.split("_h")[1])

            action2 = Action(
                action_id=make_action_id("update_config"),
                action_type="update_config",
                priority=2,
                title=f"Switch to existing label {best_label}",
                description=f"Update config to use existing {best_label} instead of missing {label_col}",
                command=f"Update config/oof_policies.json: horizon_min = {best_horizon}",
                params={
                    "config_file": "config/oof_policies.json",
                    "new_horizon_min": best_horizon,
                    "new_label": best_label
                },
                estimated_duration_sec=10,
                is_safe_auto_execute=False,
                requires_approval=True
            )
            recommended_actions.append(action2)

        # Legacy format
        report["diagnosis"].append({
            "issue": "label_column_missing",
            "severity": "critical",
            "expected_label": label_col,
            "existing_labels": existing_labels,
            "description": f"Config requires {label_col}, but it doesn't exist in database"
        })

        report["root_cause"] = "config_schema_mismatch"

        # Legacy actions
        report["recommended_actions"].append({
            "action": "generate_label_column",
            "priority": "high",
            "params": {
                "horizon_min": horizon_min,
                "symbols": [symbol],
                "start": start,
                "end": end
            },
            "command": f"venv/bin/python tools/generate_label.py --horizon {horizon_min} --symbols {symbol} --start '{start}' --end '{end}'"
        })

        if existing_labels:
            best_label = "label_h120" if "label_h120" in existing_labels else existing_labels[-1]
            best_horizon = int(best_label.split("_h")[1])

            report["recommended_actions"].append({
                "action": "switch_to_existing_label",
                "priority": "medium",
                "params": {
                    "new_label": best_label,
                    "new_horizon_min": best_horizon
                },
                "command": f"Update config/oof_policies.json: horizon_min = {best_horizon}"
            })

        print(f"  ❌ CRITICAL: Label column {label_col} does NOT exist!")
        print(f"  💡 Recommended: Generate {label_col} or switch to {existing_labels[-1] if existing_labels else 'N/A'}")
    else:
        print(f"  ✅ OK: Label column exists")

    print()

    # ========================================================================
    # 检查3: Label质量（仅当label存在时）
    # ========================================================================

    if label_col in existing_labels:
        print("[Check 3] Label Quality")
        print("-" * 80)

        label_stats = get_label_stats(cli, symbol, start, end, label_col, threshold_bps=10.0, table=features_table)

        if "error" not in label_stats:
            print(f"  Total samples:   {label_stats['total']:,}")
            print(f"  NULL rate:       {label_stats['null_rate']:.1%}")
            print(f"  Zero rate:       {label_stats['zero_rate']:.1%}")
            print(f"  Positive rate:   {label_stats['positive_rate']:.1%}")
            print(f"  Negative rate:   {label_stats['negative_rate']:.1%}")
            print(f"  Mean magnitude:  {label_stats['mean_magnitude']:.6f}")
            print(f"  Std (bps):       {label_stats['std_bps']:.1f}")

            # 判断label质量
            issue_tags = []

            if label_stats['zero_rate'] > 0.5:
                issue_tags.append("zero_rate_too_high")
                print(f"  ⚠️  WARNING: Zero rate too high ({label_stats['zero_rate']:.1%})")

            if label_stats['std_bps'] < 50:
                issue_tags.append("std_too_low")
                print(f"  ⚠️  WARNING: Std too low ({label_stats['std_bps']:.1f} bps)")

            balance = abs(label_stats['positive_rate'] - label_stats['negative_rate'])
            if balance > 0.2:
                issue_tags.append("imbalanced")
                print(f"  ⚠️  WARNING: Imbalanced ({label_stats['positive_rate']:.1%} vs {label_stats['negative_rate']:.1%})")

            if issue_tags:
                # Phase 3: Create Issue for poor label quality
                quality_issue = Issue(
                    issue_id=make_issue_id("labels", IssueType.DATA_QUALITY_POOR),
                    timestamp=datetime.now().isoformat(),
                    component="labels",
                    issue_type=IssueType.DATA_QUALITY_POOR,
                    severity=Severity.HIGH,
                    title=f"Label {label_col} quality issues",
                    description=f"Label {label_col} has quality issues: {', '.join(issue_tags)}",
                    affected_symbols=[symbol],
                    affected_tables=[features_table],
                    metrics=label_stats
                )
                issues.append(quality_issue)

                # Phase 3: Create RootCause
                quality_rc = RootCause(
                    type=RootCauseType.LABEL_QUALITY_POOR,
                    confidence=0.75,
                    evidence={
                        "label_stats": label_stats,
                        "quality_issues": issue_tags,
                        "zero_rate": label_stats['zero_rate'],
                        "std_bps": label_stats['std_bps']
                    },
                    description=f"Label {label_col} exhibits poor quality: {', '.join(issue_tags)}"
                )
                root_causes.append(quality_rc)

                # Set as primary if no other root cause
                if not primary_root_cause:
                    primary_root_cause = quality_rc

                # Phase 3: Create Action - test longer horizons
                action = Action(
                    action_id=make_action_id("diagnose_label_quality"),
                    action_type="diagnose_label_quality",
                    priority=1,
                    title="Test longer label horizons",
                    description=f"Diagnose label quality for longer horizons (30m, 60m, 120m) to find better signal",
                    command=f"venv/bin/python tools/diagnose_label_quality.py --symbols {symbol} --horizons 30 60 120",
                    params={
                        "horizons": [30, 60, 120],
                        "symbols": [symbol],
                        "start": start,
                        "end": end
                    },
                    estimated_duration_sec=180,
                    is_safe_auto_execute=True,
                    requires_approval=False
                )
                recommended_actions.append(action)

                # Legacy format
                # Legacy-friendly payload: avoid mixing Issue objects with strings
                report["diagnosis"].append({
                    "issue": "label_quality_poor",
                    "severity": "high",
                    "label_stats": label_stats,
                    "issues": issue_tags,
                    "description": f"Label {label_col} has quality issues: {', '.join(issue_tags)}"
                })

                report["recommended_actions"].append({
                    "action": "test_longer_horizons",
                    "priority": "high",
                    "params": {
                        "horizons": [30, 60, 120],
                        "symbols": [symbol],
                        "start": start,
                        "end": end
                    },
                    "command": f"venv/bin/python tools/diagnose_label_quality.py --symbols {symbol} --horizons 30 60 120 --start '{start}' --end '{end}'"
                })

                if report["root_cause"] is None:
                    report["root_cause"] = "label_quality_poor"
            else:
                print(f"  ✅ OK: Label quality is acceptable")
        else:
            print(f"  ❌ ERROR: {label_stats['error']}")

        print()

    # ========================================================================
    # Phase 3: Create standard Diagnosis object
    # ========================================================================

    diagnosis = Diagnosis(
        diagnosis_id=make_diagnosis_id("oof_training"),
        timestamp=datetime.now().isoformat(),
        target_component="training",
        target_symbols=[symbol],
        issues=issues,
        root_causes=root_causes,
        primary_root_cause=primary_root_cause,
        recommended_actions=recommended_actions,
        summary=_generate_diagnosis_summary(issues, primary_root_cause, recommended_actions),
        confidence=_calculate_diagnosis_confidence(root_causes),
        diagnostic_tool="diagnose_oof_failure",
        metadata={
            "job_id": job_id,
            "oof_metrics": {
                "auc": oof_result.get("xgb_auc", 0.0),
                "ic": oof_result.get("xgb_ic", 0.0),
                "n_train": oof_result.get("xgb_n", 0),
                "horizon_min": horizon_min
            },
            "time_range": {"start": start, "end": end},
            # propagate scheduler readiness adjustments for traceability
            "start_override": oof_result.get("start_override"),
            "end_override": oof_result.get("end_override"),
        }
    )

    # ========================================================================
    # 总结 (Print legacy format for backward compatibility)
    # ========================================================================

    print("=" * 80)
    print("DIAGNOSIS SUMMARY")
    print("=" * 80)

    if issues:
        print(f"\n🔍 Found {len(issues)} issue(s):\n")
        for i, issue in enumerate(issues, 1):
            print(f"{i}. [{issue.severity.value.upper()}] {issue.title}")
            print(f"   {issue.description}")

        print(f"\n🎯 Root Cause: {primary_root_cause.type.value if primary_root_cause else 'Unknown'}")

        if recommended_actions:
            print(f"\n💡 Recommended Actions ({len(recommended_actions)}):\n")
            for i, action in enumerate(recommended_actions, 1):
                print(f"{i}. [P{action.priority}] {action.title}")
                if action.command:
                    print(f"   Command: {action.command}")
                print(f"   Auto-executable: {action.is_safe_auto_execute}")
    else:
        print("\n✅ No critical issues found.")
        print("   The poor performance may be due to other factors (feature quality, model params, etc.)")

    print()

    # 输出报告到文件
    if output_file:
        with open(output_file, 'w') as f:
            json.dump(diagnosis.to_dict(), f, indent=2)
        print(f"📄 Diagnosis saved to: {output_file}")

    return diagnosis


def main():
    parser = argparse.ArgumentParser(description="Diagnose OOF training failure")
    parser.add_argument("--job-id", required=True, help="OOF job ID")
    parser.add_argument("--auc", type=float, default=0.5, help="AUC score")
    parser.add_argument("--ic", type=float, default=0.0, help="IC score")
    parser.add_argument("--ev", type=float, help="Expected Value")
    parser.add_argument("--beta-corr", type=float, help="Beta Correlation")
    parser.add_argument("--n-train", type=int, default=0, help="Training sample size")
    parser.add_argument("--horizon-min", type=int, default=5, help="Horizon in minutes")
    parser.add_argument("--symbol", default="TSLA", help="Stock symbol")
    parser.add_argument("--start", required=True, help="Start time (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--end", required=True, help="End time (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--features-table", default="trading.features_1m_with_labels", help="Features table to use")
    parser.add_argument("--output", help="Output JSON file path")

    args = parser.parse_args()

    oof_result = {
        "xgb_auc": args.auc,
        "xgb_ic": args.ic,
        "xgb_n": args.n_train,
        "horizon_min": args.horizon_min
    }
    if args.ev is not None:
        oof_result['ev'] = args.ev
    if args.beta_corr is not None:
        oof_result['beta_corr'] = args.beta_corr


    diagnosis = diagnose_oof_failure(
        job_id=args.job_id,
        oof_result=oof_result,
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        features_table=args.features_table,
        output_file=args.output
    )

    # Phase 3: Return error code if there are critical issues
    has_critical = any(issue.severity == Severity.CRITICAL for issue in diagnosis.issues)
    return 1 if has_critical else 0


if __name__ == "__main__":
    sys.exit(main())
