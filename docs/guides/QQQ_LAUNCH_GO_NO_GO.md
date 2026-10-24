# QQQ Launch Go / No-Go

目的：只回答一个问题：`qqq_intraday_option_v1_001` 是否满足开盘前放行条件。

## Current Decision

1. 当前结论：`BLOCK`

## Why

当前阻塞来自真实证据，不是文档缺失：

1. `cutover_still_blocked = true`
2. `post_instrumentation_day_count = 0`
3. `post_instrumentation_pass_days = 0`
4. `post_instrumentation_fail_days = 0`
5. 仅行情 ClickHouse 数据不会产出 state-shadow 审计，命令需走 `--source auto`

权威来源：

1. [qqq_state_shadow_cutover_recommendation.json](./runtime/state/qqq_state_shadow_acceptance/qqq_state_shadow_cutover_recommendation.json)

## Go Criteria

只有全部满足，才允许改成 `GO`：

1. `post_instrumentation_day_count >= 3`
2. `post_instrumentation_pass_days >= 3`
3. `post_instrumentation_fail_days == 0`
4. `cutover_still_blocked == false`
5. active strategy config 仍保持 platform admission 绿色

## Operator Command

```bash
./venv/bin/python ./tool/replay/run_qqq_state_shadow_acceptance_batch.py \
  --source auto \
  --auto-days \
  --state-instrumentation-start-date 2026-04-03 \
  --max-days 10
```

开盘执行建议（串行）：

```bash
./venv/bin/python ./scripts/run_option_launch_readiness_cycle.py --date 2026-04-06
```
