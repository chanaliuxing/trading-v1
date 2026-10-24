#!/usr/bin/env python3
"""
端到端集成测试: 数据就绪检查 → ml_scheduler → OOF 任务流程

Run:
    pytest tests/integration/test_data_readiness_flow.py -v
"""
import sys
from pathlib import Path
from datetime import date, timedelta
import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from libs.ml.data_readiness_checker import DataReadinessChecker, DataReadinessResult
from libs.db.clickhouse import ClickHouseDB


class TestDataReadinessFlow:
    """端到端集成测试"""

    @pytest.fixture
    def checker(self):
        """Create checker instance"""
        return DataReadinessChecker()

    @pytest.fixture
    def ch(self):
        """ClickHouse client"""
        return ClickHouseDB()

    @pytest.mark.integration
    def test_full_readiness_flow(self, checker, ch):
        """测试完整的数据就绪流程

        模拟 ml_scheduler 的完整流程:
        1. 查询最新数据日期
        2. 设置训练窗口
        3. 检查数据就绪状态
        4. 根据状态决定是否继续
        """
        # 1. 查询最新数据日期 (模拟 last_data_day)
        result = ch.query("""
            SELECT max(toDate(ts_end)) as last_day
            FROM trading.bars_1m_ibkr
            WHERE symbol = 'TSLA'
        """)
        last_day = result[0]['last_day']
        assert last_day is not None, "No data found for TSLA"

        # 2. 设置训练窗口 (30天)
        end_date = last_day
        start_date = end_date - timedelta(days=30)

        print(f"\n=== Full Readiness Flow Test ===")
        print(f"Training window: {start_date} to {end_date}")

        # 3. 数据就绪检查 (模拟 ml_scheduler 的检查)
        readiness_result = checker.check_training_window(
            symbol='TSLA',
            start=start_date,
            end=end_date,
            horizon='30m',
            min_utilization=0.5,
            features_table='trading.features_1m'
        )

        # 4. 验证返回结构
        assert isinstance(readiness_result, DataReadinessResult)
        assert readiness_result.status in ['ready', 'needs_window_adjust', 'needs_sync', 'not_ready']
        assert readiness_result.reason is not None
        assert readiness_result.bars_coverage is not None
        assert readiness_result.features_coverage is not None

        # 5. 打印结果 (模拟日志输出)
        print(f"\nStatus: {readiness_result.status}")
        print(f"Reason: {readiness_result.reason}")
        print(f"Bars coverage: {readiness_result.bars_coverage.utilization:.1%}")
        print(f"Features coverage: {readiness_result.features_coverage.utilization:.1%}")

        # 6. 模拟 ml_scheduler 的决策逻辑
        if readiness_result.status == 'not_ready':
            print("\n❌ Job would be BLOCKED")
            print(f"   Reason: {readiness_result.reason}")
            # 在真实场景中,这里会返回错误并记录到 ml_events
            assert readiness_result.metadata is not None

        elif readiness_result.status == 'needs_window_adjust':
            print("\n⚠️  Window adjustment suggested")
            print(f"   New window: {readiness_result.suggested_start} to {readiness_result.suggested_end}")
            assert readiness_result.suggested_start is not None
            assert readiness_result.suggested_end is not None

        elif readiness_result.status == 'needs_sync':
            print("\n⚠️  Sync required")
            print(f"   Action: {readiness_result.action}")
            assert readiness_result.action is not None

        else:  # ready
            print("\n✅ Job would PROCEED to training")
            print(f"   Features table: {readiness_result.metadata.get('features_table')}")
            # 在真实场景中,这里会继续执行 preflight 和训练
            assert readiness_result.metadata.get('features_table') is not None

    @pytest.mark.integration
    def test_multiple_symbols_readiness(self, checker):
        """测试多个 symbol 的并发检查 (模拟 ml_scheduler 检查多个 symbol)"""
        symbols = ['TSLA', 'AAPL', 'NVDA']
        end_date = date.today()
        start_date = end_date - timedelta(days=30)

        print(f"\n=== Multiple Symbols Readiness Test ===")
        print(f"Symbols: {symbols}")

        results = {}
        blocked_symbols = []

        for symbol in symbols:
            result = checker.check_training_window(
                symbol=symbol,
                start=start_date,
                end=end_date,
                horizon='30m',
                min_utilization=0.5,
                features_table='trading.features_1m'
            )

            results[symbol] = result

            print(f"\n{symbol}:")
            print(f"  Status: {result.status}")
            print(f"  Bars: {result.bars_coverage.utilization:.1%}" if result.bars_coverage else "  Bars: N/A")
            print(f"  Features: {result.features_coverage.utilization:.1%}" if result.features_coverage else "  Features: N/A")

            if result.status == 'not_ready':
                blocked_symbols.append(symbol)

        # 模拟 ml_scheduler 的逻辑: 任何一个 symbol not_ready 就阻止整个任务
        if blocked_symbols:
            print(f"\n❌ Job would be BLOCKED due to symbols: {', '.join(blocked_symbols)}")
        else:
            print(f"\n✅ All symbols ready, job would PROCEED")

        # 验证所有结果
        assert len(results) == len(symbols)
        for symbol, result in results.items():
            assert result.status in ['ready', 'needs_window_adjust', 'needs_sync', 'not_ready']

    @pytest.mark.integration
    def test_window_adjustment_logic(self, checker):
        """测试窗口调整逻辑

        使用一个较长的窗口 (90天),可能触发 needs_window_adjust
        """
        end_date = date.today()
        start_date = end_date - timedelta(days=90)

        print(f"\n=== Window Adjustment Test ===")
        print(f"Initial window: {start_date} to {end_date} (90 days)")

        result = checker.check_training_window(
            symbol='TSLA',
            start=start_date,
            end=end_date,
            horizon='30m',
            min_utilization=0.5,
            features_table='trading.features_1m'
        )

        print(f"\nStatus: {result.status}")

        if result.status == 'needs_window_adjust':
            print(f"\n✅ Window adjustment triggered")
            print(f"   Original: {start_date} to {end_date} ({(end_date - start_date).days} days)")
            print(f"   Suggested: {result.suggested_start} to {result.suggested_end} ({(result.suggested_end - result.suggested_start).days} days)")

            # 验证建议窗口比原窗口小
            original_days = (end_date - start_date).days
            suggested_days = (result.suggested_end - result.suggested_start).days
            assert suggested_days < original_days, "Suggested window should be smaller"

        elif result.status == 'ready':
            print(f"\n✅ Window is already optimal (no adjustment needed)")
            print(f"   Bars: {result.bars_coverage.utilization:.1%}")
            print(f"   Features: {result.features_coverage.utilization:.1%}")

    @pytest.mark.integration
    def test_features_table_selection(self, checker):
        """测试自动选择最佳特征表 (active vs baseline)"""
        end_date = date.today()
        start_date = end_date - timedelta(days=30)

        print(f"\n=== Features Table Auto-Selection Test ===")

        # 测试 1: 不指定 features_table (自动选择)
        result_auto = checker.check_training_window(
            symbol='TSLA',
            start=start_date,
            end=end_date,
            horizon='30m',
            features_table=None  # Auto-select
        )

        selected_table = result_auto.features_coverage.table_name if result_auto.features_coverage else None
        print(f"\nAuto-selected table: {selected_table}")
        assert selected_table is not None
        assert 'features_1m' in selected_table

        # 测试 2: 显式指定 features_1m
        result_baseline = checker.check_training_window(
            symbol='TSLA',
            start=start_date,
            end=end_date,
            horizon='30m',
            features_table='trading.features_1m'
        )

        print(f"Baseline table coverage: {result_baseline.features_coverage.utilization:.1%}")
        assert result_baseline.features_coverage.table_name == 'trading.features_1m'

        # 测试 3: 显式指定 features_1m_active (如果存在)
        try:
            result_active = checker.check_training_window(
                symbol='TSLA',
                start=start_date,
                end=end_date,
                horizon='30m',
                features_table='trading.features_1m_active'
            )
            print(f"Active table coverage: {result_active.features_coverage.utilization:.1%}")
            assert result_active.features_coverage.table_name == 'trading.features_1m_active'
        except Exception as e:
            print(f"Active table not available or error: {e}")

    @pytest.mark.integration
    def test_labels_coverage_check(self, checker):
        """测试 labels 覆盖率检查 (仅 with_labels 表)"""
        end_date = date.today()
        start_date = end_date - timedelta(days=30)

        print(f"\n=== Labels Coverage Test ===")

        # 测试 1: features_1m (无 labels)
        result_no_labels = checker.check_training_window(
            symbol='TSLA',
            start=start_date,
            end=end_date,
            horizon='30m',
            features_table='trading.features_1m'
        )

        print(f"\nfeatures_1m (no labels):")
        print(f"  Labels utilization: {result_no_labels.labels_coverage.utilization:.1%}")
        # 应该是 1.0 (跳过检查)
        assert result_no_labels.labels_coverage.utilization == 1.0

        # 测试 2: features_1m_with_labels (有 labels)
        result_with_labels = checker.check_training_window(
            symbol='TSLA',
            start=start_date,
            end=end_date,
            horizon='30m',
            features_table='trading.features_1m_with_labels'
        )

        print(f"\nfeatures_1m_with_labels:")
        print(f"  Labels utilization: {result_with_labels.labels_coverage.utilization:.1%}")
        # 应该是真实的覆盖率
        assert 0.0 <= result_with_labels.labels_coverage.utilization <= 1.0


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "-s"])
