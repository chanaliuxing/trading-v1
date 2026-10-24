# Option ML 实时行情与执行 - How-to (Paper/Live)

本文档说明如何在交易时段启动“期权 ML 实时行情 + 信号 + 下单执行”链路，并提供最小验证与排障步骤。

## 1. 前置条件

- ClickHouse 运行中，并持续写入 `trading.features_1m`。
- IB Gateway/TWS 已启动，paper 或 live 端口可连通。
- 已有已训练好的期权模型文件：`models/options_intraday/*_clf_dir.pkl` / `*_clf_move.pkl` / `*_structure_rank.pkl`。
- `trading.option_ml_controls` 中 `enabled=1`，且 `active_model_version` 指向存在的模型。

## 2. 关键环境变量 (.env)

- `TRADING_MODE=paper` 或 `live`
- `ENV_MODE=paper` 或 `live`
- `IB_GATEWAY_HOST=localhost`
- `IB_GATEWAY_PORT=4002` (paper) / `4001` (live)
- `CH_HOST=localhost` / `CH_PORT=8123`

注意：期权 ML 信号与执行不依赖 LLM Provider；LLM 相关配置不会影响该链路。

## 3. 快速健康检查

```bash
# 1) ClickHouse 是否可用
curl -s 'http://localhost:8123/?query=SELECT%20now()%20as%20now,%20max(ts)%20as%20max_ts,%20dateDiff(%27minute%27,%20max(ts),%20now())%20as%20age_min%20FROM%20trading.features_1m'

# 2) 模型控制是否启用
curl -s 'http://localhost:8123/?query=SELECT%20strategy_name,updated_at,enabled,active_model_version%20FROM%20trading.option_ml_controls%20ORDER%20BY%20updated_at%20DESC%20LIMIT%201'

# 3) IB paper 端口连通
nc -z 127.0.0.1 4002 && echo 'IB_GATEWAY_OK'
```

## 4. 推荐启动顺序（逐步）

按以下顺序启动（默认一条条执行，便于排错）：

1) 启动主系统（行情 → bars → features_1m）
```bash
PYTHONPATH=. venv/bin/python run_system.py
```

2) 如果未在 `.env` 开启自动服务，手动启动期权行情管道
```bash
PYTHONPATH=. \
  venv/bin/python tools/ingest_options_quotes.py \
  --provider ibkr \
  --symbols SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA \
  --dte 7,14,21,30 \
  --ibkr-host 127.0.0.1 \
  --ibkr-port 4002 \
  --ibkr-client-id 7
```

3) 如果未在 `.env` 开启自动服务，手动启动信号服务
```bash
PYTHONPATH=. \
  venv/bin/python services/options/ml_intraday_signal_service.py
```

4) 如果未在 `.env` 开启自动服务，手动启动执行器
```bash
PYTHONPATH=. \
  venv/bin/python tools/option_ml_exec_service.py
```

5)（可选）启动止盈止损/平仓任务
```bash
PYTHONPATH=. \
  venv/bin/python tools/close_option_intraday_trades.py
```

如果你已经在 `.env` 开启了自动启动（`OPTION_ML_AUTO_START=1`、`OPTIONS_QUOTES_AUTO_START=1`），只需要执行第 1 步。

## 5. 一键训练（离线）

训练需要先有 `features_1m` 和标签数据。训练完成后会生成模型文件到 `models/options_intraday/`，并自动评估新模型 vs 当前模型，**只有更好才会自动更新**。

```bash
PYTHONPATH=. \
  tools/option_ml_train_all.sh
```
 如果你想调整“提升阈值”，可以用：

  PYTHONPATH=. tools/option_ml_train_all.sh --min-improve 0.02
如需自定义参数，可直接传给脚本（会透传给训练与自动更新流程）。如果不提供 `--start/--end`，脚本默认训练最近 60 天。
  PYTHONPATH=. tools/option_ml_train_all.sh \
    --start "2025-11-01 09:30:00" \
    --end "2026-01-07 16:00:00"
    
  PYTHONPATH=. venv/bin/python ml/train_structure_ranker.py --start "2025-12-01 09:30:00"
  │ --end "2026-01-01 00:00:00" --universe "SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA"

PYTHONPATH=. tools/option_ml_train_all.sh \
    --start "2025-12-01 09:30:00" \
    --end "2026-01-01 00:00:00" \
    --universe "SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA"

训练只会用你实际存在标签的数据区间。

  1. 切换方向模型：

  PYTHONPATH=. venv/bin/python tools/promote_option_ml_model.py \
    --strategy-name intraday_options_long \
    --model-version options_long_v1_20260107_231542 \
    --mode paper \
    --enable 1
## 5. 启动信号服务 (Signal)

```bash
PYTHONPATH=. \
  venv/bin/python services/options/ml_intraday_signal_service.py
```

日志：`logs/option_ml_signals.log`

## 6. 启动期权行情管道 (options_quotes_1m)

期权执行依赖 `trading.options_quotes_1m`，需要持续更新期权链报价。使用 IBKR 作为数据源时：

```bash
PYTHONPATH=. \
  venv/bin/python tools/ingest_options_quotes.py \
  --provider ibkr \
  --symbols SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA \
  --dte 7,14,21,30 \
  --ibkr-host 127.0.0.1 \
  --ibkr-port 4002 \
  --ibkr-client-id 7
```

为了持续更新，建议用简单循环守护（1-3 分钟一次）：

```bash
while true; do
  PYTHONPATH=. \
    venv/bin/python tools/ingest_options_quotes.py \
    --provider ibkr \
    --symbols SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA \
    --dte 7,14,21,30
  sleep 120
done
```

验证是否更新成功：

```bash
curl -s 'http://localhost:8123/?query=SELECT%20max(ts)%20as%20max_ts,%20dateDiff(%27minute%27,%20max(ts),%20now())%20as%20age_min%20FROM%20trading.options_quotes_1m'
```

如果希望随 `run_system.py` 自动启动，设置：

```bash
export OPTIONS_QUOTES_AUTO_START=1
export OPTIONS_QUOTES_POLL_SEC=120
export OPTIONS_QUOTES_SYMBOLS=SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA
export OPTIONS_QUOTES_DTE=7,14,21,30
export OPTIONS_QUOTES_PROVIDER=ibkr
export OPTIONS_QUOTES_IBKR_HOST=127.0.0.1
export OPTIONS_QUOTES_IBKR_PORT=4002
export OPTIONS_QUOTES_IBKR_CLIENT_ID=7
```

## 7. 启动执行循环 (Execution)

执行端 `process_signals_once()` 是单次入口，需要轮询调用。示例轮询：

```bash
PYTHONPATH=. \
  venv/bin/python tools/option_ml_exec_service.py
```

日志：`logs/option_ml_exec.log`

> 注：`tools/option_ml_exec_service.py` 已内置去重逻辑，避免重复下单。

## 8. 集成到 run_system.py (无需单独执行)

在启动系统前设置环境变量即可自动集成：

```bash
export OPTION_ML_AUTO_START=1
# 可选参数
export OPTION_ML_POLL_SEC=60
export OPTION_ML_MAX_AGE_SEC=300
export OPTION_ML_STRATEGY_NAME=intraday_options_long
```

然后正常启动：

```bash
PYTHONPATH=. venv/bin/python run_system.py
```

## 9. 验证链路是否工作

```bash
# 是否持续产出信号
curl -s 'http://localhost:8123/?query=SELECT%20count()%20FROM%20trading.option_intraday_signals%20WHERE%20ts%20%3E%20now()%20-%20INTERVAL%2010%20MINUTE'

# 查看最新信号
curl -s 'http://localhost:8123/?query=SELECT%20ts,symbol,action,edge_score,rank%20FROM%20trading.option_intraday_signals%20ORDER%20BY%20ts%20DESC%20LIMIT%205'
```

如果执行端成功，会插入 `trading.option_intraday_perf` 的 OPEN 记录。

## 10. 常见问题

- **无信号**：检查 `features_1m` 是否新鲜；检查 `option_ml_controls.enabled=1`；检查 `active_model_version` 对应模型文件是否存在。
- **信号有，但不下单**：检查 `option_intraday_signals` 的 `action` 是否为 `NO_TRADE`；检查风险参数与资金上限；检查 IB Gateway 连接。
- **下单失败**：查看 `logs/option_ml_exec.log` 和 IB Gateway 日志；确认合约解析与报价表 `trading.options_quotes_1m` 是否有数据。

## 11. 停止服务

```bash
pkill -f ml_intraday_signal_service.py
pkill -f option_ml_exec_service.py
```

## 12. 升级与生产化建议

- 接入 SystemManager / Supervisor 统一管理进程生命周期。
- 引入更严谨的“去重/已执行”机制，避免重复下单。
