# 2年回补 Quick Start

> 快速开始2年历史数据回补（5分钟上手）

---

## 方案选择

### 🌟 推荐：方案 A（ML 调度器，后台运行）

适用场景：
- ✅ 生产环境
- ✅ 需要无人值守
- ✅ 需要与其他 ML 作业协调

### ⚡ 快速：方案 B（直接执行）

适用场景：
- ✅ 测试验证
- ✅ 离线环境（无 Kafka）
- ✅ 需要立即看到结果

---

## 方案 A：ML 调度器（推荐）

### Step 1: 入队回补作业（一条命令）

```bash
python scripts/backfill/enqueue_2year_backfill.py \
  --symbols TSLA AAPL NVDA \
  --start-date 2023-11-01 \
  --end-date 2025-11-01 \
  --bar-size 1m
```

**或者使用配置文件中的所有 universe symbols:**

```bash
python scripts/backfill/enqueue_2year_backfill.py \
  --use-universe \
  --start-date 2023-11-01 \
  --end-date 2025-11-01 \
  --bar-size 1m
```

### Step 2: 监控进度

```bash
# 实时监控（每30秒刷新）
python scripts/backfill/monitor_backfill.py --symbols TSLA AAPL NVDA

# 或者查看日志
tail -f runs/ai_trader/queue/ml_jobs.jsonl | jq 'select(.job_type=="bulk_backfill")'
```

### Step 3: 验证数据

```bash
python scripts/backfill/verify_backfill.py \
  --symbols TSLA AAPL NVDA \
  --start-date 2023-11-01 \
  --end-date 2025-11-01
```

**完成！** 🎉

---

## 方案 B：直接执行（快速）

### 单次执行（适合测试）

```bash
# 回补 TSLA 最近180天的 1m bars
python tool/load_hist_multi.py \
  --symbols TSLA \
  --days 180 \
  --bar-size 1m \
  --fill-mode backfill \
  --chunk-days 30
```

### 批量执行（2年）

创建并运行脚本：

```bash
cat > /tmp/backfill_loop.sh <<'EOF'
#!/bin/bash
SYMBOLS="TSLA AAPL NVDA"
for sym in $SYMBOLS; do
  python tool/load_hist_multi.py \
    --symbols $sym \
    --days 730 \
    --bar-size 1m \
    --fill-mode backfill \
    --chunk-days 30
  sleep 10  # 速率控制
done
EOF

chmod +x /tmp/backfill_loop.sh

# 在 tmux 中运行（推荐）
tmux new-session -d -s backfill "/tmp/backfill_loop.sh"
tmux attach -t backfill  # Ctrl+B D 分离
```

---

## 常用监控命令

### 查看 ClickHouse 数据量

```bash
clickhouse-client --query "
SELECT
    symbol,
    count() AS bars,
    min(ts_start) AS earliest,
    max(ts_start) AS latest
FROM trading.bars_1m_ibkr
WHERE symbol IN ('TSLA', 'AAPL', 'NVDA')
  AND ts_start >= '2023-11-01'
GROUP BY symbol
FORMAT Pretty
"
```

### 查看作业队列状态

```bash
# 统计各状态作业数量
jq -s 'group_by(.status) | map({status: .[0].status, count: length})' \
  runs/ai_trader/queue/ml_jobs.jsonl | jq '.[] | select(.status | startswith("bulk_backfill"))'
```

### 检查 BackfillService 状态

```bash
ps aux | grep backfill
tail -f logs/trading_system.log | grep "backfill"
```

---

## 完成后：生成特征

### 方案 1: 使用 ML 调度器

```bash
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
"
```

### 方案 2: 直接运行 OOF

```bash
python tools/nightly_oof_flow.py \
  --symbols TSLA \
  --start "2024-01-01 09:30:00" \
  --end "2024-01-31 16:00:00" \
  --window-days 30 \
  --features-table-override trading.features_1m
```

---

## 故障排查

### Q: 回补失败，怎么办？

```bash
# 1. 检查 IBKR 连接
python temp/check_ibkr_status.py

# 2. 查看错误日志
tail -100 logs/trading_system.log | grep "ERROR"

# 3. 重新入队失败的作业（调度器会自动处理）
# 或手动重新运行 load_hist_multi.py
```

### Q: IBKR API 限流 (错误 162)？

```bash
# 降低 chunk_days 参数
python tool/load_hist_multi.py \
  --symbols TSLA \
  --days 180 \
  --bar-size 1m \
  --chunk-days 5  # 从 30 降到 5

# 等待 10 分钟后重试
sleep 600
```

### Q: ClickHouse 磁盘空间不足？

```bash
# 检查空间
df -h /workspace/Documents/clickhouse_data

# 清理过期数据（根据 TTL）
clickhouse-client --query "OPTIMIZE TABLE trading.bars_1m_ibkr FINAL"
```

---

## 预估时间

| 场景 | 时间 |
|------|------|
| 单 symbol 2年（调度器） | ~6-10 分钟 |
| 单 symbol 2年（直接执行） | ~2-3 分钟 |
| 3 symbols 并发 | ~3-10 分钟 |
| 10 symbols 串行 | ~30-60 分钟 |

---

## 进阶用法

### 并发回补（加速）

```bash
# 每个 symbol 一个进程
for sym in TSLA AAPL NVDA; do
  python tool/load_hist_multi.py \
    --symbols $sym \
    --days 730 \
    --bar-size 1m \
    --fill-mode backfill &
done
wait
```

### 增量回补（续传）

```bash
# 使用 forward 模式（从 DB 最后时间开始）
python tool/load_hist_multi.py \
  --symbols TSLA \
  --days 365 \
  --bar-size 1m \
  --fill-mode forward
```

### 回补其他周期

```bash
# 回补 5 分钟 bars
python tool/load_hist_multi.py \
  --symbols TSLA \
  --days 730 \
  --bar-size 5m \
  --fill-mode backfill

# 回补日线
python tool/load_hist_multi.py \
  --symbols TSLA \
  --days 730 \
  --bar-size 1d \
  --fill-mode backfill
```

---

## 完整流程总结

```bash
# ========== Step 1: 回补 bars ==========
python scripts/backfill/enqueue_2year_backfill.py \
  --symbols TSLA AAPL NVDA \
  --start-date 2023-11-01 \
  --end-date 2025-11-01 \
  --bar-size 1m

# ========== Step 2: 监控 ==========
python scripts/backfill/monitor_backfill.py --symbols TSLA AAPL NVDA

# ========== Step 3: 验证 ==========
python scripts/backfill/verify_backfill.py \
  --symbols TSLA AAPL NVDA \
  --start-date 2023-11-01 \
  --end-date 2025-11-01 \
  --report backfill_report.json

# ========== Step 4: 生成特征 ==========
python tools/nightly_oof_flow.py \
  --symbols TSLA \
  --start "2024-01-01 09:30:00" \
  --end "2024-01-31 16:00:00" \
  --window-days 30 \
  --features-table-override trading.features_1m

# ========== Step 5: 最终验证 ==========
clickhouse-client --query "
SELECT symbol, count() AS feature_rows
FROM trading.features_1m
WHERE symbol IN ('TSLA', 'AAPL', 'NVDA')
  AND ts_start >= '2023-11-01'
GROUP BY symbol
"
```

---

**更多详情**: 参考 [`BACKFILL_2YEAR_GUIDE.md`](./BACKFILL_2YEAR_GUIDE.md)

**最后更新**: 2025-11-07
