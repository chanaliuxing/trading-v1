#!/usr/bin/env python3
"""
Integration Test: Variant Comparator (P4.1)

测试 A/B 测试框架:
1. compare_variants() 统计检验
2. 效应量计算
3. 置信区间
4. 多变体 ANOVA
"""
import pytest
import numpy as np
import clickhouse_connect
from datetime import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from libs.experiment.variant_comparator import VariantComparator


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
        client.command("DELETE FROM ml.experiments WHERE experiment_id LIKE 'test_variant_%'")
    except Exception:
        pass


@pytest.fixture
def comparator(ch_client):
    """VariantComparator fixture"""
    return VariantComparator(ch_client=ch_client)


def test_variant_comparator_initialization(comparator):
    """测试 VariantComparator 初始化"""
    assert comparator is not None
    assert comparator.ch is not None


def test_descriptive_stats(comparator):
    """测试描述性统计"""
    data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    stats = comparator._descriptive_stats(data)

    assert stats["mean"] == 3.0
    assert stats["median"] == 3.0
    assert stats["min"] == 1.0
    assert stats["max"] == 5.0
    assert "std" in stats
    assert "q25" in stats
    assert "q75" in stats


def test_t_test(comparator):
    """测试 t 检验"""
    # Create two significantly different datasets
    data_a = np.array([1.0, 1.1, 1.2, 0.9, 1.0] * 20)  # Mean ~1.0
    data_b = np.array([2.0, 2.1, 2.2, 1.9, 2.0] * 20)  # Mean ~2.0

    result = comparator._t_test(data_a, data_b, confidence_level=0.95)

    assert "test" in result
    assert result["test"] == "t-test"
    assert "t_statistic" in result
    assert "p_value" in result
    assert "significant" in result

    # These datasets should be significantly different
    assert result["significant"] == True  # Use == instead of is for numpy bool
    assert result["p_value"] < 0.05


def test_chi_square_test(comparator):
    """测试卡方检验（二元指标）"""
    # Create two binary datasets
    # Group A: 60% success
    data_a = np.array([1] * 60 + [0] * 40)
    # Group B: 80% success
    data_b = np.array([1] * 80 + [0] * 20)

    result = comparator._chi_square_test(data_a, data_b, confidence_level=0.95)

    assert result["test"] == "chi-square"
    assert "chi2_statistic" in result
    assert "p_value" in result
    assert "significant" in result


def test_effect_size_calculation(comparator):
    """测试效应量（Cohen's d）"""
    # Small effect
    data_a = np.random.normal(1.0, 1.0, 100)
    data_b = np.random.normal(1.2, 1.0, 100)

    effect_size = comparator._calculate_effect_size(data_a, data_b, "sharpe")

    assert isinstance(effect_size, float)
    # Effect size should be small (< 0.5)
    assert abs(effect_size) < 1.0


def test_interpret_effect_size(comparator):
    """测试效应量解释"""
    assert comparator._interpret_effect_size(0.1) == "negligible"
    assert comparator._interpret_effect_size(0.3) == "small"
    assert comparator._interpret_effect_size(0.6) == "medium"
    assert comparator._interpret_effect_size(1.0) == "large"


def test_confidence_interval(comparator):
    """测试置信区间计算"""
    data = np.array([1.0, 2.0, 3.0, 4.0, 5.0] * 20)

    ci = comparator._confidence_interval(data, confidence_level=0.95)

    assert len(ci) == 2
    assert ci[0] < np.mean(data) < ci[1]  # Mean should be within CI


def test_make_decision(comparator):
    """测试决策逻辑"""
    # Scenario 1: Significant improvement
    test_result = {"significant": True, "p_value": 0.01}
    effect_size = 0.8  # Large effect
    stats_a = {"mean": 1.0}
    stats_b = {"mean": 1.8}

    decision = comparator._make_decision(test_result, effect_size, stats_a, stats_b)

    assert decision["recommendation"] == "adopt_variant_b"
    assert decision["confidence"] == "high"

    # Scenario 2: No significant difference
    test_result = {"significant": False, "p_value": 0.5}
    effect_size = 0.1  # Negligible effect
    stats_a = {"mean": 1.0}
    stats_b = {"mean": 1.05}

    decision = comparator._make_decision(test_result, effect_size, stats_a, stats_b)

    assert decision["recommendation"] == "inconclusive"


def test_is_continuous_metric(comparator):
    """测试指标类型判断"""
    assert comparator._is_continuous_metric("sharpe") is True
    assert comparator._is_continuous_metric("auc") is True
    assert comparator._is_continuous_metric("win_rate") is False
    assert comparator._is_continuous_metric("success_rate") is False


def test_compare_variants_insufficient_samples(comparator, monkeypatch):
    """测试样本不足的情况"""
    # Mock _get_variant_data to return insufficient samples
    def mock_get_variant_data(variant_id, metric):
        if variant_id == "test_variant_a":
            return np.array([1.5, 1.6])  # Only 2 samples
        return np.array([1.7])  # Only 1 sample

    monkeypatch.setattr(comparator, "_get_variant_data", mock_get_variant_data)

    result = comparator.compare_variants(
        variant_a="test_variant_a",
        variant_b="test_variant_b",
        metric="sharpe",
        min_samples=100
    )

    assert result["ok"] is False
    assert "Insufficient samples" in result["error"]


def test_compare_variants_mock_data(comparator, monkeypatch):
    """测试 compare_variants 完整流程（使用 mock 数据）"""
    # Mock _get_variant_data to return synthetic data
    def mock_get_variant_data(variant_id, metric):
        if variant_id == "variant_a":
            return np.random.normal(1.5, 0.3, 150)  # Mean=1.5, std=0.3, n=150
        elif variant_id == "variant_b":
            return np.random.normal(1.8, 0.3, 150)  # Mean=1.8, std=0.3, n=150
        return np.array([])

    monkeypatch.setattr(comparator, "_get_variant_data", mock_get_variant_data)

    result = comparator.compare_variants(
        variant_a="variant_a",
        variant_b="variant_b",
        metric="sharpe",
        min_samples=100,
        confidence_level=0.95
    )

    assert result["ok"] is True
    assert "variant_a" in result
    assert "variant_b" in result
    assert "comparison" in result
    assert "decision" in result

    # Check variant stats
    assert result["variant_a"]["samples"] == 150
    assert "mean" in result["variant_a"]
    assert "ci_lower" in result["variant_a"]
    assert "ci_upper" in result["variant_a"]

    # Check comparison
    comparison = result["comparison"]
    assert "p_value" in comparison
    assert "statistically_significant" in comparison
    assert "effect_size" in comparison
    assert "effect_interpretation" in comparison

    # Check decision
    decision = result["decision"]
    assert "recommendation" in decision
    assert "reason" in decision
    assert "confidence" in decision


def test_compare_multi_variants(comparator, monkeypatch):
    """测试多变体对比（ANOVA）"""
    # Mock data for 3 variants
    def mock_get_variant_data(variant_id, metric):
        if variant_id == "variant_1":
            return np.random.normal(1.5, 0.3, 150)
        elif variant_id == "variant_2":
            return np.random.normal(1.6, 0.3, 150)
        elif variant_id == "variant_3":
            return np.random.normal(1.7, 0.3, 150)
        return np.array([])

    monkeypatch.setattr(comparator, "_get_variant_data", mock_get_variant_data)

    result = comparator.compare_multi_variants(
        variants=["variant_1", "variant_2", "variant_3"],
        metric="sharpe",
        min_samples=100
    )

    assert result["ok"] is True
    assert result["variants_tested"] == 3
    assert "anova" in result
    assert "f_statistic" in result["anova"]
    assert "p_value" in result["anova"]
    assert "significant" in result["anova"]
    assert "best_variant" in result
    assert result["best_variant"]["id"] in ["variant_1", "variant_2", "variant_3"]


def test_export_comparison_report(comparator, tmp_path):
    """测试导出报告功能"""
    from libs.experiment.variant_comparator import export_comparison_report

    test_result = {
        "ok": True,
        "variant_a": {"id": "a", "mean": 1.5},
        "variant_b": {"id": "b", "mean": 1.8},
        "comparison": {"p_value": 0.01},
        "decision": {"recommendation": "adopt_variant_b"}
    }

    output_path = tmp_path / "test_report.json"

    export_comparison_report(test_result, str(output_path))

    assert output_path.exists()

    # Verify content
    loaded = json.loads(output_path.read_text())
    assert loaded["ok"] is True
    assert loaded["variant_a"]["id"] == "a"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
