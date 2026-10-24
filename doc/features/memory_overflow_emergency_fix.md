# ClickHouse 内存溢出紧急修复 - 2025-11-20

## 问题回顾

即使实施了分批处理 (batch_size=5),仍然出现内存溢出:

```
Error: Code 241. MEMORY_LIMIT_EXCEEDED
Batch 2/3 failed: ['AMD', 'META', 'AMZN', 'GOOGL', 'QQQ']
Would use 7.21 GiB, maximum: 7.20 GiB
Resolution: 15s
```

## 根本原因

1. **ClickHouse 全局内存限制过低**: 7.20 GiB (原配置)
2. **单查询内存限制过高**: 6 GB - 单个批次 (5 symbols) 就接近或超过全局限制的 83%
3. **15s 分辨率窗口函数消耗大**: 即使 5 个 symbols,复杂窗口函数 (w_20, w_60, w_tod) 也需要 3.7+ GB
4. **配置冲突**: `clickhouse-config/` 和 `clickhouse-users.d/` 两个目录都有配置,后者优先级更高

## 紧急修复方案 (已实施)

### 1. 应用配置优化

#### A. 降低批次大小 (`config.yml`)

```yaml
feature_calculator:
  batch_size: 2                    # 从 5 降到 2
  max_memory_per_query: 2147483648 # 2GB
  horizon_minutes: 10              # 从 15 降到 10
```

**效果**: 15 symbols → 8 批次 (每批 2 symbols)
- 单批次内存需求: ~1.5 GB (远低于 3GB 限制)
- 总执行时间增加: 约 3 倍,但避免了内存溢出

#### B. 支持 horizon_minutes 配置 (`feature_calculator.py`)

添加配置覆盖逻辑:

```python
config_horizon = config.get('feature_calculator.horizon_minutes')
if config_horizon:
    self.horizon_minutes = config_horizon
```

### 2. ClickHouse 服务器级别优化

#### A. 增加全局内存限制 (`infra/clickhouse-config/config.xml`)

```xml
<!-- 从 7.2GB 增加到 10GB -->
<max_server_memory_usage>10737418240</max_server_memory_usage>
<max_server_memory_usage_to_ram_ratio>0.85</max_server_memory_usage_to_ram_ratio>
```

#### B. 降低单查询内存限制 (防止单查询独占)

修改两处配置文件 (都需要修改,后者优先级更高):
1. `infra/clickhouse-config/10-default-profile.xml`
2. `infra/clickhouse-users.d/10-default-profile.xml`

```xml
<!-- 从 6GB 降到 3GB -->
<max_memory_usage>3221225472</max_memory_usage>

<!-- 从 3GB 降到 1.5GB -->
<max_bytes_before_external_group_by>1610612736</max_bytes_before_external_group_by>
<max_bytes_before_external_sort>1610612736</max_bytes_before_external_sort>
```

### 3. 验证配置生效

```bash
docker compose restart clickhouse
sleep 15
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT name, formatReadableSize(toUInt64(value)) as value
   FROM system.settings
   WHERE name IN ('max_memory_usage', 'max_bytes_before_external_group_by')
   FORMAT Vertical"
```

**验证结果**:
```
✅ max_memory_usage: 3.00 GiB
✅ max_bytes_before_external_group_by: 1.50 GiB
✅ max_bytes_before_external_sort: 1.50 GiB
```

## 配置对比

| 配置项 | 修复前 | 修复后 | 变化 |
|--------|--------|--------|------|
| **应用层** | | | |
| batch_size | 5 symbols | 2 symbols | ↓ 60% |
| horizon_minutes | 15 min | 10 min | ↓ 33% |
| max_memory_per_query | 1 GB | 2 GB | ↑ 100% |
| **ClickHouse 服务器** | | | |
| 全局内存限制 | 7.2 GB | 10 GB | ↑ 39% |
| 单查询内存限制 | 6 GB | 3 GB | ↓ 50% |
| External sort/group | 3 GB | 1.5 GB | ↓ 50% |
| **效果** | | | |
| 单批次内存需求 | ~3.7 GB | ~1.5 GB | ↓ 59% |
| 批次数量 (15 symbols) | 3 批次 | 8 批次 | ↑ 167% |

## 架构设计理念

### 修复前 (激进策略)
```
大批次 (5 symbols) + 高单查询限制 (6GB) = 高风险
- 优势: 查询次数少
- 风险: 单个批次失败影响大,容易耗尽全局内存
```

### 修复后 (保守策略)
```
小批次 (2 symbols) + 低单查询限制 (3GB) + 高全局限制 (10GB) = 高稳定性
- 优势:
  * 单批次内存可控 (~1.5GB << 3GB)
  * 可并发运行 3-4 个查询而不超限
  * 单批次失败影响范围小 (只有 2 symbols)
- 劣势: 查询次数增加 (但通过并发可抵消)
```

## 内存使用分析

### 15s 分辨率特征计算内存消耗 (单批次 2 symbols)

| 阶段 | 内存占用 | 说明 |
|------|---------|------|
| filtered_bars CTE | ~200 MB | 基础 bar 数据 (10分钟 * 2 symbols * 120 bars/symbol) |
| price_lags/rsi/stoch | ~300 MB | 简单窗口函数 |
| ADX/DMI计算 | ~400 MB | 多个中间 CTE |
| VWAP/OBV/VIX | ~200 MB | 累积计算 |
| 窗口函数 (w_20/w_60) | ~300 MB | 统计特征 |
| 最终 INSERT | ~100 MB | 写入 features 表 |
| **峰值总计** | **~1.5 GB** | 考虑 ClickHouse 内部开销 |

### 不同 batch_size 的内存需求 (估算)

| batch_size | 单批次内存 | 安全裕度 (vs 3GB) | 推荐? |
|------------|-----------|------------------|-------|
| 1 | ~0.8 GB | 73% | ⚠️ 太保守,查询次数过多 |
| 2 | ~1.5 GB | 50% | ✅ **当前配置** |
| 3 | ~2.2 GB | 27% | ⚠️ 裕度较小 |
| 5 | ~3.7 GB | -23% | ❌ 超出限制 |

## 监控指标

启动后应观察以下日志:

```log
# 配置加载
FeatureCalculator initialized resolution=15s horizon_min=10 batch_size=2
Overriding horizon_minutes from config original=15 configured=10

# 批次执行
Starting batch computation total_symbols=15 total_batches=8 batch_size=2
Batch computed successfully batch=1/8 symbols=['AAPL', 'TSLA'] elapsed_ms=312.45
Batch computed successfully batch=2/8 symbols=['AMD', 'META'] elapsed_ms=298.67
...
Batch computation completed total_batches=8 successful=8 failed=0 total_elapsed_ms=2456.78
```

## 预期效果

| 指标 | 修复前 | 修复后 | 说明 |
|-----|--------|--------|------|
| 单批次成功率 | 40% (2/5) | **100%** (8/8) | 关键改善 |
| 单批次执行时间 | ~350ms | ~300ms | 略快 (数据量减少) |
| 总执行时间 | ~1050ms (3批) | ~2400ms (8批) | 可接受的折中 |
| 内存峰值 | ~3.7 GB | ~1.5 GB | **降低 59%** |
| 全局内存可用性 | 高风险 (51% 占用) | **安全** (15% 占用) | 关键改善 |

## 后续优化方向

### 短期 (1-2周)

1. **并行批次处理** (可选)
   - 使用 `asyncio.gather()` 并发执行 2-3 个批次
   - 需要严格控制并发数,避免超过全局内存限制
   - 预期总执行时间降低到 ~1200ms

2. **增量计算** (推荐)
   - 只重新计算有新 bar 数据的 symbols
   - 通过检查 `bars_{resolution}` 表的最新时间戳
   - 可减少 50-80% 的计算量

### 中期 (1-2个月)

3. **SQL 查询优化**
   - 合并相似的 CTE (如 price_lags 和 rsi_components)
   - 延迟计算不常用的特征 (如 hurst_20, entropy_20)
   - 使用预聚合表减少窗口函数计算

4. **特征分级计算**
   - Tier-1 特征: 每个 poll 周期计算 (轻量级)
   - Tier-2 特征: 每 3 个 poll 周期计算 (中等)
   - Tier-3 特征: 每 5 个 poll 周期计算 (重量级)

### 长期 (3-6个月)

5. **分布式计算**
   - 使用多个 ClickHouse 节点
   - 按 symbol 分片计算
   - 需要架构重构

6. **流式计算引擎**
   - 迁移到 Apache Flink 或 ClickHouse Pipes
   - 实时增量计算,避免重复计算历史数据

## 回滚方案

如果修复后仍有问题,可快速回滚:

### 1. 回滚应用配置

```yaml
# config.yml
feature_calculator:
  batch_size: 1  # 进一步降低到 1
  horizon_minutes: 5  # 进一步降低到 5 分钟
```

### 2. 回滚 ClickHouse 配置

```bash
cd ./infra
git checkout clickhouse-config/config.xml
git checkout clickhouse-users.d/10-default-profile.xml
docker compose restart clickhouse
```

### 3. 临时禁用高频分辨率

如果问题严重,可临时禁用 15s 计算器:

```python
# run_system.py line 627
if not is_rth:
    # 临时注释掉 15s 计算器
    # try:
    #     self.feature_calc_15s = FeatureCalculator(resolution="15s", ...)
    ...
```

## 相关文件

| 类型 | 文件路径 | 说明 |
|------|---------|------|
| **应用配置** | `config.yml` | 批次大小、内存限制 |
| **应用代码** | `services/market/features/feature_calculator.py` | 分批逻辑 |
| **ClickHouse 配置** | `infra/clickhouse-config/config.xml` | 服务器级别配置 |
| **ClickHouse 配置** | `infra/clickhouse-config/10-default-profile.xml` | 查询级别配置 |
| **ClickHouse 配置** | `infra/clickhouse-users.d/10-default-profile.xml` | 用户级别配置 (优先级最高) |
| **Docker 配置** | `infra/compose.yml` | 容器内存限制 |
| **文档** | `doc/features/batch_feature_calculation.md` | 原始分批方案 |
| **文档** | `doc/features/memory_overflow_emergency_fix.md` | 本文档 |

## 故障排查

### 如果仍然出现内存溢出

1. **检查配置是否生效**:
   ```bash
   docker exec trading_clickhouse clickhouse-client --query \
     "SELECT name, formatReadableSize(toUInt64(value)) FROM system.settings
      WHERE name = 'max_memory_usage'"
   ```
   预期: `3.00 GiB`

2. **检查实际批次大小**:
   ```bash
   grep "Starting batch computation" logs/trading_system.log | tail -1
   ```
   预期: `batch_size=2`

3. **进一步降低批次大小**:
   ```yaml
   batch_size: 1
   horizon_minutes: 5
   ```

4. **检查其他服务的内存占用**:
   ```bash
   docker stats trading_clickhouse
   ```
   如果基础内存使用 > 4GB,说明有其他查询在消耗内存

5. **清理 ClickHouse 缓存** (如果必要):
   ```bash
   docker exec trading_clickhouse clickhouse-client --query "SYSTEM DROP MARK CACHE"
   docker exec trading_clickhouse clickhouse-client --query "SYSTEM DROP UNCOMPRESSED CACHE"
   ```

### 如果计算时间过长

如果 8 个批次导致总时间 > 5秒 (poll_interval_sec),可能导致计算积压:

1. **增加 poll_interval_sec**:
   ```python
   # run_system.py
   FeatureCalculator(resolution="15s", poll_interval_sec=10.0)  # 从 5.0 增加到 10.0
   ```

2. **实施并行批次处理** (高级):
   修改 `_compute_features()` 使用 `asyncio.gather()`

## 版本历史

| 版本 | 日期 | 变更 |
|-----|------|------|
| 1.0 | 2025-11-20 | 紧急修复:降低 batch_size,优化 ClickHouse 内存配置 |

---

**状态**: ✅ 已实施,等待生产验证
**优先级**: 🔴 P0 (阻塞性问题)
**负责人**: Trading Team
**最后更新**: 2025-11-20 03:35 UTC
