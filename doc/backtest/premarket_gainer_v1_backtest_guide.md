# Premarket Gainer V1 策略回测指南

## 📋 数据准备清单

### 1. 数据下载 (已完成)

```bash
# 下载6个月1分钟数据(含盘前盘后)
venv/bin/python tool/load_hist_multi.py \
  --symbols AAPL TSLA NVDA MSFT META GOOGL AMZN AMD QQQ \
  --sessions 120 \
  --bar-size "1 min" \
  --fill-mode backfill \
  --no-rth \
  --chunk-days 5
```

**关键参数**:
- `--no-rth`: 包含盘前盘后数据 (04:00-20:00 ET) - **必须!**
- `--fill-mode backfill`: 强制回填,覆盖现有数据
- `--sessions 120`: 约6个月历史数据

### 2. 数据验证

下载完成后,运行验证脚本:

```bash
venv/bin/python tool/verify_backtest_data.py
```

检查项:
- ✅ 盘前数据覆盖 (04:00-09:30 ET)
- ✅ 数据连续性 (gap检测)
- ✅ OHLCV完整性
- ✅ 时间范围覆盖 (至少120天)

### 3. 快速检查盘前数据

```bash
venv/bin/python -c "
from libs.db.clickhouse import ClickHouseDB
ch = ClickHouseDB()
result = ch.query('''
    SELECT symbol, count() as cnt
    FROM bars_1m_ibkr
    WHERE toHour(ts_start) < 9
    GROUP BY symbol
    ORDER BY cnt DESC
''')
for row in result:
    print(f'{row[\"symbol\"]}: {row[\"cnt\"]} 盘前bars')
ch.close()
"
```

---

## 🎯 回测引擎使用

### 策略需求分析

根据 `strategies/premarket_gainer_v1.py` 的实现:

**数据需求**:
1. **分钟级K线** (`bars_1m_ibkr`)
   - 用于计算 VWAP, RVOL_1m, RVOL_5m
   - 必须包含盘前数据以计算 PM_H (盘前高点)

2. **L1价格数据** (PriceCache)
   - last, bid, ask, spread_pct
   - 在回测中可以从 bars 的 OHLC 数据模拟

3. **特征数据** (features)
   - pm_high: 盘前高点 (从04:00-09:30的bars计算)
   - orb5_high: 开盘前5分钟高点 (09:30-09:35)
   - vwap: 成交量加权平均价
   - rvol_1m, rvol_5m: 相对成交量

**时间窗口**:
- 09:27: Scanner选出Top3
- 09:30-09:32: 观察第一波动量
- 09:33-09:35: 检查4个确认条件,执行入场
- 09:37-10:10: 仓位管理 (OCA bracket自动处理)
- 10:10: 强制平仓

### 回测配置示例

#### Option 1: 使用回测引擎 + 准备好的features

```python
# scripts/backtest_premarket_v1.py
import asyncio
from datetime import datetime
from services.backtest.backtest_engine import BacktestEngine, BacktestConfig

async def main():
    # 1. 从ClickHouse查询features数据
    from libs.db.clickhouse import ClickHouseDB
    ch = ClickHouseDB()

    # 查询一段时间的features (假设已有features表)
    features_df = ch.query("""
        SELECT
            ts,
            symbol,
            open, high, low, close, volume, vwap,
            pm_high, orb5_high,
            rvol_1m, rvol_5m,
            spread_pct
        FROM features_1m
        WHERE symbol IN ('AAPL', 'TSLA', 'NVDA', 'MSFT', 'META', 'GOOGL', 'AMZN', 'AMD', 'QQQ')
          AND ts >= '2025-07-01 09:00:00'
          AND ts <= '2025-09-30 16:00:00'
        ORDER BY ts, symbol
    """)

    # 2. 配置回测
    config = BacktestConfig(
        symbols=['AAPL', 'TSLA', 'NVDA', 'MSFT', 'META', 'GOOGL', 'AMZN', 'AMD', 'QQQ'],
        start='2025-07-01',
        end='2025-09-30',
        data=features_df,  # 使用查询到的features
        strategy_path='strategies/premarket_gainer_v1.py',
        strategy_class='PremarketGainerV1',
        strategy_params={
            'premarket_config': {
                'timing': {
                    'scan_time_et': '09:27:00',
                    'observe_start_et': '09:30:00',
                    'observe_end_et': '09:32:00',
                    'entry_start_et': '09:33:00',
                    'entry_end_et': '09:35:00',
                    'force_exit_time_et': '10:10:00',
                },
                'scanner': {
                    'rows': 50,
                    'price_min': 5.0,
                    'pm_notional_min': 10_000_000,
                },
                'execution': {
                    'spread_max_observe': 0.0030,
                    'spread_max_exec': 0.0025,
                    'rvol_min': 2.0,
                    'breakout_buffer_pct': 0.002,
                    'limit_buffer_pct': 0.001,
                },
                'take_profit': {
                    'levels_pct': [0.035, 0.055, 0.08],
                    'qtys_pct': [0.30, 0.35, 0.35],
                },
                'risk': {
                    'per_trade_pct': 0.005,
                    'daily_dd_limit_pct': 0.015,
                },
            }
        },
        starting_cash=100_000.0,
        commission_per_share=0.005,
        slippage_bps=1.0,
    )

    # 3. 运行回测
    engine = BacktestEngine(config)
    result = await engine.run()

    # 4. 分析结果
    print("\n" + "="*80)
    print("回测结果")
    print("="*80)
    print(f"最终权益: ${result['metrics']['final_nav']:,.2f}")
    print(f"总手续费: ${result['metrics']['total_fee']:,.2f}")
    print(f"实现盈亏: ${result['metrics']['realized_pnl']:,.2f}")
    print(f"交易次数: {result['metrics']['trades']}")
    print(f"胜率: {result['metrics']['win_rate']*100:.1f}%")
    print(f"盈亏比: {result['metrics']['profit_factor']:.2f}")

    # 5. 输出成交明细
    print("\n成交明细:")
    for trade in result['ledger']:
        print(f"  {trade['ts']}: {trade['symbol']} {trade['side']} {trade['qty']}@${trade['price']:.2f} | PnL: ${trade.get('realized_pnl', 0):.2f}")

if __name__ == "__main__":
    asyncio.run(main())
```

#### Option 2: 从原始bars计算features (更灵活)

如果没有预计算的features表,可以从bars实时计算:

```python
# scripts/calculate_features_and_backtest.py
import pandas as pd
from libs.db.clickhouse import ClickHouseDB

def calculate_features_from_bars(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """从bars_1m_ibkr计算回测所需features"""
    ch = ClickHouseDB()

    # 1. 查询原始bars (包含盘前)
    bars = ch.query(f"""
        SELECT
            ts_start as ts,
            symbol,
            open, high, low, close, volume, vwap
        FROM bars_1m_ibkr
        WHERE symbol = '{symbol}'
          AND ts_start >= '{start_date} 04:00:00'
          AND ts_start <= '{end_date} 16:00:00'
        ORDER BY ts_start
    """)

    df = pd.DataFrame(bars)
    df['ts'] = pd.to_datetime(df['ts'])

    # 2. 计算盘前高点 PM_H (04:00-09:30)
    df['hour'] = df['ts'].dt.hour
    df['minute'] = df['ts'].dt.minute
    df['is_premarket'] = (df['hour'] >= 4) & ((df['hour'] < 9) | ((df['hour'] == 9) & (df['minute'] < 30)))

    # 按日期分组计算PM_H
    df['date'] = df['ts'].dt.date
    pm_highs = df[df['is_premarket']].groupby('date')['high'].max().reset_index()
    pm_highs.columns = ['date', 'pm_high']
    df = df.merge(pm_highs, on='date', how='left')

    # 3. 计算ORB5_H (09:30-09:35开盘前5分钟高点)
    df['is_orb5'] = ((df['hour'] == 9) & (df['minute'] >= 30) & (df['minute'] < 35))
    orb5_highs = df[df['is_orb5']].groupby('date')['high'].max().reset_index()
    orb5_highs.columns = ['date', 'orb5_high']
    df = df.merge(orb5_highs, on='date', how='left')

    # 4. 计算RVOL (相对成交量)
    # 简化版: 当前bar成交量 / 过去20天同时段平均成交量
    df['time_of_day'] = df['hour'] * 60 + df['minute']
    df['rvol_1m'] = df.groupby('time_of_day')['volume'].transform(
        lambda x: x / x.rolling(20, min_periods=5).mean()
    )

    # 5分钟RVOL (简化)
    df['rvol_5m'] = df['rvol_1m'].rolling(5, min_periods=1).mean()

    # 5. 计算spread (简化为0,实际需要bid/ask数据)
    df['spread_pct'] = 0.001  # 假设0.1% spread

    # 6. 只保留市场时段的数据用于回测
    df = df[(df['hour'] >= 9) & (df['hour'] < 16)]

    return df[['ts', 'symbol', 'open', 'high', 'low', 'close', 'volume', 'vwap',
               'pm_high', 'orb5_high', 'rvol_1m', 'rvol_5m', 'spread_pct']]

# 使用
symbols = ['AAPL', 'TSLA', 'NVDA']
all_features = []
for symbol in symbols:
    features = calculate_features_from_bars(symbol, '2025-07-01', '2025-09-30')
    all_features.append(features)

combined_df = pd.concat(all_features, ignore_index=True)
# 然后传给BacktestEngine...
```

---

## 🔧 常见问题

### Q1: 策略需要IBKR Scanner,回测时如何模拟?

**A**: 有几种方案:

1. **Mock Scanner** (推荐):
   - 在回测中,根据历史数据计算每日09:27的Top3
   - 按照策略的scoring逻辑 (change_pct + pm_notional + catalyst) 排序
   - 示例:
   ```python
   def mock_scanner_top3(date: str, symbols: list) -> list:
       # 查询当日盘前涨幅最大的3只股票
       results = ch.query(f"""
           WITH premarket_data AS (
               SELECT
                   symbol,
                   max(close) as pm_close,
                   first_value(close) OVER (PARTITION BY symbol ORDER BY ts_start) as pm_open
               FROM bars_1m_ibkr
               WHERE toDate(ts_start) = '{date}'
                 AND toHour(ts_start) >= 4
                 AND toHour(ts_start) < 9
                 AND symbol IN {tuple(symbols)}
               GROUP BY symbol
           )
           SELECT
               symbol,
               (pm_close - pm_open) / pm_open * 100 as change_pct
           FROM premarket_data
           ORDER BY change_pct DESC
           LIMIT 3
       """)
       return [r['symbol'] for r in results]
   ```

2. **简化版**: 固定候选池
   - 回测时只测试固定的几只股票 (如AAPL, NVDA, TSLA)
   - 跳过Scanner步骤,直接进入执行逻辑

### Q2: 如何处理盘前数据缺失?

**A**: 如果某些symbol盘前数据缺失:
- 运行 `tool/verify_backtest_data.py` 检测
- 使用 `--no-rth` 重新下载: `venv/bin/python tool/load_hist_multi.py --symbols XXX --sessions 30 --bar-size "1 min" --no-rth --fill-mode backfill`

### Q3: 回测速度慢怎么办?

**A**: 优化策略:
1. **减少回测时间窗口**: 只回测关键月份
2. **减少symbols**: 先测试2-3只股票
3. **使用Parquet缓存**: 将features导出为parquet,避免重复查询ClickHouse
4. **并行回测**: 按symbol并行运行多个回测实例

### Q4: 如何验证回测结果的准确性?

**A**: 对比检查点:
1. **手动复盘**: 选1-2个交易日,手动计算PM_H, VWAP等指标,对比回测结果
2. **数据完整性**: 确保没有look-ahead bias (未来数据泄漏)
3. **成交价格合理性**: 检查fill价格是否在bar的OHLC范围内
4. **仓位管理**: 验证OCA bracket逻辑 (TP/SL触发)

---

## 📊 下一步

1. ✅ 等待数据下载完成
2. ✅ 运行 `tool/verify_backtest_data.py` 验证数据质量
3. ⏭️ 决定回测方案 (Option 1 或 2)
4. ⏭️ 编写回测脚本
5. ⏭️ 运行回测并分析结果
6. ⏭️ 优化策略参数 (如果需要)

---

**Created**: 2025-10-17
**Strategy**: premarket_gainer_v1
**Data Table**: bars_1m_ibkr
**Time Range**: 2025-04-29 ~ 2025-10-16 (6 months)
