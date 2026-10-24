#!/usr/bin/env python3
"""
Automatic Experiment Comparison (P5.3)

自动对比实验变体，在新实验完成后自动与 baseline 对比。

Usage:
    PYTHONPATH=. venv/bin/python tools/auto_compare_experiment.py \
        --new-experiment exp_12345 \
        --baseline baseline_v1 \
        --metric sharpe \
        --min-samples 100

Integration:
    可以被 experiment_api.complete_experiment() 自动调用
"""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

import structlog

logger = structlog.get_logger()


def auto_compare_with_baseline(
    new_experiment_id: str,
    baseline_id: Optional[str] = None,
    metric: str = "sharpe",
    min_samples: int = 100,
    output_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    自动对比新实验与 baseline

    Args:
        new_experiment_id: 新实验 ID
        baseline_id: Baseline ID (如果为 None，自动查找最近的成功实验)
        metric: 对比指标
        min_samples: 最小样本数
        output_path: 输出报告路径

    Returns:
        对比结果字典
    """
    from libs.experiment.variant_comparator import VariantComparator, export_comparison_report

    logger.info(
        "auto_compare_start",
        new_experiment=new_experiment_id,
        baseline=baseline_id,
        metric=metric
    )

    comparator = VariantComparator()

    # 如果没有指定 baseline，尝试查找
    if not baseline_id:
        baseline_id = find_recent_baseline(new_experiment_id)
        if not baseline_id:
            logger.warning("no_baseline_found_skipping_comparison")
            return {
                "ok": False,
                "error": "No baseline found for comparison",
                "new_experiment": new_experiment_id
            }

    logger.info("comparing_variants", new=new_experiment_id, baseline=baseline_id)

    # 运行对比
    result = comparator.compare_variants(
        variant_a=baseline_id,
        variant_b=new_experiment_id,
        metric=metric,
        min_samples=min_samples,
        confidence_level=0.95
    )

    if not result.get("ok"):
        logger.error("comparison_failed", error=result.get("error"))
        return result

    # 记录对比结果
    comparison = result.get("comparison", {})
    decision = result.get("decision", {})

    logger.info(
        "comparison_complete",
        p_value=comparison.get("p_value"),
        significant=comparison.get("statistically_significant"),
        effect_size=comparison.get("effect_size"),
        recommendation=decision.get("recommendation")
    )

    # 导出报告
    if output_path:
        export_comparison_report(result, output_path)
        logger.info("comparison_report_exported", path=output_path)
    else:
        # 默认路径
        default_path = f"runs/experiments/comparison_{new_experiment_id}_vs_{baseline_id}.json"
        Path(default_path).parent.mkdir(parents=True, exist_ok=True)
        export_comparison_report(result, default_path)
        logger.info("comparison_report_exported", path=default_path)

    # 存储到 ClickHouse
    try:
        store_comparison_to_clickhouse(result, new_experiment_id, baseline_id, metric)
    except Exception as e:
        logger.warning("failed_to_store_comparison", error=str(e))

    # 发出事件
    try:
        from tools.nightly_oof_flow import publish_ml_event

        publish_ml_event(
            "compared",
            "experiment_comparison",
            new_experiment_id,
            params={
                "baseline": baseline_id,
                "metric": metric,
                "significant": comparison.get("statistically_significant"),
                "p_value": comparison.get("p_value"),
                "effect_size": comparison.get("effect_size"),
                "recommendation": decision.get("recommendation"),
            }
        )
    except Exception as e:
        logger.warning("failed_to_publish_event", error=str(e))

    return result


def find_recent_baseline(exclude_experiment_id: str) -> Optional[str]:
    """
    查找最近的成功实验作为 baseline

    Args:
        exclude_experiment_id: 排除的实验 ID (通常是当前实验)

    Returns:
        Baseline 实验 ID，如果没有找到返回 None
    """
    import clickhouse_connect

    try:
        from libs.config import config
        ccfg = config.clickhouse_config
        ch_client = clickhouse_connect.get_client(
            host=ccfg.get("host", "localhost"),
            port=int(ccfg.get("port", 8123)),
            database=ccfg.get("database", "trading"),
            username=ccfg.get("user", "default"),
            password=ccfg.get("password", ""),
        )
    except Exception:
        ch_client = clickhouse_connect.get_client(
            host="localhost",
            port=8123,
            database="trading"
        )

    query = """
    SELECT experiment_id
    FROM ml.experiments
    WHERE status = 'PASS'
      AND experiment_id != %(exclude)s
      AND timestamp >= now() - INTERVAL 30 DAY
    ORDER BY timestamp DESC
    LIMIT 1
    """

    try:
        result = ch_client.query(query, parameters={"exclude": exclude_experiment_id})
        if result.result_rows:
            return result.result_rows[0][0]
    except Exception as e:
        logger.warning("failed_to_find_baseline", error=str(e))

    return None


def store_comparison_to_clickhouse(
    comparison_result: Dict[str, Any],
    new_experiment_id: str,
    baseline_id: str,
    metric: str
):
    """存储对比结果到 ClickHouse"""
    import clickhouse_connect

    try:
        from libs.config import config
        ccfg = config.clickhouse_config
        ch_client = clickhouse_connect.get_client(
            host=ccfg.get("host", "localhost"),
            port=int(ccfg.get("port", 8123)),
            database=ccfg.get("database", "trading"),
            username=ccfg.get("user", "default"),
            password=ccfg.get("password", ""),
        )
    except Exception:
        ch_client = clickhouse_connect.get_client(
            host="localhost",
            port=8123,
            database="trading"
        )

    # 注意: 这里使用 experiment_id 作为 patch_id (因为表设计时用的是 patch)
    # 在实际部署时可能需要一个专门的 experiment_comparison 表

    comparison_data = {
        "patch_id": new_experiment_id,
        "metric_name": metric,
        "before_value": comparison_result.get("variant_a", {}).get("mean", 0.0),
        "after_value": comparison_result.get("variant_b", {}).get("mean", 0.0),
        "change_pct": comparison_result.get("comparison", {}).get("relative_change_pct", 0.0),
        "improvement": comparison_result.get("comparison", {}).get("relative_change_pct", 0.0) > 0,
        "sample_size": comparison_result.get("variant_b", {}).get("samples", 0),
        "confidence_interval_low": comparison_result.get("variant_b", {}).get("ci_lower", 0.0),
        "confidence_interval_high": comparison_result.get("variant_b", {}).get("ci_upper", 0.0),
        "p_value": comparison_result.get("comparison", {}).get("p_value", 1.0),
    }

    ch_client.insert(
        "evolution.patch_comparison",
        [list(comparison_data.values())],
        column_names=list(comparison_data.keys())
    )

    logger.info("comparison_stored_in_clickhouse", experiment_id=new_experiment_id)


def main():
    ap = argparse.ArgumentParser(description="Automatic Experiment Comparison")
    ap.add_argument("--new-experiment", required=True, help="New experiment ID")
    ap.add_argument("--baseline", default=None, help="Baseline experiment ID (auto if not specified)")
    ap.add_argument("--metric", default="sharpe", help="Metric to compare (default: sharpe)")
    ap.add_argument("--min-samples", type=int, default=100, help="Minimum samples required")
    ap.add_argument("--output", default=None, help="Output path for comparison report")
    args = ap.parse_args()

    result = auto_compare_with_baseline(
        new_experiment_id=args.new_experiment,
        baseline_id=args.baseline,
        metric=args.metric,
        min_samples=args.min_samples,
        output_path=args.output
    )

    # Print summary
    if result.get("ok"):
        comparison = result.get("comparison", {})
        decision = result.get("decision", {})

        print("\n" + "=" * 60)
        print("Experiment Comparison Summary")
        print("=" * 60)
        print(f"New Experiment: {args.new_experiment}")
        print(f"Baseline: {args.baseline or result.get('baseline', 'auto')}")
        print(f"Metric: {args.metric}")
        print("")
        print(f"Baseline Mean: {result.get('variant_a', {}).get('mean', 0.0):.4f}")
        print(f"New Mean: {result.get('variant_b', {}).get('mean', 0.0):.4f}")
        print(f"Change: {comparison.get('relative_change_pct', 0.0):+.2f}%")
        print("")
        print(f"P-value: {comparison.get('p_value', 1.0):.4f}")
        print(f"Statistically Significant: {comparison.get('statistically_significant', False)}")
        print(f"Effect Size: {comparison.get('effect_size', 0.0):.3f} ({comparison.get('effect_interpretation', 'unknown')})")
        print("")
        print(f"Recommendation: {decision.get('recommendation', 'unknown').upper()}")
        print(f"Reason: {decision.get('reason', 'N/A')}")
        print("=" * 60)

        # Return code based on recommendation
        if decision.get("recommendation") == "adopt_variant_b":
            return 0  # Success - new variant is better
        elif decision.get("recommendation") == "keep_variant_a":
            return 1  # New variant is worse
        else:
            return 2  # Inconclusive
    else:
        print(f"\n❌ Comparison failed: {result.get('error', 'Unknown error')}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
