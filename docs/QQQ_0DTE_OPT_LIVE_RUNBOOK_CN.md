# QQQ 0DTE Options（Live/Paper）Runbook（不影响股票策略）

目标：在不破坏现有 `TQQQ/SQQQ` 股票策略的前提下，把 `qqq_0dte_opt_live_001` 在 paper 先跑通，再逐步放量。

策略代码：`./strategies/qqq_0dte_opt_live_strategy.py`

## 0) 原则（强约束）
- 该策略可独立开关；即使期权链不健康，启动阶段也会自动跳过该策略，仅保留股票策略运行。
- 期权行情、合约选择、风控、下单是独立链路；策略用 `OPT:` 内部符号与股票隔离。

## 1) 启动前准备（paper）
1. IB Gateway/TWS paper 登录并保持在线（端口按你现有系统配置）。
2. ClickHouse 正常（能查询 `trading.options_quotes_1m`）。
3. Postgres 正常（能写入 `core.core_*` 表）。

## 2) 启动期权链报价（options_quotes_1m）
方式 A（推荐，run_system 自动拉起）：

使用 `config.yml`：
- `services.options_quotes_ingest.enabled=true`
- `services.options_quotes_ingest.symbols=[QQQ]`
- `services.options_quotes_ingest.dte=[0]`
- `services.options_quotes_ingest.strict_startup_gate=false`（推荐，期权链临时不健康也不阻塞股票策略启动）

方式 B（独立采集脚本）：

```bash
PYTHONPATH=. ./venv/bin/python scripts/options/capture_options_quotes_1m.py \
  --underlying QQQ --dtes 0,1 --interval-sec 60 --mode paper
```

## 3) 启动前健康检查（强烈建议）
```bash
PYTHONPATH=. ./venv/bin/python scripts/options/check_options_quotes_health.py --underlying QQQ
```

必须看到：
- 最新 `ts` 距离当前 <= 2 分钟
- dte=0 的行数 > 0
- 近 ATM strike 有 bid/ask/mid

## 4) 注册策略资金预留（否则会 STRATEGY_NOT_FOUND）
```bash
PYTHONPATH=. ./venv/bin/python scripts/register_strategy_ledger.py \
  --strategy-id qqq_0dte_opt_live_001 \
  --name qqq_0dte_opt_live_001 \
  --capital-limit-usd 20000 \
  --reserve-buffer-bps 10 \
  --enabled 0
```

## 5) 启用策略（paper）
1. 一键（推荐，先做健康检查再启动）：
```bash
./scripts/options/start_qqq_0dte_live.sh
```
说明：如果期权健康检查失败，脚本会打印告警但仍继续启动 `run_system.py`，保证股票策略可运行。
2. 或直接启动系统：
```bash
PYTHONPATH=. ./venv/bin/python run_system.py
```

## 6) 验收点（paper 通过才考虑 live）
- 期权入场为 `MKT`，60 秒未成交会自动取消
- `spread_max_pct` 生效（宽价差不会进）
- `14:50` 后不再开新仓（仍管理出场）
- `TP1/TP2/RUNNER_PROTECT/HARD_STOP/LATE_NO_MOVE_EXIT/VWAP_EXIT` 在日志中能看到退出原因
- 与股票策略并行运行，股票策略不受影响
