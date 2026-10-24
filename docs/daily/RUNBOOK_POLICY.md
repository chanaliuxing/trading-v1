# AI Trader Runbook-Policy（1页）

> 适用对象：AI Trader（总调度）统一治理 run_system 与 llm_trader 两条线。
> 时区：America/New_York（ET）。所有训练/回测/上线，必须先通过三道门（健康门/数据门/执行门）。

---

## 0. 总原则（硬约束）
- 先门后行：未通过三门，禁止训练/回测/上线（仅允许排障与回补）。
- 资本保护：账户级日内止损预算、滚动 MDD 上限，触发即降档/回滚。
- 成本后评价：E、Sharpe、Sortino、MDD、PF、HitRate 以成本/滑点后净值为准。
- 可审计：所有动作留证：`logs/system_health.json`、导出报表、Alpha Loop 工件、A/B 决策记录。

---

## 1. 三道门（必须逐一通过）

### 1) 健康门（System Health）
- 固定命令与报告：
  - 运行：`cd ./docs/daily && ./system_health_check.py`
  - 报告：`./logs/system_health.json`
  - 快查：`jq '.overall_status' logs/system_health.json`
- 通过阈值（全部满足）：
  - Docker/核心服务：Up(healthy)
  - `run_system.py` 进程存在，最近 ERROR<20、CRITICAL=0
  - Kafka Consumers LAG < 1000（≥1000 严重，停止推进）
  - 资源：CPU<90%、内存<90%、磁盘<90%
  - 报告状态必须为 `healthy`（退出码=0）。`warning`（退出码=2）须人工确认后方可限速推进；`error` 禁止推进。
- 不通过时允许：查看日志、重启服务、数据回补。禁止启动 llm_trader、Alpha Loop、参数上线。

### 2) 数据门（Data Quality）
- 近10分钟 Tick 存在：
  - `clickhouse-client --query "SELECT symbol,count() FROM lake.ticks WHERE ts_recv >= now()-INTERVAL 10 MINUTE GROUP BY symbol FORMAT Pretty"`
- 昨日日线 Bar≥390：
  - `clickhouse-client --query "SELECT symbol,count() bars FROM trading.bars_1m WHERE toDate(ts_end)=today()-1 GROUP BY symbol FORMAT Pretty"`
- 回补（如缺）：
  - 触发：`cd docs/daily && ./trigger_backfill.py --date YYYY-MM-DD --symbols AAPL TSLA`
  - 验证：`clickhouse-client --query "SELECT symbol,bar_size,status,error_msg,created_at FROM trading.backfill_tasks WHERE created_at>=today()-1 ORDER BY created_at DESC LIMIT 20 FORMAT Pretty"`
- 未达标：仅允许回补与验证，禁止训练/上线。

### 3) 执行门（Execution Readiness）
- IBKR/TWS：`python temp/check_ibkr_status.py`
- 持仓快查：`python llm_trader/check_ibkr_positions.py`
- 系统进程：`ps aux | grep -E "(run_system.py|llm_trader)"`
- `run_system.py` 异常：`pkill -f run_system.py && nohup python run_system.py > logs/run_system.log 2>&1 &`
- 未达标：禁止启动交易或上线，仅允许排障。

---

## 2. 每日调度（ET）
- 07:00 盘前检查（先健康门→数据门→执行门）
- 07:30 启动盘前 LLM：
  - `cd llm_trader && python run_single_bot.py --provider deepseek_nvidia --symbols TSLA --interval 600 --cycles 33 --close-at "09:35" --forbid-new-positions-before 9 --wait-for-premarket`
- 09:30 盘中启动（二选一）
  - 第一小时：`python run_single_bot.py --provider deepseek --symbols TSLA --interval 60 --wait-for-market --start-trading-at "9:40" --stop-trading-at "10:30" --close-at "10:45"`
  - 全天模式：`python run_single_bot.py --provider deepseek_nvidia --symbols AAPL NVDA --interval 600 --cycles 33 --close-at "15:59" --wait-for-market`
- 16:30 盘后复盘：`cd docs/daily && ./evening_review.sh`
- 20:00 隔夜：`cd llm_trader && python run_single_bot.py --provider deepseek_nvidia --symbols TSLA --interval 600 --cycles 50 --wait-for-overnight --close-at "03:50" --forbid-new-positions-before 5`

---

## 3. 违例与回滚（即时响应）
- 账户级日内止损/MDD 触发：立即降档/回滚（停止 Challenger，提高 shadow 比例）。
- 执行质量异常（滑点/延迟/拒单/无 NBBO）或健康门转为 `error`：立刻停止新仓、平旧仓、回滚至 Champion/Shadow。
- 技术动作：
  - 停止 llm_trader：`pkill -f run_single_bot.py`
  - 降档至影子/小权重（A/B 规程见 A/B 模板）

---

## 4. 审计与留痕（单点事实）
- 健康报告：`logs/system_health.json`（历史：`logs/health_history/`）
- LLM 决策导出与复盘：`docs/daily/evening_review.sh`（导出 `llm_trader/results/decisions_YYYYMMDD.csv`）
- Alpha Loop 工件：`alpha_out/<run>/gen_*/` 与 `results/alpha_loop_*.json`
- 参数应用记录：`apply_hpo_params.py` 输出 + git diff（配置文件与时间戳）

