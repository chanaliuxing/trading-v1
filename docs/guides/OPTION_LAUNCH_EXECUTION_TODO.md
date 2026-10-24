# Option Launch Execution Todo

目的：把开盘前剩余工作从“原则级计划”下沉成“执行级清单”。  
规则：只允许围绕 active path 推进，不允许横向扩散。

## 当前总状态

1. 总状态：`BLOCK`
2. active path：
- `qqq_intraday_option_v1_001`
- `spy_1dte_ma10_reject_0324_001`
3. 当前权威结论：
- [option_active_strategy_evidence_status.json](./runtime/state/option_active_strategy_evidence_status.json)
- [option_launch_go_no_go_gate.json](./runtime/state/option_launch_go_no_go_gate.json)

## P0

### T0.1 生成统一 launch gate

状态：`已完成`

输出：

1. [OPTION_LAUNCH_GO_NO_GO_GATE.md](./docs/guides/OPTION_LAUNCH_GO_NO_GO_GATE.md)
2. [QQQ_LAUNCH_GO_NO_GO.md](./docs/guides/QQQ_LAUNCH_GO_NO_GO.md)
3. [run_option_launch_go_no_go_gate.py](./tool/replay/run_option_launch_go_no_go_gate.py)

完成定义：

1. 一条命令输出 `GO/BLOCK`
2. blockers 可直接追踪到 active strategy 证据

### T0.2 固定唯一权威入口

状态：`已完成`

命令：

```bash
./venv/bin/python ./tool/replay/run_option_launch_go_no_go_gate.py
```

完成定义：

1. 开盘前只看这一份 gate

### T0.3 一键执行开盘验收循环

状态：`已完成`

命令：

```bash
./venv/bin/python ./scripts/run_option_launch_readiness_cycle.py --date 2026-04-06
```

完成定义：

1. 自动串行执行 QQQ state-shadow batch、SPY shadow parity day、active evidence、launch gate
2. 输出统一 JSON summary（含每一步 return code + blocker 列表）

### T0.4 开盘日分阶段执行器 + Runbook

状态：`已完成`

输出：

1. [run_option_opening_day_plan.py](./scripts/run_option_opening_day_plan.py)
2. [OPTION_OPENING_DAY_RUNBOOK.md](./docs/guides/OPTION_OPENING_DAY_RUNBOOK.md)

完成定义：

1. `preopen / intraday / eod` 三个 phase 可独立执行
2. 每个 phase 固化输出 `runtime/state/opening_day/<date>/<phase>_summary.json`

## P1 QQQ

### T1.1 收集 post-instrumentation state 审计

状态：`未完成`

当前事实：

1. `post_instrumentation_day_count = 0`
2. `post_instrumentation_pass_days = 0`
3. `post_instrumentation_fail_days = 0`
4. 仅行情 ClickHouse 数据不足以生成 state-shadow 验收，需使用 runtime `strategy/state` 审计（`--source auto`）

执行：

```bash
./venv/bin/python ./tool/replay/run_qqq_state_shadow_acceptance_batch.py \
  --source auto \
  --auto-days \
  --state-instrumentation-start-date 2026-04-03 \
  --max-days 10
```

完成定义：

1. `post_instrumentation_day_count >= 3`
2. `post_instrumentation_pass_days >= 3`
3. `post_instrumentation_fail_days == 0`

### T1.2 修 state 链缺失根因

状态：`未完成`

当前失败信号：

1. `no_state_audit_records_for_day`
2. `missing_state_machines:runtime,order,flatten,protection`
3. `missing_runtime_intent_submitted`
4. `missing_runtime_live`
5. `missing_protected_state`
6. `missing_order_progression`

完成定义：

1. 新交易日 state 审计不再出现上述缺口
2. acceptance 从 `FAIL` 提升到 `PASS`

### T1.3 产出 QQQ 单独 go/no-go 结论

状态：`已完成文档，未完成放行`

文档：

1. [QQQ_LAUNCH_GO_NO_GO.md](./docs/guides/QQQ_LAUNCH_GO_NO_GO.md)

完成定义：

1. 文档中的 `Current Decision` 从 `BLOCK` 变为 `GO`

## P1 SPY

### T1.4 产出 replay 根因定位文档

状态：`未完成`

当前事实：

1. `2026-03-30 active extras` 样本：`old_count=0`, `new_count=7`

必须定位：

1. signal emitted 差异
2. guard blocked 差异
3. contract selection 差异
4. quote/freshness 差异
5. ownership/open-order/state guard 差异

完成定义：

1. 新增 `SPY_OPTION_MA_PARITY_ROOT_CAUSE.md`
2. 每个差异有文件定位和修复归属

### T1.5 跑出 real matched replay day

状态：`已完成`

证据：

1. [spy_option_ma_parity_range_2026-03-20_2026-03-31_active_extras.json](./runtime/state/spy_option_ma_parity_range_2026-03-20_2026-03-31_active_extras.json)
2. 关键日：`2026-03-26`，`old_count=5`，`new_count=5`，`matched=true`

完成定义：

1. 至少一个交易日满足：
- `old_count > 0`
- `new_count > 0`
- `matched = true`

### T1.6 跑出 live-shadow matched entries

状态：`未完成`

完成定义：

1. 至少一个 day report 满足 `matched_entries > 0`

## P2 发布

### T2.1 开盘前 go/no-go

状态：`未完成`

完成定义：

1. [option_launch_go_no_go_gate.json](./runtime/state/option_launch_go_no_go_gate.json) 为 `GO`

### T2.2 回滚路径确认

状态：`未完成`

完成定义：

1. active strategy disable/rollback 步骤成文
2. runbook 中明确 fallback path

### T2.3 开盘后首小时观测清单

状态：`未完成`

完成定义：

1. 明确看哪些日志、哪些审计、哪些 snapshot
2. 明确什么情况立即 `BLOCK`
