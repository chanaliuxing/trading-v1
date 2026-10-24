#!/usr/bin/env python3
"""
数据质量检查脚本
检查ClickHouse中的数据完整性、质量和可用性
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from clickhouse_driver import Client
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def check_data_quality(symbol='TSLA', start_date='2025-11-01', end_date='2025-11-08'):
    """检查数据质量"""

    client = Client(host='localhost', port=9000, database='trading')

    print("=" * 80)
    print(f"数据质量检查报告 - {symbol}")
    print("=" * 80)
    print(f"时间范围: {start_date} to {end_date}")
    print()

    # 1. 检查数据可用性
    print("【1. 数据可用性】")
    query = f"""
    SELECT
        toDate(ts) as date,
        count(*) as bars,
        min(ts) as first_bar,
        max(ts) as last_bar
    FROM trading.features_1m FINAL
    WHERE symbol = '{symbol}'
      AND ts >= '{start_date}'
      AND ts < '{end_date}'
    GROUP BY date
    ORDER BY date
    """

    df_availability = client.query_dataframe(query)
    print(df_availability.to_string(index=False))
    print()

    if len(df_availability) == 0:
        print("❌ 致命错误: 没有任何数据！")
        print(f"   请运行: python tool/load_hist_multi.py --symbols {symbol} --start {start_date} --end {end_date}")
        return False

    # 2. 检查数据完整性
    print("【2. 数据完整性】")
    query = f"""
    SELECT
        count(*) as total_bars,
        count(DISTINCT toDate(ts)) as trading_days,
        countIf(close IS NULL) as missing_close,
        countIf(volume IS NULL OR volume = 0) as missing_volume,
        countIf(atr_14 IS NULL) as missing_atr
    FROM trading.features_1m FINAL
    WHERE symbol = '{symbol}'
      AND ts >= '{start_date}'
      AND ts < '{end_date}'
    """

    completeness = client.execute(query)[0]
    print(f"总bar数: {completeness[0]}")
    print(f"交易日数: {completeness[1]}")
    print(f"缺失close: {completeness[2]} ({completeness[2]/completeness[0]*100:.2f}%)")
    print(f"缺失volume: {completeness[3]} ({completeness[3]/completeness[0]*100:.2f}%)")
    print(f"缺失atr_14: {completeness[4]} ({completeness[4]/completeness[0]*100:.2f}%)")
    print()

    # 3. 检查数据质量统计
    print("【3. 数据质量统计】")
    query = f"""
    SELECT
        avg(close) as avg_price,
        stddevPop(close) as std_price,
        min(close) as min_price,
        max(close) as max_price,
        avg(volume) as avg_volume,
        avg(atr_14) as avg_atr,
        countIf(abs(close - lag(close) OVER (ORDER BY ts)) / lag(close) OVER (ORDER BY ts) > 0.05) as large_moves
    FROM trading.features_1m FINAL
    WHERE symbol = '{symbol}'
      AND ts >= '{start_date}'
      AND ts < '{end_date}'
    """

    stats = client.execute(query)[0]
    print(f"平均价格: ${stats[0]:.2f}")
    print(f"价格标准差: ${stats[1]:.2f}")
    print(f"价格区间: ${stats[2]:.2f} - ${stats[3]:.2f}")
    print(f"平均成交量: {stats[4]:,.0f}")
    print(f"平均ATR: ${stats[5]:.2f}")
    print(f"大幅波动(>5%): {stats[6]}次")
    print()

    # 4. 检查特征可用性
    print("【4. 特征可用性】")
    required_features = [
        'close', 'open', 'high', 'low', 'volume',
        'vwap', 'atr_14', 'rsi_14',
        'ema_10', 'ema_20', 'ema_50',
        'bb_up_20', 'bb_mid_20', 'bb_dn_20'
    ]

    query = f"""
    SELECT {', '.join([f'countIf({f} IS NOT NULL) as {f}_cnt' for f in required_features])}
    FROM trading.features_1m FINAL
    WHERE symbol = '{symbol}'
      AND ts >= '{start_date}'
      AND ts < '{end_date}'
    """

    feature_counts = client.execute(query)[0]
    total = completeness[0]

    for i, feat in enumerate(required_features):
        avail = feature_counts[i] / total * 100
        status = "✅" if avail > 95 else "⚠️" if avail > 80 else "❌"
        print(f"{status} {feat:15s}: {feature_counts[i]:6d}/{total} ({avail:5.1f}%)")
    print()

    # 5. 数据质量评分
    print("【5. 数据质量总评】")
    score = 0
    issues = []

    # 评分标准
    if completeness[0] >= 390 * completeness[1]:  # 每天至少390根bar
        score += 25
    else:
        issues.append(f"数据量不足: {completeness[0]} bars / {completeness[1]} days")

    if completeness[2] == 0:  # 无缺失close
        score += 25
    else:
        issues.append(f"缺失close: {completeness[2]}")

    if completeness[3] / completeness[0] < 0.01:  # volume缺失<1%
        score += 25
    else:
        issues.append(f"volume质量差: {completeness[3]/completeness[0]*100:.1f}%缺失")

    if all(feature_counts[i] / total > 0.95 for i in range(len(required_features))):
        score += 25
    else:
        issues.append("部分特征缺失过多")

    print(f"数据质量评分: {score}/100")

    if score >= 90:
        print("✅ 数据质量优秀，可以进行模型训练")
    elif score >= 70:
        print("⚠️ 数据质量一般，建议补充数据")
    else:
        print("❌ 数据质量差，需要重新获取数据")

    if issues:
        print("\n发现的问题:")
        for issue in issues:
            print(f"  - {issue}")

    return score >= 70

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='TSLA')
    parser.add_argument('--start', default='2025-11-01')
    parser.add_argument('--end', default='2025-11-08')
    args = parser.parse_args()

    success = check_data_quality(args.symbol, args.start, args.end)
    sys.exit(0 if success else 1)
