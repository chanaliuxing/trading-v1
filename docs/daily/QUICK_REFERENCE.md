# 每日运维快速参考卡

> 快速查询常用命令，打印此页面贴在桌面
>
> **工作目录**: `.`

---

## ⚡ 一键脚本

```bash
cd ./docs/daily

# 🔍 系统健康检查（生成 JSON 报告）⭐ 新增
./system_health_check.py
# 报告保存到: ./logs/system_health.json

# 🌅 盘前检查（每日 07:00-09:00）
./morning_check.sh

# 🌆 盘后复盘（每日 16:30-20:00）
./evening_review.sh

# 🔄 手动触发数据回补
./trigger_backfill.py
./trigger_backfill.py --date 2025-10-26
./trigger_backfill.py --symbols AAPL TSLA
```

---

## 🚨 紧急修复

### IBKR 断开
```bash
pkill -f run_system.py
cd .
nohup python run_system.py > logs/run_system.log 2>&1 &
```

### 基础设施重启
```bash
cd ./infra
docker-compose restart kafka    # 单个服务
docker-compose down && docker-compose up -d  # 全部重启
```

### 查看错误日志
```bash
tail -100 ./logs/trading_system.log | grep ERROR
docker logs trading_kafka --tail 100
```

---

## 📊 数据检查

### 实时 Tick 数据
```bash
clickhouse-client --query "
SELECT symbol, count() as ticks, max(ts_recv) as last
FROM lake.ticks WHERE ts_recv >= now() - INTERVAL 10 MINUTE
GROUP BY symbol FORMAT Pretty"
```

### 昨日 Bar 完整性
```bash
clickhouse-client --query "
SELECT symbol, count() as bars FROM trading.bars_1m
WHERE toDate(ts_end) = today() - 1
GROUP BY symbol FORMAT Pretty"
```

### Redis SFS 数据
```bash
redis-cli LRANGE "sfs:1s:AAPL" 0 5
```

### 批量加载历史数据（首次设置或大量补数据）
```bash
cd .

# 加载 200 个交易日的历史数据
python tool/load_hist_multi.py \
  --symbols AAPL \
  --bar-size 1m \
  --sessions 200 \
  --fill-mode backfill \
  --chunk-days 7

# 多个股票
python tool/load_hist_multi.py \
  --symbols AAPL TSLA NVDA GOOGL MSFT \
  --bar-size 1m \
  --sessions 100 \
  --fill-mode backfill \
  --chunk-days 7
```

### 抓取 Google News（盘前分析用）
```bash
python ./llm_trader/scripts/googleNews.py
```

---

## 🤖 LLM Trader

### 启动盘前交易 (04:00-09:30)
```bash
cd ./llm_trader

# 方法 1: 使用预设脚本
./run_premarket.sh

# 方法 2: 手动指定参数（更灵活）
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 33 \
  --close-at "09:35" \
  --forbid-new-positions-before 9 \
  --wait-for-premarket
```

### 启动盘中交易 (09:30-16:00)
```bash
# 开盘第一小时交易（快速策略）
python run_single_bot.py \
  --provider deepseek \
  --symbols TSLA \
  --interval 60 \
  --wait-for-market \
  --start-trading-at "9:40" \
  --stop-trading-at "10:30" \
  --close-at "10:45"

# 全天交易（日内交易）
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL NVDA \
  --interval 600 \
  --cycles 33 \
  --close-at "15:59" \
  --wait-for-market
```

### 启动盘后交易 (16:00-20:00)
```bash
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --wait-for-afterhours
```

### 启动隔夜交易 (20:00-次日04:00)
```bash
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 50 \
  --wait-for-overnight \
  --close-at "03:50" \
  --forbid-new-positions-before 5
```

### 查看实时决策
```bash
clickhouse-client --query "
SELECT formatDateTime(ts, '%H:%M:%S') as time, symbol, provider, decision
FROM llm_trader.decisions WHERE ts >= now() - INTERVAL 1 HOUR
ORDER BY ts DESC LIMIT 10 FORMAT Pretty"
```

---

## 🔧 Alpha Loop

### 快速优化（每日）
```bash
cd .

# 方法 1: 简单模式（XGBoost）
python alpha_loop.py \
  --model xgboost \
  --symbols AAPL TSLA NVDA \
  --lookback-days 7 \
  --n-trials 20 \
  --output results/alpha_loop_quick_$(date +%Y%m%d).json

# 方法 2: 协作优化（Optuna + LLM + ML）- 更强大
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider coop_optuna \
  --gens 2 \
  --out_dir alpha_out/$(date +%Y%m%d) \
  --k-top 10
```

### 应用新参数
```bash
python apply_hpo_params.py \
  --results results/alpha_loop_quick_$(date +%Y%m%d).json \
  --config config.yml \
  --backup

python hot_reload_test_params.py
```

### 查看优化结果
```bash
# 查看最佳参数
cat alpha_out/$(date +%Y%m%d)/best_params.json

# 查看 LLM 建议
cat alpha_out/$(date +%Y%m%d)/gen_01/llm_refine.json

# 查看合并后的网格
cat alpha_out/$(date +%Y%m%d)/gen_01/coop_merged.json
```

---

## 🔍 监控

### IBKR 连接状态
```bash
python ./temp/check_ibkr_status.py
```

### 系统进程
```bash
ps aux | grep -E "(run_system|llm_trader)"
```

### Docker 容器
```bash
cd ./infra
docker-compose ps
```

### Kafka Consumer Lag
```bash
docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --describe --all-groups
```

---

## 💾 备份与恢复

### ClickHouse 完整备份
```bash
cd .
make recon-all

# 备份会保存到 backup/ 目录
# 命名格式: full_backup_YYYYMMDD_HHMMSS
```

### PostgreSQL 备份（每日自动 16:01 运行）
```bash
# 手动触发备份
pg_dump -h localhost -U postgres -d test > backup/pg/manual_$(date +%Y%m%d_%H%M%S).sql
```

---

## 📈 今日统计

### 交易表现
```bash
clickhouse-client --query "
SELECT
  count() as trades,
  sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  round(sum(pnl), 2) as total_pnl
FROM llm_trader.decisions
WHERE toDate(ts) = today() AND executed = 1
FORMAT Pretty"
```

### 数据量统计
```bash
clickhouse-client --query "
SELECT
  'Ticks' as type, count() as count FROM lake.ticks WHERE toDate(ts_recv) = today()
UNION ALL
SELECT 'Bars', count() FROM trading.bars_1m WHERE toDate(ts_end) = today()
FORMAT Pretty"
```

---

## 🕐 每日时间表

| 时间 | 任务 | 命令 |
|------|------|------|
| **07:00** | 盘前检查 | `./docs/daily/morning_check.sh` |
| **07:30** | 启动盘前 LLM | `cd llm_trader && ./run_premarket.sh` |
| **09:00** | 开盘前检查 | `python temp/check_ibkr_status.py` |
| **09:30** | 启动盘中 LLM | `cd llm_trader && ./run_all_providers.sh market` |
| **16:30** | 盘后复盘 | `./docs/daily/evening_review.sh` |
| **17:00** | Alpha Loop | `python alpha_loop.py ...` |
| **18:00** | 更新参数 | `python apply_hpo_params.py ...` |
| **20:00** | 启动隔夜 LLM | `nohup python run_single_bot.py --trading-hours extended &` |

---

## 📞 故障排查

### 问题：无实时数据
1. 检查 IBKR 连接：`python temp/check_ibkr_status.py`
2. 检查 run_system.py：`ps aux | grep run_system.py`
3. 查看日志：`tail -50 logs/trading_system.log`
4. 重启：`pkill -f run_system.py && nohup python run_system.py > logs/run_system.log 2>&1 &`

### 问题：数据缺失
1. 检查 Backfill 任务：`clickhouse-client --query "SELECT * FROM trading.backfill_tasks ORDER BY created_at DESC LIMIT 10 FORMAT Pretty"`
2. 手动触发：`./docs/daily/trigger_backfill.py --date 2025-10-26`
3. 等待 5 分钟后验证

### 问题：LLM Trader 无决策
1. 检查进程：`ps aux | grep llm_trader`
2. 查看日志：`tail -50 llm_trader/logs/llm_trader_$(date +%Y%m%d).log`
3. 检查 API Key：`env | grep -E "(GEMINI|NVIDIA|DEEPSEEK)"`
4. 重启：重新运行启动脚本

### 问题：Kafka 错误
1. 查看日志：`docker logs trading_kafka --tail 100`
2. 检查 Consumer Lag：`docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --all-groups`
3. 重启：`cd infra && docker-compose restart kafka`

---

## 📚 完整文档

- **详细清单**: `./docs/daily/DAILY_OPERATIONS_CHECKLIST.md`
- **IBKR 容错分析**: `./temp/ibkr_connection_resilience_analysis.md`
- **LLM Trader 文档**: `./llm_trader/HOW_TO_USE.md`
- **代码修复规则**: `./CLAUDE.md`

---

**最后更新**: 2025-10-27
**打印此页面贴在桌面，方便日常使用！**
