# Phase 1 Feature Engineering - Backfill Plan

## 概述

本文档描述如何安全、高效地回填 Phase 1 新增的 30 个特征到历史数据。

**目标**: 为 2024-01-01 至今的所有交易数据计算并填充新特征
**预计时间**: 2-4 小时（取决于数据量和符号数量）
**数据量**: ~100 symbols × 365 days × 1440 minutes ≈ 52M rows

---

## 前置检查清单

### 1. 环境准备
- [ ] ClickHouse 运行正常: `docker ps | grep clickhouse`
- [ ] 磁盘空间充足: 至少 20GB 可用空间
- [ ] Python 环境正常: `python --version` (需要 3.8+)
- [ ] 依赖库已安装: `pip install clickhouse-driver pandas numpy structlog`

### 2. 数据验证
- [ ] SPY 数据可用: `python temp/verify_spy_data.py`
- [ ] bars_1m_ibkr 数据完整: 检查目标日期范围有数据

### 3. Schema 升级
- [ ] 已执行 schema 升级脚本: `schema/upgrade_features_phase1.sql`
- [ ] 验证新列已创建:
  ```sql
  SELECT name, type
  FROM system.columns
  WHERE database='trading' AND table='features_1m'
    AND name IN ('ret_5m', 'ret_vs_spy_1m', 'vwap_dist', 'minute_of_day')
  ```

---

## 回填策略

### 方案选择: **渐进式分批回填**

**原因**:
1. **安全**: 可随时中断，已完成的不受影响
2. **可监控**: 实时观察进度和错误
3. **低风险**: 避免单次大事务锁表
4. **可恢复**: 失败后可从断点继续

**避免**: 一次性全量回填（风险高，难以回滚）

---

## 执行阶段

### Phase 1: 单符号试运行（预计 5 分钟）

**目的**: 验证脚本正确性，评估性能

```bash
# 1. 干跑模式（不写入数据库）
python scripts/compute_features_phase1.py \
  --symbol TSLA \
  --start-date 2024-11-01 \
  --end-date 2024-11-05 \
  --dry-run

# 2. 检查输出，确认特征计算正确

# 3. 实际执行（写入数据库）
python scripts/compute_features_phase1.py \
  --symbol TSLA \
  --start-date 2024-11-01 \
  --end-date 2024-11-05

# 4. 验证数据写入成功
clickhouse-client --query "
SELECT
  ts, symbol,
  ret_5m, ret_vs_spy_1m, vwap_dist, minute_of_day
FROM trading.features_1m
WHERE symbol='TSLA'
  AND ts >= '2024-11-01'
  AND ts <= '2024-11-05'
LIMIT 10
"
```

**验证检查**:
- [ ] 所有 30 个新特征都有值（不全是 NULL）
- [ ] 数值范围合理（无异常极值）
- [ ] 时间特征正确（minute_of_day 在 0-1439 范围）
- [ ] 相对强度特征有值（ret_vs_spy_1m 不全是 NULL）

---

### Phase 2: 小批量回填（预计 30 分钟）

**目的**: 处理核心交易符号，覆盖最近 3 个月数据

**核心符号列表** (可根据实际情况调整):
```
TSLA, AAPL, NVDA, AMD, GOOGL, MSFT, AMZN, META, SPY, QQQ
```

**执行脚本**:
```bash
#!/bin/bash
# scripts/backfill_core_symbols.sh

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

  sleep 2  # 避免过载
done
```

**执行**:
```bash
chmod +x scripts/backfill_core_symbols.sh
./scripts/backfill_core_symbols.sh
```

**验证**:
```sql
-- 检查完成情况
SELECT
  symbol,
  count() as total_rows,
  countIf(isNotNull(ret_5m)) as has_ret_5m,
  countIf(isNotNull(ret_vs_spy_1m)) as has_relative_strength,
  countIf(isNotNull(vwap_dist)) as has_microstructure,
  countIf(minute_of_day > 0) as has_time_features
FROM trading.features_1m
WHERE symbol IN ('TSLA', 'AAPL', 'NVDA', 'AMD', 'GOOGL', 'MSFT', 'AMZN', 'META', 'SPY', 'QQQ')
  AND ts >= '2024-08-01'
GROUP BY symbol
ORDER BY symbol
```

---

### Phase 3: 全量回填（预计 2-3 小时）

**目的**: 回填所有符号的完整历史数据（2024-01-01 至今）

**方式 A: 自动发现所有符号**

```bash
python scripts/compute_features_phase1.py \
  --all-symbols \
  --start-date 2024-01-01 \
  --end-date 2024-11-05 \
  2>&1 | tee logs/backfill_all_symbols.log
```

**方式 B: 分月批次处理（推荐，更安全）**

```bash
#!/bin/bash
# scripts/backfill_by_month.sh

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
    echo "❌ Month $start_date failed, stopping"
    exit 1
  fi

  sleep 5
done

echo "✅ All months completed"
```

**执行**:
```bash
chmod +x scripts/backfill_by_month.sh
nohup ./scripts/backfill_by_month.sh &
```

**监控进度**:
```bash
# 实时监控日志
tail -f logs/backfill_month_*.log

# 检查数据库写入进度
watch -n 10 "clickhouse-client --query '
SELECT
  toYYYYMM(ts) as month,
  count() as total_rows,
  count(DISTINCT symbol) as symbols,
  countIf(isNotNull(ret_5m)) as has_phase1_features
FROM trading.features_1m
WHERE ts >= \"2024-01-01\"
GROUP BY month
ORDER BY month
'"
```

---

## 性能优化

### 1. 并行处理（可选）

如果单线程太慢，可以并行处理多个符号：

```bash
# 获取所有符号列表
clickhouse-client --query "
SELECT DISTINCT symbol
FROM trading.bars_1m_ibkr
WHERE ts_end >= '2024-01-01'
" > /tmp/all_symbols.txt

# 使用 GNU parallel 并行处理（4 个并发）
cat /tmp/all_symbols.txt | parallel -j 4 \
  "python scripts/compute_features_phase1.py \
    --symbol {} \
    --start-date 2024-01-01 \
    --end-date 2024-11-05 \
    2>&1 | tee logs/backfill_{}.log"
```

**注意**: 确保 ClickHouse 有足够的并发写入能力（max_concurrent_queries 配置）

### 2. 批量大小调整

如果遇到内存问题，可以缩小批次大小：

```python
# 修改 compute_features_phase1.py
# 将日期范围拆分为更小的块（如每次处理 7 天）
```

---

## 验证与质量检查

### 1. 完整性检查

```sql
-- 检查每个符号的特征覆盖率
SELECT
  symbol,
  count() as total_rows,
  countIf(isNotNull(ret_5m)) * 100.0 / count() as pct_has_multi_timeframe,
  countIf(isNotNull(ret_vs_spy_1m)) * 100.0 / count() as pct_has_relative_strength,
  countIf(isNotNull(vwap_dist)) * 100.0 / count() as pct_has_microstructure,
  countIf(minute_of_day > 0) * 100.0 / count() as pct_has_time_features
FROM trading.features_1m
WHERE ts >= '2024-01-01'
GROUP BY symbol
HAVING pct_has_multi_timeframe < 95  -- 找出覆盖率不足的符号
ORDER BY pct_has_multi_timeframe
```

### 2. 数据质量检查

```sql
-- 检查异常值
SELECT
  symbol,
  quantile(0.01)(ret_5m) as ret_5m_p1,
  quantile(0.99)(ret_5m) as ret_5m_p99,
  quantile(0.01)(ret_vs_spy_1m) as rel_ret_p1,
  quantile(0.99)(ret_vs_spy_1m) as rel_ret_p99,
  avg(vwap_dist) as avg_vwap_dist,
  avg(volume_surge) as avg_volume_surge
FROM trading.features_1m
WHERE ts >= '2024-01-01'
  AND isNotNull(ret_5m)
GROUP BY symbol
ORDER BY symbol
```

### 3. 缺失值检查

```sql
-- 统计每个特征的缺失率
SELECT
  'ret_5m' as feature, countIf(isNull(ret_5m)) * 100.0 / count() as null_pct FROM trading.features_1m WHERE ts >= '2024-01-01'
UNION ALL
SELECT 'ret_vs_spy_1m', countIf(isNull(ret_vs_spy_1m)) * 100.0 / count() FROM trading.features_1m WHERE ts >= '2024-01-01'
UNION ALL
SELECT 'vwap_dist', countIf(isNull(vwap_dist)) * 100.0 / count() FROM trading.features_1m WHERE ts >= '2024-01-01'
UNION ALL
SELECT 'beta_spy_20', countIf(isNull(beta_spy_20)) * 100.0 / count() FROM trading.features_1m WHERE ts >= '2024-01-01'
UNION ALL
SELECT 'minute_of_day', countIf(minute_of_day = 0) * 100.0 / count() FROM trading.features_1m WHERE ts >= '2024-01-01'
ORDER BY null_pct DESC
```

**预期**:
- 前 30 个 bar（用于 rolling 计算）可能为 NULL - 正常
- SPY 相关特征如果 SPY 数据缺失会是 NULL - 需修复
- 时间特征应该 100% 有值

---

## 错误处理

### 常见问题

#### 1. SPY 数据缺失
**症状**: ret_vs_spy_1m 等特征全是 NULL

**解决**:
```bash
# 检查 SPY 数据
python temp/verify_spy_data.py

# 如果缺失，先处理 SPY
python scripts/compute_features_phase1.py \
  --symbol SPY \
  --start-date 2024-01-01 \
  --end-date 2024-11-05
```

#### 2. 内存不足
**症状**: `MemoryError` 或进程被杀

**解决**:
- 减小批次大小（按周而不是按月处理）
- 增加系统 swap 空间
- 使用更小的 rolling window（在脚本中调整）

#### 3. ClickHouse 连接超时
**症状**: `Connection timeout`

**解决**:
```bash
# 检查 ClickHouse 状态
docker ps | grep clickhouse
docker logs <clickhouse_container_id>

# 增加超时时间
# 修改脚本: Client(host='localhost', port=9000, connect_timeout=60)
```

#### 4. 特征计算错误（NaN/Inf）
**症状**: 某些行的特征值异常

**解决**:
- 脚本已包含 `replace([np.inf, -np.inf], np.nan)` 处理
- 检查源数据是否有问题（zero volume, missing prices）

---

## 回滚方案

如果回填出现严重问题，可以回滚：

### 方案 1: 删除新特征列（不推荐，会丢失数据）

```sql
-- 执行 schema/upgrade_features_phase1.sql 中的 PART 7: Rollback 部分
```

### 方案 2: 清空新特征列（推荐）

```sql
-- 将所有新特征列设置为 NULL
ALTER TABLE trading.features_1m
UPDATE
  ret_5m = NULL,
  ret_15m = NULL,
  ret_1h = NULL,
  rsi_14_5m = NULL,
  rsi_14_15m = NULL,
  ema_20_5m = NULL,
  ema_20_15m = NULL,
  atr_14_5m = NULL,
  atr_14_15m = NULL,
  volume_5m = NULL,
  volume_15m = NULL,
  volume_1h = NULL,
  ret_vs_spy_1m = NULL,
  ret_vs_spy_5m = NULL,
  ret_vs_spy_15m = NULL,
  beta_spy_20 = NULL,
  corr_spy_20 = NULL,
  relative_volume = NULL,
  relative_volatility = NULL,
  outperformance_score = NULL,
  vwap_dist = NULL,
  volume_surge = NULL,
  high_low_range = NULL,
  intraday_volatility = NULL,
  trade_intensity = NULL,
  momentum_strength = NULL,
  minute_of_day = 0,
  hour_of_day = 0,
  session_phase = '',
  time_to_close = 0
WHERE ts >= '2024-01-01'
```

### 方案 3: 从备份恢复（终极方案）

```bash
# 如果有 ClickHouse 备份
# 参考 docs/CLICKHOUSE_BACKUP_SETUP.md
```

---

## 完成后检查清单

- [ ] 所有符号都已处理（检查日志无失败）
- [ ] 特征覆盖率 ≥ 95%（允许前 30 行为 NULL）
- [ ] 无异常值（99分位数在合理范围）
- [ ] SPY 相关特征有值（非全 NULL）
- [ ] 时间特征 100% 有值
- [ ] 磁盘空间使用在预期范围（< 20GB 增长）
- [ ] ClickHouse 查询性能正常（SELECT 响应时间 < 1s）

---

## 后续步骤

完成回填后：

1. **重新训练模型**:
   ```bash
   python backtest_ml_engine.py --symbol TSLA --start-date 2024-01-01 --end-date 2024-11-05
   ```

2. **对比性能**:
   - V2 BiLSTM (16 features) 的 IC
   - V2 BiLSTM (46 features) 的 IC
   - 预期: IC 从 0.030 提升至 ≥ 0.050

3. **特征重要性分析**:
   ```python
   from sklearn.ensemble import RandomForestRegressor
   # 使用 feature_importances_ 找出最重要的 30 个特征
   ```

4. **更新 Alpha Loop 配置**:
   ```json
   {
     "model_params": {
       "lstm": [{
         "use_v2_architecture": true,
         "bidirectional": true,
         "use_attention": true,
         "hidden": 128,
         "num_layers": 2,
         "lookback": 64
       }]
     }
   }
   ```

5. **生产环境部署**:
   - 更新实时特征计算流程（SFS 服务）
   - 测试 Paper Trading
   - 监控模型性能

---

## 预计时间表

| 阶段 | 预计时间 | 可中断 |
|------|----------|--------|
| Phase 1: 单符号试运行 | 5 分钟 | ✅ |
| Phase 2: 核心符号 | 30 分钟 | ✅ |
| Phase 3: 全量回填 | 2-3 小时 | ✅ |
| 验证与质量检查 | 15 分钟 | ✅ |
| **总计** | **3-4 小时** | |

**建议执行时间**: 非交易时段（周末或晚上），避免影响实时交易系统

---

## 联系与支持

如遇到问题：
1. 检查日志: `logs/backfill_*.log`
2. 查看 ClickHouse 日志: `docker logs <container>`
3. 参考文档: `FEATURE_ENGINEERING_IMPROVEMENT_PLAN.md`

---

**文档版本**: 1.0
**创建日期**: 2025-11-05
**最后更新**: 2025-11-05
