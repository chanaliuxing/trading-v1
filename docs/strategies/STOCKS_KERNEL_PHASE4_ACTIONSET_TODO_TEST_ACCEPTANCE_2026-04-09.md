# Stocks Kernel Phase 4 Action-Set 落地清单（2026-04-09）

## TODO（立即落地项）

- [x] 修复同 tick 多动作自阻断：`entry_submission` 冲突判定改为使用同 tick 基线 lot 视图，避免首个 pending lot 反向阻断后续同 tick 动作。
- [x] 修复 `decision_id` 唯一性：`build_envelope` 增加 `trigger/action_type/lane/qty` 维度，消除同 trigger 多动作碰撞。
- [x] 修复 action-set 测试失真：移除 `_submit_intent` mock，改为真实 `_submit_intent -> ctx.submit_intent` 链路断言，验证 pending lot 真实生成。
- [x] 补齐 authority-scoped reconcile 模板到 DB 同步路径：`_sync_db_position` 在 reconcile 前统一走 adopt/reconcile authority。
- [x] 新增同票多策略 lane-scope 集成矩阵：验证 shared execution 下双策略可并行提交且 lot 归属隔离。
- [x] action arbitration 增加跨动作预算协同：支持 `max_entry_actions_per_tick` 与 `max_entry_total_qty_per_tick`，避免同 tick entry 家族动作无限放大。
- [x] 修复 TP1 后生命周期卡死：partial exit fill 后清理 in-flight `flatten_order_refs`，允许 trailing/breakeven/dynamic stop 继续接管。
- [x] regression matrix runner 增强：支持空场景默认矩阵、覆盖率统计（`coverage/pass_rate`）与最小场景数约束。
- [ ] 下一阶段（Phase 4 后续）：action DAG 组合执行（ENTRY/ADD/REDUCE/PROTECT）与跨动作风险预算协同（按文档继续推进）。

## 测试计划（本轮）

1. 单元测试：动作集真实提交流程  
   命令：
   `venv/bin/pytest -q tests/unit/strategies/test_stocks_kernel_policy_and_strategy.py -k "entry_submission_executes_entry_action_set_in_one_tick"`
   目标：
   验证同 tick `ENTRY + ADD` 均会提交，且 ledger 中出现 2 个 `pending_entry` lots。

2. 单元测试：`decision_id` 唯一性与稳定性  
   命令：
   `venv/bin/pytest -q tests/unit/strategies/test_stocks_kernel_submit_bridge.py -k "entry_decision_id"`
   目标：
   验证同 tick 下：
   - trigger 不同 => `decision_id` 不同；
   - trigger 相同但 qty 不同 => `decision_id` 不同；
   - `decision_id` 可解析出 `action_type/lane/qty` 关键信息。

3. 回归测试：stocks kernel 关键路径  
   命令：
   `venv/bin/pytest -q tests/unit/strategies/test_stocks_kernel_policy_and_strategy.py tests/unit/strategies/test_stocks_kernel_action_arbiter_module.py tests/unit/strategies/test_stocks_kernel_submit_bridge.py tests/unit/strategies/test_stocks_kernel_lot_ledger.py tests/unit/strategies/test_stocks_kernel_e2e_sessions_and_generic.py tests/unit/scripts/test_run_stocks_kernel_regression_matrix.py tests/integration/strategies/test_stocks_kernel_runtime_cycles.py`
   目标：
   确认本轮改动未引入策略执行链与审计链回归。

## 验收标准（本轮 Done Definition）

- 同一个 tick 中同 symbol 的 `ENTRY + ADD` 不再被 `existing_exec_lot_present` 自阻断。
- 每个提交动作都有唯一 `decision_id`，同 trigger 下也可通过动作维度与 qty 区分。
- action-set 测试必须覆盖真实 `_submit_intent` 路径，不允许仅用 mock 验证调用次数。
- DB position reconcile 必须受 adopt/reconcile authority 约束，禁止越权接管。
- 同票多策略 lane-scoped 场景在 shared execution 下可并行提交且不串 strategy_id。
- TP1 partial fill 后，后续 trailing/保护管理链不允许因历史 exit refs 残留而停止。
- matrix 输出必须包含覆盖率和通过率，可直接用于 go/no-go 机读。
- 回归测试通过，且未修改老策略执行入口和配置语义。

## 风险与回滚点

- 风险：冲突判定基线化后，若同 tick 允许动作过多，可能放大单 tick 暴露。
- 控制：继续依赖 `max_trades_per_day`、portfolio exposure policy、ownership policy。
- 回滚点：
  - `strategies/stocks_kernel/strategy.py` 中 baseline conflict 逻辑；
  - `strategies/stocks_kernel/runtime/submit_bridge.py` 中 `decision_id` 组装逻辑；
  - 相关新增测试可单独回滚。
