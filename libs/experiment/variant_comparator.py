#!/usr/bin/env python3
"""
Variant Comparator: A/B 测试和实验对比工具

实现蓝皮书 v3 的实验接口要求:
- compare_variants(variant_a, variant_b): 对比两个实验变体
- 统计显著性检验
- 性能指标对比
- 可视化报告生成

Usage:
  from libs.experiment.variant_comparator import VariantComparator

  comparator = VariantComparator()
  result = comparator.compare_variants("exp_A", "exp_B",
                                       metric="sharpe",
                                       min_samples=100)
"""
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
import numpy as np
from scipy import stats
import clickhouse_connect
import structlog

logger = structlog.get_logger()


class VariantComparator:
    """实验变体对比器：支持 A/B 测试和多变体对比"""

    def __init__(self, ch_client=None):
        self.ch = ch_client or self._get_ch_client()
        self.logger = logger.bind(component="variant_comparator")

    def compare_variants(
        self,
        variant_a: str,
        variant_b: str,
        metric: str = "sharpe",
        min_samples: int = 100,
        confidence_level: float = 0.95
    ) -> Dict[str, Any]:
        """
        对比两个实验变体的性能

        Args:
            variant_a: 变体 A 的 ID (通常是 control/baseline)
            variant_b: 变体 B 的 ID (通常是 treatment/new_version)
            metric: 对比指标 ('sharpe', 'win_rate', 'auc', etc.)
            min_samples: 最小样本数要求
            confidence_level: 置信水平 (default: 0.95 for 95% CI)

        Returns:
            对比结果字典，包含统计检验、置信区间、结论等
        """
        self.logger.info("compare_variants_start",
                        variant_a=variant_a,
                        variant_b=variant_b,
                        metric=metric)

        # 1. 获取两个变体的数据
        data_a = self._get_variant_data(variant_a, metric)
        data_b = self._get_variant_data(variant_b, metric)

        # 2. 检查样本大小
        if len(data_a) < min_samples or len(data_b) < min_samples:
            return {
                "ok": False,
                "error": f"Insufficient samples (A: {len(data_a)}, B: {len(data_b)}, required: {min_samples})",
                "variant_a": variant_a,
                "variant_b": variant_b
            }

        # 3. 描述性统计
        stats_a = self._descriptive_stats(data_a)
        stats_b = self._descriptive_stats(data_b)

        # 4. 统计检验 (t-test for continuous metrics, chi-square for binary)
        if self._is_continuous_metric(metric):
            test_result = self._t_test(data_a, data_b, confidence_level)
        else:
            test_result = self._chi_square_test(data_a, data_b, confidence_level)

        # 5. 效应量 (Effect Size)
        effect_size = self._calculate_effect_size(data_a, data_b, metric)

        # 6. 置信区间
        ci_a = self._confidence_interval(data_a, confidence_level)
        ci_b = self._confidence_interval(data_b, confidence_level)

        # 7. 决策建议
        decision = self._make_decision(test_result, effect_size, stats_a, stats_b)

        result = {
            "ok": True,
            "variant_a": {
                "id": variant_a,
                "samples": len(data_a),
                "mean": stats_a["mean"],
                "std": stats_a["std"],
                "ci_lower": ci_a[0],
                "ci_upper": ci_a[1]
            },
            "variant_b": {
                "id": variant_b,
                "samples": len(data_b),
                "mean": stats_b["mean"],
                "std": stats_b["std"],
                "ci_lower": ci_b[0],
                "ci_upper": ci_b[1]
            },
            "comparison": {
                "metric": metric,
                "difference": stats_b["mean"] - stats_a["mean"],
                "relative_change_pct": ((stats_b["mean"] - stats_a["mean"]) / stats_a["mean"] * 100) if stats_a["mean"] != 0 else 0,
                "p_value": test_result["p_value"],
                "statistically_significant": test_result["significant"],
                "effect_size": effect_size,
                "effect_interpretation": self._interpret_effect_size(effect_size)
            },
            "decision": decision,
            "timestamp": datetime.now().isoformat()
        }

        self.logger.info("compare_variants_complete",
                        variant_a=variant_a,
                        variant_b=variant_b,
                        significant=test_result["significant"],
                        decision=decision["recommendation"])

        return result

    def compare_multi_variants(
        self,
        variants: List[str],
        metric: str = "sharpe",
        min_samples: int = 100
    ) -> Dict[str, Any]:
        """
        对比多个变体 (ANOVA)

        Args:
            variants: 变体 ID 列表
            metric: 对比指标
            min_samples: 最小样本数

        Returns:
            多变体对比结果
        """
        self.logger.info("compare_multi_variants_start",
                        variants=variants,
                        metric=metric)

        # 获取所有变体的数据
        all_data = {}
        for variant_id in variants:
            data = self._get_variant_data(variant_id, metric)
            if len(data) >= min_samples:
                all_data[variant_id] = data

        if len(all_data) < 2:
            return {
                "ok": False,
                "error": "Need at least 2 variants with sufficient samples"
            }

        # ANOVA 检验
        groups = list(all_data.values())
        f_stat, p_value = stats.f_oneway(*groups)

        # 计算每个变体的统计量
        variant_stats = {}
        for variant_id, data in all_data.items():
            variant_stats[variant_id] = {
                "samples": len(data),
                "mean": float(np.mean(data)),
                "std": float(np.std(data)),
                "median": float(np.median(data))
            }

        # 找出最佳变体
        best_variant = max(variant_stats.items(), key=lambda x: x[1]["mean"])

        result = {
            "ok": True,
            "metric": metric,
            "variants_tested": len(all_data),
            "anova": {
                "f_statistic": float(f_stat),
                "p_value": float(p_value),
                "significant": p_value < 0.05
            },
            "variant_stats": variant_stats,
            "best_variant": {
                "id": best_variant[0],
                "mean": best_variant[1]["mean"]
            },
            "timestamp": datetime.now().isoformat()
        }

        self.logger.info("compare_multi_variants_complete",
                        best_variant=best_variant[0],
                        significant=result["anova"]["significant"])

        return result

    # ===== Helper Methods =====

    def _get_variant_data(self, variant_id: str, metric: str) -> np.ndarray:
        """从 ClickHouse 获取变体的指标数据"""
        # 假设实验结果存储在 ml.experiments 或 trading.oof_xgb
        query = """
        SELECT %(metric)s AS value
        FROM ml.experiments
        WHERE experiment_id = %(variant_id)s
          AND %(metric)s IS NOT NULL
        """

        try:
            df = self.ch.query_df(query, parameters={"variant_id": variant_id, "metric": metric})
            return df["value"].values
        except Exception as e:
            self.logger.warning("get_variant_data_failed", variant_id=variant_id, error=str(e))
            return np.array([])

    def _descriptive_stats(self, data: np.ndarray) -> Dict[str, float]:
        """计算描述性统计"""
        return {
            "mean": float(np.mean(data)),
            "median": float(np.median(data)),
            "std": float(np.std(data)),
            "min": float(np.min(data)),
            "max": float(np.max(data)),
            "q25": float(np.percentile(data, 25)),
            "q75": float(np.percentile(data, 75))
        }

    def _t_test(
        self,
        data_a: np.ndarray,
        data_b: np.ndarray,
        confidence_level: float
    ) -> Dict[str, Any]:
        """执行 t 检验"""
        t_stat, p_value = stats.ttest_ind(data_a, data_b)

        return {
            "test": "t-test",
            "t_statistic": float(t_stat),
            "p_value": float(p_value),
            "significant": p_value < (1 - confidence_level)
        }

    def _chi_square_test(
        self,
        data_a: np.ndarray,
        data_b: np.ndarray,
        confidence_level: float
    ) -> Dict[str, Any]:
        """执行卡方检验 (用于二元指标，如转化率)"""
        # 构建列联表
        success_a = np.sum(data_a == 1)
        failure_a = np.sum(data_a == 0)
        success_b = np.sum(data_b == 1)
        failure_b = np.sum(data_b == 0)

        contingency_table = np.array([[success_a, failure_a], [success_b, failure_b]])
        chi2, p_value, dof, expected = stats.chi2_contingency(contingency_table)

        return {
            "test": "chi-square",
            "chi2_statistic": float(chi2),
            "p_value": float(p_value),
            "significant": p_value < (1 - confidence_level)
        }

    def _calculate_effect_size(
        self,
        data_a: np.ndarray,
        data_b: np.ndarray,
        metric: str
    ) -> float:
        """计算效应量 (Cohen's d)"""
        mean_a = np.mean(data_a)
        mean_b = np.mean(data_b)
        std_pooled = np.sqrt((np.var(data_a) + np.var(data_b)) / 2)

        if std_pooled == 0:
            return 0.0

        cohens_d = (mean_b - mean_a) / std_pooled
        return float(cohens_d)

    def _interpret_effect_size(self, effect_size: float) -> str:
        """解释效应量大小"""
        abs_effect = abs(effect_size)
        if abs_effect < 0.2:
            return "negligible"
        elif abs_effect < 0.5:
            return "small"
        elif abs_effect < 0.8:
            return "medium"
        else:
            return "large"

    def _confidence_interval(
        self,
        data: np.ndarray,
        confidence_level: float
    ) -> Tuple[float, float]:
        """计算置信区间"""
        mean = np.mean(data)
        sem = stats.sem(data)
        ci = stats.t.interval(confidence_level, len(data) - 1, loc=mean, scale=sem)
        return (float(ci[0]), float(ci[1]))

    def _is_continuous_metric(self, metric: str) -> bool:
        """判断指标是否为连续型"""
        binary_metrics = ["win_rate", "success_rate", "conversion"]
        return metric not in binary_metrics

    def _make_decision(
        self,
        test_result: Dict,
        effect_size: float,
        stats_a: Dict,
        stats_b: Dict
    ) -> Dict[str, Any]:
        """基于统计结果做出决策"""
        significant = test_result["significant"]
        improvement = stats_b["mean"] > stats_a["mean"]
        abs_effect = abs(effect_size)

        if significant and improvement and abs_effect >= 0.2:
            recommendation = "adopt_variant_b"
            reason = f"Statistically significant improvement (p={test_result['p_value']:.4f}, effect={effect_size:.2f})"
        elif significant and not improvement:
            recommendation = "keep_variant_a"
            reason = f"Variant B is significantly worse (p={test_result['p_value']:.4f})"
        elif not significant:
            recommendation = "inconclusive"
            reason = f"No statistically significant difference (p={test_result['p_value']:.4f})"
        elif abs_effect < 0.2:
            recommendation = "inconclusive"
            reason = f"Effect size too small ({effect_size:.2f})"
        else:
            recommendation = "collect_more_data"
            reason = "Trending towards improvement but needs more data"

        return {
            "recommendation": recommendation,
            "reason": reason,
            "confidence": "high" if significant and abs_effect >= 0.5 else "medium" if significant else "low"
        }

    def _get_ch_client(self):
        """获取 ClickHouse 客户端"""
        try:
            from libs.config import config
            ccfg = config.clickhouse_config
            return clickhouse_connect.get_client(
                host=ccfg.get("host", "localhost"),
                port=int(ccfg.get("port", 8123)),
                database=ccfg.get("database", "trading"),
                username=ccfg.get("user", "default"),
                password=ccfg.get("password", ""),
            )
        except Exception:
            return clickhouse_connect.get_client(host="localhost", port=8123, database="trading")


def export_comparison_report(comparison_result: Dict[str, Any], output_path: str):
    """导出对比报告为 JSON"""
    import json
    from pathlib import Path
    Path(output_path).write_text(json.dumps(comparison_result, indent=2))
    print(f"✅ Comparison report exported to {output_path}")


# ===== 使用示例 =====
if __name__ == "__main__":
    import json

    # 示例：对比两个实验变体
    comparator = VariantComparator()

    result = comparator.compare_variants(
        variant_a="baseline_v1",
        variant_b="new_feature_v2",
        metric="sharpe",
        min_samples=100,
        confidence_level=0.95
    )

    print(json.dumps(result, indent=2))

    # 导出报告
    # export_comparison_report(result, "runs/experiments/comparison_report.json")
