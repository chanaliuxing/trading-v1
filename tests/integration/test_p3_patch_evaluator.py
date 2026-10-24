#!/usr/bin/env python3
"""
Integration Test: Patch Evaluator (P3.2)

测试 Patch 评估流程:
1. 评估工具运行
2. 收益/风险评分计算
3. Patch 审批队列写入
"""
import pytest
import clickhouse_connect
from datetime import datetime
import json
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools.patch_evaluator import PatchEvaluator


@pytest.fixture
def ch_client():
    """ClickHouse client fixture"""
    try:
        from libs.config import config
        ccfg = config.clickhouse_config
        client = clickhouse_connect.get_client(
            host=ccfg.get("host", "localhost"),
            port=int(ccfg.get("port", 8123)),
            database=ccfg.get("database", "trading"),
            username=ccfg.get("user", "default"),
            password=ccfg.get("password", ""),
        )
    except Exception:
        client = clickhouse_connect.get_client(
            host="localhost",
            port=8123,
            database="trading"
        )

    yield client

    # Cleanup
    try:
        client.command("DELETE FROM evolution.patch_approval_queue WHERE patch_id LIKE 'test_%'")
        client.command("DELETE FROM evolution.patch_evaluation_details WHERE patch_id LIKE 'test_%'")
    except Exception:
        pass


def test_patch_approval_tables_exist(ch_client):
    """测试 Patch Approval 表是否存在"""
    tables = ch_client.query("SHOW TABLES FROM evolution").result_set
    table_names = [row[0] for row in tables]

    assert "patch_approval_queue" in table_names
    assert "patch_evaluation_details" in table_names
    assert "patch_application_history" in table_names
    assert "patch_comparison" in table_names


def test_patch_approval_queue_insert(ch_client):
    """测试 Patch Approval Queue 插入"""
    test_data = {
        "patch_id": "test_patch_001",
        "target_module": "tool:test_module",
        "target_file": "src/test.py",
        "patch_content": "diff --git a/test.py",
        "benefit_score": 0.75,
        "risk_score": 0.25,
        "confidence": 0.85,
        "unit_tests_passed": True,
        "unit_tests_total": 10,
        "unit_tests_failed": 0,
        "integration_tests_passed": True,
        "regression_tests_passed": True,
        "test_coverage": 0.80,
        "static_analysis_passed": True,
        "ruff_issues": 0,
        "mypy_issues": 0,
        "bandit_issues": 0,
        "complexity_score": 5.0,
        "performance_impact": json.dumps({"latency_ms": -10}),
        "resource_usage_change": json.dumps({"cpu": -5}),
        "domain": "trading",
        "priority": 2,
        "tags": ["bugfix", "performance"],
    }

    ch_client.insert(
        "evolution.patch_approval_queue",
        [list(test_data.values())],
        column_names=list(test_data.keys())
    )

    # Verify
    result = ch_client.query(
        "SELECT * FROM evolution.patch_approval_queue WHERE patch_id = 'test_patch_001'"
    ).result_set

    assert len(result) >= 1
    row = result[0]
    # Find patch_id column (should be early in the row)
    patch_id_idx = None
    for i, val in enumerate(row):
        if val == "test_patch_001":
            patch_id_idx = i
            break
    assert patch_id_idx is not None


def test_patch_evaluator_basic(ch_client):
    """测试 PatchEvaluator 基本功能"""
    # Create a dummy patch file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as f:
        f.write("""--- a/test.py
+++ b/test.py
@@ -1,3 +1,3 @@
 def foo():
-    return 1
+    return 2
""")
        patch_file = Path(f.name)

    try:
        evaluator = PatchEvaluator(
            patch_id="test_patch_002",
            patch_file=patch_file,
            target_module="tool:test"
        )

        # Test evaluation structure (don't run full evaluation, too slow)
        assert evaluator.patch_id == "test_patch_002"
        assert evaluator.target_module == "tool:test"
        assert evaluator.evaluation_results["patch_id"] == "test_patch_002"

        # Test helper methods
        affected_files = evaluator._get_affected_files()
        assert isinstance(affected_files, list)

    finally:
        patch_file.unlink()


def test_patch_evaluation_details_insert(ch_client):
    """测试 Patch Evaluation Details 插入"""
    test_data = {
        "patch_id": "test_patch_003",
        "evaluation_stage": "unit_tests",
        "result": "pass",
        "details": json.dumps({"tests_passed": 10, "tests_failed": 0}),
        "duration_ms": 1500,
    }

    ch_client.insert(
        "evolution.patch_evaluation_details",
        [list(test_data.values())],
        column_names=list(test_data.keys())
    )

    # Verify
    result = ch_client.query(
        "SELECT * FROM evolution.patch_evaluation_details WHERE patch_id = 'test_patch_003'"
    ).result_set

    assert len(result) == 1
    assert result[0][1] == "test_patch_003"


def test_patch_comparison_insert(ch_client):
    """测试 Patch Comparison 表"""
    test_data = {
        "patch_id": "test_patch_004",
        "metric_name": "sharpe",
        "before_value": 1.5,
        "after_value": 1.8,
        "change_pct": 20.0,
        "improvement": True,
        "sample_size": 1000,
        "confidence_interval_low": 1.7,
        "confidence_interval_high": 1.9,
        "p_value": 0.01,
    }

    ch_client.insert(
        "evolution.patch_comparison",
        [list(test_data.values())],
        column_names=list(test_data.keys())
    )

    # Verify
    result = ch_client.query(
        "SELECT * FROM evolution.patch_comparison WHERE patch_id = 'test_patch_004'"
    ).result_set

    assert len(result) >= 1
    assert result[0][1] == "test_patch_004"
    assert result[0][2] == "sharpe"


def test_patch_application_history(ch_client):
    """测试 Patch Application History"""
    test_data = {
        "patch_id": "test_patch_005",
        "action": "applied",
        "environment": "sandbox",
        "success": True,
        "error_message": "",
        "applied_by": "system",
        "rollback_reason": "",
        "rollback_triggered_by": "",
    }

    ch_client.insert(
        "evolution.patch_application_history",
        [list(test_data.values())],
        column_names=list(test_data.keys())
    )

    # Verify
    result = ch_client.query(
        "SELECT * FROM evolution.patch_application_history WHERE patch_id = 'test_patch_005'"
    ).result_set

    assert len(result) >= 1
    assert result[0][1] == "test_patch_005"


def test_patch_evaluator_score_calculation():
    """测试评分计算逻辑（单元测试级别）"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as f:
        f.write("--- test")
        patch_file = Path(f.name)

    try:
        evaluator = PatchEvaluator(
            patch_id="test_patch_006",
            patch_file=patch_file,
            target_module="tool:test"
        )

        # Mock evaluation results
        evaluator.evaluation_results["stages"] = {
            "static_analysis": {
                "passed": True,
                "ruff_issues": 0,
                "mypy_issues": 0,
                "bandit_issues": 0,
                "complexity_score": 5.0
            },
            "unit_tests": {
                "passed": True,
                "coverage": 0.8
            },
            "performance_tests": {
                "improvement_pct": 10.0
            },
            "regression_tests": {
                "passed": True
            }
        }

        scores = evaluator.calculate_scores()

        assert "benefit_score" in scores
        assert "risk_score" in scores
        assert "confidence" in scores
        assert "net_score" in scores

        assert 0 <= scores["benefit_score"] <= 1
        assert 0 <= scores["risk_score"] <= 1
        assert scores["net_score"] == scores["benefit_score"] - scores["risk_score"]

        # With good results, benefit should be > 0
        assert scores["benefit_score"] > 0

    finally:
        patch_file.unlink()


def test_patch_decision_making():
    """测试决策逻辑"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as f:
        f.write("--- test")
        patch_file = Path(f.name)

    try:
        evaluator = PatchEvaluator(
            patch_id="test_patch_007",
            patch_file=patch_file,
            target_module="tool:test"
        )

        # Test auto_approve scenario
        scores_good = {
            "benefit_score": 0.8,
            "risk_score": 0.2,
            "confidence": 0.9,
            "net_score": 0.6
        }

        decision = evaluator.make_decision(scores_good)
        assert decision["recommendation"] == "auto_approve"
        assert decision["can_auto_apply"] is True

        # Test reject scenario
        scores_bad = {
            "benefit_score": 0.2,
            "risk_score": 0.9,
            "confidence": 0.5,
            "net_score": -0.7
        }

        decision = evaluator.make_decision(scores_bad)
        assert decision["recommendation"] == "reject"
        assert decision["can_auto_apply"] is False

    finally:
        patch_file.unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
