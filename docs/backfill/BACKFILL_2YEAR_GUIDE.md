# 2年历史数据回补指南

> 安全、分段、可恢复的大规模历史数据回补方案
>
> **目标**: 回补2年（~730天）的1分钟 bars 到基础表 `trading.bars_1m_ibkr`
>
> **原则**: 不污染在线表 `features_1m_active`（7天TTL），历史训练用 `features_1m`

---

## 一、方案对比

### 方案 A：ML 调度器 + bulk_backfill（推荐）

**优点**:
- ✅ 后台运行（不需要保持终端连接）
- ✅ 自动分月执行（每月一个作业，天然分段）
- ✅ 优先级队列管理（可与其他 ML 作业协调）
- ✅ 失败自动重试（调度器会重新入队失败作业）
- ✅ 完整日志记录（`runs/ai_trader/*/ml_events.jsonl`）
- ✅ 进度可追踪（通过 `ml_jobs.jsonl` 查看队列状态）

**缺点**:
- ⚠️ 依赖 BackfillService 运行（需要 Kafka + IBKR 连接）
- ⚠️ 单槽执行（一次只能跑一个 ML 作业，其他作业会排队）

**适用场景**:
- 生产环境长时间回补
- 需要与 OOF/HPO 等 ML 作业协调
- 希望无人值守自动完成

---

### 方案 B：load_hist_multi.py 直接执行

**优点**:
- ✅ 简单直接（一条命令即可启动）
- ✅ 不依赖后台服务（直接连 IBKR）
- ✅ 更灵活（可自定义分段策略、重试逻辑）
- ✅ 实时反馈（终端可见所有日志）

**缺点**:
- ❌ 需要保持终端连接（或使用 tmux/screen）
- ❌ 手动管理分段（需要编写循环脚本）
- ❌ 失败需手动重试（除非自己写重试逻辑）
- ❌ 并发控制复杂（多进程回补需手动协调）

**适用场景**:
- 快速验证（回补少量数据测试）
- 离线环境（没有 Kafka/调度器）
- 需要自定义控制流程

---

## 二、推荐执行流程（方案 A）

### Step 1: 验证前置条件

```bash
# 1. 检查 BackfillService 是否运行
ps aux | grep backfill

# 2. 检查 IBKR 连接（确保 IB Gateway/TWS 运行）
python temp/check_ibkr_status.py

# 3. 检查 Kafka 连接
docker ps | grep kafka

# 4. 检查 ClickHouse 空间（2年数据约需 50-100GB）
clickhouse-client --query "
SELECT
    database,
    formatReadableSize(sum(bytes)) AS size
FROM system.parts
WHERE database = 'trading'
GROUP BY database
"

# 5. 检查 ML 调度器是否运行
ps aux | grep ml_scheduler
```

---

### Step 2: 使用辅助脚本入队回补作业

创建并运行以下脚本（已提供 `scripts/backfill/enqueue_2year_backfill.py`）:

```bash
python scripts/backfill/enqueue_2year_backfill.py \
  --symbols TSLA AAPL NVDA \
  --start-date 2023-11-01 \
  --end-date 2025-11-01 \
  --bar-size 1m \
  --priority 70
```

**参数说明**:
- `--symbols`: 股票列表（建议先测试1-2个，确认无误后再全量）
- `--start-date/--end-date`: 回补时间范围（2年约730天）
- `--bar-size`: Bar 大小（1m/5m/10m/15m/30m/1h/1d）
- `--priority`: 优先级（70=bulk_backfill 默认优先级）

**脚本会做什么**:
1. 按月切分时间范围（2023-11 → 2023-12 → ... → 2025-11）
2. 为每个月生成一个 `bulk_backfill` 作业
3. 调用 `enqueue_job()` 入队到 ML 调度器
4. 输出预估完成时间和监控命令

---

### Step 3: 监控回补进度

```bash
# 查看 ML 作业队列（实时状态）
tail -f runs/ai_trader/queue/ml_jobs.jsonl | jq 'select(.job_type=="bulk_backfill")'

# 查看 ML 事件流（完成/失败通知）
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_events.jsonl | jq 'select(.job_type=="bulk_backfill")'

# 查看 BackfillService 日志（底层执行）
tail -f logs/trading_system.log | grep "backfill"

# 查看 ClickHouse 数据量增长
watch -n 60 'clickhouse-client --query "
SELECT
    symbol,
    count() AS bars,
    min(ts_start) AS earliest,
    max(ts_start) AS latest
FROM trading.bars_1m_ibkr
WHERE symbol IN ('TSLA', 'AAPL', 'NVDA')
GROUP BY symbol
FORMAT Pretty
"'
```

---

### Step 4: 验证数据完整性

```bash
# 1. 检查时间连续性（是否有缺口）
clickhouse-client --query "
WITH
    data AS (
        SELECT
            symbol,
            ts_start,
            lagInFrame(ts_start) OVER (PARTITION BY symbol ORDER BY ts_start) AS prev_ts
        FROM trading.bars_1m_ibkr
        WHERE symbol = 'TSLA'
          AND ts_start >= '2023-11-01'
          AND ts_start < '2025-11-01'
    )
SELECT
    symbol,
    count() AS gaps
FROM data
WHERE dateDiff('minute', prev_ts, ts_start) > 1
GROUP BY symbol
FORMAT Pretty
"

# 2. 检查每日数据量（正常应该 ~390 bars/天，RTH 6.5小时）
clickhouse-client --query "
SELECT
    symbol,
    toDate(ts_start) AS date,
    count() AS bars
FROM trading.bars_1m_ibkr
WHERE symbol = 'TSLA'
  AND ts_start >= '2023-11-01'
  AND ts_start < '2025-11-01'
GROUP BY symbol, date
ORDER BY date DESC
LIMIT 30
FORMAT Pretty
"

# 3. 对比数据源质量（is_backfill=1 的比例）
clickhouse-client --query "
SELECT
    symbol,
    countIf(is_backfill = 1) AS backfill_bars,
    count() AS total_bars,
    round(backfill_bars / total_bars * 100, 2) AS backfill_pct
FROM trading.bars_1m_ibkr
WHERE symbol IN ('TSLA', 'AAPL', 'NVDA')
  AND ts_start >= '2023-11-01'
GROUP BY symbol
FORMAT Pretty
"
```

---

### Step 5: 生成特征（回补完成后）

```bash
# 方案 5.1: 使用 ML 调度器入队 feature_build 作业
python -c "
from scripts.ai_trader.ml_scheduler import enqueue_job

enqueue_job({
    'job_type': 'feature_build',
    'priority': 90,
    'params': {
        'catalog': 'config/features_catalog.json',
        'start_date': '2023-11-01',
        'end_date': '2025-11-01',
        'symbols': ['TSLA', 'AAPL', 'NVDA']
    }
})
print('✅ Feature build job enqueued')
"

# 方案 5.2: 直接运行 feature_build 工具（如果有的话）
python tools/feature_build.py \
  --catalog config/features_catalog.json \
  --start 2023-11-01 \
  --end 2025-11-01 \
  --symbols TSLA AAPL NVDA

# 方案 5.3: 手动 SQL 计算特征（如果没有工具）
# 参考现有 MV 定义，写一次性 INSERT INTO features_1m SELECT ... FROM bars_1m_ibkr
```

---

### Step 6: 最终验证

```bash
# 1. 验证 features_1m 数据量
clickhouse-client --query "
SELECT
    symbol,
    count() AS feature_rows,
    min(ts_start) AS earliest,
    max(ts_start) AS latest
FROM trading.features_1m
WHERE symbol IN ('TSLA', 'AAPL', 'NVDA')
  AND ts_start >= '2023-11-01'
GROUP BY symbol
FORMAT Pretty
"

# 2. 快速回测验证数据可用性
python tools/nightly_oof_flow.py \
  --symbols TSLA \
  --start "2024-01-01 09:30:00" \
  --end "2024-01-31 16:00:00" \
  --window-days 30 \
  --features-table-override trading.features_1m \
  --quick

# 3. 检查特征完整性（NULL 值比例）
clickhouse-client --query "
SELECT
    symbol,
    countIf(rsi IS NULL) / count() AS rsi_null_pct,
    countIf(volume IS NULL) / count() AS vol_null_pct
FROM trading.features_1m
WHERE symbol = 'TSLA'
  AND ts_start >= '2023-11-01'
GROUP BY symbol
FORMAT Pretty
"
```

---

## 三、备用方案（方案 B）：直接执行

如果调度器/BackfillService 不可用，可使用以下脚本直接回补：

```bash
# 创建并运行批量回补脚本
cat > /tmp/backfill_2year.sh <<'SCRIPT'
#!/bin/bash
set -e

SYMBOLS=("TSLA" "AAPL" "NVDA")
START_DATE="2023-11-01"
END_DATE="2025-11-01"
BAR_SIZE="1m"
CHUNK_DAYS=30  # 每次回补30天（安全上限）

# 计算总天数
start_ts=$(date -d "$START_DATE" +%s)
end_ts=$(date -d "$END_DATE" +%s)
total_days=$(( (end_ts - start_ts) / 86400 ))

echo "========================================"
echo "  2年历史数据回补"
echo "========================================"
echo "股票: ${SYMBOLS[@]}"
echo "时间范围: $START_DATE ~ $END_DATE ($total_days 天)"
echo "Bar 大小: $BAR_SIZE"
echo "分片大小: $CHUNK_DAYS 天"
echo "========================================"
echo ""

# 分段回补
current=$(date -d "$START_DATE" +%s)
chunk_num=1
while [ $current -lt $end_ts ]; do
    chunk_start=$(date -d "@$current" +%Y-%m-%d)
    chunk_end_ts=$((current + CHUNK_DAYS * 86400))
    [ $chunk_end_ts -gt $end_ts ] && chunk_end_ts=$end_ts
    chunk_end=$(date -d "@$chunk_end_ts" +%Y-%m-%d)

    echo "🔄 Chunk $chunk_num: $chunk_start ~ $chunk_end"

    # 回补当前分片
    python tool/load_hist_multi.py \
        --symbols ${SYMBOLS[@]} \
        --days $CHUNK_DAYS \
        --bar-size $BAR_SIZE \
        --fill-mode backfill \
        --chunk-days 5

    chunk_num=$((chunk_num + 1))
    current=$chunk_end_ts

    # 速率控制（避免 IBKR pacing violation）
    echo "⏸️  Sleeping 10s..."
    sleep 10
done

echo ""
echo "✅ 回补完成！"
echo "📊 验证命令: clickhouse-client --query \"SELECT symbol, count() FROM trading.bars_1m_ibkr WHERE symbol IN ('TSLA','AAPL','NVDA') GROUP BY symbol\""
SCRIPT

chmod +x /tmp/backfill_2year.sh

# 在 tmux 中运行（推荐）
tmux new-session -d -s backfill "/tmp/backfill_2year.sh"
echo "✅ 回补任务已在 tmux session 'backfill' 中启动"
echo "📺 查看进度: tmux attach -t backfill"
echo "🔌 分离会话: Ctrl+B 然后按 D"
```

---

## 四、常见问题 FAQ

### Q1: 回补会影响在线交易吗？

**A**: 不会。原因：
1. `bars_1m_ibkr` 是基础表，在线策略读取 Redis/SFS（实时流）
2. `features_1m` 是历史训练表，在线策略读取 `features_1m_active`（7天TTL）
3. BackfillService 有独立的 IBKR client_id（不冲突）
4. ClickHouse 写入是异步的，不阻塞查询

**注意**: 如果回补期间运行 OOF，可能会读到"正在写入的"历史数据，建议：
- 优先级设置为 70（低于 OOF 的 100）
- 或者在非交易时段回补

---

### Q2: 如何恢复中断的回补？

**A**:
- **方案 A（调度器）**: 自动恢复。调度器会重新入队失败作业
- **方案 B（直接执行）**: 手动恢复
  ```bash
  # 查询已回补的最新时间
  clickhouse-client --query "SELECT max(ts_start) FROM trading.bars_1m_ibkr WHERE symbol='TSLA'"

  # 从最新时间继续（使用 forward 模式）
  python tool/load_hist_multi.py \
    --symbols TSLA \
    --days 365 \
    --bar-size 1m \
    --fill-mode forward
  ```

---

### Q3: IBKR API 限流怎么办？

**A**:
1. **Pacing violation (错误 162)**:
   - 降低 `--chunk-days` 参数（默认30天 → 改为5天）
   - 增加 `pacing_sleep_sec`（load_hist_multi.py:158 行）
   - 等待 10 分钟后重试

2. **Historical data farm connection broken (错误 1102)**:
   - 检查 IBKR Gateway 状态
   - 重启 Gateway/TWS
   - 减少并发（单线程回补）

3. **Request limit exceeded**:
   - IBKR 每 10 分钟最多 60 个历史数据请求
   - 单 symbol 回补 2 年（~730 天，30天/次）= 25 个请求
   - 多 symbol 并发时需控制速率：`sleep 10` between symbols

---

### Q4: 磁盘空间不足怎么办？

**A**:
```bash
# 1. 检查当前空间
df -h /workspace/Documents/clickhouse_data

# 2. 清理过期数据（根据 TTL 策略）
clickhouse-client --query "OPTIMIZE TABLE trading.bars_1m_ibkr FINAL"

# 3. 删除不需要的旧数据（谨慎！）
clickhouse-client --query "
ALTER TABLE trading.bars_1m_ibkr
DELETE WHERE ts_start < '2022-01-01'
"

# 4. 调整 TTL（如果需要）
# 参考 infra/clickhouse-config/config.xml
```

---

### Q5: 如何加速回补？

**A**:
1. **多进程并发**（推荐）:
   ```bash
   # 每个 symbol 一个进程
   for sym in TSLA AAPL NVDA; do
       python tool/load_hist_multi.py \
           --symbols $sym \
           --days 730 \
           --bar-size 1m \
           --fill-mode backfill \
           --chunk-days 30 &
   done
   wait
   ```

2. **增大 chunk_days**（风险：可能触发 pacing）:
   ```bash
   # 1m bars: 30天 → 60天（需测试 IBKR 是否允许）
   --chunk-days 60
   ```

3. **使用更快的 bar size**（然后聚合）:
   ```bash
   # 回补 5m bars（更快），然后降采样到 1m
   python tool/load_hist_multi.py --bar-size 5m --days 730
   # TODO: 编写降采样脚本
   ```

---

## 五、预估时间与成本

### 时间预估

**单 Symbol 回补 2 年（1m bars）**:
- 总天数: 730 天（日历天）
- 交易日: ~500 天（扣除周末/节假日）
- 每次请求: 30 天（分片）
- 总请求数: 730 / 30 ≈ 25 次
- 单次耗时: ~3-5 秒（含 pacing_sleep_sec=1.2）
- **总耗时: ~2-3 分钟/symbol**

**3 Symbols 并发**:
- 串行: 3 × 3 分钟 = 9 分钟
- 并行: 3 分钟（如果 IBKR 允许并发）

**24 个月分片（调度器方式）**:
- 每月 1 个作业
- 单作业耗时: ~10-15 秒（每月 ~21 交易日）
- **总耗时: 24 × 15秒 = 6 分钟/symbol**
- **如果 ML 槽繁忙**: 可能需要等待队列（取决于其他作业）

---

### 成本预估（ClickHouse 存储）

**单 Symbol 1m bars**:
- 每天 ~390 bars（RTH 6.5小时 × 60分钟）
- 每 bar ~100 bytes（压缩后）
- 每天: 390 × 100B ≈ 39 KB
- 2 年: 730 × 39 KB ≈ **28 MB/symbol**

**10 Symbols**:
- 10 × 28 MB = **280 MB**

**实际可能更多**:
- 包含 ver, is_backfill, metadata 等列
- ReplacingMergeTree 可能有重复版本（未 OPTIMIZE 前）
- **预估: 500 MB - 1 GB（10 symbols, 2年）**

---

## 六、最佳实践总结

✅ **DO（推荐做法）**:
1. **先小范围测试**: 回补 1 个 symbol + 30 天，验证流程
2. **使用调度器**: 长时间回补优先用 `bulk_backfill` 作业
3. **监控日志**: 实时查看 `ml_events.jsonl` 和 `trading_system.log`
4. **验证数据**: 每次回补后运行完整性检查 SQL
5. **分段执行**: 按月/周分片，避免单次任务过大
6. **速率控制**: 遵守 IBKR pacing 限制（1.2秒/请求）

❌ **DON'T（避免做法）**:
1. ❌ 不要一次性回补所有 symbol × 2年（容易失败）
2. ❌ 不要在交易时段回补（可能影响实时系统）
3. ❌ 不要跳过验证步骤（数据错误难以修复）
4. ❌ 不要修改 TTL 而不评估存储（磁盘爆满）
5. ❌ 不要直接写入 `features_1m_active`（会破坏在线数据）
6. ❌ 不要使用 `docker-compose down -v`（会删除所有数据！）

---

## 七、紧急情况处理

### 场景 1: BackfillService 崩溃

```bash
# 1. 检查进程状态
ps aux | grep backfill

# 2. 查看崩溃日志
tail -100 logs/trading_system.log | grep "backfill"

# 3. 重启服务（如果是独立进程）
python scripts/run_backfill_service.py &

# 4. 或重启整个系统
./run_system.py
```

---

### 场景 2: ClickHouse 写入失败

```bash
# 1. 检查 ClickHouse 状态
docker ps | grep clickhouse

# 2. 检查磁盘空间
df -h /workspace/Documents/clickhouse_data

# 3. 检查表结构是否匹配
clickhouse-client --query "DESCRIBE TABLE trading.bars_1m_ibkr"

# 4. 查看 ClickHouse 错误日志
docker logs clickhouse 2>&1 | tail -100
```

---

### 场景 3: IBKR 连接断开

```bash
# 1. 检查 Gateway 状态
# 打开浏览器访问: http://localhost:5000 (IB Gateway web interface)

# 2. 重启 Gateway（如果需要）
# 手动操作 IB Gateway/TWS

# 3. 重新连接测试
python temp/check_ibkr_status.py

# 4. 恢复回补（调度器会自动重试）
# 或手动重新入队失败的作业
```

---

## 八、附录

### A. 辅助脚本路径

- **入队脚本**: `scripts/backfill/enqueue_2year_backfill.py`
- **监控脚本**: `scripts/backfill/monitor_backfill.py`
- **验证脚本**: `scripts/backfill/verify_backfill.py`

### B. 相关文档

- 回补服务设计: `services/market/backfill_service.py`
- ML 调度器文档: `scripts/ai_trader/ml_scheduler.py`
- ClickHouse 备份: `docs/CLICKHOUSE_BACKUP_SETUP.md`
- SFS 架构: `docs/SFS_QUICK_REFERENCE.md`

### C. 监控 Grafana Dashboard

如果配置了 Grafana（根目录 docker-compose.yml 已废弃，需手动配置）:

- **Backfill Progress**: 回补进度（bars/小时）
- **IBKR Request Rate**: API 请求速率（防止限流）
- **ClickHouse Write Latency**: 写入延迟（监控性能）

---

**最后更新**: 2025-11-07
**维护者**: Trading Team
**关联**: CLAUDE.md, SFS_QUICK_REFERENCE.md
