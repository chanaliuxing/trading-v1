# Option Execution Gap List

目的：按《[TRADING_EXECUTION_RED_BOOK](./docs/guides/TRADING_EXECUTION_RED_BOOK.md)》对当前期权执行链做差距清单，明确哪些问题已经修，哪些问题还未达到生产级纪律。

范围：
- `strategies/options_kernel/*`
- `services/exec/*`
- `services/reconciliation/*`
- `strategies/options_plugins/*`

判断标准：
- 先看不变量是否成立
- 再看主链是否自洽
- 最后看 reconciler 是否只是兜底而非主路径

---

## P0

### P0-1 Protection Invariant 还没有系统级硬约束

现象：
- live option position 在某些异常路径下，仍可能出现 broker-side 保护单缺失、stale child order 残留、重启后 child order 重新接管/误触发。
- 这类问题已经在 `SPY` 上真实出现过。

根因：
- 保护单生命周期仍然分散在 `entry fill -> safety kernel attach -> fill attribution -> reconciler cleanup` 多段逻辑中。
- 系统没有一个“只要 live position 存在，就必须满足 protection invariant”的统一断言与收敛器。

涉及区域：
- [safety_kernel.py](./services/exec/safety_kernel.py)
- [fill_event_handler.py](./services/exec/fill_event_handler.py)
- [base.py](./strategies/options_kernel/base.py)
- [runtime_reconciler.py](./services/reconciliation/runtime_reconciler.py)

必须做到：
1. 任何 `live` 期权仓位都必须满足至少一种状态：
   - broker-side stop 有效
   - 或系统进入 `protection_recovery/freeze_new_entry`
2. 持仓归零后，相关 `SL/TP` 子单必须被验证撤干净。
3. 重启恢复时，不允许仅凭 DB/order_ref 推断 child order 仍活着，必须先看 broker truth。

验收标准：
1. 模拟 entry partial fill / full fill / restart / broker reconnect，系统都能保持：有仓即有保护。
2. 持仓归零后，broker open orders / DB open orders 中不再保留该 position lane 的 stop/tp。
3. 不再出现 stale `SL/TP` 子单在重启后重新触发成交。

---

### P0-2 Flatten 闭环还没有完全机械化

现象：
- `EOD flatten` 语义已经补强，但历史上仍出现过：
  - close-only exit 被错误译成 LMT
  - flatten 后 broker/DB/strategy 没能一次性归零
  - 依赖 reconciler/cleanup_tool 收尾

根因：
- flatten 虽然有了 `flatten_action`，但“冻结 lane -> 撤工作单 -> flatten -> 验证 broker qty == 0 -> 清 child order -> 清 DB projection”这条链还没有做成统一强流程。

涉及区域：
- [base.py](./strategies/options_kernel/base.py)
- [execution_service.py](./services/exec/execution_service.py)
- [translator.py](./services/decision/translator.py)
- [runtime_reconciler.py](./services/reconciliation/runtime_reconciler.py)

必须做到：
1. flatten 期间，entry/TP/trailing/logic_retreat 不得争抢控制权。
2. flatten 前必须先 cancel 同 lane 相关 working orders。
3. flatten 后必须确认：
   - broker qty == 0
   - broker open orders == 0
   - DB order terminal
   - DB position == 0
4. 这条链必须独立于普通 exit，不得复用普通收益管理语义。

验收标准：
1. 收盘强平和手工 flatten 都能稳定完成，且结束后四面归零。
2. 日终不再依赖 cleanup_tool 才收干净。
3. flatten 提交后，不会再出现同策略同 lane 新 entry。

---

## P1

### P1-1 Broker truth -> DB position 的自动收敛还不够硬

现象：
- 审计与 reconciler 已显示 clean，但 `core.core_position` 仍可能残留旧仓位。
- 这次 `SPY` 就出现了 `broker/open orders/strategy state` 已 clean，DB 还留 `-12` 的情况。

根因：
- position 投影归零仍依赖多条路径共同完成：fill fanout、terminal processing、reconciler、cleanup。
- 缺少一个独立的、可解释的“position 收敛规则”。

涉及区域：
- [fill_event_handler.py](./services/exec/fill_event_handler.py)
- [runtime_reconciler.py](./services/reconciliation/runtime_reconciler.py)
- `core.core_position` 相关写入路径

必须做到：
1. 明确 position 投影的 authoritative update 规则。
2. broker 已 flat 且相关 orders 已 terminal 时，DB position 必须自动收敛为 0 或进入 quarantine。
3. 不允许“审计显示 clean，DB position 仍非 0”长期存在。

验收标准：
1. 平仓完成后，`core.core_position` 在可预期时限内自动归零。
2. 若不能自动归零，系统必须显式标记 quarantine，不允许继续当正常状态运行。

---

### P1-2 Fill traceability 仍存在 fallback / unknown 常态化风险

现象：
- 仍出现过：
  - `decision_id=unknown`
  - `strategy_run_id=unknown`
  - `fill_context_missing_fields`
  - fallback attribution

根因：
- `decision_id / order_ref / position_lot_id / broker_order_id / perm_id` 的贯穿已经加强，但还不是所有正常路径都完整带齐。
- 某些 terminal/fill 仍需要 fallback 推断。

涉及区域：
- [fill_event_handler.py](./services/exec/fill_event_handler.py)
- [safety_kernel.py](./services/exec/safety_kernel.py)
- [execution_service.py](./services/exec/execution_service.py)

必须做到：
1. 正常路径的 fill attribution 不依赖 fallback。
2. `perm_id` 一旦可用必须持久化并回流。
3. `decision_id / order_ref / position_lot_id` 必须在 fill、cancel、terminal status 上都能反查。

验收标准：
1. 正常 live 订单链路中，不再出现 `unknown` 作为常态。
2. `fill_context_missing_fields` 只允许在历史脏数据或外部单 adopt 场景出现。

---

### P1-3 Protection child order 恢复/认领逻辑仍需单独收紧

现象：
- stale child order 容易在 restart/adopt/bootstrap 时再次进入活跃管理范围。
- 这是当前最危险的 operational hazard 之一。

根因：
- protection child order 的恢复逻辑和普通 order 的 adopt/ghost/orphan 逻辑仍有耦合。
- broker truth 检查、role 识别、strategy scope 识别还不够“先验封死”。

涉及区域：
- [safety_kernel.py](./services/exec/safety_kernel.py)
- [runtime_reconciler.py](./services/reconciliation/runtime_reconciler.py)

必须做到：
1. child order 恢复必须先验证：
   - broker 仍可见
   - role 确认是 `STOP/TP`
   - strategy_id/symbol/order_ref/perm_id 一致
2. stale child order 不得仅凭 DB 存在就重新接管。
3. protection recover 与普通 orphan adopt 分层处理。

验收标准：
1. 重启后不会把历史 stale stop/tp 重新激活。
2. `ADOPTED` 不再误接管应归属策略自身的保护单。

---

## P2

### P2-1 Reconciler 仍有成为主路径收尾者的倾向

现象：
- 系统最终能 clean，但经常是 reconciler/cleanup_tool 收尾，而不是主执行链一次跑干净。

根因：
- 主链的 terminal convergence、position convergence、child order cleanup 还不够机械化。

涉及区域：
- [runtime_reconciler.py](./services/reconciliation/runtime_reconciler.py)
- cleanup tool / manual flatten 工具链

建议：
1. reconciler 继续保留，但角色限定为：
   - 兜底
   - 校正
   - 告警
2. 主执行链要以“不需要 reconciler 收尾”为目标。

验收标准：
1. 正常交易日内，多数订单闭环不依赖 reconciler 修正。
2. reconciler 的高频告警、ghost cleanup 次数显著下降。

---

### P2-2 审计字段已经较全，但 correctness alert 还可继续分层

现象：
- 审计链路较完整，但 noise alert 与 correctness alert 的区分还不够制度化。

建议：
1. 统一定义 correctness alert：
   - live position without protection
   - broker/db position mismatch
   - fill attribution missing on normal path
   - flatten incomplete
2. 其余 probe miss、snapshot stale、one-off fallback 划为 noise / degraded。

验收标准：
1. 运维看到告警能直接判断是否需要人工介入。
2. 不再把低风险 noisy warning 淹没高风险 correctness 问题。

---

## 已有修复（对齐红宝书方向）

1. `accepted != filled` 已在 `option kernel` 状态机中补强。
2. `EOD flatten` 已加入 `flatten_action` 与 `position_qty_hint`。
3. `close_only` exit 已修正为尊重 `MKT` style，不再被错误翻译回 `LMT`。
4. `cancel open orders` 已开始按 `order_ref + symbol + strategy` 精确匹配，降低 orderId reuse 风险。
5. OCO child persistence conflict update 已补强归属字段刷新。
6. pending entry 不再直接表现为 `POSITION_OPEN`。
7. `runtime_reconciler` 现在允许对 internal option symbol 做 stale DB position auto-heal，并补 position mismatch 审计。
8. `safety_kernel` bootstrap 现在先检查 broker position truth；broker 已 flat 的 stale DB 残仓不会再被恢复成 live plan。
9. `safety_kernel` 在 `PROTECTION_FAILED` 时会给对应 `strategy_id + symbol` 打 entry lock，防止未受保护仓位继续触发新开仓。
10. `flatten_action` 提交后会给对应 `strategy_id + symbol` 打 entry lock；当 fill 后确认 `DB position == 0` 且无 open orders 时自动解锁。
11. `flatten_lifecycle` 现在已持久化到 `core.core_order.note`，至少覆盖 `created / submitted / confirmed / pending / cancelled / rejected`。
12. `runtime_reconciler` 现在会对超时仍处于 `flatten_lifecycle=submitted/pending` 的工作单发出 `flatten_incomplete_detected` correctness audit。
13. `runtime_reconciler` 现在会在 `broker flat + 无 IB open orders` 时自动把 `flatten_lifecycle` 推进到 `confirmed`，并只释放 `flatten_action` 锁，不碰 `manual_flatten / protection_failed` 锁。

这些修复是必要的，但还不足以说明整条链已经完全生产级收口。

---

## 建议推进顺序

1. `P0-1 Protection Invariant`
2. `P0-2 Flatten 闭环`
3. `P1-1 DB position 自动收敛`
4. `P1-3 child order 恢复/认领逻辑`
5. `P1-2 traceability 去 fallback 常态化`
6. `P2` 审计与 reconciler 降噪

顺序原因：
- 先保本金与收口
- 再保账本一致
- 最后提可观测性与运维效率

---

## 上线标准

期权执行链要算达到红宝书标准，至少要满足：

1. entry 不重复
2. accepted 不会假装 live
3. live 仓位始终有保护
4. flatten 一定能完成并确认
5. child order 不会 stale 复活
6. broker / DB / strategy 三处最终一致
7. 正常路径 fill attribution 不依赖 fallback
8. 审计可直接串完整链路

在这些条件全部满足前，系统仍应按“可控但未完全收口”对待，而不是宣称已经生产级完备。
