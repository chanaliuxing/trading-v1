# SPY Stocks Kernel Rollout TODO

## Goal

Make the SPY stock strategy runnable in the pluggable stocks kernel with trader-meaningful signal, confirmation, execution, and validation paths, then push it through replay/backtest/shadow before any live enablement.

## Completed

- [x] Added `spy_intraday_core` template to the stocks kernel.
- [x] Added `spy_intraday_policy_v1` so SPY no longer hides behind the QQQ policy id.
- [x] Wired SPY default execution mapping to `SPY -> SPXL / SPXS`.
- [x] Wired SPY confirmation mapping to `RSP` instead of leaving the QQQ `QQEW` assumption in place.
- [x] Taught the rule-engine truth fabric to consume generic confirm-scope aliases rather than only `qqew_*`.
- [x] Added a default SPY strategy block in [config.yml](./config.yml).
- [x] Added an example backtest spec in [spy_intraday_core_backtest.example.json](./configs/stocks_kernel/spy_intraday_core_backtest.example.json).
- [x] Added a generic daily ATR context rebuild tool plus a SPY wrapper.
- [x] Rebuilt `runtime/state/spy_daily_atr_context.json` from current `bars_1m_ibkr`.
- [x] Ran a stocks-kernel replay smoke with `SPY + RSP + SPXL/SPXS`; strategy loop executed and blocked on `seed_ready`, which is expected for un-warmed minimal frames.
- [x] Made the stocks-kernel backtest adapter expand experiment specs into real `StocksKernelStrategy` runtime extras instead of routing through the research seed shell.
- [x] Made adapter data loading include SPY signal, `RSP` confirm, and `SPXL/SPXS` execution symbols so fills can be priced in backtest.
- [x] Disabled live-only startup preflight in backtest so broker-adapter absence does not block historical runs.
- [x] Added explicit execution proxy pricing (`SPXL/SPXS <- SPY`) for backtest windows where the history store lacks levered ETF bars.
- [x] Added a direct CLI runner: [run_stocks_kernel_backtest_experiment.py](./tool/run_stocks_kernel_backtest_experiment.py).
- [x] Ran a real short-window backtest smoke on `2026-04-10 09:30-11:00 ET`; adapter produced 2 fills, `net_pnl=951.35`, `max_drawdown_pct=0.1848`, with execution priced via the explicit `SPY -> SPXL` proxy.

## Still Required Before Claiming Profitability

- [ ] Run stocks-kernel backtest over SPY and capture `net_pnl`, `win_rate`, `profit_factor`, `max_drawdown_pct`.
- [ ] Sweep SPY-specific threshold pack rather than inheriting QQQ defaults.
- [ ] Run same-day replay/shadow validation and compare block reasons, entry timing, and EOD flatten behavior.
- [ ] Decide whether `RSP` breadth confirmation stays required all day or only for opening / elite windows.
- [ ] Only after the above, flip `stocks_kernel_shadow_spy_001` from `dry_run=true` to paper/live staging.

## Immediate Commands

```bash
python3 tool/rebuild_spy_daily_atr_context.py
python3 tool/build_stocks_kernel_instance_config.py --strategy-id stocks_kernel_shadow_spy_001 --template spy_intraday_core --signal-symbol SPY --long-exec-symbol SPXL --short-exec-symbol SPXS --confirm-symbol RSP
```

```bash
AI_TRADER_BACKTEST_AUTO_DATA=1 python3 tool/run_stocks_kernel_backtest_experiment.py \
  --spec configs/stocks_kernel/spy_intraday_core_backtest.example.json \
  --output runs/ai_trader/experiments/exp-stocks-spy-intraday-core-001/manual_summary.json
```

```bash
python3 tool/replay/run_stocks_kernel_replay.py \
  --config /tmp/spy_kernel_config.json \
  --frames /tmp/spy_kernel_frames.json
```

## Trader Notes

- The objective is not “SPY should look like QQQ in code”. The objective is “SPY should preserve the same edge family”.
- `RSP` is the current breadth-confirm substitute for `QQEW`. If that breadth edge does not improve trade selection in replay, it should be downgraded or windowed, not kept for aesthetics.
- `SPXL/SPXS` are execution amplifiers. That improves monetization of a lower-vol underlying but also increases stop/retreat sensitivity, so SPY threshold tuning must be done on execution reality, not on raw SPY signal shape alone.
- Current backtest evidence is still approximation-heavy because the sampled history store has `SPY` minute bars but not `RSP/SPXL/SPXS`. The CLI now proxies missing execution legs from `SPY` so fills can run, but that remains research-grade evidence until those legs are loaded historically.
