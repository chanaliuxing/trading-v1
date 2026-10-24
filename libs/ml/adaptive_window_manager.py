#!/usr/bin/env python3
"""
Adaptive Window Manager - 动态训练窗口管理

根据数据质量自动调整训练窗口,最大化数据利用率:
- 自动缩窗至数据覆盖最佳区域
- 智能扩窗以包含足够训练样本
- 考虑 RTH、节假日、数据缺失等因素

Usage:
    from libs.ml.adaptive_window_manager import AdaptiveWindowManager

    manager = AdaptiveWindowManager()
    adjusted = manager.adjust_window(
        symbol="TSLA",
        desired_start=date(2025, 7, 1),
        desired_end=date(2025, 11, 1),
        min_samples=5000,
        horizon="30m"
    )

    if adjusted.adjusted:
        # 使用调整后的窗口
        train_start = adjusted.actual_start
        train_end = adjusted.actual_end
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

from libs.logger import get_logger
from libs.db.clickhouse import ClickHouseDB
from libs.ml.data_readiness_checker import DataReadinessChecker, DataReadinessResult

logger = get_logger("ml.adaptive_window_manager")


@dataclass
class WindowAdjustment:
    """窗口调整结果"""
    adjusted: bool  # 是否进行了调整
    desired_start: date
    desired_end: date
    actual_start: date
    actual_end: date
    reason: str
    samples_estimated: int
    utilization: float  # 调整后的数据利用率
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class DataGap:
    """数据缺失段"""
    start_date: date
    end_date: date
    missing_days: int
    reason: str  # "weekend", "holiday", "data_missing"


class AdaptiveWindowManager:
    """动态窗口管理器"""

    def __init__(self):
        self.ch = ClickHouseDB()
        self.readiness_checker = DataReadinessChecker()

        # 配置参数
        self.min_utilization_threshold = 0.6  # 最低利用率阈值
        self.target_utilization = 0.85  # 目标利用率
        self.min_training_days = 20  # 最少训练天数
        self.max_training_days = 180  # 最多训练天数

    def adjust_window(
        self,
        symbol: str,
        desired_start: date,
        desired_end: date,
        min_samples: int = 5000,
        horizon: str = "30m",
        features_table: Optional[str] = None
    ) -> WindowAdjustment:
        """动态调整训练窗口

        策略:
        1. 检查期望窗口的数据质量
        2. 如果利用率低,尝试缩窗至数据密集区域
        3. 如果缩窗后样本不足,尝试扩窗
        4. 避开大的数据缺失段

        Args:
            symbol: 股票代码
            desired_start: 期望的开始日期
            desired_end: 期望的结束日期
            min_samples: 最少样本数 (RTH bars)
            horizon: 预测horizon
            features_table: 特征表名称

        Returns:
            WindowAdjustment
        """
        logger.info(
            "adaptive_window_adjust_start",
            symbol=symbol,
            desired_start=desired_start.isoformat(),
            desired_end=desired_end.isoformat(),
            min_samples=min_samples
        )

        # 1. 检查期望窗口的数据质量
        readiness = self.readiness_checker.check_training_window(
            symbol=symbol,
            start=desired_start,
            end=desired_end,
            horizon=horizon,
            features_table=features_table
        )

        # 2. 如果数据质量良好,不调整
        if readiness.status == "ready" and \
           readiness.bars_coverage.utilization >= self.min_utilization_threshold:

            estimated_samples = readiness.bars_coverage.total_bars

            logger.info(
                "window_no_adjustment_needed",
                symbol=symbol,
                utilization=readiness.bars_coverage.utilization,
                samples=estimated_samples
            )

            return WindowAdjustment(
                adjusted=False,
                desired_start=desired_start,
                desired_end=desired_end,
                actual_start=desired_start,
                actual_end=desired_end,
                reason="data_quality_sufficient",
                samples_estimated=estimated_samples,
                utilization=readiness.bars_coverage.utilization,
                metadata={"readiness_status": readiness.status}
            )

        # 3. 数据质量不佳,需要调整
        # 策略 A: 使用 DataReadinessChecker 的建议窗口
        if readiness.status == "needs_window_adjust" and \
           readiness.suggested_start and readiness.suggested_end:

            # 验证建议窗口的样本数是否足够
            suggested_readiness = self.readiness_checker.check_training_window(
                symbol=symbol,
                start=readiness.suggested_start,
                end=readiness.suggested_end,
                horizon=horizon,
                features_table=features_table
            )

            if suggested_readiness.bars_coverage.total_bars >= min_samples:
                logger.info(
                    "window_adjusted_to_suggested",
                    symbol=symbol,
                    original_days=(desired_end - desired_start).days,
                    adjusted_days=(readiness.suggested_end - readiness.suggested_start).days,
                    samples=suggested_readiness.bars_coverage.total_bars
                )

                return WindowAdjustment(
                    adjusted=True,
                    desired_start=desired_start,
                    desired_end=desired_end,
                    actual_start=readiness.suggested_start,
                    actual_end=readiness.suggested_end,
                    reason="used_readiness_suggestion",
                    samples_estimated=suggested_readiness.bars_coverage.total_bars,
                    utilization=suggested_readiness.bars_coverage.utilization,
                    metadata={
                        "original_utilization": readiness.bars_coverage.utilization,
                        "adjusted_utilization": suggested_readiness.bars_coverage.utilization
                    }
                )

        # 策略 B: 智能缩窗至最后 N 天有数据的区域
        actual_end = self._find_last_data_day(symbol, desired_end)
        if not actual_end:
            actual_end = desired_end

        # 从最后数据日向前推算,找到满足 min_samples 的最短窗口
        actual_start = self._find_optimal_start(
            symbol=symbol,
            end=actual_end,
            min_samples=min_samples,
            max_days=min((desired_end - desired_start).days, self.max_training_days),
            features_table=features_table
        )

        # 验证调整后的窗口
        final_readiness = self.readiness_checker.check_training_window(
            symbol=symbol,
            start=actual_start,
            end=actual_end,
            horizon=horizon,
            features_table=features_table
        )

        logger.info(
            "window_adjusted_smart_shrink",
            symbol=symbol,
            original_days=(desired_end - desired_start).days,
            adjusted_days=(actual_end - actual_start).days,
            samples=final_readiness.bars_coverage.total_bars,
            utilization=final_readiness.bars_coverage.utilization
        )

        return WindowAdjustment(
            adjusted=True,
            desired_start=desired_start,
            desired_end=desired_end,
            actual_start=actual_start,
            actual_end=actual_end,
            reason="smart_shrink_to_data_dense_region",
            samples_estimated=final_readiness.bars_coverage.total_bars,
            utilization=final_readiness.bars_coverage.utilization,
            metadata={
                "original_utilization": readiness.bars_coverage.utilization,
                "strategy": "smart_shrink"
            }
        )

    def _find_last_data_day(self, symbol: str, max_date: date) -> Optional[date]:
        """查找最后有数据的日期"""
        query = """
        SELECT max(toDate(ts_end)) as last_day
        FROM trading.bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND toDate(ts_end) <= %(max_date)s
          AND toHour(ts_end) >= 9 AND toHour(ts_end) < 16
        """

        result = self.ch.query(
            query,
            parameters={
                "symbol": symbol,
                "max_date": max_date
            }
        )

        if result and result[0]['last_day']:
            return result[0]['last_day']
        return None

    def _find_optimal_start(
        self,
        symbol: str,
        end: date,
        min_samples: int,
        max_days: int,
        features_table: Optional[str]
    ) -> date:
        """找到满足 min_samples 的最短窗口起点

        二分查找策略:
        - 从最短窗口 (min_training_days) 开始
        - 逐步扩大窗口直到满足 min_samples
        """
        # 初始尝试: 最小训练天数
        start = end - timedelta(days=self.min_training_days)

        # 查询实际样本数
        for window_days in [self.min_training_days, 30, 45, 60, 90, 120, max_days]:
            if window_days > max_days:
                break

            test_start = end - timedelta(days=window_days)

            # 快速检查样本数 (只查 bars,不查 features/labels)
            query = """
            SELECT count() as samples
            FROM trading.bars_1m_ibkr
            WHERE symbol = %(symbol)s
              AND ts_end >= %(start)s
              AND ts_end <= %(end)s
              AND toHour(ts_end) >= 9 AND toHour(ts_end) < 16
            """

            result = self.ch.query(
                query,
                parameters={
                    "symbol": symbol,
                    "start": datetime.combine(test_start, datetime.min.time()),
                    "end": datetime.combine(end, datetime.max.time())
                }
            )

            samples = result[0]['samples'] if result else 0

            if samples >= min_samples:
                start = test_start
                logger.info(
                    "optimal_window_found",
                    symbol=symbol,
                    window_days=window_days,
                    samples=samples
                )
                break

        return start

    def analyze_data_gaps(
        self,
        symbol: str,
        start: date,
        end: date
    ) -> List[DataGap]:
        """分析数据缺失段 (用于高级窗口调整策略)

        识别:
        - 周末 (正常)
        - 节假日 (正常)
        - 数据缺失 (异常,需避开)
        """
        # 查询每日 bar 数
        query = """
        SELECT
            toDate(ts_end) as dt,
            count() as bars,
            toDayOfWeek(toDate(ts_end)) as dow
        FROM trading.bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND ts_end >= %(start)s
          AND ts_end <= %(end)s
          AND toHour(ts_end) >= 9 AND toHour(ts_end) < 16
        GROUP BY dt, dow
        ORDER BY dt
        """

        result = self.ch.query(
            query,
            parameters={
                "symbol": symbol,
                "start": datetime.combine(start, datetime.min.time()),
                "end": datetime.combine(end, datetime.max.time())
            }
        )

        # 分析缺失段
        gaps = []
        current_gap_start = None

        for i in range((end - start).days + 1):
            check_date = start + timedelta(days=i)

            # 查找该日期的数据
            day_data = next((r for r in result if r['dt'] == check_date), None)

            dow = check_date.weekday()  # 0=Monday, 6=Sunday

            # 判断是否缺失
            is_missing = False
            reason = ""

            if dow in [5, 6]:  # 周末
                reason = "weekend"
            elif not day_data:
                is_missing = True
                reason = "data_missing"
            elif day_data['bars'] < 100:  # 少于100个bars视为异常 (正常应有~390)
                is_missing = True
                reason = "insufficient_bars"

            if is_missing:
                if current_gap_start is None:
                    current_gap_start = check_date
            else:
                if current_gap_start is not None:
                    # 缺失段结束
                    gaps.append(DataGap(
                        start_date=current_gap_start,
                        end_date=check_date - timedelta(days=1),
                        missing_days=(check_date - current_gap_start).days,
                        reason=reason
                    ))
                    current_gap_start = None

        # 处理末尾缺失段
        if current_gap_start is not None:
            gaps.append(DataGap(
                start_date=current_gap_start,
                end_date=end,
                missing_days=(end - current_gap_start).days + 1,
                reason="data_missing"
            ))

        return gaps
