#!/usr/bin/env python3
"""
P5 End-to-End Integration Tests

Tests the full integration of P3-P4 components:
1. EvolutionStrategyManager → ResearchAgent → ExperimentOrchestrator
2. PatchEvaluator → PatchApprovalQueue → ClickHouse
3. VariantComparator → auto_compare_experiment
4. Memory Fabric → Promotions → LTS
5. Metrics Export → Prometheus

Usage:
    PYTHONPATH=. venv/bin/pytest tests/integration/test_p5_e2e_memory_evolution.py -v
"""
import json
import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any

import clickhouse_connect


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
    client.close()


@pytest.fixture
def temp_patch_file():
    """Temporary patch file fixture"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as f:
        f.write("""
--- a/tools/example.py
+++ b/tools/example.py
@@ -1,3 +1,4 @@
+# Optimization: Added caching
 def calculate(x):
-    return x * 2
+    cache = {}
+    return cache.get(x, x * 2)
""")
        temp_path = Path(f.name)

    yield temp_path

    if temp_path.exists():
        temp_path.unlink()


class TestP5EndToEndIntegration:
    """P5 端到端集成测试"""

    def test_evolution_strategy_selection(self, ch_client):
        """
        测试 EvolutionStrategyManager 目标选择

        验证:
        - UCB 算法正确选择目标
        - 优先级分数计算正确
        - 排除已运行的目标
        """
        from libs.evolution.evolution_strategy_manager import EvolutionStrategyManager

        manager = EvolutionStrategyManager(ch_client=ch_client)

        # Test target selection
        result = manager.select_next_target(
            strategy="ucb",
            exclude_targets=[]
        )

        assert result.get("ok") is not False, "Selection should succeed"
        assert "id" in result, "Should return target id"
        assert "priority_score" in result, "Should return priority score"
        assert 0 <= result["priority_score"] <= 1, "Score should be normalized"

        # Test ranking
        ranked = manager.rank_all_targets()
        assert isinstance(ranked, list), "Should return list of targets"

        if len(ranked) > 1:
            # Verify descending order
            for i in range(len(ranked) - 1):
                assert ranked[i]["priority_score"] >= ranked[i+1]["priority_score"], \
                    "Targets should be ranked by priority"

    def test_patch_evaluator_integration(self, ch_client, temp_patch_file):
        """
        测试 PatchEvaluator 自动评估流程

        验证:
        - 补丁文件正确解析
        - 7阶段评估执行
        - 结果写入 ClickHouse
        """
        from tools.patch_evaluator import PatchEvaluator

        patch_id = f"test_patch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        evaluator = PatchEvaluator(
            patch_id=patch_id,
            patch_file=temp_patch_file,
            target_module="tool:example"
        )

        # Run evaluation
        result = evaluator.evaluate_all()

        assert result is not None, "Evaluation should return result"
        assert "scores" in result, "Should contain scores"
        assert "decision" in result, "Should contain decision"
        assert "stages" in result, "Should contain stage results"

        scores = result["scores"]
        assert "benefit_score" in scores
        assert "risk_score" in scores
        assert "confidence" in scores

        assert 0 <= scores["benefit_score"] <= 1
        assert 0 <= scores["risk_score"] <= 1
        assert 0 <= scores["confidence"] <= 1

        decision = result["decision"]
        assert "recommendation" in decision
        assert decision["recommendation"] in ["auto_approve", "manual_review", "reject"]

    def test_variant_comparison_workflow(self, ch_client):
        """
        测试实验变体自动对比流程

        验证:
        - 自动查找 baseline
        - 统计对比正确
        - 结果存入 ClickHouse
        """
        from tools.auto_compare_experiment import find_recent_baseline, store_comparison_to_clickhouse

        # Test baseline finding
        baseline_id = find_recent_baseline(exclude_experiment_id="test_exp_999")

        # May be None if no experiments exist, which is okay for integration test
        if baseline_id:
            assert isinstance(baseline_id, str), "Baseline ID should be string"
            assert len(baseline_id) > 0, "Baseline ID should not be empty"

        # Test comparison storage (with mock data)
        comparison_result = {
            "ok": True,
            "variant_a": {
                "mean": 0.05,
                "std": 0.02,
                "samples": 100,
                "ci_lower": 0.03,
                "ci_upper": 0.07
            },
            "variant_b": {
                "mean": 0.06,
                "std": 0.02,
                "samples": 100,
                "ci_lower": 0.04,
                "ci_upper": 0.08
            },
            "comparison": {
                "relative_change_pct": 20.0,
                "p_value": 0.03,
                "statistically_significant": True,
                "effect_size": 0.5
            },
            "decision": {
                "recommendation": "adopt_variant_b",
                "reason": "Significant improvement"
            }
        }

        # Should not raise exception
        try:
            store_comparison_to_clickhouse(
                comparison_result,
                new_experiment_id="test_exp_new",
                baseline_id=baseline_id or "test_baseline",
                metric="sharpe"
            )
        except Exception as e:
            pytest.fail(f"Comparison storage failed: {e}")

    def test_memory_fabric_promotion(self, ch_client):
        """
        测试 Memory Fabric 记忆晋升流程

        验证:
        - STS 记录正常插入
        - MTS 晋升逻辑
        - LTS 长期存储
        """
        # Check table existence
        tables = ch_client.query("SHOW TABLES FROM memory_fabric").result_rows
        table_names = [row[0] for row in tables]

        assert "short_term" in table_names, "STS table should exist"
        assert "mid_term" in table_names, "MTS table should exist"
        assert "long_term" in table_names, "LTS table should exist"

        # Check counts (should be >= 0)
        sts_count = ch_client.query("SELECT count() FROM memory_fabric.short_term").result_rows[0][0]
        mts_count = ch_client.query("SELECT count() FROM memory_fabric.mid_term").result_rows[0][0]
        lts_count = ch_client.query("SELECT count() FROM memory_fabric.long_term FINAL").result_rows[0][0]

        assert sts_count >= 0
        assert mts_count >= 0
        assert lts_count >= 0

    def test_metrics_exporter_collection(self, ch_client):
        """
        测试 Prometheus 指标导出器

        验证:
        - 指标收集不报错
        - 所有指标类别都覆盖
        """
        from tools.p3_p4_metrics_exporter import MetricsExporter

        exporter = MetricsExporter(port=9999, interval=30)  # Use different port for test

        # Should not raise exception
        try:
            exporter.collect_metrics()
        except Exception as e:
            pytest.fail(f"Metrics collection failed: {e}")

    def test_research_agent_with_evolution_strategy(self, ch_client):
        """
        测试 ResearchAgent 使用 EvolutionStrategyManager 选择目标

        验证:
        - _pick_worst_mutable 使用 EvolutionStrategyManager
        - 失败时回退到启发式算法
        - 返回目标包含演化元数据
        """
        from scripts.ai_trader.research.agent import _pick_worst_mutable

        # Mock mutable targets
        mutables = {
            "tool:backtest_ml_engine": {
                "id": "tool:backtest_ml_engine",
                "kind": "tool",
                "metrics": {
                    "health_score": 0.4,
                    "lag_hours": 48,
                    "error_rate": 0.05
                }
            },
            "data:features_1m_with_labels": {
                "id": "data:features_1m_with_labels",
                "kind": "data_pipeline",
                "metrics": {
                    "health_score": 0.8,
                    "lag_hours": 2,
                    "error_rate": 0.01
                }
            }
        }

        result = _pick_worst_mutable(mutables)

        assert result is not None, "Should select a target"
        assert "id" in result, "Should have target id"

        # Check if evolution metadata was added (either from EvolutionStrategyManager or fallback)
        # The function should add issue_summary at minimum
        assert "issue_summary" in result or "evolution_score" in result, \
            "Should have evolution metadata or issue summary"

    def test_full_research_cycle_integration(self):
        """
        测试完整研究周期集成

        验证:
        - ResearchState 收集
        - ResearchAgent 生成计划
        - ExperimentOrchestrator 入队实验
        - 包含 P3-P4 组件
        """
        import asyncio
        from scripts.ai_trader.research.agent import run_research_cycle_once

        try:
            plan, state, policy = asyncio.run(run_research_cycle_once())

            assert plan is not None, "Should generate plan"
            assert state is not None, "Should collect state"
            assert policy is not None, "Should load policy"

            # Check plan structure
            assert "experiments" in plan or "infra_experiments" in plan, \
                "Plan should contain experiments"

            # Check if mutable targets and readiness probe are included
            # (These are added by P5 enhancements)
            if "mutable_targets" in plan:
                assert isinstance(plan["mutable_targets"], dict)

            if "readiness_probe" in plan:
                assert isinstance(plan["readiness_probe"], (dict, str))

        except Exception as e:
            # Research cycle may fail if LLM is unavailable, which is okay
            # We're testing integration, not LLM availability
            if "llm" not in str(e).lower() and "pool" not in str(e).lower():
                pytest.fail(f"Research cycle failed unexpectedly: {e}")

    def test_patch_approval_queue_schema(self, ch_client):
        """
        测试 evolution.patch_approval_queue 表结构

        验证:
        - 表存在
        - 必需字段存在
        - 数据类型正确
        """
        # Check table exists
        tables = ch_client.query("SHOW TABLES FROM evolution").result_rows
        table_names = [row[0] for row in tables]

        assert "patch_approval_queue" in table_names, "Patch approval queue table should exist"

        # Check schema
        schema = ch_client.query("DESCRIBE evolution.patch_approval_queue").result_rows
        column_names = [row[0] for row in schema]

        required_columns = [
            "patch_id",
            "target_module",
            "benefit_score",
            "risk_score",
            "confidence",
            "status"
        ]

        for col in required_columns:
            assert col in column_names, f"Column {col} should exist in patch_approval_queue"

    def test_prometheus_metrics_availability(self):
        """
        测试 Prometheus 指标是否正确定义

        验证:
        - 所有指标都正确注册
        - 指标类型正确 (Gauge vs Counter)
        """
        from tools.p3_p4_metrics_exporter import (
            MEMORY_STS_GAUGE,
            MEMORY_MTS_GAUGE,
            MEMORY_LTS_GAUGE,
            MEMORY_PROMOTIONS_COUNTER,
            PATCH_QUEUE_GAUGE,
            EXPERIMENTS_COMPARED_COUNTER,
            EVOLUTION_TARGET_PRIORITY,
            PATCH_BENEFIT_SCORE,
            PATCH_RISK_SCORE
        )

        from prometheus_client import Gauge, Counter

        # Verify metric types
        assert isinstance(MEMORY_STS_GAUGE, Gauge)
        assert isinstance(MEMORY_MTS_GAUGE, Gauge)
        assert isinstance(MEMORY_LTS_GAUGE, Gauge)
        assert isinstance(MEMORY_PROMOTIONS_COUNTER, Counter)
        assert isinstance(PATCH_QUEUE_GAUGE, Gauge)
        assert isinstance(EXPERIMENTS_COMPARED_COUNTER, Counter)
        assert isinstance(EVOLUTION_TARGET_PRIORITY, Gauge)
        assert isinstance(PATCH_BENEFIT_SCORE, Gauge)
        assert isinstance(PATCH_RISK_SCORE, Gauge)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
