# Option Opening Day Runbook

目标：开盘当天严格按同一流程执行，不临场改规则；只允许 `GO/BLOCK` 结论驱动动作。

适用 active path：
1. `qqq_intraday_option_v1_001`
2. `spy_1dte_ma10_reject_0324_001`

权威输出：
1. [option_launch_go_no_go_gate.json](./runtime/state/option_launch_go_no_go_gate.json)
2. [option_active_strategy_evidence_status.json](./runtime/state/option_active_strategy_evidence_status.json)

## 时间与动作（America/New_York）

### 1. Preopen（09:20）

```bash
./venv/bin/python ./scripts/run_option_opening_day_plan.py --phase preopen --date 2026-04-06
```

验收：
1. `spy_readiness_check` return code = 0
2. `preopen_summary.json` 已写入 `runtime/state/opening_day/2026-04-06/`

### 2. Intraday（10:05）

```bash
./venv/bin/python ./scripts/run_option_opening_day_plan.py --phase intraday --date 2026-04-06 --state-instrumentation-start-date 2026-04-03 --max-days 10
```

验收：
1. 生成最新 QQQ state-shadow batch / active evidence / launch gate
2. 有当日 SPY shadow parity day 报告

### 3. EOD（16:10）

```bash
./venv/bin/python ./scripts/run_option_opening_day_plan.py --phase eod --date 2026-04-06 --state-instrumentation-start-date 2026-04-03 --max-days 10
```

验收：
1. `eod_summary.json` 已写入
2. 当日 blockers 与下一交易日动作明确

## GO / BLOCK 判定

只看：
1. [option_launch_go_no_go_gate.json](./runtime/state/option_launch_go_no_go_gate.json)

规则：
1. `recommendation = GO` 才允许进入正式放行
2. 任何 `BLOCK` 都按阻塞原因执行修复，不做主观放行

## 回滚规则

触发任一即回滚到 `BLOCK`：
1. 新增 `qqq_*` 或 `spy_*` blocker
2. `spy_live_shadow_not_closed` 持续存在
3. QQQ `post_instrumentation_pass_days < 3`

回滚动作：
1. 保持当前 `BLOCK`
2. 继续下一交易日执行同一 runbook，不改变验收标准

## 产出文件

每次 phase 运行都会输出：
1. `runtime/state/opening_day/<date>/preopen_summary.json`
2. `runtime/state/opening_day/<date>/intraday_summary.json`
3. `runtime/state/opening_day/<date>/eod_summary.json`

## 注意

1. 仅行情数据（bars）不能替代 state-shadow 审计证据。
2. 不改稳定 QQQ 股票策略，仅推进 option active path 验收闭环。
