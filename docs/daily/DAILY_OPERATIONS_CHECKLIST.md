# 交易系统每日运维清单

> 每日必做任务清单，每个任务配有具体命令
>
> **更新日期**: 2025-10-27
> **工作目录**: `.`

---

## 📅 每日时间表总览

| 时段 | 美东时间 | 北京时间 | 主要任务 |
|------|---------|---------|---------|
| **盘前准备** | 07:00-09:30 | 19:00-21:30 | 系统检查、数据验证、LLM 盘前分析 |
| **开盘前** | 09:00-09:30 | 21:00-21:30 | 最后检查、启动 LLM 盘前策略 |
| **盘中** | 09:30-16:00 | 21:30-04:00+1 | LLM 盘中交易、实时监控 |
| **盘后** | 16:00-20:00 | 04:00-08:00+1 | LLM 复盘、ML 参数优化、策略更新 |
| **隔夜** | 20:00-07:00+1 | 08:00-19:00+1 | LLM 隔夜交易、Alpha Loop 运行 |

---

## ☀️ 盘前准备 (07:00-09:30 ET)

### 0️⃣ 获取市场新闻（可选，5分钟）

```bash
# 抓取 Google News 作为盘前参考
cd .
python llm_trader/scripts/googleNews.py

# 新闻会保存到文件，可用于 LLM 分析
```

### 1️⃣ 系统健康检查 (最重要！)

#### 1.1 检查基础设施容器状态
```bash
# 进入 infra 目录
cd ./infra

# 检查所有容器健康状态
docker-compose ps

# 预期输出：所有服务都应该是 Up (healthy)
# - trading_clickhouse: Up (healthy)
# - trading_kafka: Up (healthy)
# - trading_redis: Up (healthy)
# - trading_schema_registry: Up (healthy)
# - trading_prometheus: Up (healthy)
# - trading_grafana: Up (healthy)
```

**如果有容器异常**:
```bash
# 查看具体容器日志
docker logs trading_kafka --tail 100

# 重启单个容器
docker-compose restart kafka

# 如果需要重启所有服务（数据不会丢失）
cd ./infra
docker-compose down
docker-compose up -d
```

#### 1.2 检查 run_system.py 运行状态
```bash
# 检查进程是否运行
ps aux | grep run_system.py

# 检查最近日志（查看是否有异常）
tail -100 logs/trading_system.log | grep -E "(ERROR|WARNING|CRITICAL)"

# 检查 IBKR 连接状态
tail -100 logs/trading_system.log | grep -i "ibkr\|connection\|disconnect"
```

**如果 run_system.py 未运行或异常**:
```bash
# 停止旧进程（如果有）
pkill -f run_system.py

# 启动新进程
cd .
nohup python run_system.py > logs/run_system.log 2>&1 &

# 验证启动成功
sleep 5
tail -50 logs/run_system.log
```

#### 1.3 检查 IBKR 连接
```bash
# 使用专用脚本检查 IBKR 状态
cd .
python temp/check_ibkr_status.py
```

**预期输出**:
```
✅ IBKR Connected: True
✅ Account: DU123456
✅ Buying Power: $xxxxx
✅ Positions: [...]
```

**如果 IBKR 断开**:
```bash
# 1. 检查 TWS/Gateway 是否运行
# 2. 检查网络连接
# 3. 重启 run_system.py（会自动重连）
pkill -f run_system.py
nohup python run_system.py > logs/run_system.log 2>&1 &
```

---

### 2️⃣ 数据质量检查（最重要！）

#### 2.1 检查实时 Tick 数据
```bash
cd .

# 检查最近 1 小时的 tick 数据
clickhouse-client --query "
SELECT
    symbol,
    count() as tick_count,
    min(ts_recv) as first_tick,
    max(ts_recv) as last_tick,
    formatDateTime(max(ts_recv), '%Y-%m-%d %H:%M:%S', 'America/New_York') as last_tick_et
FROM lake.ticks
WHERE ts_recv >= now() - INTERVAL 1 HOUR
GROUP BY symbol
ORDER BY symbol
FORMAT Pretty
"
```

**预期输出**: 应该看到所有关注的股票都有最近的 tick 数据

**如果没有 tick 数据**:
```bash
# 1. 检查 Kafka topic
docker exec trading_kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 \
  --topic md.tick.quote.v1 \
  --max-messages 5

# 2. 检查 market_data_service 是否运行
tail -50 logs/trading_system.log | grep -i "market_data"

# 3. 重启 run_system.py
pkill -f run_system.py
nohup python run_system.py > logs/run_system.log 2>&1 &
```

#### 2.2 检查前一天 Bar 数据完整性
```bash
# 检查前一天的 1 分钟 Bar 数据
clickhouse-client --query "
SELECT
    symbol,
    toDate(ts_end) as date,
    count() as bar_count,
    min(ts_end) as first_bar,
    max(ts_end) as last_bar
FROM trading.bars_1m
WHERE toDate(ts_end) = today() - 1
GROUP BY symbol, date
ORDER BY symbol
FORMAT Pretty
"
```

**预期**: 每个股票应该有 390 个 bar（正常交易时间 9:30-16:00）

**如果数据缺失**:
```bash
# 检查 backfill 任务状态
clickhouse-client --query "
SELECT
    symbol,
    bar_size,
    status,
    error_msg,
    created_at
FROM trading.backfill_tasks
WHERE created_at >= today() - 1
ORDER BY created_at DESC
LIMIT 20
FORMAT Pretty
"

# 手动触发回补（稍后会详细说明）
```

#### 2.3 检查特征计算正确性
```bash
# 检查 SFS 1s 数据（最近 5 分钟）
redis-cli LRANGE "sfs:1s:AAPL" 0 10 | python -m json.tool

# 检查 trading.sfs_features 表
clickhouse-client --query "
SELECT
    symbol,
    count() as feature_count,
    max(ts) as last_update,
    formatDateTime(max(ts), '%Y-%m-%d %H:%M:%S', 'America/New_York') as last_update_et
FROM trading.sfs_features
WHERE ts >= now() - INTERVAL 1 HOUR
GROUP BY symbol
ORDER BY symbol
FORMAT Pretty
"
```

#### 2.4 数据质量综合检查
```bash
# 创建数据质量检查脚本（一键检查）
cat > ./docs/daily/check_data_quality.sh << 'EOF'
#!/bin/bash
set -e

echo "========================================="
echo "数据质量每日检查 - $(date)"
echo "========================================="
echo ""

echo "1️⃣ 检查实时 Tick 数据..."
clickhouse-client --query "
SELECT symbol, count() as ticks, max(ts_recv) as last_tick
FROM lake.ticks
WHERE ts_recv >= now() - INTERVAL 10 MINUTE
GROUP BY symbol
FORMAT Pretty
" || echo "❌ Tick 数据检查失败"

echo ""
echo "2️⃣ 检查昨日 Bar 数据完整性..."
clickhouse-client --query "
SELECT symbol, count() as bars
FROM trading.bars_1m
WHERE toDate(ts_end) = today() - 1
GROUP BY symbol
HAVING bars < 390
FORMAT Pretty
" || echo "✅ 昨日 Bar 数据完整"

echo ""
echo "3️⃣ 检查 SFS Features..."
clickhouse-client --query "
SELECT symbol, count() as features, max(ts) as last_update
FROM trading.sfs_features
WHERE ts >= now() - INTERVAL 30 MINUTE
GROUP BY symbol
FORMAT Pretty
" || echo "❌ SFS Features 检查失败"

echo ""
echo "4️⃣ 检查 Backfill 任务失败记录..."
clickhouse-client --query "
SELECT symbol, bar_size, status, error_msg
FROM trading.backfill_tasks
WHERE created_at >= today() - 1 AND status = 'failed'
ORDER BY created_at DESC
LIMIT 10
FORMAT Pretty
" || echo "✅ 无 Backfill 失败记录"

echo ""
echo "========================================="
echo "数据质量检查完成"
echo "========================================="
EOF

chmod +x ./docs/daily/check_data_quality.sh

# 运行检查
./docs/daily/check_data_quality.sh
```

---

### 3️⃣ 数据回补（如有缺失）

#### 3.0 批量加载历史数据（首次设置或大量补数据）

```bash
cd .

# 单个股票加载 200 个交易日
python tool/load_hist_multi.py \
  --symbols AAPL \
  --bar-size 1m \
  --sessions 200 \
  --fill-mode backfill \
  --chunk-days 7

# 多个股票加载 100 个交易日
python tool/load_hist_multi.py \
  --symbols AAPL TSLA NVDA GOOGL MSFT META AMZN \
  --bar-size 1m \
  --sessions 100 \
  --fill-mode backfill \
  --chunk-days 7

# 说明：
# --sessions 200: 加载 200 个交易日
# --chunk-days 7: 每次请求 7 天的数据（避免 IBKR 限流）
# --fill-mode backfill: 使用 backfill 模式
```

**注意**: 此命令用于首次设置或大量补数据，不是每日必做。

#### 3.1 检查需要回补的数据
```bash
# 查询昨日缺失的 Bar 数据
clickhouse-client --query "
WITH expected AS (
    SELECT arrayJoin(['AAPL', 'TSLA', 'NVDA', 'GOOGL', 'MSFT']) as symbol
)
SELECT
    e.symbol,
    COALESCE(b.bar_count, 0) as actual_bars,
    390 - COALESCE(b.bar_count, 0) as missing_bars
FROM expected e
LEFT JOIN (
    SELECT symbol, count() as bar_count
    FROM trading.bars_1m
    WHERE toDate(ts_end) = today() - 1
    GROUP BY symbol
) b ON e.symbol = b.symbol
WHERE COALESCE(b.bar_count, 0) < 390
FORMAT Pretty
"
```

#### 3.2 手动触发 Backfill
```bash
# 方法 1: 使用 Catchup Service（自动检测并回补）
# Catchup Service 应该已经在 run_system.py 中运行，会自动检测并触发回补

# 方法 2: 手动发送 Backfill 请求到 Kafka
python << 'PYEOF'
import asyncio
from aiokafka import AIOKafkaProducer
import json
from datetime import datetime, timedelta

async def trigger_backfill(symbol, start_date, end_date):
    producer = AIOKafkaProducer(
        bootstrap_servers='localhost:9092',
        value_serializer=lambda v: json.dumps(v).encode('utf-8')
    )
    await producer.start()

    try:
        payload = {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "bar_size": "1m",
            "source": "manual_trigger"
        }

        await producer.send('backfill.request.v1', key=symbol.encode(), value=payload)
        print(f"✅ Backfill request sent for {symbol}: {start_date} to {end_date}")
    finally:
        await producer.stop()

# 示例：回补昨天的数据
yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
symbols = ['AAPL', 'TSLA', 'NVDA']

for sym in symbols:
    asyncio.run(trigger_backfill(sym, yesterday, yesterday))
PYEOF
```

#### 3.3 验证回补结果
```bash
# 等待 5 分钟后验证
sleep 300

# 再次检查数据完整性
clickhouse-client --query "
SELECT symbol, count() as bars
FROM trading.bars_1m
WHERE toDate(ts_end) = today() - 1
GROUP BY symbol
ORDER BY symbol
FORMAT Pretty
"
```

---

## 🌅 盘前交易 (07:00-09:30 ET)

### 4️⃣ LLM Trader 盘前启动

#### 4.1 盘前 LLM 分析（gemini + nvidia + deepseek）
```bash
cd ./llm_trader

# 方法 1: 使用预设脚本（推荐）
./run_premarket.sh

# 方法 2: DeepSeek NVIDIA（推荐，免费且强大）
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 33 \
  --close-at "09:35" \
  --forbid-new-positions-before 9 \
  --wait-for-premarket

# 方法 3: 多股票盘前交易
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL NVDA TSLA \
  --interval 600 \
  --cycles 33 \
  --close-at "09:35" \
  --forbid-new-positions-before 9 \
  --wait-for-premarket

# 方法 4: 多模型竞赛模式
./run_fast_providers.sh premarket

# 说明：
# --interval 600: 每 10 分钟分析一次（适合 LLM，不做高频）
# --cycles 33: 运行 33 个周期（约 5.5 小时）
# --close-at "09:35": 在 9:35 前平掉所有仓位
# --forbid-new-positions-before 9: 9点前不开新仓
# --wait-for-premarket: 等待盘前交易时段（04:00）自动开始
```

**推荐配置**:
- **Gemini**: 免费，适合快速分析
- **NVIDIA**: 免费，适合技术分析
- **DeepSeek**: 便宜，适合深度推理

#### 4.2 监控 LLM 运行状态
```bash
# 查看实时日志
tail -f ./llm_trader/logs/llm_trader_$(date +%Y%m%d).log

# 检查决策记录
clickhouse-client --query "
SELECT
    ts,
    symbol,
    provider,
    decision,
    reasoning
FROM llm_trader.decisions
WHERE ts >= today()
ORDER BY ts DESC
LIMIT 10
FORMAT Pretty
"
```

---

## 📈 盘中交易 (09:30-16:00 ET)

### 5️⃣ LLM Trader 盘中运行

#### 5.1 开盘前最后检查（09:00-09:30）
```bash
# 运行完整的系统健康检查
cd .
./temp/check_data_quality.sh

# 检查 IBKR 连接和账户状态
python temp/check_ibkr_status.py

# 检查持仓
python ./llm_trader/check_ibkr_positions.py
```

#### 5.2 启动盘中 LLM 交易
```bash
cd ./llm_trader

# 方法 1: 开盘第一小时快速交易（scalping）
python run_single_bot.py \
  --provider deepseek \
  --symbols TSLA \
  --interval 60 \
  --wait-for-market \
  --start-trading-at "9:40" \
  --stop-trading-at "10:30" \
  --close-at "10:45"

# 方法 2: 全天日内交易（推荐）
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL NVDA \
  --interval 600 \
  --cycles 33 \
  --close-at "15:59" \
  --wait-for-market

# 方法 3: 使用预设脚本
./run_first_hour.sh           # 第一小时快速策略
./run_all_providers.sh market # 多模型全天交易

# 方法 4: LLM 竞赛模式（所有免费模型同时运行）
python run_llm_race.py \
  --symbols AAPL TSLA NVDA \
  --interval 600 \
  --providers gemini nvidia deepseek
```

**盘中策略建议**:
- **间隔**: 10 分钟（600 秒）- 不做高频交易
- **交易类型**: Day Trading, Scalping
- **模型选择**:
  - Gemini: 快速反应市场变化
  - NVIDIA: 技术分析优势
  - DeepSeek: 深度推理，适合复杂市况

#### 5.3 盘中监控
```bash
# 实时监控 LLM 决策
watch -n 30 'clickhouse-client --query "
SELECT
    formatDateTime(ts, \"%H:%M:%S\") as time,
    symbol,
    provider,
    decision,
    confidence
FROM llm_trader.decisions
WHERE ts >= now() - INTERVAL 30 MINUTE
ORDER BY ts DESC
LIMIT 10
FORMAT Pretty
"'

# 监控账户 PnL
watch -n 60 'python ./llm_trader/check_ibkr_positions.py'
```

---

## 🌆 盘后分析 (16:00-20:00 ET)

### 6️⃣ LLM 复盘分析

#### 6.1 收集今日 LLM 决策数据
```bash
cd ./llm_trader

# 导出今日所有决策
clickhouse-client --query "
SELECT
    formatDateTime(ts, '%Y-%m-%d %H:%M:%S', 'America/New_York') as time_et,
    symbol,
    provider,
    decision,
    confidence,
    reasoning,
    executed,
    pnl
FROM llm_trader.decisions
WHERE toDate(ts) = today()
ORDER BY ts
FORMAT CSVWithNames
" > results/decisions_$(date +%Y%m%d).csv

echo "✅ 决策数据已导出到 results/decisions_$(date +%Y%m%d).csv"
```

#### 6.2 对比 K 线分析（决策 vs 实际走势）
```bash
# 创建复盘分析脚本
cat > ./llm_trader/scripts/review_decisions.py << 'PYEOF'
import pandas as pd
from clickhouse_driver import Client
import matplotlib.pyplot as plt
from datetime import datetime

client = Client('localhost')

# 1. 获取今日决策
decisions = client.query_dataframe("""
    SELECT
        ts,
        symbol,
        provider,
        decision,
        confidence,
        reasoning,
        executed,
        entry_price,
        exit_price,
        pnl
    FROM llm_trader.decisions
    WHERE toDate(ts) = today()
    ORDER BY ts
""")

# 2. 获取对应时间段的 K 线
bars = client.query_dataframe("""
    SELECT
        ts_end,
        symbol,
        open,
        high,
        low,
        close,
        volume
    FROM trading.bars_1m
    WHERE toDate(ts_end) = today()
    ORDER BY ts_end
""")

# 3. 分析每个决策的准确性
results = []
for idx, dec in decisions.iterrows():
    symbol = dec['symbol']
    decision_time = dec['ts']
    decision = dec['decision']

    # 获取决策后 10-60 分钟的价格变化
    future_bars = bars[
        (bars['symbol'] == symbol) &
        (bars['ts_end'] > decision_time) &
        (bars['ts_end'] <= decision_time + pd.Timedelta(minutes=60))
    ]

    if len(future_bars) > 0:
        entry_price = future_bars.iloc[0]['close']
        max_price = future_bars['high'].max()
        min_price = future_bars['low'].min()
        exit_price = future_bars.iloc[-1]['close']

        # 判断决策是否正确
        if decision == 'BUY':
            correct = (exit_price > entry_price)
            max_gain = (max_price - entry_price) / entry_price * 100
            actual_gain = (exit_price - entry_price) / entry_price * 100
        elif decision == 'SELL':
            correct = (exit_price < entry_price)
            max_gain = (entry_price - min_price) / entry_price * 100
            actual_gain = (entry_price - exit_price) / entry_price * 100
        else:
            correct = None
            max_gain = 0
            actual_gain = 0

        results.append({
            'time': decision_time,
            'symbol': symbol,
            'provider': dec['provider'],
            'decision': decision,
            'confidence': dec['confidence'],
            'correct': correct,
            'max_gain_pct': max_gain,
            'actual_gain_pct': actual_gain,
            'reasoning': dec['reasoning'][:100]
        })

# 4. 生成复盘报告
review_df = pd.DataFrame(results)
print("\n" + "="*80)
print(f"LLM 交易复盘报告 - {datetime.now().strftime('%Y-%m-%d')}")
print("="*80 + "\n")

print("📊 决策统计:")
print(f"总决策数: {len(review_df)}")
if len(review_df) > 0:
    print(f"正确决策: {review_df['correct'].sum()} ({review_df['correct'].mean()*100:.1f}%)")
    print(f"错误决策: {(~review_df['correct']).sum()}")
    print(f"平均潜在收益: {review_df['max_gain_pct'].mean():.2f}%")
    print(f"平均实际收益: {review_df['actual_gain_pct'].mean():.2f}%")

print("\n📈 各模型表现:")
provider_stats = review_df.groupby('provider').agg({
    'correct': ['count', 'sum', 'mean'],
    'actual_gain_pct': 'mean'
}).round(2)
print(provider_stats)

print("\n🎯 最佳决策 (Top 5):")
top5 = review_df.nlargest(5, 'actual_gain_pct')[['time', 'symbol', 'provider', 'decision', 'actual_gain_pct', 'reasoning']]
print(top5.to_string(index=False))

print("\n❌ 最差决策 (Bottom 5):")
bottom5 = review_df.nsmallest(5, 'actual_gain_pct')[['time', 'symbol', 'provider', 'decision', 'actual_gain_pct', 'reasoning']]
print(bottom5.to_string(index=False))

# 5. 保存详细报告
report_path = f'results/review_{datetime.now().strftime("%Y%m%d")}.csv'
review_df.to_csv(report_path, index=False)
print(f"\n✅ 详细复盘报告已保存: {report_path}")

# 6. 生成建议
print("\n💡 Prompt 优化建议:")
if review_df['correct'].mean() < 0.5:
    print("⚠️ 决策准确率低于 50%，建议：")
    print("  - 增加风险评估权重")
    print("  - 添加市场趋势判断")
    print("  - 优化止损逻辑")
else:
    print("✅ 决策准确率良好，继续优化：")
    print("  - 提高置信度阈值")
    print("  - 优化仓位管理")

PYEOF

# 运行复盘分析
python ./llm_trader/scripts/review_decisions.py
```

#### 6.3 Prompt 优化
```bash
# 基于复盘结果，编辑 prompt 配置
# 假设 prompt 在 llm_trader/config/prompts.yaml

# 查看当前 prompt
cat ./llm_trader/config/prompts.yaml

# 根据复盘建议编辑 prompt
# 例如：增加风险评估、市场趋势判断等
vim ./llm_trader/config/prompts.yaml

# 或者使用版本管理记录变更
git diff llm_trader/config/prompts.yaml
git commit -m "优化 prompt: 基于 $(date +%Y%m%d) 复盘结果"
```

---

### 7️⃣ 机器学习参数优化

#### 7.1 盘后数据准备
```bash
cd .

# 导出今日训练数据
clickhouse-client --query "
SELECT
    symbol,
    ts_end,
    open, high, low, close, volume,
    -- SFS features
    vwap, rsi, macd, signal, histogram,
    -- 其他特征...
FROM trading.sfs_features
WHERE toDate(ts_end) = today()
ORDER BY symbol, ts_end
FORMAT CSVWithNames
" > data/training_$(date +%Y%m%d).csv

echo "✅ 训练数据已导出"
```

#### 7.2 运行 Alpha Loop 优化
```bash
cd .

# 方法 1: 快速优化（XGBoost）
python alpha_loop.py \
  --model xgboost \
  --symbols AAPL TSLA NVDA \
  --lookback-days 30 \
  --n-trials 100 \
  --output results/alpha_loop_xgb_$(date +%Y%m%d).json

# 方法 2: 深度学习优化（LSTM）
python alpha_loop.py \
  --model lstm \
  --symbols AAPL TSLA \
  --lookback-days 60 \
  --n-trials 50 \
  --epochs 100 \
  --output results/alpha_loop_lstm_$(date +%Y%m%d).json

# 方法 3: 快速测试（适合每日更新）
python alpha_loop.py \
  --model xgboost \
  --symbols AAPL \
  --lookback-days 7 \
  --n-trials 20 \
  --output results/alpha_loop_quick_$(date +%Y%m%d).json
```

**说明**:
- `--lookback-days`: 使用最近 N 天的数据训练
- `--n-trials`: Optuna 优化尝试次数（越多越好，但耗时更长）
- `--model`: 选择模型类型（xgboost, lstm, lightgbm）

#### 7.3 分析优化结果
```bash
# 查看优化结果
python << 'PYEOF'
import json
import pandas as pd
from datetime import datetime

result_file = f'results/alpha_loop_xgb_{datetime.now().strftime("%Y%m%d")}.json'

with open(result_file) as f:
    results = json.load(f)

print("\n" + "="*80)
print(f"Alpha Loop 优化结果 - {datetime.now().strftime('%Y-%m-%d')}")
print("="*80 + "\n")

for symbol, data in results.items():
    print(f"\n📊 {symbol}:")
    print(f"  最佳参数: {data['best_params']}")
    print(f"  Sharpe Ratio: {data['sharpe_ratio']:.3f}")
    print(f"  总收益率: {data['total_return']:.2%}")
    print(f"  最大回撤: {data['max_drawdown']:.2%}")
    print(f"  胜率: {data['win_rate']:.2%}")

PYEOF
```

#### 7.4 更新策略参数
```bash
# 应用新参数到策略配置
python apply_hpo_params.py \
  --results results/alpha_loop_xgb_$(date +%Y%m%d).json \
  --config config.yml \
  --backup

# 验证参数更新
git diff config.yml

# 提交变更
git add config.yml
git commit -m "更新策略参数: Alpha Loop $(date +%Y%m%d)"

# 热重载参数（无需重启系统）
python hot_reload_test_params.py
```

---

### 8️⃣ 策略回测

#### 8.1 回测今日参数
```bash
cd .

# 使用新参数回测最近 30 天
python backtest_ml_engine.py \
  --config config.yml \
  --start-date $(date -v-30d +%Y-%m-%d) \
  --end-date $(date +%Y-%m-%d) \
  --symbols AAPL TSLA NVDA \
  --output results/backtest_$(date +%Y%m%d).json

# 生成回测报告
python plot_backtest.py \
  --input results/backtest_$(date +%Y%m%d).json \
  --output results/backtest_$(date +%Y%m%d).html

echo "✅ 回测报告: results/backtest_$(date +%Y%m%d).html"
```

#### 8.2 对比参数性能
```bash
# 对比今日参数 vs 昨日参数
python compare_hpo_params.py \
  --old results/alpha_loop_xgb_$(date -v-1d +%Y%m%d).json \
  --new results/alpha_loop_xgb_$(date +%Y%m%d).json \
  --backtest-days 30

# 输出会显示哪些参数改进了，哪些退化了
```

---

## 🌙 盘后/隔夜交易 (16:00-次日07:00)

### 9️⃣ LLM Trader 盘后/隔夜运行

#### 9.1 启动盘后 LLM 分析
```bash
cd ./llm_trader

# 盘后时段（16:00-20:00）
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --wait-for-afterhours

# 隔夜时段（20:00-次日04:00）
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 50 \
  --wait-for-overnight \
  --close-at "03:50" \
  --forbid-new-positions-before 5

# 后台运行（推荐）
nohup python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 50 \
  --wait-for-overnight \
  --close-at "03:50" \
  --forbid-new-positions-before 5 \
  > logs/overnight_$(date +%Y%m%d).log 2>&1 &

# 说明：
# --wait-for-afterhours: 等待盘后时段（16:00）
# --wait-for-overnight: 等待隔夜时段（20:00）
# --close-at "03:50": 在 3:50 前平仓
# --forbid-new-positions-before 5: 5点前不开新仓
```

---

## 🔄 每日总结任务

### 🔟 每日数据质量报告

```bash
# 创建每日总结脚本
cat > ./docs/daily/daily_summary.sh << 'EOF'
#!/bin/bash

echo "========================================="
echo "交易系统每日总结 - $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================="
echo ""

echo "📊 系统运行状态:"
echo "---"
docker-compose -f ./infra/compose.yml ps | grep -E "trading_(kafka|clickhouse|redis)" | awk '{print $1, $NF}'
echo ""

echo "📈 今日数据统计:"
echo "---"
clickhouse-client --query "
SELECT
    '今日 Tick 数据' as metric,
    formatReadableQuantity(count()) as value
FROM lake.ticks
WHERE toDate(ts_recv) = today()
UNION ALL
SELECT
    '今日 Bar 数据' as metric,
    formatReadableQuantity(count()) as value
FROM trading.bars_1m
WHERE toDate(ts_end) = today()
UNION ALL
SELECT
    '今日 LLM 决策' as metric,
    formatReadableQuantity(count()) as value
FROM llm_trader.decisions
WHERE toDate(ts) = today()
FORMAT Pretty
"

echo ""
echo "💰 今日交易表现:"
echo "---"
clickhouse-client --query "
SELECT
    symbol,
    count() as trades,
    sum(pnl) as total_pnl,
    avg(pnl) as avg_pnl
FROM llm_trader.decisions
WHERE toDate(ts) = today() AND executed = 1
GROUP BY symbol
ORDER BY total_pnl DESC
FORMAT Pretty
"

echo ""
echo "⚠️ 今日告警/错误:"
echo "---"
grep -E "(ERROR|CRITICAL)" ./logs/trading_system.log | tail -10 || echo "✅ 无严重错误"

echo ""
echo "========================================="
echo "总结完成"
echo "========================================="
EOF

chmod +x ./docs/daily/daily_summary.sh

# 运行总结
./docs/daily/daily_summary.sh
```

---

## 📋 每日检查清单（Checklist）

### 盘前 (07:00-09:30)
- [ ] 检查基础设施容器状态 (`docker-compose ps`)
- [ ] 检查 run_system.py 运行状态 (`ps aux | grep run_system`)
- [ ] 检查 IBKR 连接 (`python temp/check_ibkr_status.py`)
- [ ] 检查实时 Tick 数据
- [ ] 检查前一天 Bar 数据完整性
- [ ] 检查 SFS Features 计算正确性
- [ ] 运行数据质量综合检查 (`./docs/daily/check_data_quality.sh`)
- [ ] 如有缺失数据，触发 Backfill
- [ ] 启动盘前 LLM Trader (`./llm_trader/run_premarket.sh`)

### 盘中 (09:30-16:00)
- [ ] 开盘前最后检查（09:00-09:30）
- [ ] 启动盘中 LLM Trader
- [ ] 实时监控 LLM 决策和账户 PnL
- [ ] 监控系统异常和 IBKR 连接状态

### 盘后 (16:00-20:00)
- [ ] 导出今日 LLM 决策数据
- [ ] 运行复盘分析（决策 vs K 线）
- [ ] 根据复盘结果优化 Prompt
- [ ] 导出今日训练数据
- [ ] 运行 Alpha Loop 优化（XGBoost/LSTM）
- [ ] 分析优化结果
- [ ] 更新策略参数
- [ ] 回测新参数
- [ ] 对比参数性能
- [ ] 启动盘后 LLM Trader

### 隔夜 (20:00-次日07:00)
- [ ] 启动隔夜 LLM Trader
- [ ] 设置监控告警

### 每日结束
- [ ] 运行每日总结报告 (`./docs/daily/daily_summary.sh`)
- [ ] 检查日志中的错误和告警
- [ ] 备份重要数据和配置
- [ ] 提交代码变更（如有）

---

## 🚨 常见问题处理

### Q1: IBKR 断开连接怎么办？
```bash
# 1. 检查 TWS/Gateway 是否运行
# 2. 查看日志
tail -100 logs/trading_system.log | grep -i ibkr

# 3. run_system.py 会自动重连，等待 5-300 秒
# 4. 如果长时间未恢复，手动重启
pkill -f run_system.py
nohup python run_system.py > logs/run_system.log 2>&1 &
```

### Q2: Kafka/ClickHouse/Redis 异常？
```bash
cd ./infra

# 查看容器日志
docker logs trading_kafka --tail 100
docker logs trading_clickhouse --tail 100
docker logs trading_redis --tail 100

# 重启单个服务
docker-compose restart kafka

# 重启所有服务（数据不会丢失）
docker-compose down
docker-compose up -d
```

### Q3: 数据缺失怎么办？
```bash
# 1. 检查 Backfill 任务状态
clickhouse-client --query "
SELECT * FROM trading.backfill_tasks
WHERE created_at >= today() - 1
ORDER BY created_at DESC
LIMIT 20
FORMAT Pretty
"

# 2. 手动触发 Backfill（见 3.2 节）

# 3. 验证 Catchup Service 是否运行
tail -50 logs/trading_system.log | grep -i catchup
```

### Q4: LLM Trader 报错？
```bash
# 查看详细日志
tail -100 ./llm_trader/logs/llm_trader_$(date +%Y%m%d).log

# 检查 API Key 配置
env | grep -E "(GEMINI|NVIDIA|DEEPSEEK)_API_KEY"

# 检查账户余额和权限
python ./llm_trader/check_ibkr_positions.py
```

### Q5: Alpha Loop 优化失败？
```bash
# 检查数据是否充足
clickhouse-client --query "
SELECT
    symbol,
    count() as bars,
    min(toDate(ts_end)) as first_date,
    max(toDate(ts_end)) as last_date
FROM trading.sfs_features
GROUP BY symbol
FORMAT Pretty
"

# 降低试验次数，快速测试
python alpha_loop.py --n-trials 10 --lookback-days 7

# 查看详细错误日志
tail -100 logs/alpha_loop.log
```

---

## 💾 备份与恢复

### ClickHouse 完整备份

```bash
cd .

# 使用 Makefile 一键备份所有数据库
make recon-all

# 备份会保存到 backup/ 目录
# 命名格式: full_backup_YYYYMMDD_HHMMSS
# 包含: trading, lake, sfs 三个数据库
```

**备份频率建议**:
- **每日自动备份**: 已配置 cron 每日 04:01 AM 自动运行
- **手动备份**: 重大变更前、系统升级前

### PostgreSQL 备份

```bash
# PostgreSQL 每日自动备份（16:01 运行）
# 手动触发备份
cd .
pg_dump -h localhost -U postgres -d test > backup/pg/manual_$(date +%Y%m%d_%H%M%S).sql
```

### 备份验证

```bash
# 检查最近的备份
ls -lht backup/clickhouse/ | head -5
ls -lht backup/pg/ | head -5

# 验证备份文件大小（应该 > 100MB）
du -sh backup/clickhouse/full_backup_*
```

---

## 📊 监控指标

### 关键健康指标

| 指标 | 命令 | 健康阈值 |
|------|------|---------|
| **IBKR 连接** | `python temp/check_ibkr_status.py` | Connected = True |
| **Tick 延迟** | `redis-cli GET "last_tick:AAPL"` | < 5 秒 |
| **Bar 完整性** | 见 2.2 节 | 390 bars/天 |
| **Kafka Lag** | `docker exec trading_kafka kafka-consumer-groups.sh --describe --all-groups` | < 1000 |
| **ClickHouse 查询** | `clickhouse-client --query "SELECT 1"` | < 100ms |
| **Redis 内存** | `redis-cli INFO memory | grep used_memory_human` | < 80% |

---

## 🔗 相关文档

- `./temp/ibkr_connection_resilience_analysis.md` - IBKR 连接容错分析
- `./llm_trader/HOW_TO_USE.md` - LLM Trader 使用指南
- `./llm_trader/LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md` - LLM Trader 完整文档
- `./CLAUDE.md` - Claude 代码修复规则

---

**最后更新**: 2025-10-27
**维护者**: Trading Team
