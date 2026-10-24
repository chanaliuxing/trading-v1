# 历史数据回补文档

> 安全、高效、可恢复的历史数据回补方案与工具

---

## 📚 文档索引

### 🚀 [Quick Start](./QUICK_START.md) - **从这里开始！**
5分钟快速上手，最常用的命令和流程。

**适合**:
- 第一次使用回补功能
- 需要快速参考命令
- 不想读长篇文档

---

### 📖 [完整指南](./BACKFILL_2YEAR_GUIDE.md) - **详细文档**
2年历史数据回补的完整指南，包含：
- 方案对比与选择
- 详细执行流程
- 监控与验证
- 故障排查 FAQ
- 最佳实践

**适合**:
- 生产环境部署
- 需要深入理解原理
- 遇到复杂问题

---

## 🛠️ 工具脚本

位于 `scripts/backfill/`:

### 1. `enqueue_2year_backfill.py` - 入队脚本
将2年回补任务按月切分，入队到 ML 调度器。

```bash
python scripts/backfill/enqueue_2year_backfill.py \
  --symbols TSLA AAPL NVDA \
  --start-date 2023-11-01 \
  --end-date 2025-11-01 \
  --bar-size 1m
```

**特性**:
- ✅ 自动按月切分（避免单次任务过大）
- ✅ 支持 `--use-universe`（使用配置的 universe）
- ✅ 支持 `--dry-run`（预览不执行）
- ✅ 输出预估完成时间和监控命令

---

### 2. `monitor_backfill.py` - 监控脚本
实时监控回补进度和 ClickHouse 数据增长。

```bash
python scripts/backfill/monitor_backfill.py \
  --symbols TSLA AAPL NVDA \
  --interval 30
```

**特性**:
- ✅ 显示作业队列状态（queued/running/completed/failed）
- ✅ 显示 ClickHouse 数据量实时增长
- ✅ 显示进度条
- ✅ 检测异常作业

---

### 3. `verify_backfill.py` - 验证脚本
验证回补数据的完整性和质量。

```bash
python scripts/backfill/verify_backfill.py \
  --symbols TSLA AAPL NVDA \
  --start-date 2023-11-01 \
  --end-date 2025-11-01 \
  --report report.json
```

**特性**:
- ✅ 检查数据覆盖率（bars/天）
- ✅ 检测时间缺口
- ✅ 验证数据质量（VWAP 缺失、零成交量）
- ✅ 生成 JSON 报告

---

## 📊 核心组件

### 1. BackfillService (`services/market/backfill_service.py`)
后台服务，监听 Kafka gap 事件并自动回补。

**职责**:
- 监听 `md.gap.v1` 和 `md.backfill.request.v1` topic
- 从 IBKR 拉取历史数据
- 写入 Kafka `md.bar.5s.v2` topic
- 最终落地到 ClickHouse

**配置**:
```yaml
backfill:
  instances:
    - name: "backfill-1m"
      bar_size: "1 min"
      request_topic: "md.backfill.request.1m.v1"
```

---

### 2. load_hist_multi.py (`tool/load_hist_multi.py`)
直接从 IBKR 拉取数据并写入 ClickHouse（不经过 Kafka）。

**特性**:
- 支持多种 bar size（1m/5m/1h/1d）
- 支持两种模式：
  - `forward`: 续传（从 DB 最后时间开始）
  - `backfill`: 全量覆盖（忽略 DB 最后时间）
- 自动分片（避免 IBKR pacing violation）
- 幂等写入（ReplacingMergeTree）

**用法**:
```bash
python tool/load_hist_multi.py \
  --symbols TSLA \
  --days 730 \
  --bar-size 1m \
  --fill-mode backfill \
  --chunk-days 30
```

---

### 3. ML 调度器 (`scripts/ai_trader/ml_scheduler.py`)
单槽 ML 任务调度器，支持 `bulk_backfill` 作业类型。

**特性**:
- 优先级队列（OOF > alpha_loop > bulk_backfill）
- 单槽执行（避免资源竞争）
- 自动重试失败作业
- 持久化队列（JSONL + Kafka）

**用法**:
```python
from scripts.ai_trader.ml_scheduler import enqueue_job

enqueue_job({
    "job_type": "bulk_backfill",
    "priority": 70,
    "params": {
        "symbols": ["TSLA"],
        "start_date": "2023-11-01",
        "end_date": "2023-11-30",
        "bar_size": "1m"
    }
})
```

---

## 🎯 使用场景

### 场景 1: 首次部署（回补2年历史数据）
**推荐**: 方案 A（ML 调度器）

```bash
# 1. 入队
python scripts/backfill/enqueue_2year_backfill.py \
  --use-universe \
  --start-date 2023-11-01 \
  --end-date 2025-11-01

# 2. 监控
python scripts/backfill/monitor_backfill.py

# 3. 验证
python scripts/backfill/verify_backfill.py \
  --use-universe \
  --start-date 2023-11-01 \
  --end-date 2025-11-01
```

---

### 场景 2: 每日增量回补
**推荐**: BackfillService（自动）或 load_hist_multi.py（手动）

```bash
# 自动（BackfillService 监听 gap 事件）
# 无需手动操作

# 手动增量
python tool/load_hist_multi.py \
  --symbols TSLA AAPL NVDA \
  --days 1 \
  --bar-size 1m \
  --fill-mode forward
```

---

### 场景 3: 修复特定日期缺口
**推荐**: trigger_backfill.py（手动触发）

```bash
python docs/daily/trigger_backfill.py \
  --date 2025-10-26 \
  --symbols AAPL \
  --bar-size 1m
```

---

### 场景 4: 快速测试验证
**推荐**: load_hist_multi.py（直接执行）

```bash
# 回补最近30天
python tool/load_hist_multi.py \
  --symbols TSLA \
  --days 30 \
  --bar-size 1m \
  --fill-mode backfill
```

---

## ⚙️ 配置说明

### ClickHouse 表结构

**基础表** (`trading.bars_1m_ibkr`):
```sql
CREATE TABLE trading.bars_1m_ibkr (
    ts_start DateTime,
    ts_end DateTime,
    symbol String,
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    vwap Nullable(Float64),
    volume UInt64,
    trades UInt64,
    bid_last Nullable(Float64),
    ask_last Nullable(Float64),
    mid_last Float64,
    ver UInt64,
    is_backfill UInt8 DEFAULT 0,
    -- 可选元数据列 --
    what_to_show Nullable(String),
    is_rth Nullable(UInt8),
    source Nullable(String),
    exchange Nullable(String),
    currency Nullable(String),
    tz Nullable(String),
    d Nullable(Date),
    minute_of_day Nullable(UInt16),
    ingest_ts Nullable(DateTime)
) ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMM(ts_start)
ORDER BY (symbol, ts_start);
```

**TTL 策略**:
- `bars_1m_ibkr`: 无 TTL（永久保留）
- `features_1m`: 无 TTL（历史训练用）
- `features_1m_active`: 7天 TTL（在线实时用）

---

### IBKR 限流参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `pacing_sleep_sec` | 1.2 | 每次请求间隔（秒） |
| `max_ib_concurrency` | 2 | 最大并发请求数 |
| `chunk_days` | 30 | 单次请求最大天数 |

**IBKR 官方限制**:
- 每 10 分钟最多 60 个历史数据请求
- 1m bars: 最多 30 天/次
- 5m bars: 最多 365 天/次
- 1d bars: 最多 365 天/次

---

## 📈 监控指标

### ClickHouse 查询

```sql
-- 数据量统计
SELECT
    symbol,
    count() AS bars,
    formatReadableSize(sum(length(toString(ts_start)) * 8 + 8*9)) AS approx_size
FROM trading.bars_1m_ibkr
WHERE ts_start >= '2023-11-01'
GROUP BY symbol;

-- 缺口检测
WITH data AS (
    SELECT
        symbol,
        ts_start,
        lagInFrame(ts_start) OVER (PARTITION BY symbol ORDER BY ts_start) AS prev_ts
    FROM trading.bars_1m_ibkr
    WHERE symbol = 'TSLA'
)
SELECT count() AS gaps
FROM data
WHERE dateDiff('minute', prev_ts, ts_start) > 1;

-- 数据质量
SELECT
    symbol,
    countIf(is_backfill = 1) / count() AS backfill_ratio,
    countIf(vwap IS NULL) / count() AS null_vwap_pct
FROM trading.bars_1m_ibkr
WHERE symbol = 'TSLA'
GROUP BY symbol;
```

### Grafana Dashboard（可选）

如果配置了 Grafana:
- **Backfill Progress**: 回补进度（bars/小时）
- **IBKR Request Rate**: API 请求速率
- **ClickHouse Write Latency**: 写入延迟

---

## 🚨 常见问题

### Q1: 回补会影响在线交易吗？
**A**: 不会。回补写入基础表 `bars_1m_ibkr`，在线策略读取 Redis/SFS 实时流。

### Q2: 如何恢复中断的回补？
**A**: 使用 `--fill-mode forward` 从 DB 最后时间续传，或重新入队失败作业。

### Q3: IBKR API 限流怎么办？
**A**: 降低 `--chunk-days` 参数（30 → 5），等待 10 分钟后重试。

### Q4: 磁盘空间不足怎么办？
**A**: 清理过期数据 `OPTIMIZE TABLE ... FINAL`，或调整 TTL 策略。

### Q5: 如何加速回补？
**A**: 多进程并发回补（每个 symbol 一个进程），或增大 `chunk_days`（注意 pacing）。

**更多 FAQ**: 参考 [完整指南](./BACKFILL_2YEAR_GUIDE.md#四常见问题-faq)

---

## 📝 变更日志

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2025-11-07 | 初始版本：完整指南 + 工具脚本 |

---

## 🔗 相关文档

- [CLAUDE.md](../../CLAUDE.md) - 代码修复规则
- [SFS_QUICK_REFERENCE.md](../SFS_QUICK_REFERENCE.md) - SFS 架构
- [CLICKHOUSE_BACKUP_SETUP.md](../CLICKHOUSE_BACKUP_SETUP.md) - 备份恢复

---

**维护者**: Trading Team
**最后更新**: 2025-11-07
**反馈**: 在 `#trading-tech` Slack 频道报告问题
