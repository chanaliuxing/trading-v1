# QQQ State Shadow Acceptance Pipeline

目的：
把 `QQQIntradayStrategy` 的四条 shadow state 链路从“有审计事件”提升到“可直接形成验收结论”的工程流水线。

这份文档对应代码：
- [build_qqq_state_shadow_readiness.py](./tool/report/build_qqq_state_shadow_readiness.py)
- [build_qqq_state_shadow_acceptance.py](./tool/report/build_qqq_state_shadow_acceptance.py)
- [run_qqq_state_shadow_acceptance.py](./tool/replay/run_qqq_state_shadow_acceptance.py)
- [build_qqq_state_shadow_batch.py](./tool/report/build_qqq_state_shadow_batch.py)
- [build_qqq_state_shadow_cutover_recommendation.py](./tool/report/build_qqq_state_shadow_cutover_recommendation.py)
- [build_qqq_state_shadow_rollup.py](./tool/report/build_qqq_state_shadow_rollup.py)
- [run_qqq_state_shadow_acceptance_batch.py](./tool/replay/run_qqq_state_shadow_acceptance_batch.py)
- [qqq_intraday_option_v1_001_replica_baseline.json](./docs/baselines/qqq_intraday_option_v1_001_replica_baseline.json)

---

## 1. First-Principles Goal

状态机升级最终不是为了“代码更统一”，而是为了回答一个可执行问题：

`今天的 QQQ shadow state 证据是否足够支持后续 acceptance / cutover decision？`

因此必须把验收拆成 3 层：

1. `State Coverage`
- 四条独立状态机是否都已产出 shadow 证据
- `runtime / order / flatten / protection`

2. `State Integrity`
- 是否存在跨状态机矛盾
- 是否出现 protection failure
- flatten 是否缺少终态

3. `Acceptance Decision`
- `PASS / WARN / FAIL`
- 是否允许继续 shadow acceptance
- 是否明确禁止 cutover

---

## 2. Required Inputs

### 2.1 Audit Input

必须存在当天 `domain="state"` 审计文件，且至少包含 QQQ 的：

1. `runtime_transition`
2. `order_transition`
3. `flatten_transition`
4. `protection_transition`

### 2.2 Baseline Contract

必须读取 baseline：
- [qqq_intraday_option_v1_001_replica_baseline.json](./docs/baselines/qqq_intraday_option_v1_001_replica_baseline.json)

关键约束：
1. `shadow_required_before_cutover = true`
2. `qqq_cutover_policy = last_strategy_to_cut_over`
3. `independent_state_machines = runtime/order/flatten/protection`

---

## 3. Acceptance Decision Rules

### PASS

满足全部条件：
1. 四条状态机链都存在
2. `cross_state_inconsistency_count == 0`
3. runtime 至少出现：
   - `intent_submitted`
   - `live`
4. flatten 若出现，必须存在终态：
   - `confirmed / rejected / cancelled / failed / quarantined`
5. protection 中不能出现：
   - `protection_failed`
6. baseline contract 有效

### WARN

满足以下任一：
1. baseline 缺失但当天 state 链基本完整
2. 某些状态链存在，但当天没有形成完整交易回合
3. 证据不足以 cutover，但足以继续 shadow 观察
4. `inactive_strategy_day_no_state_transitions`（策略有运行审计，但没有交易活动导致无 state 迁移）

### FAIL

满足以下任一：
1. 缺少任一必需状态机链
2. 存在 `cross_state_inconsistency_detected`
3. 存在 `protection_failed`
4. flatten 进入流程但没有终态
5. baseline contract 无效

数据链路诊断：
1. `no_state_audit_records_for_day` 表示当天根本没有 `domain="state"` 审计
2. `no_state_records_for_target_strategy` 表示当天有 state 审计，但目标 `strategy_id` 没有状态事件
3. `pre_instrumentation_no_state_audit_records` 表示当天早于 state 仪表上线日期，缺失 state 审计按历史期 `WARN` 处理
4. `state_projection_from_strategy_audit_used` 表示当天使用了 strategy 审计反推状态链，只能用于历史证据降级（`WARN`），不能作为 cutover 通过证据

---

## 4. Long-Term Plan

这套流水线不是一次性工具，而是长期验收层。

### Phase A
已完成：
1. QQQ 四条 shadow state 链
2. readiness report
3. daily summary 内嵌 readiness

### Phase B
本阶段完成：
1. acceptance report
2. one-click runner
3. PASS / WARN / FAIL decision

### Phase C
后续必须继续：
1. 接真实 multi-day session archive
2. 做 trend-day / chop-day / restart-day acceptance batch
3. 把 acceptance 结果接到 cutover checklist

### Phase D
本阶段完成后必须具备：
1. multi-day batch acceptance report
2. daily rollup summary
3. cutover recommendation report
4. one-click batch runner

现状：
1. `multi-day batch acceptance report` 已落地
2. `cutover recommendation report` 已落地
3. `rollup summary` 已落地
4. `one-click batch runner` 已落地

---

## 5. Operator Commands

### 5.1 Build one-day acceptance

```bash
./venv/bin/python tool/replay/run_qqq_state_shadow_acceptance.py --date 2026-04-03
```

如果回放历史审计，可显式指定 state 仪表上线日期：

```bash
./venv/bin/python tool/replay/run_qqq_state_shadow_acceptance.py --date 2026-04-01 --state-instrumentation-start-date 2026-04-03
```

### 5.2 Read one-day readiness only

```bash
./venv/bin/python tool/report/build_qqq_state_shadow_readiness.py --date 2026-04-03
```

### 5.3 Read one-day acceptance report

```bash
./venv/bin/python tool/report/build_qqq_state_shadow_acceptance.py --date 2026-04-03
```

### 5.4 Run multi-day batch acceptance

```bash
./venv/bin/python tool/replay/run_qqq_state_shadow_acceptance_batch.py --days 2026-04-01 2026-04-02 2026-04-03
```

自动选择上线后日期（推荐）：

```bash
./venv/bin/python tool/replay/run_qqq_state_shadow_acceptance_batch.py --auto-days --state-instrumentation-start-date 2026-04-03 --max-days 5
```

从 ClickHouse 读取（优先用于线上真实验收）：

```bash
./venv/bin/python tool/replay/run_qqq_state_shadow_acceptance_batch.py --source auto --state-instrumentation-start-date 2026-04-03 --days 2026-04-03 2026-04-04 2026-04-05
```

### 5.5 Read multi-day batch report

```bash
./venv/bin/python tool/report/build_qqq_state_shadow_batch.py --baseline-path docs/baselines/qqq_intraday_option_v1_001_replica_baseline.json --days 2026-04-01 2026-04-02 2026-04-03
```

### 5.6 Read cutover recommendation

```bash
./venv/bin/python tool/report/build_qqq_state_shadow_cutover_recommendation.py
```

### 5.7 Read rollup summary

```bash
./venv/bin/python tool/report/build_qqq_state_shadow_rollup.py
```

---

## 6. Cutover Policy

即使 acceptance 是 `PASS`，也不代表允许直接切主路径。

`QQQ` 仍必须遵守：
1. 先多日 shadow
2. 再 replay / parity / failure injection
3. 最后才进入 cutover decision

这份 acceptance pipeline 的作用是：
`把“今天的状态机 shadow 证据是否合格”自动化，而不是提前放开 QQQ cutover。`

补充约束：
1. `cutover recommendation` 只认可 `state instrumentation start date` 之后的交易日
2. 上线前历史日即使是 `WARN(pre_instrumentation_no_state_audit_records)`，也不会计入可切换证明天数
3. `projection` 证据日（`state_projection_from_strategy_audit_used`）不会计入 cutover 放行样本
