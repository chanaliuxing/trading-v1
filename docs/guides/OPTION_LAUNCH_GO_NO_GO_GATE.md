# Option Launch Go / No-Go Gate

目的：开盘前只看这一份结论，不再手工拼多个报告。

## One Command

```bash
./venv/bin/python ./tool/replay/run_option_launch_go_no_go_gate.py
```

输出：

1. [option_launch_go_no_go_gate.json](./runtime/state/option_launch_go_no_go_gate.json)

## Gate Inputs

1. [option_platform_admission_gate.json](./runtime/state/option_platform_admission_gate.json)
2. [option_strategy_rollout_evidence_report.json](./runtime/state/option_strategy_rollout_evidence_report.json)
3. [option_active_strategy_evidence_status.json](./runtime/state/option_active_strategy_evidence_status.json)
4. [qqq_state_shadow_cutover_recommendation.json](./runtime/state/qqq_state_shadow_acceptance/qqq_state_shadow_cutover_recommendation.json)

## Hard Block Rules

任一命中，直接 `BLOCK`：

1. platform admission blocked
2. rollout contract not ready
3. active strategy evidence not ready
4. qqq cutover blocked
5. qqq post-instrumentation days < 3
6. qqq post-instrumentation pass days < 3
7. qqq post-instrumentation fail days > 0
8. spy replay parity not closed
9. spy live-shadow parity not closed

## Current Expectation

按照当前证据，运行结果应为：

1. `recommendation = BLOCK`
