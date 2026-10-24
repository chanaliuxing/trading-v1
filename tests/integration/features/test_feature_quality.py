#!/usr/bin/env python3
"""
测试 D: 增强特征质量检查

包装 tools/verify_feature_experiment.py，对不同特征组进行质量断言。

运行方式:
    pytest tests/integration/features/test_feature_quality.py -v
    pytest tests/integration/features/test_feature_quality.py::test_core_enhanced_features -v
"""
import pytest
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Dict

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
VERIFY_TOOL = PROJECT_ROOT / 'tools' / 'verify_feature_experiment.py'

# 特征组定义
FEATURE_GROUPS = {
    'core_enhanced': {
        'features': ['ret_5m', 'vol_5m', 'realized_vol_5m', 'vwap_dist_5m', 'volume_rank_20'],
        'min_coverage': 0.90,
        'description': '核心增强特征'
    },
    'cross_asset': {
        'features': ['corr_spy_20', 'beta_spy_20', 'ret_vs_spy_5m'],
        'min_coverage': 0.90,
        'description': '跨资产 SPY 特征'
    },
    'vix': {
        'features': ['vix_z_20'],
        'min_coverage': 0.50,  # VIX 数据从 2025-10-13 才有
        'description': 'VIX 特征（允许早期缺失）'
    }
}

# 测试配置
TEST_SYMBOLS = ['TSLA', 'NVDA']
START_DATE = '2025-11-20'
END_DATE = '2025-11-25'


def run_verify_tool(feature_cols: List[str], symbols: List[str]) -> Dict:
    """
    调用 verify_feature_experiment.py，返回 JSON 结果

    Returns:
        {
            "TSLA": {
                "ret_5m": {
                    "non_null_count": 390,
                    "total_count": 390,
                    "non_null_ratio": 1.0,
                    "mean": 0.001,
                    "std": 0.015,
                    "all_zero": False,
                    "all_constant": False
                }
            }
        }
    """
    if not VERIFY_TOOL.exists():
        pytest.skip(f"verify_feature_experiment.py not found: {VERIFY_TOOL}")

    cmd = [
        str(PROJECT_ROOT / 'venv' / 'bin' / 'python'),
        str(VERIFY_TOOL),
        '--symbols', ','.join(symbols),
        '--feature-cols', ','.join(feature_cols),
        '--start', START_DATE,
        '--end', END_DATE,
        '--table', 'trading.features_1m'
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=PROJECT_ROOT
        )

        if result.returncode != 0:
            pytest.fail(f"verify_feature_experiment.py failed: {result.stderr}")

        # 解析 JSON 输出
        return json.loads(result.stdout)

    except subprocess.TimeoutExpired:
        pytest.fail("verify_feature_experiment.py timed out after 60s")
    except json.JSONDecodeError as e:
        pytest.fail(f"Failed to parse JSON output: {e}\nOutput: {result.stdout}")
    except Exception as e:
        pytest.fail(f"Error running verify tool: {e}")


def _skip_if_no_rows(result: Dict, symbols: List[str], feature_cols: List[str]) -> None:
    total = 0
    for sym in symbols:
        for col in feature_cols:
            try:
                total += int((result.get(sym, {}).get(col, {}) or {}).get('total_rows', 0) or 0)
            except Exception:
                continue
    if total <= 0:
        pytest.skip("No feature rows in selected window; skipping environment-dependent quality assertions")


@pytest.mark.parametrize('group_name', ['core_enhanced', 'cross_asset'])
def test_feature_group_quality(group_name):
    """测试核心特征组质量"""
    group = FEATURE_GROUPS[group_name]
    feature_cols = group['features']
    min_coverage = group['min_coverage']

    print(f"\n测试特征组: {group['description']}")
    print(f"特征: {', '.join(feature_cols)}")
    print(f"最低覆盖率: {min_coverage:.0%}")

    result = run_verify_tool(feature_cols, TEST_SYMBOLS)
    _skip_if_no_rows(result, TEST_SYMBOLS, feature_cols)

    for symbol in TEST_SYMBOLS:
        assert symbol in result, f"Missing results for {symbol}"

        for col in feature_cols:
            assert col in result[symbol], f"{symbol}: Missing column {col}"

            col_result = result[symbol][col]

            # 断言 1: 非空率 >= min_coverage
            non_null_ratio = col_result['non_null_ratio']
            assert non_null_ratio >= min_coverage, \
                f"{symbol}.{col}: non_null_ratio={non_null_ratio:.2%} < {min_coverage:.0%}"

            # 断言 2: 不全为零
            assert not col_result.get('all_zero', False), \
                f"{symbol}.{col}: all values are zero (空气列)"

            # 断言 3: 不全为常数
            assert not col_result.get('all_constant', False), \
                f"{symbol}.{col}: all values are constant (无变化)"

            # 断言 4: 有标准差（如果覆盖率 > 50%）
            if non_null_ratio > 0.5:
                std = col_result.get('std')
                assert std is not None and std > 0, \
                    f"{symbol}.{col}: std={std} (应该 > 0)"

            print(f"  ✅ {symbol}.{col}: {non_null_ratio:.1%} 覆盖, std={col_result.get('std', 0):.4f}")


def test_vix_feature_acceptable():
    """VIX 特征允许部分缺失（早期数据不可用）"""
    group = FEATURE_GROUPS['vix']
    feature_cols = group['features']
    min_coverage = group['min_coverage']

    print(f"\n测试特征组: {group['description']}")
    print(f"特征: {', '.join(feature_cols)}")
    print(f"最低覆盖率: {min_coverage:.0%} (允许早期缺失)")

    result = run_verify_tool(feature_cols, TEST_SYMBOLS)
    _skip_if_no_rows(result, TEST_SYMBOLS, feature_cols)

    for symbol in TEST_SYMBOLS:
        for col in feature_cols:
            col_result = result[symbol][col]

            # VIX 只要求 >= 50% 覆盖（因为 2025-10-13 前无数据）
            non_null_ratio = col_result['non_null_ratio']
            assert non_null_ratio >= min_coverage, \
                f"{symbol}.{col}: non_null_ratio={non_null_ratio:.2%} < {min_coverage:.0%}"

            print(f"  ✅ {symbol}.{col}: {non_null_ratio:.1%} 覆盖 (VIX 早期缺失可接受)")


def test_all_feature_groups_summary():
    """汇总测试：一次性检查所有特征组"""
    all_features = []
    for group in FEATURE_GROUPS.values():
        all_features.extend(group['features'])

    all_features = list(set(all_features))  # 去重

    print(f"\n汇总测试: 检查所有 {len(all_features)} 个特征")

    result = run_verify_tool(all_features, TEST_SYMBOLS)
    _skip_if_no_rows(result, TEST_SYMBOLS, all_features)

    # 统计
    total_checks = len(TEST_SYMBOLS) * len(all_features)
    passed = 0
    failed_details = []

    for symbol in TEST_SYMBOLS:
        for col in all_features:
            col_result = result[symbol][col]

            # 根据特征所属组确定最低覆盖率
            min_coverage = 0.50  # 默认 VIX 标准
            for group in FEATURE_GROUPS.values():
                if col in group['features']:
                    min_coverage = group['min_coverage']
                    break

            # 检查
            non_null_ratio = col_result['non_null_ratio']
            all_zero = col_result.get('all_zero', False)
            all_constant = col_result.get('all_constant', False)

            if non_null_ratio >= min_coverage and not all_zero and not all_constant:
                passed += 1
            else:
                failed_details.append({
                    'symbol': symbol,
                    'column': col,
                    'coverage': non_null_ratio,
                    'min_required': min_coverage,
                    'all_zero': all_zero,
                    'all_constant': all_constant
                })

    # 输出汇总
    print(f"\n汇总结果:")
    print(f"  总检查数: {total_checks}")
    print(f"  通过: {passed} ({passed/total_checks*100:.1f}%)")
    print(f"  失败: {len(failed_details)}")

    if failed_details:
        print(f"\n失败详情:")
        for fail in failed_details:
            print(f"  ❌ {fail['symbol']}.{fail['column']}: "
                  f"coverage={fail['coverage']:.1%} (需要 >{fail['min_required']:.0%}), "
                  f"all_zero={fail['all_zero']}, all_constant={fail['all_constant']}")

    # 断言: 至少 90% 的检查应该通过
    assert passed / total_checks >= 0.90, \
        f"总体通过率 {passed/total_checks:.1%} < 90%"


if __name__ == "__main__":
    # 允许直接运行
    pytest.main([__file__, '-v'])
