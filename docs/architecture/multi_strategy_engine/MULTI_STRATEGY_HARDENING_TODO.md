# Multi-Strategy Hardening TODO (同票多策略并行版)

目标：支持“同票不同策略可同时开仓/持仓”，并保持链路可追溯、可恢复、可验收。

## P0 必做（已落地）

- [x] `entry_isolation` 增加模式化配置：`cross_strategy | strategy_only`
  - 现网配置切到 `strategy_only`（允许跨策略同票并行）
  - 文件：
    - `config.yml`
    - `run_system.py`
    - `services/exec/execution_service.py`

- [x] 保留同策略防重：同一策略同一票未终态订单存在时阻断重复进场
  - 阻断原因：`entry_isolation_blocked_same_strategy_pending_order`
  - 文件：`services/exec/execution_service.py`

- [x] close-only 强制按策略仓位检查，不使用账户净仓（策略动作）
  - 文件：`services/exec/execution_service.py`

- [x] 终态 finalize 闭环：`SafetyKernel + RuntimeReconciler` 都可回传 `decision_finalizer`
  - `note` 无法解析时回查 `core_order_event` 的 `decision_id`
  - 文件：
    - `services/exec/safety_kernel.py`
    - `services/reconciliation/runtime_reconciler.py`

## P1 本周完成（进行中）

- [ ] 新增三链一致性巡检脚本（order/order_event/broker）
  - 目标：每分钟输出一致率与异常明细（按 strategy_id）
  - 验收：`>=99.9%` 一致率，异常可定位到 `decision_id`

- [ ] 手动平仓入口策略化约束
  - 默认只允许 `flatten <strategy_id>`，`flatten_all` 仅保留管理员开关
  - 验收：误操作不会跨策略清仓

- [ ] 报警最小集
  - `entry_isolation_blocked_*`、`decision_finalizer_failed`、`orphan/ghost`
  - 验收：告警含 `strategy_id/decision_id/trigger_id/order_ref`

## P2 下周完成（可选）

- [ ] 启动恢复：保护单幂等补挂（按 `strategy_id + parent_order_id + order_ref`）
- [ ] 资金软桶（strategy+symbol）仅告警不阻断
- [ ] 收盘 16:01 报告按策略与 trigger 分组输出（财务口径）

## 验收命令（当前）

```bash
venv/bin/pytest -q \
  tests/unit/exec/test_execution_service_entry_isolation_lock.py \
  tests/unit/exec/test_execution_service_entry_isolation_mode.py \
  tests/unit/services/test_runtime_reconciler_decision_finalize.py \
  tests/unit/exec/test_safety_kernel_decision_finalize_fallback.py \
  tests/unit/decision/test_pipeline_safety_and_sign.py

venv/bin/python tool/smoke/test_decision_chain_regression.py
```

## 上线前检查

- [ ] 重启后日志出现：`entry_isolation_mode=strategy_only`
- [ ] 同票双策略并行入场至少 1 轮，无跨策略阻断
- [ ] 无 `close_only_no_long_position` 的误拒（非目标策略）
- [ ] 任意订单可通过 `decision_id` 追溯到 `strategy_id + trigger_id + order_ref`
