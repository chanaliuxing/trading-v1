# 特征工程改进计划
## 从16特征到50+专业特征

**制定日期**: 2025-11-05
**目标**: 将模型从"中级"(56分)提升到"专业级"(80+分)
**核心瓶颈**: 特征数量和质量（当前30/100分）

---

## 执行摘要

### 当前状态 ❌
- **特征数量**: 16个基础特征
- **特征覆盖率**: 43.5%（10/23个必需特征）
- **模型效果**: IC = 0.0301（专业级: 0.05+）
- **评分**: 30/100

### 目标状态 ✅
- **特征数量**: 50+个专业特征
- **特征覆盖率**: >90%
- **预期IC**: 0.05+（提升66%）
- **预期评分**: 70/100+

### 时间投入
- **Phase 1**: 1-2周（+30特征，最重要）
- **Phase 2**: 1周（+20特征，可选）
- **总计**: 2-3周全职工作

### 预期收益
- **模型质量**: 40 → 60 (+20分)
- **特征工程**: 30 → 70 (+40分)
- **总分提升**: 56 → 86 (+30分)

---

## Phase 1: 核心特征扩展（+30特征）
### 预计时间：1-2周，预期提升：+25分

这是**最关键的一步**，专注于经过验证的高价值特征。

### 1.1 多时间框架特征（+12特征）
**重要性**: ⭐⭐⭐⭐⭐
**难度**: 低
**时间**: 2-3天

#### 特征列表

| 特征名 | 计算公式 | 业务含义 |
|--------|----------|----------|
| `ret_5m` | `(close - close_5m_ago) / close_5m_ago` | 5分钟动量 |
| `ret_15m` | `(close - close_15m_ago) / close_15m_ago` | 15分钟动量 |
| `ret_1h` | `(close - close_60m_ago) / close_60m_ago` | 1小时动量 |
| `rsi_14_5m` | `RSI(5m avg)` | 5分钟RSI |
| `rsi_14_15m` | `RSI(15m avg)` | 15分钟RSI |
| `vol_5m_avg` | `AVG(volume, 5)` | 5分钟平均成交量 |
| `vol_15m_avg` | `AVG(volume, 15)` | 15分钟平均成交量 |
| `vol_1h_avg` | `AVG(volume, 60)` | 1小时平均成交量 |
| `atr_14_5m` | `ATR(5m)` | 5分钟波动率 |
| `atr_14_15m` | `ATR(15m)` | 15分钟波动率 |
| `ema_cross_5m` | `EMA9_5m > EMA21_5m` | 5分钟均线交叉 |
| `ema_cross_15m` | `EMA9_15m > EMA21_15m` | 15分钟均线交叉 |

#### ClickHouse SQL实现

```sql
-- 创建新列（ALTER TABLE）
ALTER TABLE trading.features_1m
  ADD COLUMN IF NOT EXISTS ret_5m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS ret_15m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS ret_1h Nullable(Float64),
  ADD COLUMN IF NOT EXISTS rsi_14_5m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS rsi_14_15m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS vol_5m_avg Nullable(Float64),
  ADD COLUMN IF NOT EXISTS vol_15m_avg Nullable(Float64),
  ADD COLUMN IF NOT EXISTS vol_1h_avg Nullable(Float64),
  ADD COLUMN IF NOT EXISTS atr_14_5m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS atr_14_15m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS ema_cross_5m Nullable(Int8),
  ADD COLUMN IF NOT EXISTS ema_cross_15m Nullable(Int8);

-- 示例：计算ret_5m的SQL
SELECT
  symbol,
  ts,
  (close - lagInFrame(close, 5) OVER (PARTITION BY symbol ORDER BY ts)) /
    nullIf(lagInFrame(close, 5) OVER (PARTITION BY symbol ORDER BY ts), 0) AS ret_5m
FROM trading.bars_1m_ibkr FINAL
WHERE symbol = 'TSLA'
ORDER BY ts;
```

---

### 1.2 相对强度特征（+8特征）
**重要性**: ⭐⭐⭐⭐⭐
**难度**: 中
**时间**: 3-4天（需要SPY数据）

#### 特征列表

| 特征名 | 计算公式 | 业务含义 |
|--------|----------|----------|
| `ret_vs_spy_1m` | `ret_1m - spy_ret_1m` | 相对SPY的超额收益 |
| `ret_vs_spy_5m` | `ret_5m - spy_ret_5m` | 5分钟超额收益 |
| `beta_spy_20` | `Cov(ret, spy_ret) / Var(spy_ret)` | 对SPY的敏感度 |
| `corr_spy_20` | `CORR(ret, spy_ret, 20)` | 与SPY的相关性 |
| `ret_vs_sector` | `ret_1m - sector_avg_ret` | 相对行业的超额收益 |
| `volume_rank_20` | `RANK(volume) / 20` | 成交量分位数 |
| `price_rank_20` | `RANK(close) / 20` | 价格分位数 |
| `rank_vs_spy` | `(ret > spy_ret ? 1 : 0).rolling(20)` | 跑赢SPY的频率 |

#### 前置条件

**必须先加载SPY数据**：
```sql
-- 检查SPY数据
SELECT count(*)
FROM trading.bars_1m_ibkr FINAL
WHERE symbol = 'SPY'
  AND ts >= now() - INTERVAL 30 DAY;
```

#### ClickHouse SQL实现（示例）

```sql
-- 创建新列
ALTER TABLE trading.features_1m
  ADD COLUMN IF NOT EXISTS ret_vs_spy_1m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS ret_vs_spy_5m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS beta_spy_20 Nullable(Float64),
  ADD COLUMN IF NOT EXISTS corr_spy_20 Nullable(Float64),
  ADD COLUMN IF NOT EXISTS ret_vs_sector Nullable(Float64),
  ADD COLUMN IF NOT EXISTS volume_rank_20 Nullable(Float64),
  ADD COLUMN IF NOT EXISTS price_rank_20 Nullable(Float64),
  ADD COLUMN IF NOT EXISTS rank_vs_spy Nullable(Float64);

-- 计算ret_vs_spy_1m（需要JOIN SPY数据）
WITH stock_ret AS (
  SELECT
    symbol,
    ts,
    (close - lagInFrame(close, 1) OVER (PARTITION BY symbol ORDER BY ts)) /
      nullIf(lagInFrame(close, 1) OVER (PARTITION BY symbol ORDER BY ts), 0) AS ret_1m
  FROM trading.bars_1m_ibkr FINAL
  WHERE symbol IN ('TSLA', 'AAPL', 'NVDA')
),
spy_ret AS (
  SELECT
    ts,
    (close - lagInFrame(close, 1) OVER (ORDER BY ts)) /
      nullIf(lagInFrame(close, 1) OVER (ORDER BY ts), 0) AS spy_ret_1m
  FROM trading.bars_1m_ibkr FINAL
  WHERE symbol = 'SPY'
)
SELECT
  s.symbol,
  s.ts,
  s.ret_1m - sp.spy_ret_1m AS ret_vs_spy_1m
FROM stock_ret s
LEFT JOIN spy_ret sp ON s.ts = sp.ts;
```

---

### 1.3 微观结构特征（+6特征）
**重要性**: ⭐⭐⭐⭐
**难度**: 低
**时间**: 2天

#### 特征列表

| 特征名 | 计算公式 | 业务含义 |
|--------|----------|----------|
| `vwap_dist` | `(close - vwap) / atr_14` | 价格偏离VWAP |
| `minute_volume_pct` | `volume / sum(volume_today)` | 当前分钟占全天成交量比例 |
| `trades_per_minute` | `trades` | 每分钟交易次数（如有） |
| `avg_trade_size` | `volume / trades` | 平均每笔成交量 |
| `volume_surge` | `volume / vol_ema_20` | 成交量激增检测 |
| `high_low_range` | `(high - low) / close` | 价格波动幅度 |

#### ClickHouse SQL实现

```sql
-- 创建新列
ALTER TABLE trading.features_1m
  ADD COLUMN IF NOT EXISTS vwap_dist Nullable(Float64),
  ADD COLUMN IF NOT EXISTS minute_volume_pct Nullable(Float64),
  ADD COLUMN IF NOT EXISTS trades_per_minute Nullable(UInt64),
  ADD COLUMN IF NOT EXISTS avg_trade_size Nullable(Float64),
  ADD COLUMN IF NOT EXISTS volume_surge Nullable(Float64),
  ADD COLUMN IF NOT EXISTS high_low_range Nullable(Float64);

-- 计算vwap_dist
SELECT
  symbol,
  ts,
  (close - vwap) / nullIf(atr_14, 0) AS vwap_dist
FROM trading.features_1m FINAL;

-- 计算minute_volume_pct（需要当日累计）
SELECT
  symbol,
  ts,
  volume / nullIf(sum(volume) OVER (
    PARTITION BY symbol, toDate(ts)
    ORDER BY ts
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  ), 0) AS minute_volume_pct
FROM trading.bars_1m_ibkr FINAL;
```

---

### 1.4 时间特征（+4特征）
**重要性**: ⭐⭐⭐⭐
**难度**: 极低
**时间**: 0.5天

#### 特征列表

| 特征名 | 计算公式 | 业务含义 |
|--------|----------|----------|
| `minute_of_day` | `toHour(ts) * 60 + toMinute(ts) - 570` | 盘中第几分钟（0-390） |
| `hour_of_day` | `toHour(ts)` | 小时（9-16） |
| `day_of_week` | `toDayOfWeek(ts) - 1` | 星期几（0=周一） |
| `session_phase` | `CASE WHEN minute < 60 THEN 0 WHEN minute > 330 THEN 2 ELSE 1` | 开盘/中盘/尾盘 |

#### ClickHouse SQL实现

```sql
-- 创建新列
ALTER TABLE trading.features_1m
  ADD COLUMN IF NOT EXISTS minute_of_day Nullable(Int16),
  ADD COLUMN IF NOT EXISTS hour_of_day Nullable(Int8),
  ADD COLUMN IF NOT EXISTS day_of_week Nullable(Int8),
  ADD COLUMN IF NOT EXISTS session_phase Nullable(Int8);

-- 计算时间特征（非常快）
SELECT
  symbol,
  ts,
  toHour(ts) * 60 + toMinute(ts) - 570 AS minute_of_day,
  toHour(ts) AS hour_of_day,
  toDayOfWeek(ts) - 1 AS day_of_week,
  CASE
    WHEN minute_of_day < 60 THEN 0  -- 开盘
    WHEN minute_of_day > 330 THEN 2  -- 尾盘
    ELSE 1  -- 中盘
  END AS session_phase
FROM trading.bars_1m_ibkr FINAL;
```

---

## Phase 2: 高级特征扩展（+20特征）
### 预计时间：1周，预期提升：+10分

这些是**锦上添花**的特征，可在Phase 1验证后再实施。

### 2.1 高级技术指标（+8特征）

| 特征名 | 说明 |
|--------|------|
| `bb_position` | 布林带位置 |
| `bb_width` | 布林带宽度 |
| `macd_hist` | MACD柱状图 |
| `stochastic_k` | 随机指标K |
| `stochastic_d` | 随机指标D |
| `adx` | 趋势强度ADX |
| `cci_20` | 顺势指标CCI |
| `willr_14` | 威廉指标 |

### 2.2 价格形态特征（+6特征）

| 特征名 | 说明 |
|--------|------|
| `doji` | 十字星形态 |
| `hammer` | 锤子形态 |
| `engulfing` | 吞噬形态 |
| `gap_up` | 向上跳空 |
| `gap_down` | 向下跳空 |
| `inside_bar` | 内包线 |

### 2.3 订单流特征（+6特征，需Level 2数据）

| 特征名 | 说明 |
|--------|------|
| `bid_ask_spread` | 买卖价差 |
| `bid_size` | 买单深度 |
| `ask_size` | 卖单深度 |
| `order_book_imbalance` | 订单簿失衡 |
| `trade_direction` | 交易方向 |
| `aggressive_buy_pct` | 主动买单占比 |

---

## 实施路线图

### Week 1: 基础设施准备
**目标**: 建立自动化特征计算流程

#### Day 1-2: Schema扩展
```bash
# 1. 备份现有schema
docker exec -it clickhouse-server clickhouse-client \
  --query "SELECT create_table_query FROM system.tables WHERE name='features_1m'" \
  > backups/schema/features_1m_schema_backup_$(date +%Y%m%d).sql

# 2. 执行Phase 1的ALTER TABLE语句
clickhouse-client < schema/features_1m_phase1_add_columns.sql

# 3. 验证schema
clickhouse-client --query "DESCRIBE trading.features_1m"
```

#### Day 3-4: SPY数据加载
```bash
# 检查SPY数据是否存在
clickhouse-client --query "
SELECT
  symbol,
  min(ts) as first_date,
  max(ts) as last_date,
  count(*) as rows
FROM trading.bars_1m_ibkr FINAL
WHERE symbol = 'SPY'
GROUP BY symbol
"

# 如果没有，从IBKR回填
python scripts/backfill_historical.py --symbol SPY --start 2024-01-01 --end 2025-11-05
```

#### Day 5: 特征计算脚本
创建 `tools/compute_features_phase1.py`:
```python
#!/usr/bin/env python3
"""
Phase 1特征计算脚本
计算30个新特征并更新features_1m表
"""

import clickhouse_connect as ch
from datetime import datetime, timedelta

def compute_multiframe_features(client, symbol, start_date, end_date):
    """计算多时间框架特征"""
    sql = """
    INSERT INTO trading.features_1m
    (ts, symbol, ret_5m, ret_15m, ret_1h, ...)
    SELECT
      ts,
      symbol,
      -- 计算ret_5m
      (close - lagInFrame(close, 5) OVER w) /
        nullIf(lagInFrame(close, 5) OVER w, 0) AS ret_5m,
      -- 计算ret_15m
      (close - lagInFrame(close, 15) OVER w) /
        nullIf(lagInFrame(close, 15) OVER w, 0) AS ret_15m,
      -- ...
    FROM trading.bars_1m_ibkr FINAL
    WHERE symbol = '{symbol}'
      AND ts >= '{start_date}'
      AND ts < '{end_date}'
    WINDOW w AS (PARTITION BY symbol ORDER BY ts)
    """.format(symbol=symbol, start_date=start_date, end_date=end_date)

    client.command(sql)
    print(f"✅ Computed multiframe features for {symbol}")

# 主逻辑...
```

---

### Week 2: 特征计算与回填
**目标**: 计算Phase 1的30个特征

#### Day 6-7: 多时间框架特征
```bash
python tools/compute_features_phase1.py \
  --features multiframe \
  --symbols TSLA,AAPL,NVDA \
  --start 2024-01-01 \
  --end 2025-11-05 \
  --workers 4
```

#### Day 8-9: 相对强度特征
```bash
python tools/compute_features_phase1.py \
  --features relative_strength \
  --symbols TSLA,AAPL,NVDA \
  --start 2024-01-01 \
  --end 2025-11-05 \
  --workers 4
```

#### Day 10-11: 微观结构 + 时间特征
```bash
python tools/compute_features_phase1.py \
  --features microstructure,time \
  --symbols TSLA,AAPL,NVDA \
  --start 2024-01-01 \
  --end 2025-11-05 \
  --workers 4
```

#### Day 12: 验证特征质量
```bash
python test_professional_grade.py --use-new-features
```

**预期结果**:
- IC: 0.0301 → 0.05+ (+66%)
- 模型质量评分: 40 → 60 (+20分)
- 特征工程评分: 30 → 70 (+40分)
- **总分: 56 → 86 (+30分)**

---

### Week 3: 优化与验证
**目标**: 特征选择和模型重训练

#### Day 13-14: 特征重要性分析
```python
from sklearn.ensemble import RandomForestRegressor

# 1. 训练基准模型
rf = RandomForestRegressor(n_estimators=100)
rf.fit(X_train, y_train)

# 2. 特征重要性
importances = pd.DataFrame({
    'feature': feature_names,
    'importance': rf.feature_importances_
}).sort_values('importance', ascending=False)

# 3. 选择Top-50
top_features = importances.head(50)['feature'].tolist()
```

#### Day 15-16: 模型重训练
```bash
# 使用新特征重新运行HPO
python alpha_loop.py \
  --config alpha_config_phase1_features.json \
  --provider coop_optuna \
  --trials 20
```

#### Day 17: 最终评估
```bash
python test_professional_grade.py --final-report
```

---

## 关键技术细节

### 数据一致性保证

**问题**: 特征计算需要历史数据，如何保证point-in-time正确性？

**解决方案**: 使用ClickHouse窗口函数 + feature_cutoff_ts

```sql
-- 正确的方式（避免未来信息泄露）
SELECT
  ts,
  symbol,
  -- 使用lagInFrame确保只用过去数据
  (close - lagInFrame(close, 5) OVER (
    PARTITION BY symbol
    ORDER BY ts
    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
  )) / nullIf(lagInFrame(close, 5) OVER (...), 0) AS ret_5m,

  -- feature_cutoff_ts确保训练时不用未来数据
  ts AS feature_cutoff_ts
FROM trading.bars_1m_ibkr FINAL;
```

### 性能优化

**问题**: 计算50个特征在9000+行数据上会很慢？

**优化策略**:

1. **增量计算**（只计算新数据）
```sql
INSERT INTO trading.features_1m
SELECT ...
FROM trading.bars_1m_ibkr FINAL
WHERE ts > (SELECT max(ts) FROM trading.features_1m WHERE symbol = 'TSLA')
```

2. **并行计算**（多个symbol并行）
```python
from concurrent.futures import ProcessPoolExecutor

symbols = ['TSLA', 'AAPL', 'NVDA', ...]
with ProcessPoolExecutor(max_workers=4) as executor:
    futures = [executor.submit(compute_features, sym) for sym in symbols]
```

3. **物化视图**（预计算常用特征）
```sql
CREATE MATERIALIZED VIEW trading.features_1m_multiframe_mv
ENGINE = ReplacingMergeTree()
ORDER BY (symbol, ts)
AS SELECT
  ts, symbol,
  -- 预计算的特征
  ret_5m, ret_15m, ...
FROM trading.bars_1m_ibkr FINAL;
```

---

## 风险与缓解

### 风险1: 特征过多导致过拟合
**概率**: 中
**影响**: 高
**缓解**:
- 使用L1/L2正则化
- 交叉验证
- 特征选择（保留Top-50）
- 增加Dropout (0.2 → 0.3)

### 风险2: 计算时间过长
**概率**: 中
**影响**: 中
**缓解**:
- 增量计算
- 并行处理
- 使用物化视图

### 风险3: SPY数据缺失
**概率**: 低
**影响**: 高
**缓解**:
- 提前验证SPY数据完整性
- 准备fallback方案（使用QQQ或不计算相对强度特征）

### 风险4: ClickHouse存储空间不足
**概率**: 低
**影响**: 中
**缓解**:
- 监控磁盘使用（`df -h /workspace/Documents/clickhouse_data`）
- 特征使用Nullable减少空间
- 设置合理的TTL

---

## 成功验收标准

### 必须达到（P0）
- [ ] features_1m表有 ≥ 46个特征（26基础 + 30新增）
- [ ] 特征覆盖率 ≥ 90%
- [ ] 所有特征无NULL值超过10%
- [ ] IC提升至 ≥ 0.045（50%提升）
- [ ] 模型质量评分 ≥ 55/100
- [ ] 特征工程评分 ≥ 65/100

### 推荐达到（P1）
- [ ] IC ≥ 0.05（66%提升）
- [ ] 总分 ≥ 80/100（专业级）
- [ ] V2完整配置不再失败（IC > 0）
- [ ] 特征重要性分析完成
- [ ] Top-50特征选择完成

### 可选达到（P2）
- [ ] Phase 2高级特征实施
- [ ] 自动化特征监控dashboard
- [ ] 实时特征计算pipeline

---

## 下一步行动

### 立即执行（今天）

1. **验证SPY数据**
```bash
clickhouse-client --query "
SELECT symbol, count(*), min(ts), max(ts)
FROM trading.bars_1m_ibkr FINAL
WHERE symbol = 'SPY' AND ts >= '2024-01-01'
GROUP BY symbol
"
```

2. **创建Schema扩展脚本**
```bash
cat > schema/features_1m_phase1_add_columns.sql << 'EOF'
-- Phase 1: Add 30 new feature columns
ALTER TABLE trading.features_1m
  ADD COLUMN IF NOT EXISTS ret_5m Nullable(Float64),
  ADD COLUMN IF NOT EXISTS ret_15m Nullable(Float64),
  -- ... (复制上面的完整列表)
EOF
```

3. **执行Schema扩展**
```bash
docker exec -it clickhouse-server clickhouse-client < schema/features_1m_phase1_add_columns.sql
```

### 本周完成（Week 1）

4. **编写特征计算脚本**（`tools/compute_features_phase1.py`）
5. **测试单个symbol**（TSLA，1天数据）
6. **验证特征正确性**（手工检查几个值）

### 下周完成（Week 2）

7. **回填历史数据**（2024-01-01 至今）
8. **验证特征质量**（NULL率，分布）
9. **重新运行专业级测试**

---

## 资源清单

### 需要的工具
- ✅ ClickHouse客户端（已有）
- ✅ Python 3.9+（已有）
- ✅ pandas, numpy, clickhouse-connect（已有）
- ⏳ 磁盘空间（预留10GB）

### 需要的数据
- ✅ TSLA, AAPL, NVDA bars_1m数据（已有）
- ⏳ SPY bars_1m数据（需验证）
- ⏳ 行业分类数据（可选，用于ret_vs_sector）

### 需要的时间
- **你的时间**: 每天2-3小时审查 + 决策
- **AI时间**: 每天6-8小时实施
- **机器时间**: 约8小时计算（一次性）

---

## 附录A: 完整SQL脚本模板

见独立文件：
- `schema/features_1m_phase1_add_columns.sql` - 添加列
- `tools/compute_features_phase1.sql` - 计算特征
- `tools/validate_features_phase1.sql` - 验证特征

---

## 附录B: 特征计算公式速查表

| 类别 | 特征数 | 关键公式 |
|------|--------|----------|
| 多时间框架 | 12 | `lagInFrame(close, N)` |
| 相对强度 | 8 | `stock_ret - spy_ret` |
| 微观结构 | 6 | `(close - vwap) / atr` |
| 时间 | 4 | `toHour(ts) * 60 + toMinute(ts)` |

---

**制定人**: Claude Code
**审核**: 待人工审核
**批准**: 待执行
**预计完成日期**: 2025-11-19

**核心信念**: 特征工程是当前唯一的瓶颈。架构已经专业级，GPU已经激活，只差好的燃料（特征）。

---

## 快速开始（TL;DR）

```bash
# 1. 验证SPY数据
clickhouse-client --query "SELECT count(*) FROM trading.bars_1m_ibkr WHERE symbol='SPY'"

# 2. 扩展schema
docker exec clickhouse-server clickhouse-client < schema/features_1m_phase1_add_columns.sql

# 3. 计算特征
python tools/compute_features_phase1.py --symbols TSLA --start 2024-11-01 --end 2024-11-05

# 4. 验证效果
python test_professional_grade.py
```

**预期结果**: IC从0.03提升到0.05+，总分从56提升到86（专业级）
