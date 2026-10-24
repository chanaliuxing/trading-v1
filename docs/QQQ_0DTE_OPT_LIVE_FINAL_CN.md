# QQQ 0DTE Options（单腿买方）最终可落地版本（并行运行，不影响股票策略）

本文件描述当前“可直接接入系统”的 QQQ 0DTE 执行层策略（MKT、60s 未成交取消重评估、每次 3 张）。

代码入口：
- 策略：`./strategies/qqq_0dte_opt_live_strategy.py`
- 内部期权符号：`./libs/options/internal_symbol.py`
- IBKR 适配：`./services/exec/adapters/ib_adapter.py`
- 60s 超时取消：`./services/exec/safety_kernel.py`

## 1. 总体定位
- 标的：QQQ 0DTE（CALL/PUT 二选一，单腿买方）
- 方向：复用 QQQ 的 breakout→confirm（并保留 VWAP 同侧）
- 下单：入场 `MKT`；发单 60 秒未成交自动取消；取消后回到入场评估（不追单、不继承旧快照）
- 仓位：每次固定 3 张合约（`contracts=3`），不加仓
- 目标：快进快出，但允许 runner 在锁盈地板保护下扩展右尾
- 并行：与股票策略同时运行；使用 `OPT:` 内部符号防止 symbol 冲突

## 2. 数据依赖（必须满足）
策略执行依赖 ClickHouse 表 `trading.options_quotes_1m`，至少要包含：
- ts（按分钟对齐，America/New_York）
- underlying=QQQ
- dte=0
- expiry=当日
- strike（覆盖 spot 附近）
- right=C/P
- bid/ask/mid（mid>0）
- created_at（用于 quote age）

采集方式二选一：
- 你现有管道（若支持 dte=0）
- 脚本：`./scripts/options/capture_options_quotes_1m.py`

## 3. 下单与风控的 extras 约定（系统内必须透传）
期权 intent 必须携带（策略已内置）：
- instrument_type=OPTION
- underlying=QQQ
- bars_symbol=QQQ
- risk_symbol=OPT:QQQ:0DTE
- contracts_mult=100
- force_order_style=true
- disable_oco=true
- entry_timeout_sec=60
- opt_bid_px/opt_ask_px/opt_mid_px
- opt_quote_age_sec

退出单额外携带：
- close_only=true
- position_qty_hint（用于避免 DB lag 阻塞 close-only）

## 4. 入场时段（ET）
- 允许评估：ab_time_start=09:30:00 到 ab_time_end=15:59:00
- 禁止新开仓：no_new_entry_after=14:50:00（强约束）

## 5. 入场逻辑（break→confirm→entry）
做多 CALL：
- break：close > prev_high 且 close > vwap
- confirm：下一根 close > break_level 且 close > vwap 且 close_loc >= confirm_bar_close_loc_min
做空 PUT：
- break：close < prev_low 且 close < vwap
- confirm：下一根 close < break_level 且 close < vwap 且 close_loc <= 1-confirm_bar_close_loc_min

0DTE 专用趋势过滤（满足其一才允许入场）：
- RVOL >= rvol_min（默认 1.5）
- 或 ATR 扩张：atr_1m >= atr_expansion_mult * atr_1m_ma（默认 1.2 倍）

入场门控（不看未来）：
- 动量 deadzone（ATR 缩放）
- anti-chase（可选，避免追尾端）
- early_override（09:45 前需要“早盘强势确认”）
- delayed_confirm（09:45-10:00 需要 5m move/ATR_D 达标）

合约选择：
- 从 `options_quotes_1m` 按分钟取当日 dte=0 的 C 或 P
- 选择最接近 spot 的 strike（近 ATM）
- spread 门控： (ask-bid)/mid <= spread_max_pct

## 6. 出场逻辑（按期权 mid mark）
执行优先级（从上到下）：
- TP2（TP1 后 runner）：mark >= entry * tp2_mult → 全平
- TP1：mark >= entry * tp1_mult → 先卖 2 张
- RUNNER_PROTECT（锁盈地板 + 分段抬高）：mark <= floor → 全平剩余
- HARD_STOP：mark <= entry * stop_loss_mult → 全平
- LATE_NO_MOVE_EXIT：持仓>=no_move_min_hold_min 且 标的没走出来 且 option_return < no_move_min_opt_return → 全平
- VWAP_EXIT（仅 TP1 前）：持仓>=vwap_exit_minutes 且 价格回到 VWAP 反侧 → 全平

默认参数建议（可回测后调整）：
- tp1_mult=1.20
- tp2_mult=2.50
- stop_loss_mult=0.65
- no_move_min_hold_min=5.0
- no_move_underlying_move_atr=0.15
- no_move_min_opt_return=0.10
- vwap_exit_only_pre_tp1=true
- vwap_exit_minutes=12.0

## 7. 日内交易次数控制（第二笔只在第一笔“坏结局”时允许）
- max_trades_per_day=2（按“真实成交持仓出现”计数，未成交取消不占用）
- second_trade_only_if_first_bad_exit=true
- second_trade_first_exit_allow=["HARD_STOP","LATE_NO_MOVE_EXIT"]

## 8. config.yml 最小接入模板（默认禁用）
把以下策略块加入 `config.yml` 的 strategies 列表（先保持 enabled=false）：

```yaml
- id: qqq_0dte_opt_live_001
  enabled: false
  module: strategies.qqq_0dte_opt_live_strategy
  class: QQQ0DTEOptionsLiveStrategy
  capital_limit_usd: 0
  params:
    cadence_s: 1
    ttl_ms: 2000
    extras:
      underlying: QQQ
      risk_symbol: "OPT:QQQ:0DTE"
      contracts: 3
      entry_timeout_sec: 60
      ab_time_start: "09:30:00"
      ab_time_end: "15:59:00"
      no_new_entry_after: "14:50:00"
      max_trades_per_day: 2
      second_trade_only_if_first_bad_exit: true
      second_trade_first_exit_allow: ["HARD_STOP","LATE_NO_MOVE_EXIT"]
      confirm_bar_close_loc_min: 0.55
      spread_max_pct: 0.15
      tp1_mult: 1.20
      tp2_mult: 2.50
      stop_loss_mult: 0.65
      no_move_min_hold_min: 5.0
      no_move_underlying_move_atr: 0.15
      no_move_min_opt_return: 0.10
      vwap_exit_only_pre_tp1: true
      vwap_exit_minutes: 12.0
```

## 9. 启用前必须做的 DB 注册（否则会 STRATEGY_NOT_FOUND）
资金预留（ReservationService）依赖 Postgres 的 `core.core_strategy` / `core.core_strategy_ledger`。

用脚本注册（建议在 paper 环境先跑）：

```bash
PYTHONPATH=. ./venv/bin/python scripts/register_strategy_ledger.py \
  --strategy-id qqq_0dte_opt_live_001 \
  --name qqq_0dte_opt_live_001 \
  --capital-limit-usd 20000 \
  --reserve-buffer-bps 10 \
  --enabled 0
```
