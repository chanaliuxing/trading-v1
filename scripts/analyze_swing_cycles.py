"""
涨幅周期分析脚本
分析股票的历史上升波段，找出规律：每隔 X 分钟出现一次 ≥Y% 的上升波段

用法:
    python scripts/analyze_swing_cycles.py --symbol TSLA --days 20 --min_gain 2.0
"""
import os
import sys
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
import pandas as pd
import numpy as np

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from libs.dal.market_data_facade import MarketDataFacade
from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)


class SwingCycleAnalyzer:
    """分析股票的涨幅周期"""

    def __init__(self):
        self.ch = ClickHouseDB()
        self.facade = MarketDataFacade(self.ch)

    def get_historical_data(self, symbol: str, days: int, resolution: str = '1m') -> pd.DataFrame:
        """
        获取历史数据

        Args:
            symbol: 股票代码
            days: 天数
            resolution: 时间周期 ('1m', '5m', etc.)

        Returns:
            DataFrame with columns: ts, open, high, low, close, volume
        """
        logger.info(f"获取 {symbol} 过去 {days} 天的 {resolution} 数据...")

        # 获取过去 N 天的交易日数据
        sql = f"""
        SELECT
            ts_start as ts,
            open,
            high,
            low,
            close,
            volume
        FROM trading.bars_{resolution}_current
        WHERE symbol = {{symbol:String}}
          AND ts_start >= now() - INTERVAL {{days:UInt32}} DAY
        ORDER BY ts ASC
        """

        result = self.ch.query(sql, {"symbol": symbol, "days": days})
        df = pd.DataFrame(result)

        if len(df) == 0:
            logger.warning(f"没有找到 {symbol} 的数据")
            return df

        # 只保留交易时间段的数据 (9:30-16:00 EST)
        df['ts'] = pd.to_datetime(df['ts'])
        df = df.set_index('ts')

        # 过滤盘前盘后
        df = df.between_time('09:30', '16:00')

        logger.info(f"获取到 {len(df)} 条记录，时间范围: {df.index.min()} 到 {df.index.max()}")
        return df

    def detect_swing_points(self, df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
        """
        检测局部高点和低点

        Args:
            df: OHLC 数据
            window: 检测窗口大小（前后多少根 bar）

        Returns:
            DataFrame with 'swing_low' and 'swing_high' columns
        """
        logger.info(f"检测波段转折点 (window={window})...")

        df = df.copy()

        # 检测局部低点：low 是前后 window 根 bar 中最低的
        df['swing_low'] = df['low'].rolling(window=window*2+1, center=True).min() == df['low']

        # 检测局部高点：high 是前后 window 根 bar 中最高的
        df['swing_high'] = df['high'].rolling(window=window*2+1, center=True).max() == df['high']

        # 去掉 NaN
        df['swing_low'] = df['swing_low'].fillna(False)
        df['swing_high'] = df['swing_high'].fillna(False)

        num_lows = df['swing_low'].sum()
        num_highs = df['swing_high'].sum()

        logger.info(f"发现 {num_lows} 个局部低点, {num_highs} 个局部高点")

        return df

    def extract_upswings(self, df: pd.DataFrame, intraday_only: bool = True) -> List[Dict]:
        """
        提取上升波段（从局部低点到下一个局部高点）

        Args:
            df: 包含 swing_low 和 swing_high 的 DataFrame
            intraday_only: 是否只提取当天内的波段（日内交易模式）

        Returns:
            List of swing dictionaries with:
                - start_ts: 开始时间
                - end_ts: 结束时间
                - start_price: 起点价格
                - end_price: 终点价格
                - gain_pct: 涨幅百分比
                - duration_min: 持续时间（分钟）
                - time_of_day: 开始时间段 ('morning', 'midday', 'afternoon')
                - date: 交易日
        """
        logger.info(f"提取上升波段 (intraday_only={intraday_only})...")

        swings = []

        # 按交易日分组处理
        df['date'] = df.index.date
        grouped = df.groupby('date')

        for date, day_df in grouped:
            # 获取当天的低点和高点
            lows = day_df[day_df['swing_low']].copy()
            highs = day_df[day_df['swing_high']].copy()

            if len(lows) == 0 or len(highs) == 0:
                continue

            lows['price'] = lows['low']
            highs['price'] = highs['high']

            # 遍历每个低点，找到之后的第一个高点
            for low_ts, low_row in lows.iterrows():
                # 找到这个低点之后的所有高点
                future_highs = highs[highs.index > low_ts]

                if intraday_only:
                    # 只取同一天的高点
                    future_highs = future_highs[future_highs.index.date == low_ts.date()]

                if len(future_highs) == 0:
                    continue

                # 取第一个高点
                high_ts = future_highs.index[0]
                high_row = future_highs.iloc[0]

                start_price = low_row['price']
                end_price = high_row['price']

                # 计算涨幅
                gain_pct = ((end_price - start_price) / start_price) * 100

                # 计算持续时间（分钟）
                duration = (high_ts - low_ts).total_seconds() / 60

                # 确定时间段
                hour = low_ts.hour
                if hour < 11:
                    time_of_day = 'morning'  # 9:30-11:00
                elif hour < 14:
                    time_of_day = 'midday'   # 11:00-14:00
                else:
                    time_of_day = 'afternoon'  # 14:00-16:00

                swings.append({
                    'start_ts': low_ts,
                    'end_ts': high_ts,
                    'start_price': start_price,
                    'end_price': end_price,
                    'gain_pct': gain_pct,
                    'duration_min': duration,
                    'time_of_day': time_of_day,
                    'date': date
                })

        logger.info(f"提取到 {len(swings)} 个上升波段")

        return swings

    def analyze_cycles(self, swings: List[Dict], min_gain_pct: float = 2.0) -> Dict:
        """
        分析涨幅周期统计数据

        Args:
            swings: 上升波段列表
            min_gain_pct: 最小涨幅过滤条件

        Returns:
            统计分析结果
        """
        if len(swings) == 0:
            logger.warning("没有波段数据可分析")
            return {}

        df = pd.DataFrame(swings)

        # 过滤掉涨幅太小的波段
        significant_swings = df[df['gain_pct'] >= min_gain_pct]

        logger.info(f"分析涨幅 >={min_gain_pct}% 的波段 ({len(significant_swings)}/{len(df)} 个)")

        if len(significant_swings) == 0:
            logger.warning(f"没有涨幅 >={min_gain_pct}% 的波段")
            return {}

        # 计算每天的波段数
        daily_counts = significant_swings.groupby('date').size()
        trading_days = len(daily_counts)
        swings_per_day = daily_counts.mean() if trading_days > 0 else 0

        # 计算同一天内的波段间隔
        intraday_intervals = []
        for date, day_swings in significant_swings.groupby('date'):
            day_swings = day_swings.sort_values('start_ts')
            for i in range(len(day_swings) - 1):
                time_gap = (day_swings.iloc[i+1]['start_ts'] -
                           day_swings.iloc[i]['end_ts']).total_seconds() / 60
                # 只统计合理的间隔（< 360分钟，即一个交易日内）
                if 0 < time_gap < 360:
                    intraday_intervals.append(time_gap)

        stats = {
            'total_swings': len(df),
            'significant_swings': len(significant_swings),
            'min_gain_threshold': min_gain_pct,
            'trading_days': trading_days,
            'swings_per_day': swings_per_day,

            # 涨幅统计
            'avg_gain_pct': significant_swings['gain_pct'].mean(),
            'median_gain_pct': significant_swings['gain_pct'].median(),
            'max_gain_pct': significant_swings['gain_pct'].max(),
            'min_gain_pct': significant_swings['gain_pct'].min(),

            # 持续时间统计
            'avg_duration_min': significant_swings['duration_min'].mean(),
            'median_duration_min': significant_swings['duration_min'].median(),
            'max_duration_min': significant_swings['duration_min'].max(),
            'min_duration_min': significant_swings['duration_min'].min(),

            # 间隔时间统计（日内）
            'avg_interval_min': np.mean(intraday_intervals) if intraday_intervals else 0,
            'median_interval_min': np.median(intraday_intervals) if intraday_intervals else 0,
            'num_intervals': len(intraday_intervals),

            # 时间段分布
            'time_distribution': significant_swings['time_of_day'].value_counts().to_dict(),

            # 详细数据
            'swings_detail': significant_swings.to_dict('records')
        }

        return stats

    def print_report(self, symbol: str, stats: Dict):
        """打印分析报告"""
        if not stats:
            print(f"\n⚠️  没有足够的数据来分析 {symbol}")
            return

        print(f"\n{'='*80}")
        print(f"📊 {symbol} 涨幅周期分析报告 (日内交易)")
        print(f"{'='*80}")

        print(f"\n【波段概况】")
        print(f"  分析交易日: {stats['trading_days']} 天")
        print(f"  总波段数: {stats['total_swings']}")
        print(f"  涨幅 ≥{stats['min_gain_threshold']}% 的波段: {stats['significant_swings']}")
        print(f"  每天平均波段数: {stats['swings_per_day']:.1f} 次")

        print(f"\n【涨幅统计】")
        print(f"  平均涨幅: {stats['avg_gain_pct']:.2f}%")
        print(f"  中位涨幅: {stats['median_gain_pct']:.2f}%")
        print(f"  最大涨幅: {stats['max_gain_pct']:.2f}%")
        print(f"  最小涨幅: {stats['min_gain_pct']:.2f}%")

        print(f"\n【持续时间统计】")
        print(f"  平均持续: {stats['avg_duration_min']:.1f} 分钟")
        print(f"  中位持续: {stats['median_duration_min']:.1f} 分钟")
        print(f"  最长持续: {stats['max_duration_min']:.1f} 分钟")
        print(f"  最短持续: {stats['min_duration_min']:.1f} 分钟")

        print(f"\n【周期规律】⭐")
        if stats['num_intervals'] > 0:
            print(f"  平均每隔: {stats['avg_interval_min']:.1f} 分钟出现一次 ≥{stats['min_gain_threshold']}% 的上升波段")
            print(f"  中位间隔: {stats['median_interval_min']:.1f} 分钟")
            print(f"  (基于 {stats['num_intervals']} 个日内连续波段间隔)")
        else:
            print(f"  ⚠️  没有足够的日内连续波段来计算间隔")

        print(f"\n【时间段分布】")
        time_dist = stats['time_distribution']
        for period, count in time_dist.items():
            pct = (count / stats['significant_swings']) * 100
            print(f"  {period:10s}: {count:3d} 次 ({pct:.1f}%)")

        print(f"\n【前 10 个最强波段】")
        swings = sorted(stats['swings_detail'], key=lambda x: x['gain_pct'], reverse=True)[:10]
        print(f"  {'时间':<20s} {'涨幅%':>8s} {'持续(分钟)':>12s} {'时段':>10s}")
        print(f"  {'-'*60}")
        for swing in swings:
            ts_str = swing['start_ts'].strftime('%Y-%m-%d %H:%M')
            print(f"  {ts_str:<20s} {swing['gain_pct']:>7.2f}% {swing['duration_min']:>11.1f} {swing['time_of_day']:>10s}")

        print(f"\n{'='*80}")
        print(f"\n💡 实战建议:")
        print(f"   1. 关注时段: {max(time_dist, key=time_dist.get)} 时段最活跃")
        print(f"   2. 预期涨幅: 每次波段平均可获得 {stats['avg_gain_pct']:.2f}% 涨幅")
        print(f"   3. 持仓时间: 平均持仓 {stats['avg_duration_min']:.1f} 分钟")
        if stats['num_intervals'] > 0:
            print(f"   4. 出现频率: 平均每 {stats['avg_interval_min']:.1f} 分钟出现一次机会")
            print(f"   5. 每日机会: 平均每天 {stats['swings_per_day']:.1f} 次交易机会")
        else:
            print(f"   4. 每日机会: 平均每天 {stats['swings_per_day']:.1f} 次交易机会")
        print(f"{'='*80}\n")

    def export_csv(self, symbol: str, stats: Dict, filename: str = None):
        """导出详细数据到 CSV"""
        if not stats or 'swings_detail' not in stats:
            logger.warning("没有数据可导出")
            return

        if filename is None:
            # 默认保存到 img 文件夹
            img_dir = os.path.join(PROJECT_ROOT, 'img')
            os.makedirs(img_dir, exist_ok=True)
            filename = os.path.join(img_dir, f"swing_analysis_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

        df = pd.DataFrame(stats['swings_detail'])
        df.to_csv(filename, index=False)
        logger.info(f"数据已导出到: {filename}")
        print(f"\n📄 详细数据已导出到: {filename}")


def main():
    parser = argparse.ArgumentParser(description="分析股票的涨幅周期")
    parser.add_argument('--symbol', type=str, default='TSLA', help='股票代码')
    parser.add_argument('--days', type=int, default=20, help='分析天数')
    parser.add_argument('--min_gain', type=float, default=2.0, help='最小涨幅过滤条件 (%)')
    parser.add_argument('--window', type=int, default=5, help='波段检测窗口大小')
    parser.add_argument('--resolution', type=str, default='1m', help='时间周期 (1m, 5m, etc.)')
    parser.add_argument('--export', action='store_true', help='导出 CSV')

    args = parser.parse_args()

    analyzer = SwingCycleAnalyzer()

    # 1. 获取历史数据
    df = analyzer.get_historical_data(args.symbol, args.days, args.resolution)

    if len(df) == 0:
        logger.error("没有数据，退出")
        return

    # 2. 检测波段转折点
    df = analyzer.detect_swing_points(df, window=args.window)

    # 3. 提取上升波段
    swings = analyzer.extract_upswings(df)

    # 4. 统计分析
    stats = analyzer.analyze_cycles(swings, min_gain_pct=args.min_gain)

    # 5. 打印报告
    analyzer.print_report(args.symbol, stats)

    # 6. 导出 CSV (可选)
    if args.export and stats:
        analyzer.export_csv(args.symbol, stats)


if __name__ == '__main__':
    main()
