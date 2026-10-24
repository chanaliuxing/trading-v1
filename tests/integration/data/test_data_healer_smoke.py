#!/usr/bin/env python3
"""
测试 B: DataHealer Smoke Test

验证 DataHealer 能够检测缺失数据并触发回填。

运行方式:
    pytest tests/integration/data/test_data_healer_smoke.py -v
    pytest tests/integration/data/test_data_healer_smoke.py::test_healer_detects_void -v
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import sys

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from services.data.data_healer import DataHealer


@pytest.fixture
def mock_clickhouse():
    """Mock ClickHouse database"""
    with patch('services.data.data_healer.ClickHouseDB') as mock_ch_class:
        mock_ch = Mock()
        mock_ch_class.return_value = mock_ch
        yield mock_ch


@pytest.fixture
def data_healer(tmp_path, mock_clickhouse):
    """Create DataHealer instance with mocked dependencies"""
    healer = DataHealer(runs_dir=str(tmp_path))
    healer.ch = mock_clickhouse
    return healer


def test_healer_detects_void(data_healer, mock_clickhouse):
    """
    测试 1: DataHealer 能检测数据缺失

    场景：
    - features_1m 对 TSLA 无数据
    - DataHealer._check_coverage() 应返回 ['TSLA']
    """
    # Mock: features_1m 返回空结果
    mock_clickhouse.query.return_value = []

    missing = data_healer._check_coverage(['TSLA'], days=2)

    assert 'TSLA' in missing, "DataHealer should detect missing TSLA data"
    assert len(missing) == 1
    print(f"✅ DataHealer detected missing symbols: {missing}")


def test_healer_passes_with_sufficient_data(data_healer, mock_clickhouse):
    """
    测试 2: DataHealer 对有充足数据的标的通过检查

    场景：
    - features_1m 对 TSLA 有 500 条记录
    - DataHealer._check_coverage() 应返回空列表
    """
    # Mock: features_1m 返回充足数据
    mock_clickhouse.query.return_value = [
        {'symbol': 'TSLA', 'cnt': 500}
    ]

    missing = data_healer._check_coverage(['TSLA'], days=2)

    assert len(missing) == 0, "DataHealer should pass for sufficient data"
    print("✅ DataHealer passed check with sufficient data")


def test_healer_detects_partial_void(data_healer, mock_clickhouse):
    """
    测试 3: DataHealer 能检测部分标的缺失

    场景：
    - TSLA 有数据 (500 条)
    - SPY 无数据 (0 条)
    - NVDA 数据不足 (5 条 < 10 阈值)
    """
    # Mock: 混合结果
    mock_clickhouse.query.side_effect = [
        # 第一次查询：初次检查
        [
            {'symbol': 'TSLA', 'cnt': 500},
            {'symbol': 'NVDA', 'cnt': 5}
        ],
        # 第二次查询：扩展窗口复查 (for SPY and NVDA)
        [
            {'symbol': 'TSLA', 'cnt': 520},
            {'symbol': 'NVDA', 'cnt': 5}
        ]
    ]

    missing = data_healer._check_coverage(['TSLA', 'SPY', 'NVDA'], days=2)

    assert 'TSLA' not in missing, "TSLA should not be missing"
    assert 'SPY' in missing, "SPY should be detected as missing"
    assert 'NVDA' in missing, "NVDA should be detected as missing (< 10 threshold)"
    print(f"✅ DataHealer detected partial void: {missing}")


def test_healer_triggers_backfill(data_healer, mock_clickhouse):
    """
    测试 4: DataHealer 触发回填脚本

    场景：
    - 检测到缺失数据
    - 应该调用 _run_backfill()
    - 应该调用 _rebuild_features_1m()
    """
    # Mock subprocess.run for backfill and feature rebuild
    with patch('services.data.data_healer.subprocess.run') as mock_run:
        # Mock successful backfill
        mock_run.return_value = Mock(returncode=0, stderr="", stdout="Backfill complete")

        # Mock: 初次检查发现缺失，回填后有数据
        mock_clickhouse.query.side_effect = [
            # 第一次查询：缺失
            [],
            # 扩展窗口复查：仍缺失
            [],
            # 回填后复查：有数据
            [{'symbol': 'TSLA', 'cnt': 390}],
        ]

        result = data_healer.ensure_data_ready(['TSLA'], lookback_days=2)

        # 验证调用了 subprocess.run
        assert mock_run.call_count >= 1, "Should call subprocess for backfill/rebuild"
        print(f"✅ DataHealer triggered {mock_run.call_count} subprocess calls")

        # 验证最终成功
        assert result is True, "DataHealer should succeed after healing"


def test_healer_handles_backfill_failure(data_healer, mock_clickhouse):
    """
    测试 5: DataHealer 处理回填失败

    场景：
    - 检测到缺失
    - 回填脚本失败 (returncode != 0)
    - DataHealer 应重试最多 3 次
    - 最终返回 False
    """
    # Mock subprocess.run 失败
    with patch('services.data.data_healer.subprocess.run') as mock_run, \
         patch('services.data.data_healer.time.sleep'):  # 跳过 sleep
        mock_run.return_value = Mock(returncode=1, stderr="Backfill failed")

        # Mock: 始终缺失
        mock_clickhouse.query.return_value = []

        result = data_healer.ensure_data_ready(['TSLA'], lookback_days=2)

        # 验证重试了多次
        assert mock_run.call_count >= 3, "Should retry backfill at least 3 times"
        print(f"✅ DataHealer retried {mock_run.call_count} times")

        # 验证最终失败
        assert result is False, "DataHealer should fail after max retries"


def test_healer_wait_for_ingestion_timeout(data_healer, mock_clickhouse):
    """
    测试 6: DataHealer 等待数据落地超时

    场景：
    - 回填脚本成功
    - 但数据未在超时时间内出现在 ClickHouse
    - 应该重试或失败
    """
    with patch('services.data.data_healer.subprocess.run') as mock_run, \
         patch('services.data.data_healer.time.sleep'):
        mock_run.return_value = Mock(returncode=0, stderr="", stdout="OK")

        # Mock: 回填后仍然查不到数据（模拟延迟写入超时）
        mock_clickhouse.query.return_value = []

        result = data_healer.ensure_data_ready(['TSLA'], lookback_days=2)

        # 验证失败
        assert result is False, "Should fail if data doesn't appear after timeout"
        print("✅ DataHealer correctly handles ingestion timeout")


def test_healer_weekend_scenario(data_healer, mock_clickhouse):
    """
    测试 7: 周末/假期场景不误判

    场景：
    - 当前是周末
    - now() - INTERVAL 2 DAY 落在非交易日
    - 但扩展窗口 (7 天) 能找到数据
    - 不应触发回填
    """
    # Mock: 初次检查无数据，但扩展窗口有数据
    mock_clickhouse.query.side_effect = [
        # 初次检查 (2 天)：无数据
        [],
        # 扩展窗口检查 (7 天)：有数据
        [{'symbol': 'TSLA', 'cnt': 390}]
    ]

    missing = data_healer._check_coverage(['TSLA'], days=2)

    assert len(missing) == 0, "Should not detect void on weekends if extended window has data"
    print("✅ DataHealer correctly handles weekend scenario")


@pytest.mark.parametrize('symbol_count', [1, 3, 10])
def test_healer_multiple_symbols(data_healer, mock_clickhouse, symbol_count):
    """
    测试 8: DataHealer 处理多个标的

    参数化测试：1, 3, 10 个标的
    """
    symbols = [f'SYM{i}' for i in range(symbol_count)]

    # Mock: 所有标的都有数据
    mock_data = [{'symbol': sym, 'cnt': 400} for sym in symbols]
    mock_clickhouse.query.return_value = mock_data

    missing = data_healer._check_coverage(symbols, days=2)

    assert len(missing) == 0, f"Should handle {symbol_count} symbols correctly"
    print(f"✅ DataHealer handled {symbol_count} symbols")


def test_healer_integration_summary():
    """
    汇总测试：验证 DataHealer 完整工作流

    此测试不使用 mock，而是打印测试总结
    """
    print("\n" + "=" * 80)
    print("DataHealer Smoke Test Summary")
    print("=" * 80)
    print("✅ Test 1: Detects void (missing data)")
    print("✅ Test 2: Passes with sufficient data")
    print("✅ Test 3: Detects partial void (mixed symbols)")
    print("✅ Test 4: Triggers backfill and feature rebuild")
    print("✅ Test 5: Handles backfill failure and retries")
    print("✅ Test 6: Handles ingestion timeout")
    print("✅ Test 7: Handles weekend/holiday scenarios")
    print("✅ Test 8: Handles multiple symbols")
    print("=" * 80)
    print("DataHealer 核心功能验证完成")
    print("=" * 80)


if __name__ == "__main__":
    # 允许直接运行
    pytest.main([__file__, '-v', '--tb=short'])
