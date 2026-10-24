# QQQ 0DTE Options 上线前 Checklist（风控阈值、告警阈值、停机条件）

适用策略：`./strategies/qqq_0dte_opt_live_strategy.py`

## 1. 数据与行情（不满足则禁止启用）
- ClickHouse 表 `trading.options_quotes_1m` 连续更新，且包含 `underlying=QQQ`、`dte=0`、当日 `expiry`
- 字段齐全：`bid/ask/mid/created_at`（mid>0）
- 数据新鲜度：`now - max(ts)` 不超过 2 分钟
- 报价新鲜度：`opt_quote_age_sec <= 10s`（建议先告警，稳定后改成硬拦截）
- 如果用 `run_system.py` 自动启动 ingest：确保 `config.yml` 中 `services.options_quotes_ingest.enabled=true` 且 `dte` 包含 `0`（环境变量仅用于临时覆盖）

## 2. 合约与流动性（入场硬拦截）
- spread：`(ask-bid)/mid <= spread_max_pct`（默认 0.15；你后续可收紧到 0.08~0.12）
- bid/ask 非零：bid>0 且 ask>0
- mid 合理：mid>0 且 mid 不为 NaN/inf

## 3. 下单执行（MKT 专用风控）
- 入场订单：`MKT`
- 超时：`entry_timeout_sec=60`，60 秒未成交自动取消
- 取消后：回到入场评估（不追单、不继承旧 quote）
- 成交滑点告警（建议）：`abs(fill-mid)/mid > 0.08` 记一次高危

## 4. 日内限额（建议先保守）
- max_trades_per_day=2（按真实成交计数）
- 第二笔仅在第一笔为 `HARD_STOP` 或 `LATE_NO_MOVE_EXIT` 时允许

## 5. 停机条件（触发即当日停）
- 连续 2 笔 `HARD_STOP`
- 或当日累计亏损达到阈值（你可以按 $ 计，也可以按 NAV% 计）
- 或连续 2 次出现“入场滑点高危”告警
- 或 `options_quotes_1m` 断流超过 3 分钟

## 6. 观察与告警（上线前必须能看到）
- 每笔 trade 必须记录：entry_time、opt_symbol、strike、side、bid/ask/mid、exit_reason、exit_time
- 关键 reason 必须可检索：`TP1`、`TP2`、`RUNNER_PROTECT`、`HARD_STOP`、`LATE_NO_MOVE_EXIT`、`VWAP_EXIT`、`ENTRY_TIMEOUT_CANCEL`
