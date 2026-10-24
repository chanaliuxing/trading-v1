# 数据就绪检查与特征表同步系统

> AI 大脑智能化补强 - 自动数据对齐、特征表同步、动态窗口管理
>
> **目标**: 消除空跑、提升训练成功率、减少人工介入

---

## 一、系统概览

### 核心模块

| 模块 | 文件 | 功能 | 状态 |
|------|------|------|------|
| **DataReadinessChecker** | `libs/ml/data_readiness_checker.py` | 训练前数据就绪检查 | ✅ 已部署 |
| **ActiveTableSyncManager** | `tools/sync_features_active.py` | Features表同步与校验 | ✅ 已部署 |
| **Cron调度** | `infra/cron/` | 定时同步与监控 | ✅ 已配置 |

### 数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                     训练前数据对齐流程                            │
└─────────────────────────────────────────────────────────────────┘

1. Brain/Scheduler 请求训练 (symbol, start, end, horizon)
                    ↓
2. DataReadinessChecker 检查:
   ├─ Bars 完整性 (RTH: 9:30-16:00)
   ├─ Features 覆盖率 (active vs baseline)
   └─ Labels 对齐率
                    ↓
           ┌────────┴────────┐
           ↓                 ↓
   【ready】            【needs_window_adjust】
   继续训练             ├─ 缩窗至 30/45/60 天
                       └─ 更新 start/end
           ↓
   【needs_sync】
   ├─ Active 表滞后 >7天
   ├─ 触发 sync_features_active.py
   └─ 失败后切换 baseline
           ↓
   【not_ready】
   ├─ Bars 覆盖率 <30%
   ├─ Labels 覆盖率 <90%
   └─ 记录 error_code, 不启动训练
```

---

## 二、使用指南

### 2.1 数据就绪检查 (DataReadinessChecker)

#### Python API

```python
from datetime import date
from libs.ml.data_readiness_checker import DataReadinessChecker

checker = DataReadinessChecker()

# 训练前检查
result = await checker.check_training_window(
    symbol="TSLA",
    start=date(2025, 7, 1),
    end=date(2025, 11, 1),
    horizon="5m",
    min_utilization=0.5  # 最低数据利用率
)

if result.status == "ready":
    # 继续训练
    print(f"Data ready! Features table: {result.metadata['features_table']}")

elif result.status == "needs_window_adjust":
    # 使用建议窗口
    new_start = result.suggested_start
    new_end = result.suggested_end
    print(f"Window adjusted: {new_start} → {new_end}")

elif result.status == "needs_sync":
    # 触发同步
    print(f"Action required: {result.action}")

else:  # not_ready
    # 不可训练
    print(f"Training blocked: {result.reason}")
```

#### 集成到 Scheduler

✅ **已集成**: DataReadinessChecker 已集成到 `scripts/ai_trader/ml_scheduler.py:724-802`

**集成位置**: 在 `_execute_oof` 函数的 preflight 检查之前

**工作流程**:
1. 解析 horizon 和 features_table 参数
2. 对每个 symbol 运行 DataReadinessChecker
3. 记录每个 symbol 的状态和利用率
4. 如果任何 symbol 状态为 `not_ready`，则阻止任务执行
5. 如果状态为 `needs_window_adjust`，记录建议窗口（暂不自动应用）
6. 通过检查后，继续执行原有的 preflight 逻辑

**日志示例**:
```json
{
  "event": "data_readiness_check",
  "job_id": "20251202103045-abc123",
  "symbol": "TSLA",
  "status": "ready",
  "reason": "all_checks_passed",
  "bars_util": 0.964,
  "features_util": 1.0
}
```

**被阻止任务示例**:
```json
{
  "event": "job_blocked_data_not_ready",
  "job_id": "20251202103045-abc123",
  "blocked_symbols": ["NVDA"],
  "error_code": "data_not_ready",
  "features_table": "trading.features_1m_active"
}
```

---

### 2.2 特征表同步 (sync_features_active.py)

#### CLI 使用

```bash
# 1. 同步昨天的数据 (Cron 模式)
venv/bin/python tools/sync_features_active.py \
    --symbols TSLA AAPL NVDA \
    --yesterday \
    --validate

# 2. 同步特定日期范围
venv/bin/python tools/sync_features_active.py \
    --symbols TSLA \
    --start 2025-11-01 \
    --end 2025-11-30 \
    --validate

# 3. 仅校验 (不同步)
venv/bin/python tools/sync_features_active.py \
    --symbols TSLA \
    --start 2025-11-28 \
    --end 2025-12-01 \
    --validate-only

# 4. 自定义批次大小
venv/bin/python tools/sync_features_active.py \
    --symbols TSLA \
    --yesterday \
    --batch-days 3  # 每3天一批 (避免OOM)
```

#### Python API

```python
from datetime import date
from tools.sync_features_active import ActiveTableSyncManager

manager = ActiveTableSyncManager()

# 同步并校验
result = await manager.sync_with_validation(
    symbol="TSLA",
    start=date(2025, 11, 1),
    end=date(2025, 11, 30),
    validate=True,
    batch_size_days=7
)

if result.status == "success":
    print(f"Synced {result.rows_synced} rows")
    print(f"Schema changes: {result.schema_changes}")
else:
    print(f"Sync failed: {result.reason}")
```

#### 同步策略

| 场景 | 触发条件 | 处理方式 |
|------|---------|---------|
| **定时同步** | 每日 4:00 AM | 同步昨天的数据 (Cron) |
| **按需同步** | Active 表滞后 >7天 | Scheduler 检测后触发 |
| **同步失败** | 校验失败 (行数差异 >5%) | 重试3次后切换 baseline |
| **Schema 漂移** | Baseline 有新列 | 自动 ALTER TABLE ADD COLUMN |

---

### 2.3 Cron 定时任务配置

#### 安装 Cron 任务

```bash
# 编辑 crontab
crontab -e

# 添加以下行:

# 1. 每日 4:00 AM 同步 active 表 (在 daily_data_pipeline.sh 后)
0 4 * * * ./infra/cron/sync_features_active_daily.sh >> ./logs/sync_active.log 2>&1

# 2. 每日 9:00 AM 数据质量检查 (仅工作日)
0 9 * * 1-5 ./infra/cron/data_quality_check_daily.sh >> ./logs/data_quality.log 2>&1
```

#### 查看 Cron 日志

```bash
# 同步日志
tail -f ./logs/sync_active.log

# 数据质量日志
tail -f ./logs/data_quality.log
```

#### 手动运行

```bash
# 测试同步脚本
bash ./infra/cron/sync_features_active_daily.sh

# 测试数据质量检查
bash ./infra/cron/data_quality_check_daily.sh
```

---

## 三、监控与告警

### 3.1 ML Events 记录

所有关键操作都会写入 `runs/ai_trader/YYYY-MM-DD/ml_events.jsonl`:

```json
{
  "event": "ml.job",
  "status": "failed",
  "job_type": "oof",
  "job_id": "20251202103045-abc123",
  "error_code": "coverage_insufficient",
  "features_table": "trading.features_1m_active",
  "metadata": {
    "bars_utilization": 0.25,
    "features_utilization": 0.18,
    "reason": "features_coverage_low"
  },
  "ts": "2025-12-02T10:30:45"
}
```

### 3.2 关键指标

| 指标 | 含义 | 阈值 | 告警条件 |
|------|------|------|---------|
| `bars_utilization` | Bars 数据覆盖率 | >0.3 | <0.3 → 不可训练 |
| `features_utilization` | Features 利用率 | >0.5 | <0.5 → 缩窗或同步 |
| `labels_utilization` | Labels 覆盖率 | >0.9 | <0.9 → 补算标签 |
| `lag_days` | Active 表滞后天数 | <7 | >7 → 触发同步 |
| `sync_success_rate` | 同步成功率 | >0.95 | <0.95 → 人工介入 |

### 3.3 Grafana Dashboard (待实现)

```yaml
# Metrics 来源: trading.ml_events 表
queries:
  - name: Training Success Rate (24h)
    sql: |
      SELECT
        countIf(status='completed' AND ok=true) / count() as success_rate
      FROM trading.ml_events
      WHERE created_at >= now() - INTERVAL 24 HOUR
        AND event_type='ml.job'

  - name: Top Failure Reasons (7d)
    sql: |
      SELECT
        payload->>'error_code' as error_code,
        count() as cnt
      FROM trading.ml_events
      WHERE created_at >= now() - INTERVAL 7 DAY
        AND status='failed'
      GROUP BY error_code
      ORDER BY cnt DESC
      LIMIT 5
```

---

## 四、故障排查

### 4.1 常见问题

#### 问题: Active 表同步失败

```bash
# 查看同步日志
tail -100 ./logs/sync_active.log

# 手动同步特定日期
venv/bin/python tools/sync_features_active.py \
    --symbols TSLA \
    --start 2025-11-28 \
    --end 2025-11-28 \
    --validate

# 检查 Schema 差异
clickhouse-client --query "
  SELECT name, type
  FROM system.columns
  WHERE database='trading' AND table='features_1m'
  EXCEPT
  SELECT name, type
  FROM system.columns
  WHERE database='trading' AND table='features_1m_active'
"
```

#### 问题: 训练因 coverage_insufficient 失败

```bash
# 检查 Bars 数据
clickhouse-client --query "
  SELECT
    symbol,
    toDate(ts) as dt,
    count() as bars
  FROM trading.bars
  WHERE symbol='TSLA'
    AND ts >= '2025-11-01'
    AND ts < '2025-12-01'
    AND toHour(ts) >= 9 AND toHour(ts) < 16
  GROUP BY symbol, dt
  ORDER BY dt
"

# 检查 Features 覆盖率
venv/bin/python -c "
import asyncio
from datetime import date
from libs.ml.data_readiness_checker import DataReadinessChecker

async def check():
    checker = DataReadinessChecker()
    result = await checker.check_training_window(
        symbol='TSLA',
        start=date(2025, 11, 1),
        end=date(2025, 11, 30),
        horizon='5m'
    )
    print(f'Status: {result.status}')
    print(f'Bars utilization: {result.bars_coverage.utilization:.2%}')
    print(f'Features utilization: {result.features_coverage.utilization:.2%}')

asyncio.run(check())
"
```

#### 问题: features_table 参数未传递

```bash
# 检查诊断调用
grep -r "diagnose_oof_failure" scripts/ tools/ --include="*.py" | grep -v "features_table"

# 确认所有调用点都传递了 features_table
# 应该类似:
venv/bin/python tools/diagnose_oof_failure.py \
    --job-id 20251202-abc \
    --symbol TSLA \
    --start "2025-11-01 09:30:00" \
    --end "2025-11-30 16:00:00" \
    --features-table trading.features_1m_active \
    --auc 0.52 \
    --ic 0.01 \
    --n-train 5000 \
    --horizon-min 30
```

---

## 五、开发与测试

### 5.1 单元测试

```bash
# 测试 DataReadinessChecker
pytest tests/unit/test_data_readiness_checker.py -v

# 测试 ActiveTableSyncManager
pytest tests/unit/test_sync_features_active.py -v
```

### 5.2 集成测试

```bash
# 端到端测试: 同步 → 校验 → 训练
pytest tests/integration/test_data_readiness_flow.py -v

# 模拟 Active 表滞后场景
venv/bin/python tests/integration/simulate_active_lag.py
```

### 5.3 添加新 Symbol

```bash
# 1. 更新 Cron 脚本
vim ./infra/cron/sync_features_active_daily.sh
# 修改 SYMBOLS="TSLA,AAPL,NVDA,GOOGL,新SYMBOL"

# 2. 手动同步历史数据 (假设需要 90 天)
venv/bin/python tools/sync_features_active.py \
    --symbols 新SYMBOL \
    --start 2025-09-01 \
    --end 2025-11-30 \
    --validate \
    --batch-days 7

# 3. 验证同步
clickhouse-client --query "
  SELECT
    symbol,
    min(toDate(ts)) as earliest,
    max(toDate(ts)) as latest,
    count() as rows
  FROM trading.features_1m_active
  WHERE symbol='新SYMBOL'
  GROUP BY symbol
"
```

---

## 六、性能与优化

### 6.1 ClickHouse 优化

```sql
-- 1. 确保分区剪枝生效
EXPLAIN indexes = 1
SELECT *
FROM trading.features_1m_active
WHERE symbol = 'TSLA'
  AND toDate(ts) >= '2025-11-01'
  AND toDate(ts) < '2025-12-01';

-- 2. 检查 active 表压缩率
SELECT
    table,
    formatReadableSize(sum(bytes)) as size,
    sum(rows) as rows,
    round(sum(bytes) / sum(rows), 2) as bytes_per_row
FROM system.parts
WHERE database = 'trading'
  AND table IN ('features_1m', 'features_1m_active')
  AND active
GROUP BY table;
```

### 6.2 Python 优化

- **批次同步**: 使用 `--batch-days 7` 避免单次查询 OOM
- **异步并发**: `DataReadinessChecker` 使用 `asyncio` 并发检查多表
- **连接池**: 复用 ClickHouse 连接,避免频繁建连

---

## 七、Roadmap

| 功能 | 优先级 | 状态 | 预计完成 |
|------|--------|------|---------|
| DataReadinessChecker | P0 | ✅ 已完成 | 2025-12-02 |
| ActiveTableSyncManager | P0 | ✅ 已完成 | 2025-12-02 |
| Cron 定时同步 | P0 | ✅ 已完成 | 2025-12-02 |
| AdaptiveWindowManager | P1 | 🟡 进行中 | 2025-12-05 |
| 诊断自修复钩子 | P1 | 🟡 进行中 | 2025-12-06 |
| 特征表健康评分 | P2 | 📋 计划中 | 2025-12-10 |
| Grafana Dashboard | P2 | 📋 计划中 | 2025-12-15 |
| Brain 决策树硬约束 | P3 | 📋 计划中 | 2025-12-20 |

---

## 八、参考资料

- **CLAUDE.md**: 代码修复规则与生产就绪清单
- **SFS_QUICK_REFERENCE.md**: SFS 系统快速参考
- **preflight.py**: 训练前预检实现
- **ml_scheduler.py**: ML 任务调度器

---

**最后更新**: 2025-12-02
**维护者**: Trading AI Team
**版本**: 1.0
