# 每日 Action Plan（样例，ET：2025-10-28）

> 说明：本计划以三道门为前置；所有训练/上线必须先读取 `logs/system_health.json` 并通过健康门/数据门/执行门。
> 路径与命令全部取自现有清单与速查卡，可直接复制执行。

---

## 0) 三道门（所有动作前执行）
- 健康门：
  - `cd ./docs/daily && ./system_health_check.py`
  - 快查：`jq -r '.overall_status' ./logs/system_health.json`
  - 门限：LAG<1000；CRITICAL=0；ERROR<20；CPU/内存/磁盘<90%
- 数据门：
  - 近10分钟 Tick：
    `clickhouse-client --query "SELECT symbol,count() FROM lake.ticks WHERE ts_recv>=now()-INTERVAL 10 MINUTE GROUP BY symbol FORMAT Pretty"`
  - 昨日日线 Bar≥390：
    `clickhouse-client --query "SELECT symbol,count() bars FROM trading.bars_1m WHERE toDate(ts_end)=today()-1 GROUP BY symbol FORMAT Pretty"`
  - 如缺：`cd docs/daily && ./trigger_backfill.py --date $(date -v-1d +%Y-%m-%d) --symbols AAPL TSLA NVDA`
- 执行门：
  - IBKR：`python temp/check_ibkr_status.py`
  - 进程：`ps aux | grep -E "(run_system.py|llm_trader)"`
  - 异常重启：`pkill -f run_system.py && nohup python run_system.py > logs/run_system.log 2>&1 &`

若任一门不达标：仅允许回补/排障，禁止训练、回测、上线与开仓。

---

## A. 盘前（07:00–09:30 ET）
1) 07:00 健康/数据/执行门（见 0）
2) 07:10（可选）抓取新闻：`python llm_trader/scripts/googleNews.py`
3) 07:30 启动盘前 llm_trader（10 分钟节奏，9:35 前清仓）
```
cd ./llm_trader
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 33 \
  --close-at "09:35" \
  --forbid-new-positions-before 9 \
  --wait-for-premarket
```
4) 监控
- 日志：`tail -f llm_trader/logs/llm_trader_$(date +%Y%m%d).log`
- 决策快查（最近10条）：
```
clickhouse-client --query "SELECT ts,symbol,provider,decision,reasoning FROM llm_trader.decisions WHERE ts>=today() ORDER BY ts DESC LIMIT 10 FORMAT Pretty"
```

---

## B. 盘中（09:30–16:00 ET）
1) 09:00–09:30 最后门禁复核（见 0）
2) 启动盘中 llm_trader（二选一）
- 第一小时快速：
```
cd ./llm_trader
python run_single_bot.py \
  --provider deepseek \
  --symbols TSLA \
  --interval 60 \
  --wait-for-market \
  --start-trading-at "9:40" \
  --stop-trading-at "10:30" \
  --close-at "10:45"
```
- 全天模式（10 分钟节奏）：
```
cd ./llm_trader
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL NVDA \
  --interval 600 \
  --cycles 33 \
  --close-at "15:59" \
  --wait-for-market
```
3) A/B 策略（默认 70/30）
- Champion：稳定配置（现网）
- Challenger：新 Prompt/参数（小权重），满足门禁后推进；不达标即时回滚
4) 实时监控
- 决策面板（30 秒）：
```
watch -n 30 'clickhouse-client --query "SELECT formatDateTime(ts, \"%H:%M:%S\") t,symbol,provider,decision,confidence FROM llm_trader.decisions WHERE ts>=now()-INTERVAL 30 MINUTE ORDER BY ts DESC LIMIT 10 FORMAT Pretty"'
```
- 账户持仓/PNL：`watch -n 60 'python ./llm_trader/check_ibkr_positions.py'`
- Kafka LAG：`docker exec trading_kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --all-groups`
5) 回滚触发（即时）
- 账户级日内止损/滚动 MDD 触发
- 滑点/延迟/拒单异常；健康门转 `error`
- 动作：`pkill -f run_single_bot.py`，停止新仓并清理旧仓，权重降至 Champion/Shadow

---

## C. 盘后（16:00–20:00 ET）
1) 决策导出与复盘：
```
cd ./docs/daily
./evening_review.sh
```
2) Prompt 战术/元认知微调（如需）：`llm_trader/config/prompts.yaml`（使用模板：`docs/daily/PROMPT_CHANGE_TEMPLATE.md`）
3) Alpha Loop（run_system 线）
- 快速日更：
```
python ./alpha_loop.py \
  --model xgboost \
  --symbols AAPL TSLA NVDA \
  --lookback-days 7 \
  --n-trials 20 \
  --output results/alpha_loop_quick_$(date +%Y%m%d).json
```
- 完整两阶段（按需/每周）：见 `ALPHA_LOOP_GUIDE.md`
4) 参数应用（如通过决策门）：
```
python ./apply_hpo_params.py \
  --hpo-params alpha_out/<run>/gen_03/gen_summary.json \
  --strategy-config strategies/configs/tsla_v7_2.json \
  --output strategies/configs/tsla_v7_2_alpha_optimized.json
```

---

## D. 隔夜（20:00–翌日 07:00 ET）
1) 启动隔夜 llm_trader（严格 03:50 清仓）
```
cd ./llm_trader
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 50 \
  --wait-for-overnight \
  --close-at "03:50" \
  --forbid-new-positions-before 5
```
2) 定时健康巡检（可选 cron，每小时）：
`0 * * * * cd ./docs/daily && ./system_health_check.py >> ./logs/health_cron.log 2>&1`
3) 备份（如未自动）：`make recon-all`

---

## E. 留痕与交付物
- 健康门快照：`logs/system_health.json`（历史：`logs/health_history/`）
- 当日 Action Plan（本文件）与执行记录（时间戳/终端截屏）
- 盘后复盘导出：`llm_trader/results/decisions_YYYYMMDD.csv`
- Alpha Loop 工件与参数应用记录（含 `apply_hpo_params.py` 输出）
- 当日 A/B 配置与回滚/晋升判断（使用 `docs/daily/AB_ROLLOUT_TEMPLATE.md`）

