# Feature Engineering Phase 1 - 完整实施计划

## 执行摘要

**目标**: 将 LSTM 模型从中级（56/100）提升至专业级（80+/100）

**当前瓶颈**: 特征工程不足（16 个简单特征 vs 需要 50-200 个）

**解决方案**: Phase 1 新增 30 个关键特征，预计 IC 从 0.030 提升至 ≥ 0.050

**时间成本**: 3-4 小时（主要是数据回填）

**风险级别**: 🟢 低（所有变更向后兼容，可随时回滚）

---

## 背景与动机

### 现状分析

根据客观测试（test_professional_grade.py）：

```
架构设计: 90/100 ✅ (V2 架构已达专业级)
计算效率: 70/100 ⚠️  (GPU 加速正常)
模型质量: 40/100 ❌ (IC=0.0301, 未达标)
特征工程: 30/100 ❌ (16 features vs 需要 50-200)
─────────────────────────────
总分: 56.0/100 📚 中级
```

**结论**: Gemini 正确 - 架构已经专业，但特征不足（GIGO）

### V2 Full 配置失败分析

```python
V2_Full: IC=-0.0282 ❌ (灾难性失败)
原因: 过拟合
- 参数: 210K
- 样本: 6,700
- 参数/样本比: 31:1 (健康范围应 < 1:10)
```

**对策**: 增加特征数量以支撑复杂模型

---

## Phase 1 特征设计

### 新增 30 个特征

#### 1. Multi-Timeframe Features (12)
捕捉不同时间尺度的模式

| 特征 | 计算方法 | 作用 |
|------|----------|------|
| `ret_5m` | 5 分钟收益率 | 短期趋势 |
| `ret_15m` | 15 分钟收益率 | 中期趋势 |
| `ret_1h` | 1 小时收益率 | 长期趋势 |
| `rsi_14_5m` | RSI(14) on 5m bars | 短期超买超卖 |
| `rsi_14_15m` | RSI(14) on 15m bars | 中期超买超卖 |
| `ema_20_5m` | EMA(20) on 5m bars | 短期均线 |
| `ema_20_15m` | EMA(20) on 15m bars | 中期均线 |
| `atr_14_5m` | ATR(14) on 5m bars | 短期波动率 |
| `atr_14_15m` | ATR(14) on 15m bars | 中期波动率 |
| `volume_5m` | 5 分钟累计成交量 | 短期流动性 |
| `volume_15m` | 15 分钟累计成交量 | 中期流动性 |
| `volume_1h` | 1 小时累计成交量 | 长期流动性 |

#### 2. Relative Strength Features (8)
相对 SPY 基准的表现

| 特征 | 计算方法 | 作用 |
|------|----------|------|
| `ret_vs_spy_1m` | stock_ret - spy_ret (1m) | 短期相对强度 |
| `ret_vs_spy_5m` | stock_ret - spy_ret (5m) | 中期相对强度 |
| `ret_vs_spy_15m` | stock_ret - spy_ret (15m) | 长期相对强度 |
| `beta_spy_20` | 20 周期 Beta vs SPY | 系统性风险 |
| `corr_spy_20` | 20 周期相关系数 | 市场关联度 |
| `relative_volume` | volume / vol_mean_20 | 相对交易活跃度 |
| `relative_volatility` | atr_14 / atr_mean_20 | 相对波动率 |
| `outperformance_score` | 综合跑赢得分 (0-1) | 整体相对强度 |

#### 3. Microstructure Features (6)
日内市场微观结构

| 特征 | 计算方法 | 作用 |
|------|----------|------|
| `vwap_dist` | (close - vwap) / vwap | 价格偏离度 |
| `volume_surge` | volume / vol_ema_20 | 成交量异动 |
| `high_low_range` | (high - low) / close | 波动幅度 |
| `intraday_volatility` | 5 周期 return stddev | 短期波动率 |
| `trade_intensity` | trades / volume | 订单流强度 |
| `momentum_strength` | 5 周期价格斜率 | 动量强度 |

#### 4. Time Features (4)
时间与交易时段效应

| 特征 | 计算方法 | 作用 |
|------|----------|------|
| `minute_of_day` | hour * 60 + minute | 时间位置 |
| `hour_of_day` | hour (0-23) | 小时效应 |
| `session_phase` | premarket/open/mid/close/after | 交易时段 |
| `time_to_close` | 距收盘分钟数 | 临近收盘效应 |

---

## 实施步骤

### Step 1: 前置准备（10 分钟）

#### 1.1 验证环境

```bash
# 检查 ClickHouse
docker ps | grep clickhouse

# 检查磁盘空间（需要至少 20GB）
df -h /workspace/Documents/clickhouse_data

# 验证 Python 环境
python --version
pip install clickhouse-driver pandas numpy structlog
```

#### 1.2 验证数据

```bash
# 检查 SPY 数据可用性
python temp/verify_spy_data.py

# 预期输出:
# ✅ SPY数据充足，可以计算相对强度特征
```

**检查点**:
- [ ] ClickHouse 运行正常
- [ ] 磁盘空间充足（> 20GB）
- [ ] SPY 数据可用

---

### Step 2: Schema 升级（5 分钟）

#### 2.1 备份当前 schema（可选但推荐）

```bash
clickhouse-client --query "SHOW CREATE TABLE trading.features_1m" > \
  backups/schema/features_1m_before_phase1.sql
```

#### 2.2 执行 schema 升级

```bash
# 方式 1: 使用 clickhouse-client（如果安装）
clickhouse-client < schema/upgrade_features_phase1.sql

# 方式 2: 使用 Python 脚本
python -c "
from clickhouse_driver import Client
client = Client(host='localhost', port=9000)
with open('schema/upgrade_features_phase1.sql') as f:
    sql = f.read()
    # 分割并执行每个 ALTER TABLE 语句
    for statement in sql.split(';'):
        if 'ALTER TABLE' in statement:
            client.execute(statement.strip() + ';')
"
```

#### 2.3 验证 schema

```bash
python -c "
from clickhouse_driver import Client
client = Client(host='localhost', port=9000)

result = client.execute('''
  SELECT name, type
  FROM system.columns
  WHERE database='trading' AND table='features_1m'
    AND name IN ('ret_5m', 'ret_vs_spy_1m', 'vwap_dist', 'minute_of_day')
''')

if len(result) == 4:
    print('✅ Schema 升级成功，新列已创建')
    for row in result:
        print(f'  {row[0]}: {row[1]}')
else:
    print(f'❌ Schema 升级失败，只找到 {len(result)} 列')
"
```

**检查点**:
- [ ] 4 个代表性新列已创建
- [ ] 列类型正确（Float64 或 UInt16）

---

### Step 3: 数据回填（2-4 小时）

#### 3.1 Phase A: 单符号试运行（5 分钟）

```bash
# 干跑模式（不写数据库）
python scripts/compute_features_phase1.py \
  --symbol TSLA \
  --start-date 2024-11-01 \
  --end-date 2024-11-05 \
  --dry-run

# 检查输出，确认无误后实际执行
python scripts/compute_features_phase1.py \
  --symbol TSLA \
  --start-date 2024-11-01 \
  --end-date 2024-11-05
```

**验证**:
```sql
SELECT
  ts, symbol,
  ret_5m, ret_vs_spy_1m, vwap_dist, minute_of_day
FROM trading.features_1m
WHERE symbol='TSLA'
  AND ts >= '2024-11-01'
LIMIT 10
```

**检查点**:
- [ ] 所有 30 个新特征有值（允许前 30 行为 NULL）
- [ ] 数值范围合理（ret_5m 通常在 -5% 到 +5%）
- [ ] SPY 相关特征不全是 NULL

#### 3.2 Phase B: 核心符号（30 分钟）

```bash
# 创建并执行批处理脚本
cat > /tmp/backfill_core.sh << 'EOF'
#!/bin/bash
SYMBOLS=("TSLA" "AAPL" "NVDA" "AMD" "GOOGL" "MSFT" "AMZN" "META" "SPY" "QQQ")
START_DATE="2024-08-01"
END_DATE="2024-11-05"

for symbol in "${SYMBOLS[@]}"; do
  echo "Processing $symbol..."
  python scripts/compute_features_phase1.py \
    --symbol "$symbol" \
    --start-date "$START_DATE" \
    --end-date "$END_DATE" \
    2>&1 | tee -a logs/backfill_core_${symbol}.log

  if [ $? -eq 0 ]; then
    echo "✅ $symbol completed"
  else
    echo "❌ $symbol failed"
  fi
  sleep 2
done
EOF

chmod +x /tmp/backfill_core.sh
/tmp/backfill_core.sh
```

**验证**:
```sql
SELECT
  symbol,
  count() as total_rows,
  countIf(isNotNull(ret_5m)) as has_features,
  countIf(isNotNull(ret_5m)) * 100.0 / count() as coverage_pct
FROM trading.features_1m
WHERE symbol IN ('TSLA', 'AAPL', 'NVDA')
  AND ts >= '2024-08-01'
GROUP BY symbol
ORDER BY symbol
```

**检查点**:
- [ ] 10 个核心符号全部完成
- [ ] 特征覆盖率 ≥ 95%

#### 3.3 Phase C: 全量回填（2-3 小时，可选）

**方式 1: 一次性回填（快速但风险高）**

```bash
nohup python scripts/compute_features_phase1.py \
  --all-symbols \
  --start-date 2024-01-01 \
  --end-date 2024-11-05 \
  > logs/backfill_all.log 2>&1 &
```

**方式 2: 分月回填（推荐，更安全）**

```bash
# 创建月度批处理脚本
cat > /tmp/backfill_by_month.sh << 'EOF'
#!/bin/bash
MONTHS=(
  "2024-01-01 2024-01-31"
  "2024-02-01 2024-02-29"
  "2024-03-01 2024-03-31"
  "2024-04-01 2024-04-30"
  "2024-05-01 2024-05-31"
  "2024-06-01 2024-06-30"
  "2024-07-01 2024-07-31"
  "2024-08-01 2024-08-31"
  "2024-09-01 2024-09-30"
  "2024-10-01 2024-10-31"
  "2024-11-01 2024-11-05"
)

for month_range in "${MONTHS[@]}"; do
  read start_date end_date <<< "$month_range"
  echo "Processing $start_date to $end_date..."

  python scripts/compute_features_phase1.py \
    --all-symbols \
    --start-date "$start_date" \
    --end-date "$end_date" \
    2>&1 | tee -a logs/backfill_month_${start_date}.log

  if [ $? -eq 0 ]; then
    echo "✅ Month $start_date completed"
  else
    echo "❌ Month $start_date failed"
    exit 1
  fi
  sleep 5
done
EOF

chmod +x /tmp/backfill_by_month.sh
nohup /tmp/backfill_by_month.sh > logs/backfill_master.log 2>&1 &
```

**监控进度**:
```bash
# 实时日志
tail -f logs/backfill_master.log

# 数据库进度
watch -n 30 "python -c \"
from clickhouse_driver import Client
client = Client(host='localhost', port=9000)
result = client.execute('''
  SELECT
    toYYYYMM(ts) as month,
    count() as rows,
    count(DISTINCT symbol) as symbols,
    countIf(isNotNull(ret_5m)) as with_features
  FROM trading.features_1m
  WHERE ts >= '2024-01-01'
  GROUP BY month
  ORDER BY month
''')
for row in result:
    print(f'{row[0]}: {row[1]:,} rows, {row[2]} symbols, {row[3]:,} with features')
\""
```

**检查点**:
- [ ] 所有月份回填完成
- [ ] 无失败日志
- [ ] 特征覆盖率 ≥ 95%

---

### Step 4: 数据质量验证（15 分钟）

#### 4.1 完整性检查

```sql
-- 检查每个符号的覆盖率
SELECT
  symbol,
  count() as total_rows,
  countIf(isNotNull(ret_5m)) * 100.0 / count() as pct_multi_timeframe,
  countIf(isNotNull(ret_vs_spy_1m)) * 100.0 / count() as pct_relative_strength,
  countIf(isNotNull(vwap_dist)) * 100.0 / count() as pct_microstructure,
  countIf(minute_of_day > 0) * 100.0 / count() as pct_time_features
FROM trading.features_1m
WHERE ts >= '2024-01-01'
GROUP BY symbol
HAVING pct_multi_timeframe < 95
ORDER BY pct_multi_timeframe
LIMIT 10
```

**预期**: 所有符号覆盖率 ≥ 95%（允许前 30 行为 NULL）

#### 4.2 异常值检查

```sql
SELECT
  symbol,
  quantile(0.01)(ret_5m) as ret_5m_p1,
  quantile(0.99)(ret_5m) as ret_5m_p99,
  quantile(0.01)(ret_vs_spy_1m) as rel_ret_p1,
  quantile(0.99)(ret_vs_spy_1m) as rel_ret_p99,
  avg(vwap_dist) as avg_vwap_dist
FROM trading.features_1m
WHERE ts >= '2024-01-01'
  AND isNotNull(ret_5m)
GROUP BY symbol
ORDER BY ret_5m_p99 DESC
LIMIT 10
```

**预期**:
- ret_5m: p1 ≈ -3%, p99 ≈ +3%
- ret_vs_spy_1m: p1 ≈ -2%, p99 ≈ +2%
- vwap_dist: 均值接近 0

#### 4.3 缺失值统计

```sql
SELECT
  'ret_5m' as feature,
  countIf(isNull(ret_5m)) as null_count,
  countIf(isNull(ret_5m)) * 100.0 / count() as null_pct
FROM trading.features_1m
WHERE ts >= '2024-01-01'

UNION ALL

SELECT 'ret_vs_spy_1m',
  countIf(isNull(ret_vs_spy_1m)),
  countIf(isNull(ret_vs_spy_1m)) * 100.0 / count()
FROM trading.features_1m
WHERE ts >= '2024-01-01'

UNION ALL

SELECT 'minute_of_day',
  countIf(minute_of_day = 0),
  countIf(minute_of_day = 0) * 100.0 / count()
FROM trading.features_1m
WHERE ts >= '2024-01-01'

ORDER BY null_pct DESC
```

**预期**:
- multi-timeframe: < 5% NULL（前 60 行）
- relative_strength: < 5% NULL（如果 SPY 数据完整）
- time_features: 0% NULL

**检查点**:
- [ ] 覆盖率 ≥ 95%
- [ ] 无异常极值
- [ ] 缺失率在预期范围

---

### Step 5: 模型重训练与评估（30 分钟）

#### 5.1 更新特征工程代码

查看并确认 `libs/features/feature_engineer_v2.py` 是否需要更新以使用新特征。

#### 5.2 重新训练模型

```bash
# 使用新特征训练 V2 BiLSTM
python test_professional_grade.py
```

**预期输出**:
```
V2_BiLSTM (46 features):
  训练时间: ~25s
  MSE: 0.000012
  IC: ≥ 0.050 ✅ (vs 之前 0.0301)

总分: ≥ 80/100 🎓 专业级
```

#### 5.3 对比分析

| 配置 | 特征数 | IC | 评分 | 状态 |
|------|--------|-------|------|------|
| V1 | 16 | 0.0299 | 56/100 | 基线 |
| V2 BiLSTM (旧) | 16 | 0.0301 | 56/100 | 架构改进，但特征不足 |
| V2 BiLSTM (新) | 46 | ≥ 0.050 | ≥ 80/100 | **目标** |

**检查点**:
- [ ] IC 提升至 ≥ 0.050
- [ ] 总分 ≥ 80/100
- [ ] 无过拟合（训练/验证 MSE 接近）

---

### Step 6: 生产部署（1 小时）

#### 6.1 更新 Alpha Loop 配置

编辑 `alpha_config.json`:

```json
{
  "hpo": {
    "grid": {
      "model_type": ["lstm"],
      "model_params": {
        "lstm": [
          {
            "use_v2_architecture": true,
            "bidirectional": true,
            "use_attention": true,
            "layer_norm": true,
            "pooling": "mean",
            "hidden": 128,
            "num_layers": 2,
            "dropout": 0.2,
            "lookback": 64,
            "epochs": 50,
            "device": "auto",
            "use_lr_scheduler": true,
            "verbose": true
          }
        ]
      }
    }
  }
}
```

#### 6.2 实时特征计算集成

**选项 A: 使用现有 feature_engineer_v2.py**

确认它已经包含新特征计算逻辑（根据之前分析，它已经设计好但未集成）。

**选项 B: 创建 SFS 扩展**

如果需要实时计算新特征，需要扩展 SFS 服务：

```bash
# 参考现有 SFS 架构
# services/market/sfs_1s_service.py
# services/market/sfs_5s_service.py
```

#### 6.3 Paper Trading 测试

```bash
# 启用 Paper Trading 模式测试新特征
# config.yml 中设置:
# paper_trading: true
# dry_run: true

python scripts/ai_trader/auto_loop.py
```

**监控指标**:
- 特征计算延迟 < 100ms
- 模型推理延迟 < 500ms
- 无异常日志

**检查点**:
- [ ] Alpha Loop 配置更新
- [ ] 实时特征计算正常
- [ ] Paper Trading 测试通过

---

## 成功标准

### 必须达成（Go/No-Go）

- [ ] IC ≥ 0.050（从 0.030 提升 67%）
- [ ] 专业级评分 ≥ 80/100
- [ ] 特征覆盖率 ≥ 95%
- [ ] 无数据质量问题（异常值、缺失值）
- [ ] 生产环境 Paper Trading 通过

### 期望达成（Nice-to-Have）

- [ ] IC ≥ 0.060（提升 100%）
- [ ] 特征重要性分析完成
- [ ] Top 30 特征识别
- [ ] 实时特征计算延迟 < 50ms

---

## 风险与缓解措施

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| SPY 数据缺失 | 低 | 高 | ✅ 已验证 SPY 数据充足 |
| 回填失败 | 低 | 中 | 分批执行，可随时中断恢复 |
| 磁盘空间不足 | 中 | 中 | 预留 20GB，监控磁盘使用 |
| 特征计算错误 | 低 | 高 | Dry-run 模式验证，逐步回滚 |
| 模型性能未提升 | 中 | 高 | 特征重要性分析，迭代优化 |
| 生产环境异常 | 低 | 高 | Paper Trading 充分测试 |

---

## 回滚方案

### 紧急回滚（5 分钟）

```sql
-- 清空所有新特征（不删除列）
ALTER TABLE trading.features_1m
UPDATE
  ret_5m = NULL, ret_15m = NULL, ret_1h = NULL,
  rsi_14_5m = NULL, rsi_14_15m = NULL,
  ema_20_5m = NULL, ema_20_15m = NULL,
  atr_14_5m = NULL, atr_14_15m = NULL,
  volume_5m = NULL, volume_15m = NULL, volume_1h = NULL,
  ret_vs_spy_1m = NULL, ret_vs_spy_5m = NULL, ret_vs_spy_15m = NULL,
  beta_spy_20 = NULL, corr_spy_20 = NULL,
  relative_volume = NULL, relative_volatility = NULL, outperformance_score = NULL,
  vwap_dist = NULL, volume_surge = NULL, high_low_range = NULL,
  intraday_volatility = NULL, trade_intensity = NULL, momentum_strength = NULL,
  minute_of_day = 0, hour_of_day = 0, session_phase = '', time_to_close = 0
WHERE ts >= '2024-01-01'
```

### 完整回滚（15 分钟）

```bash
# 1. 恢复模型配置
git checkout alpha_config.json

# 2. 删除新特征列（参考 schema/upgrade_features_phase1.sql PART 7）

# 3. 重启服务
make down
make up
```

---

## 时间表与里程碑

| 阶段 | 时长 | 累计时间 | 里程碑 |
|------|------|----------|--------|
| Step 1: 前置准备 | 10m | 10m | ✅ 环境就绪 |
| Step 2: Schema 升级 | 5m | 15m | ✅ 表结构更新 |
| Step 3: 数据回填 | 2-4h | 2-4h | ✅ 历史数据完整 |
| Step 4: 质量验证 | 15m | 2.5-4.5h | ✅ 数据质量合格 |
| Step 5: 模型训练 | 30m | 3-5h | ✅ IC ≥ 0.050 |
| Step 6: 生产部署 | 1h | 4-6h | ✅ Paper Trading 通过 |
| **总计** | **4-6h** | | |

**建议执行时间**: 非交易日（周末）或交易日晚上，避免影响实时系统

---

## 后续计划

### Phase 2: 特征优化（Week 2）

- [ ] 特征重要性分析
- [ ] 筛选 Top 50 特征
- [ ] 移除冗余特征
- [ ] 添加交互特征（10 个）

### Phase 3: 高级特征（Week 3）

- [ ] 订单流不平衡（Order Flow Imbalance）
- [ ] 期权隐含波动率（如果有数据）
- [ ] 新闻情绪得分（整合现有 News API）
- [ ] 行业轮动因子

### Phase 4: 模型优化（Week 4）

- [ ] Ensemble 模型（LSTM + LightGBM）
- [ ] 多任务学习（预测收益 + 波动率）
- [ ] 强化学习（RL-based position sizing）

---

## 附录

### A. 文件清单

```
schema/
  └── upgrade_features_phase1.sql          (Schema 升级脚本)

scripts/
  └── compute_features_phase1.py           (特征计算主脚本)

docs/
  ├── FEATURE_PHASE1_BACKFILL_PLAN.md     (详细回填指南)
  └── FEATURE_ENGINEERING_IMPROVEMENT_PLAN.md (Phase 1 设计文档)

temp/
  └── verify_spy_data.py                   (SPY 数据验证脚本)

logs/
  ├── backfill_core_*.log                  (核心符号回填日志)
  ├── backfill_month_*.log                 (月度回填日志)
  └── backfill_master.log                  (主日志)

backups/
  └── schema/
      └── features_1m_before_phase1.sql    (Schema 备份)
```

### B. 快速参考命令

```bash
# 检查环境
docker ps | grep clickhouse
df -h /workspace/Documents/clickhouse_data
python temp/verify_spy_data.py

# Schema 升级
python -c "from clickhouse_driver import Client; ..."

# 单符号测试
python scripts/compute_features_phase1.py --symbol TSLA --start-date 2024-11-01 --end-date 2024-11-05 --dry-run

# 核心符号回填
/tmp/backfill_core.sh

# 全量回填
nohup /tmp/backfill_by_month.sh > logs/backfill_master.log 2>&1 &

# 监控进度
tail -f logs/backfill_master.log

# 验证数据
clickhouse-client --query "SELECT count() FROM trading.features_1m WHERE ts >= '2024-01-01' AND isNotNull(ret_5m)"

# 重新训练
python test_professional_grade.py
```

### C. 常见问题 FAQ

**Q1: 回填需要多久？**
A: 2-4 小时，取决于符号数量和日期范围。可以分批执行，随时中断恢复。

**Q2: 会影响现有查询吗？**
A: 不会。所有新列都是 NULL 或有 DEFAULT 值，现有查询不受影响。

**Q3: 如果 SPY 数据缺失怎么办？**
A: 相对强度特征会是 NULL，但其他 22 个特征不受影响。建议先回填 SPY。

**Q4: 如何确认回填成功？**
A: 运行 Step 4 的验证查询，确认覆盖率 ≥ 95%，无异常值。

**Q5: 如果模型性能没提升？**
A: 进行特征重要性分析，可能需要调整特征或模型超参数。

---

## 联系与支持

**文档**:
- 详细设计: `FEATURE_ENGINEERING_IMPROVEMENT_PLAN.md`
- 回填指南: `docs/FEATURE_PHASE1_BACKFILL_PLAN.md`
- LSTM 升级: `LSTM_PROFESSIONAL_GRADE_ASSESSMENT.md`

**日志位置**:
- 主日志: `logs/backfill_master.log`
- ClickHouse: `docker logs <container_id>`

**数据验证**:
```bash
python temp/verify_spy_data.py
python test_professional_grade.py
```

---

**文档版本**: 1.0
**创建日期**: 2025-11-05
**状态**: ✅ Ready for Execution
**预期完成**: 2025-11-06

---

## 立即开始

```bash
# 1. 验证环境
python temp/verify_spy_data.py

# 2. Schema 升级（见 Step 2）

# 3. 单符号测试
python scripts/compute_features_phase1.py --symbol TSLA --start-date 2024-11-01 --end-date 2024-11-05 --dry-run

# 4. 正式回填（见 Step 3）
```

**Good luck! 🚀**
