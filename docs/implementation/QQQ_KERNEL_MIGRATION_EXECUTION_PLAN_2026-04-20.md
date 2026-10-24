# QQQ Kernel Migration Execution Plan / Test Plan / Acceptance

**创建日期**: 2026-04-20
**状态**: Active / Canonical
**关系**:
- 补充: `docs/STOCKS_KERNEL_PLATFORM_UPGRADE_MASTER_PLAN.md`
- 承接: `docs/implementation/IBKR_RECONNECT_HARDENING_PLAN.md`
- 关联状态跟踪: `docs/implementation/QQQ_KERNEL_MIGRATION_STATUS.md`
- 关联基线目录: `reports/qqq_kernel_migration/baseline/`
- Stage 0.5 系统硬化主计划: `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md`

---

## 0. 本文件的定位

这份文档是 QQQ 从 monolith 迁移到 `stocks_kernel` 的唯一主计划。

它不回答“架构上哪个更优雅”，只回答 3 个问题：

1. QQQ 从 monolith 迁到 kernel 的顺序是什么。
2. 每一步的 hard gate、测试计划、验收标准是什么。
3. `legacy_trader_parity_mode` 如何从现网兼容态退役，而不是无限延期。

本文件的原则是：

1. 所有阶段必须可量化、可回放、可审计。
2. 所有发布结论必须 fail-closed，不允许“看起来差不多”。
3. 没有 signal authority 证据、双模式 parity 证据、lifecycle 证据，就不允许 cutover。

本文档职责边界：

1. 本文档负责 QQQ 迁移阶段、cutover 顺序、QQQ 特有 gate。
2. Stage 0.5 的详细技术方案、Remove Alternatives 时间线、生产零事件级 hard gate 以 `PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md` 为准。
3. 若 Stage 0.5 的细项与本文档有重复，以四柱硬化主计划为准。

---

## 1. 当前事实基线(2026-04-20)

| 项 | 事实 | 证据 |
|---|---|---|
| QQQ live 实现 | `qqq_intraday_001` 当前直接运行 `QQQIntradayStrategy`，不是 kernel | `config.yml:828-831`, `strategies/qqq_intraday_strategy.py:84` |
| QQQ kernel policy | `qqq_intraday_policy_v1.py` 已实现、已测试，但当前没有任何 config 实例引用 | `strategies/stocks_kernel/policies/qqq_intraday_policy_v1.py`, `grep qqq_intraday_policy_v1 config.yml -> 0 hits` |
| stocks_kernel 当前定位 | 当前是 shadow / pre-production 通道，不是 QQQ live writer | `docs/STOCKS_KERNEL_PLATFORM_UPGRADE_MASTER_PLAN.md` |
| 期权上游信号 | `qqq_intraday_option_v1_001` 的 `upstream_stock_signal` 当前来自 `qqq_intraday_001` | `config.yml:1648-1652`, `strategies/qqq_shared_signal_producer.py:55` |
| Legacy 豁免 | 现网 QQQ 依赖 `legacy_trader_parity_mode: true` 且 `enforce_stock_execution_blockers: false` | `config.yml:851-852`, `strategies/qqq_intraday_strategy.py:1020-1028` |
| 已有 parity / release gate 能力 | 仓库已具备 `trader parity gate` 与 `release pipeline` 能力 | `tool/replay/run_stocks_kernel_trader_parity_gate.py`, `tool/replay/run_stocks_kernel_release_pipeline.py` |
| logic 抽取件状态 | 17 个 `qqq_intraday_*_logic.py` 全部是现用公共模块，不是死代码 | `reports/qqq_kernel_migration/logic_reference_audit.txt` |

关键结论：

1. QQQ 当前真正的生产正本仍是 monolith。
2. QQQ 的 kernel 实现不是废弃物，而是未落地的 pre-production 通道。
3. `legacy_trader_parity_mode` 不是背景噪音，而是迁移对象的一部分。
4. 不先抽 signal authority，后续 kernel shadow diff 无法清晰归因。

---

## 2. 第一性原理

这次迁移是否成立，不看代码相似度，只看下面 5 类证据：

1. **Signal authority 一致性**
   monolith、kernel、option 消费的是同一份显式 authority 输出。
2. **Decision parity 一致性**
   allow / side / reason / exec_symbol 对齐。
3. **Lifecycle parity 一致性**
   entry / protection / flatten / orphan / quarantine / cleanup 对齐。
4. **Legacy 双模式收敛性**
   `legacy_trader_parity_mode=on` 和 `off` 两条线都能重放、对齐、归因。
5. **Cutover 可回退性**
   任一阶段失败，都能一键退回 monolith live 路径。

---

## 3. 迁移硬约束

1. **现网权威不动**: 在 Stage 4 完成前，`qqq_intraday_001` 继续是 QQQ live writer。
2. **禁止同票 live**: 在 Stage 4 前，任何 kernel 侧 QQQ 实例都只能 shadow / replay / dry-run。
3. **先信号、后执行**: 必须先抽出 signal authority，再建 QQQ kernel shadow。
4. **先抽象、后切 writer**: Stage 1 先独立 authority service，但 writer 初期仍由 monolith 承担。
5. **双模式必须并行**: `legacy_on` 和 `legacy_off` 两条线都必须跑，不接受单模式迁移。
6. **门槛必须 fail-closed**: identity report、daily summary、parity report 任一缺失，结论必须 `HOLD/BLOCK`。
7. **不许用替代标的证明 QQQ 成熟**: SMH/SPY shadow 不能作为 QQQ kernel cutover 证据。
8. **不许把 logic 抽取件当遗迹一刀切**: 共享模块先审计、先保护、后清理。
9. **Legacy 豁免有终点**: `legacy_trader_parity_mode` 必须进入退役倒计时和 hard gate。
10. **终态依赖必须显式写明**: 任何“根治版”能力若依赖 Stage 5 后的删除动作，必须在文档中标明，不允许默认以后自然发生。

---

## 4. 六个阶段

### Stage 0 — Freeze + Baseline

**目标**: 冻结现网 `qqq_intraday_001` 交易语义，建立可重放基线。

**已落地**:

1. `reports/qqq_kernel_migration/baseline/` 目录已建立。
2. `docs/implementation/QQQ_KERNEL_MIGRATION_STATUS.md` 已建立。
3. `config.yml:851` 已增加 `legacy_trader_parity_mode` 退役注释。
4. `reports/qqq_kernel_migration/logic_reference_audit.txt` 已生成。

**待完成**:

1. 设计并实现 `tool/replay/run_qqq_monolith_baseline_capture.py`。
2. 固化 baseline schema：
   - 每分钟 allow / side / reason / exec_symbol
   - `submit_intent` envelope 摘要
   - fill attribution: `decision_id -> order_ref -> broker_order_id -> permId`
   - protection / flatten / quarantine / orphan 关键事件
3. 连续采集最近 10 个交易日。

**通过门槛**:

1. 近 10 个交易日 baseline 已落盘。
2. baseline 可由一条命令重放。
3. `legacy_trader_parity_mode` 的当前值、目标终态、退役时点都已文档化。

**禁止事项**:

1. 本阶段不允许修改 monolith 的交易语义。

### Stage 0.5 — System Safeties Prerequisites

**目标**: 在进入 signal authority 抽离和 QQQ kernel shadow 前，先补齐 3 个系统级保险丝。

**本阶段是 Stage 1 的前置条件**，未通过不得开始 Stage 1。

**依赖主计划**:

1. `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md`
2. `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_STATUS.md`

**职责边界**:

1. 本节只定义“QQQ 迁移必须等待哪些系统保险丝就绪”。
2. 不重复维护四柱 A/B/C/D 的具体实现细节、删除时间线、零事件指标定义。
3. 上述细节统一在 `PLUGGABLE_ARCHITECTURE_HARDENING_PLAN_2026-04-20.md` 维护。

**必须完成的 3 个工作项**:

1. `FlipGuardRegistry`
   - 在 SafetyKernel 增加系统级 flip/reopen 防线
   - hard gate: 连续 5 个交易日 0 次“flat 后立即反向 reopen”漏拦截
2. `shared_underlying_data_plane`
   - QQQ monolith publish 到 shared plane
   - QQQ kernel shadow 和 option consumer 从同一 plane 读取
   - hard gate: 同一 tick 下 monolith / kernel / option 的 `last_price`、`ts_epoch_s` diff = 0
3. `kernel_contracts`
   - 抽出共享 `ExecutionProfile / KernelPositionState / KernelIntentRecord`
   - 股票 kernel 补 `BaseStockPlugin`
   - hard gate: 股票/期权 kernel 各至少一条 intent/position 流程跑通共享 record 类型

**通过门槛**:

1. 四柱硬化计划中与 QQQ 迁移直接相关的 A/B/C/D gate 全部至少达到“可进入 Stage 1”的状态。
2. 以下 gate 已定义并可执行:
   - `shared plane 字节级一致`
   - `FlipGuard shadow BLOCK 验证`
   - `Kernel contracts 抽取后 options 单测通过`
   - `IB epoch mismatch reject`
3. 以下“生产零事件”gate 已写入对应主计划与状态页:
   - `shared_plane_readback_diff_total`
   - `flat_to_reverse_reopen_total`
   - `ghost_order_detected_total`
   - `ib_epoch_mismatch_rejects_total`

**禁止事项**:

1. 不允许跳过 Stage 0.5 直接做 QQQ kernel shadow。
2. 不允许在数据面、flip 防线、契约层未统一前宣布“QQQ 可完美迁移”。

### Stage 1 — Signal Authority Extraction

**前置**: Stage 0 与 Stage 0.5 全部通过。

**目标**: 先解掉 signal 自我依赖环，再建立 kernel shadow。

**动作**:

1. 将 `strategies/qqq_shared_signal_producer.py` 抽成显式 authority service。
2. 初期仍由 monolith 作为唯一 writer。
3. option live 路径改为消费显式 authority 输出，而不是依赖 monolith 私有流程。
4. kernel shadow 后续也消费同一 authority。

**硬要求**:

1. 先抽象 authority，不先切 writer。
2. signal schema 不允许在 parity 期间漂移。
3. upstream option consumer 契约必须兼容。

**通过门槛**:

1. 连续 5 个交易日，抽离前后 authority payload 字节级一致。
2. 连续 5 个交易日，`qqq_intraday_option_v1_001` 的每分钟 `allow/side/reason/strike/expiry` 零差异。
3. monolith 与一个空壳 kernel test instance 可同时订阅同一 authority，无争用。

**回退方案**:

1. 保留 legacy wrapper / feature flag，一键切回 monolith 内联发布。

### Stage 2 — QQQ Kernel Shadow Bring-Up

**前置**: Stage 1 全部通过。

**目标**: 建立真正对标 QQQ 的 kernel shadow，而不是继续用替代标的旁证。

**动作**:

1. 在 `config.yml` 新增两个实例：
   - `qqq_kernel_shadow_legacy_on_001`
   - `qqq_kernel_shadow_legacy_off_001`
2. 两者均显式引用 `qqq_intraday_policy_v1`。
3. 两者均消费 Stage 1 的同一 authority 输出。
4. 两者均为 `dry_run: true`，只写 audit / summary / parity 产物，不 live submit。

**Readiness gate(2026-04-20 补充)**:

1. 两个 shadow 实例初始必须 `enabled: false`。
2. 两个 shadow 实例必须同时满足:
   - `dry_run: true`
   - `shadow_only: true`
   - `policy_id: qqq_intraday_policy_v1`
   - `enable_shared_underlying_data_plane: true`
   - `shared_underlying_read_mode: plane_only`
   - `shadow_origin_strategy_id: qqq_intraday_001`
   - `shadow_legacy_mode` 分别为 `legacy_on` / `legacy_off`
3. `core.shadow_orders` 必须支持区分来源:
   - `origin_strategy_id`
   - `legacy_mode`
   - `shadow_profile_id`
4. `tests/unit/strategies/test_qqq_kernel_shadow_config.py` 必须通过后，才允许进入 D+2 parity runner。

**通过门槛**:

| 指标 | `legacy_on` 要求 | `legacy_off` 要求 |
|---|---|---|
| 每分钟 allow | 0 条不同 | 输出全部差异并归因 |
| 每分钟 side | 0 条不同 | 输出全部差异并归因 |
| 每分钟 reason | <= 2 条/日不同，且有归因 | 全量归因 |
| exec_symbol | 0 条不同 | 0 条不同或有明确业务许可 |
| 决策触发时刻 | <= 30 秒漂移 | <= 30 秒漂移或明确归因 |

**周期要求**:

1. `legacy_on` 连续 5 到 10 个交易日通过。
2. `legacy_off` 连续 5 到 10 个交易日通过。

**禁止事项**:

1. 不允许为了对齐直接改写 `qqq_intraday_policy_v1` 的业务语义。
2. 不允许把 `legacy_off` 差异视为“以后再说”。

### Stage 3 — Lifecycle Parity Proof

**前置**: Stage 2 的 `legacy_on` 与 `legacy_off` 均通过。

**目标**: 证明 kernel 对齐的是完整生命周期，而不是“是否想交易”。

**动作**:

1. 继续保持 `dry_run` / execution mirror。
2. 对拍 entry -> protection -> flatten -> reconcile -> cleanup 全链路。
3. 强制检查新纪律字段：
   - `ib_client_id`
   - `ib_session_role`
   - `ib_connection_epoch`
   - quarantine / orphan / foreign ownership
4. 强制检查 startup reconcile / ghost closure：
   - 启动是否带 unknown broker order
   - startup block 是否正确解除

**通过门槛**:

| 指标 | 要求 |
|---|---|
| fill attribution 映射 | 0 条错配 |
| `open_trigger_count` | 0 |
| EOD `position_notional_abs_usd` | 0 |
| flatten timing | <= 5 秒偏移 |
| quarantine 触发 | 同一分钟内一致 |
| `ib_*` 身份字段 | 完全一致 |
| orphan / unresolved quarantine | 0 |
| 账本平衡 | position lot / order / fill 日级平衡，0 条未解释差异 |
| 反向 reopen 漏拦截 | 0 |
| startup unknown broker order | 0 次带病进入交易 |

**周期要求**:

1. 连续 3 到 5 个交易日通过。

### Stage 4 — Half-Step Cutover + Legacy Retirement Countdown

**前置**: Stage 3 全部通过。

**目标**: 先做半步切换，再推进 legacy 豁免退役。

**半步切换定义**:

1. monolith 继续发 authority / target_qty。
2. kernel 成为 live writer，负责 submit / protection / flatten / manage / cleanup。
3. monolith 退化为只读观测 + 应急回退。

**Legacy 退役倒计时**:

1. **D+0**: Stage 4 启动日，在 config 和文档中标注 `target disable D+30 / code removal D+45`。
2. **D+15**: `legacy_trader_parity_mode` 从“默认开”收缩为“显式配置才开”，并打日志告警。
3. **D+30**: kernel live 路径忽略该标志，统一走 `legacy_off`。
4. **D+45**: 若门槛满足，从 monolith 删除 `_legacy_trader_parity_mode` 分支和对应 config 字段。

**通过门槛**:

1. 连续 10 个交易日，半步切换下日级 fill/PnL 偏离基线 <= 0.5%。
2. 无 ghost order / foreign flatten / duplicate submit。
3. IBKR 重连、epoch、quarantine 行为与既有 hardening 计划一致。
4. D+0 / D+15 / D+30 / D+45 每个节点都有审计记录。
5. 财务账本与日级仓位收敛无未解释偏差。
6. 生产零事件 gate 连续保持为 0:
   - `shared_plane_readback_diff_total`
   - `flat_to_reverse_reopen_total`
   - `ghost_order_detected_total`

**熔断条件**:

1. 单日 fill/PnL 偏离基线 > 2%。
2. 出现 ghost order / 无主持仓 / ownership leak。
3. IBKR 重连后 kernel 与 monolith 的 `ib_connection_epoch` 读数不一致。

### Stage 5 — Final Live Cutover

**前置**: Stage 4 全部通过，且 D+45 退役条件满足。

**目标**: kernel 成为 QQQ 的唯一 live writer，monolith 降级。

**动作**:

1. kernel 从“消费 shared authority”进化为“自算信号 + 与 authority 交叉校验”。
2. monolith 切到 `enabled: false`，仅保留只读审计 / rollback path。
3. option 上游 signal source 从 monolith writer 切到 kernel live writer。

**通过门槛**:

1. 连续 10 个交易日，kernel 自算信号与 authority 交叉校验 diff 在业务容忍度内。
2. option 在信号源切换前后 5 个交易日，日级差异 <= 0.3%。
3. rollback runbook 已演练通过。

---

## 5. Hard Gate 汇总

| Gate | Stage | 评估方式 | 当前状态 |
|---|---|---|---|
| Baseline 可一键重放 | Stage 0 | baseline replay | ⏳ |
| Legacy 退役注释落地 | Stage 0 | config + doc | ✅ |
| FlipGuard shadow BLOCK 验证 | Stage 0.5 | flip fault injection | ⏳ |
| Shared plane 字节级一致 | Stage 0.5 | same-tick diff | ⏳ |
| Kernel contracts 抽取后 options 单测通过 | Stage 0.5 | unit/integration | ⏳ |
| IB epoch mismatch reject | Stage 0.5 | integration fault injection | ⏳ |
| Signal authority 字节级一致 | Stage 1 | payload diff | ⏳ |
| 期权零差异 | Stage 1 | 分钟级对账 | ⏳ |
| `legacy_on` shadow 对齐 monolith | Stage 2 | allow/side/reason/exec_symbol | ⏳ |
| `legacy_off` shadow 归因完备 | Stage 2 | 归因覆盖率 100% | ⏳ |
| 下游生命周期一致 | Stage 3 | fill/lot/epoch/quarantine 对账 | ⏳ |
| IBKR 身份字段一致 | Stage 3 | `ib_client_id/role/epoch` diff | ⏳ |
| Legacy 退役 D+45 完成 | Stage 4 | config 删除 + 代码删除 | ⏳ |
| 半步切换日级 fill/PnL 偏离 <= 0.5% | Stage 4 | 日报对账 | ⏳ |
| Kernel live 自算信号交叉校验 | Stage 5 | authority cross-check | ⏳ |

每周五更新一次到 `docs/implementation/QQQ_KERNEL_MIGRATION_STATUS.md`。

---

## 6. 测试计划

### 6.1 Unit Tests

#### Signal Authority

1. authority writer 输出 schema 与当前 option consumer 兼容。
2. authority health / age / source identity 字段稳定存在。
3. option `upstream_stock_signal` 读取 authority 输出不退化。
4. `independent_rule_replay` 仍保持 shadow-only / parity-only 语义。

建议覆盖：

1. `tests/unit/strategies/test_qqq_shared_signal_producer.py`
2. `tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py`
3. `tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py`
4. `tests/unit/strategies/options_kernel/test_signal_source_authority_rules.py`

#### Kernel QQQ Policy / Shadow Config

1. `qqq_intraday_policy_v1` 可被 config 正确实例化。
2. `qqq_kernel_shadow_legacy_on_001` / `legacy_off_001` 都禁止 live submit。
3. dual-mode gate 输出包含 mode、failed_checks、recommendation。
4. `legacy_on` 与 `legacy_off` 可被显式审计。

建议覆盖：

1. `tests/unit/strategies/test_stocks_kernel_policy_registry.py`
2. `tests/unit/strategies/test_stocks_kernel_policy_and_strategy.py`
3. 新增 `tests/unit/strategies/test_qqq_kernel_shadow_config.py`
4. 新增 `tests/unit/scripts/test_run_qqq_kernel_dual_mode_gate.py`

#### Release Gate / Legacy Gate

1. `legacy_on` 失败时 release gate 必须 `BLOCK`。
2. `legacy_off` 失败时 release gate 必须 `BLOCK`。
3. identity report / daily summary 缺失时必须 fail-closed。
4. 任何 gate 失败都必须给出 failed_checks。

建议覆盖：

1. `tests/unit/scripts/test_run_stocks_kernel_trader_parity_gate.py`
2. `tests/unit/scripts/test_run_stocks_kernel_release_pipeline.py`
3. 新增 `tests/unit/scripts/test_run_qqq_kernel_dual_mode_release_gate.py`

#### Stage 0.5 系统保险丝

1. `FlipGuardRegistry`
2. `shared_underlying_data_plane`
3. `kernel_contracts`

建议覆盖：

1. 新增 `tests/unit/services/test_flip_guard_registry.py`
2. 新增 `tests/integration/services/test_flip_guard_persistence.py`
3. 新增 `tests/integration/strategies/test_shared_plane_consistency.py`
4. 新增 `tests/integration/strategies/test_qqq_monolith_shared_plane_publish.py`
5. 新增 `tests/unit/strategies/test_kernel_contracts_compatibility.py`
6. 新增 `tests/unit/strategies/test_base_stock_plugin.py`
7. 新增 `tests/integration/services/test_startup_ghost_reconcile_gate.py`

### 6.2 Integration Tests

#### Authority Decoupling

1. monolith 继续写 authority。
2. option live path 消费 authority 无回归。
3. kernel shadow 消费同一 authority。
4. authority 输出不依赖 stock submit 成败。

建议覆盖：

1. `tests/integration/strategies/test_qqq_option_upstream_signal_flow.py`
2. `tests/integration/strategies/test_qqq_option_signal_source_shadow_drift.py`
3. 新增 `tests/integration/strategies/test_qqq_kernel_shadow_authority_flow.py`

#### QQQ Kernel Shadow

1. shadow 模式下 0 次 live submit。
2. identity report、daily summary、parity report 均稳定生成。
3. 同 symbol 下 ownership isolation 不破坏 monolith writer。
4. diff 能区分 signal mismatch 与 lifecycle mismatch。

建议覆盖：

1. 新增 `tests/integration/strategies/test_qqq_kernel_shadow_no_live_submit.py`
2. 新增 `tests/integration/strategies/test_qqq_kernel_shadow_daily_summary.py`
3. 新增 `tests/integration/strategies/test_qqq_kernel_shadow_same_symbol_isolation.py`

#### Lifecycle Parity

1. protection rebuild 对齐。
2. flatten timing 对齐。
3. broker truth mismatch 的 quarantine / freeze 行为对齐。
4. orphan / residual cleanup 不越权、不漏收敛。
5. startup unknown broker order 不得带病进入交易。

建议覆盖：

1. `tests/unit/exec/test_safety_kernel_oco_persistence.py`
2. `tests/unit/reconciliation/test_runtime_reconciler_orphans.py`
3. `tests/unit/exec/test_qqq_v1_trade_safety_regression.py`
4. 新增 `tests/integration/strategies/test_qqq_kernel_lifecycle_parity.py`

### 6.3 Replay / Shadow Day Tests

必须建立下面 4 类日级证据：

1. `legacy_on` replay parity day
2. `legacy_off` replay parity day
3. `legacy_on` shadow parity day
4. `legacy_off` shadow parity day

日级报告至少包含：

1. `allow_match_ratio`
2. `side_match_ratio`
3. `reason_match_ratio`
4. `exec_symbol_match_ratio`
5. `fills_count_delta`
6. `open_trigger_count_delta`
7. `eod_position_delta`
8. `protection / flatten / quarantine / orphan mismatch count`

建议新增脚本：

1. `tool/replay/run_qqq_monolith_baseline_capture.py`
2. `scripts/run_qqq_kernel_shadow_parity_day.py`
3. `scripts/run_qqq_kernel_dual_mode_parity_range.py`
4. `tool/replay/run_qqq_kernel_dual_mode_gate.py`

### 6.4 Regression / Safety

必须确认不误伤：

1. `qqq_intraday_001` 当前 live path
2. `qqq_intraday_option_v1_001` upstream signal path
3. `stocks_kernel_shadow_smh_001`
4. `stocks_kernel_shadow_spy_001`

建议最小回归命令：

```bash
./venv/bin/pytest -q tests/unit/strategies/test_qqq_shared_signal_producer.py
./venv/bin/pytest -q tests/unit/strategies/options_kernel/test_upstream_stock_signal_source.py tests/unit/strategies/options_kernel/test_qqq_option_upstream_signal_consumer.py
./venv/bin/pytest -q tests/unit/scripts/test_run_stocks_kernel_trader_parity_gate.py tests/unit/scripts/test_run_stocks_kernel_release_pipeline.py
./venv/bin/pytest -q tests/unit/strategies/test_stocks_kernel_policy_and_strategy.py tests/unit/strategies/test_stocks_kernel_e2e_sessions_and_generic.py
./venv/bin/pytest -q tests/unit/exec/test_qqq_v1_trade_safety_regression.py tests/unit/reconciliation/test_runtime_reconciler_orphans.py
```

扩展回归命令：

```bash
./venv/bin/pytest -q tests/integration/strategies/test_qqq_option_upstream_signal_flow.py
./venv/bin/pytest -q tests/integration/strategies/test_qqq_option_signal_source_shadow_drift.py
```

---

## 7. 验收标准

### 7.1 阶段验收

#### Stage 1 验收

1. authority payload 连续 5 个交易日字节级一致。
2. option live 路径连续 5 个交易日零差异。
3. 没有 consumer 再依赖 monolith 私有内存状态。

#### Stage 2 验收

1. `qqq_kernel_shadow_legacy_on_001` 和 `legacy_off_001` 都成功运行 shadow。
2. 0 次 live submit。
3. 两者均稳定产出 identity report、daily summary、parity report。
4. `legacy_on` 与 `legacy_off` 都跑满至少 5 个交易日。

#### Stage 3 验收

1. lifecycle parity 连续 3 到 5 个交易日通过。
2. `open_trigger_count == 0`。
3. `EOD position_notional_abs_usd == 0`。
4. `ib_client_id / ib_session_role / ib_connection_epoch` 完全一致。
5. 账本平衡通过。
6. 0 次反向 reopen 漏拦截。
7. 0 次 startup unknown broker order 带病进入交易。

#### Stage 4 验收

1. 半步切换连续 10 个交易日通过。
2. fill/PnL 日级偏离 <= 0.5%。
3. D+0 / D+15 / D+30 / D+45 退役节点全部有记录。
4. 无 ghost order / foreign flatten / ownership leak。
5. 财务账本与日级仓位收敛无未解释偏差。
6. 生产零事件 gate 连续保持为 0。

#### Stage 5 验收

1. kernel live writer 连续 10 个交易日通过 authority cross-check。
2. option 在信号源切换前后 5 个交易日差异 <= 0.3%。
3. rollback runbook 已演练。

### 7.2 指标阈值

默认 hard gate：

1. `allow_match_ratio >= 0.99`
2. `side_match_ratio >= 0.99`
3. `reason_match_ratio >= 0.98`
4. `exec_symbol_match_ratio == 1.0`
5. `kernel_fills_count <= legacy_fills_count * 1.2`
6. `open_trigger_count == 0`
7. `EOD position_notional_abs_usd == 0`
8. `unresolved_quarantine_count == 0`
9. `orphan_order_count == 0`
10. `foreign_ownership_violation_count == 0`

规则：

1. 任一阈值放宽，必须写清原因、有效期、回收日期。
2. 不允许 cutover 当周临时放宽阈值再宣布通过。

### 7.3 Final Completion 标准

只有下面全部满足，才允许宣布 QQQ kernel migration 完成：

1. signal authority 已独立。
2. `legacy_on` 和 `legacy_off` 双模式 parity 都已通过。
3. lifecycle parity 已连续通过。
4. 半步切换已连续通过。
5. kernel live writer 已连续稳定运行至少 5 到 10 个交易日。
6. `legacy_trader_parity_mode` 已从 live path 退役。
7. rollback runbook 已通过演练。
8. 替代路径删除计划已进入执行，不再只是“兼容保留”。

---

## 8. Legacy 豁免的明确终止条件

`legacy_trader_parity_mode` 当前是现网行为的一部分，不是背景事实。

**终止条件**:

1. `legacy_off` shadow 连续 5 到 10 个交易日通过。
2. `legacy_off` 下的 lifecycle parity 连续 3 个交易日通过。
3. Stage 4 D+30 节点前无熔断事件。

**终止动作**:

1. D+30: kernel live 路径忽略 `legacy_trader_parity_mode`。
2. D+45: monolith 删除 `_legacy_trader_parity_mode` 分支和 config 字段。

**若条件不满足**:

1. 不允许“延期默认开”。
2. 必须回退到 Stage 3，找出 `legacy_off` 与 monolith 的具体差异根因。

---

## 9. Logic 抽取件审计结论

`reports/qqq_kernel_migration/logic_reference_audit.txt` 已确认：

1. 全部 17 个 `qqq_intraday_*_logic.py` 都是现用公共模块。
2. `qqq_intraday_strategy.py` 自身不 import 这些 logic 文件。
3. `qqq_intraday_manage_logic` 是高风险共享点，被以下路径同时消费：
   - `strategies/stocks_kernel/modules/protection.py`
   - `strategies/options_kernel/modules/profit_management_policy.py`
   - `strategies/options_kernel/modules/risk_policy.py`
   - `strategies/index_super_win_replica_core.py`

迁移含义：

1. Stage 5 冻结 monolith 时，不应顺手删除这些逻辑模块。
2. 对 `qqq_intraday_manage_logic` 的任何改动都必须加多策略 regression 保护。

---

## 10. 明确 blocker

以下任一存在，禁止进入 live cutover：

1. `qqq_intraday_policy_v1` 仍未被实际 QQQ shadow config 引用。
2. signal authority 仍由 monolith 私有流程隐式托管。
3. 只存在 `legacy_on` 证据，没有 `legacy_off` 证据。
4. option live 仍依赖未文档化的 monolith 内部状态。
5. 任一交易日出现 unexplained duplicate submit、foreign flatten、ownership leak。
6. shared underlying data plane 仍未在 QQQ monolith 与 QQQ kernel shadow 间强制统一。
7. 没有系统级 FlipGuard。
8. startup ghost reconcile gate 仍不存在。
9. 生产零事件 gate 未定义或未连续达标。

---

## 11. 交付物清单

最低交付物：

1. authority schema / contract 文档。
2. `qqq_kernel_shadow_legacy_on_001` / `legacy_off_001` config 与 runbook。
3. dual-mode parity gate 脚本与单测。
4. dual-mode parity 日报脚本。
5. lifecycle parity 日报脚本。
6. final cutover + rollback runbook。
7. legacy retirement checklist。

---

## 12. 一句话执行摘要

推荐顺序不是“直接迁 kernel live”，而是：

1. 冻结现网语义并采集 baseline。
2. 先抽出 signal authority，但先不切 writer。
3. 建真正的 QQQ kernel shadow。
4. 同时跑 `legacy_on` / `legacy_off` 双模式对齐。
5. 通过 lifecycle parity 后再做半步切换。
6. 最终切 live，并以 `legacy_trader_parity_mode` 退役作为完工门槛。
