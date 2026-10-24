# Pluggable Architecture Hardening Plan / Test Plan / Acceptance

**创建日期**: 2026-04-20
**状态**: Active / Canonical
**关系**:
- 并行于: `docs/implementation/QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md`(QQQ 迁移)
- 并行于: `docs/implementation/IBKR_RECONNECT_HARDENING_PLAN.md`(IBKR 纪律)
- 主纲: `docs/STOCKS_KERNEL_PLATFORM_UPGRADE_MASTER_PLAN.md`

**文档职责**:

1. 本文档负责:
   - 四柱 A/B/C/D 的架构硬化目标
   - Remove Alternatives 时间线
   - 生产零事件级 hard gate
2. `QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md` 负责:
   - QQQ 迁移阶段
   - cutover 顺序
   - QQQ 特有的 parity / lifecycle / legacy 退役门槛
3. `PLUGGABLE_ARCHITECTURE_HARDENING_STATUS.md` 负责:
   - 周级状态追踪
   - gate 状态
   - owner / next action

如果三份文档冲突，以本文档定义的四柱硬化约束为准，以 QQQ 迁移文档定义的阶段顺序为准。

---

## 0. 本文件要回答什么

当前用户问题集合,按四个能力拆解:

1. **可插拔股票/期权策略,随意拼接**
2. **多策略共用同一标的的行情/特征**
3. **幽灵订单 + 反向反复开单**
4. **IB 同步 / 下单管理**

本计划不重复 QQQ 迁移计划的阶段定义,只解决"QQQ 迁移 5 个 Stage 都走完也不能自动闭合"的系统级架构缺口。

与现有计划的关系:

1. 本文件是四柱系统硬化主计划。
2. `QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md` 是 QQQ 迁移主计划。
3. 四柱 A/B/C/D 是 QQQ 迁移 Stage 1-4 的前置或并行能力,没有四柱通过,QQQ 不允许 cutover。
4. 柱 D 是 `IBKR_RECONNECT_HARDENING_PLAN.md` 在策略层和意图层的最后一公里闭环。

为避免文档漂移:

1. Stage 0.5 的详细技术要求只在本文档维护。
2. QQQ 迁移文档只保留 Stage 0.5 的前置关系和高层 gate，不再重复四柱的细部实现。

本计划额外回答一个更严格的问题：

> 如果目标不是“能迁”，而是“从根源解决、从架构解决”，哪些旧路径必须最终删除，哪些事故必须在生产连续 N 日不再发生，才算真正闭环。

**彻底解决的定义**: 以下 4 类情况在生产上不再可能发生,或发生时必然被系统级闸门拦截、记账、告警:

- P1: 同一标的下,同一账户内**多策略各自订阅行情/特征**,导致重复 I/O 和状态不一致
- P2: 平仓完成后,**同一 symbol 立刻被另一侧(同策略或邻策略)反向开单**
- P3: 幽灵订单(broker 端存在、系统不认)
- P4: IB 重连后**策略层读到的身份字段**与 broker truth 不一致

如果一个替代路径仍然存在并可被调用，那么这个问题就不能算“从根源解决”。

---

## 1. 当前架构缺口(基于 grep 实证)

### 1.1 反向反复开单 — 无系统级闸门
- `grep -rn "flip_cooldown\|reverse_open" services/exec` → **0 命中**
- 所有防抖逻辑分散在策略层:
  - `qqq_intraday_strategy.py:269-270` 自己维护 `_last_flatten_submit_sig / _mono`
  - `qqq_intraday_strategy.py:7153-7155` 自己做 dedupe
  - `max_trades_per_day` / `third_trade_requires_positive_day_pnl` 等参数在策略内
- **含义**: 一旦策略自己漏判或 kernel 迁移漏移,同 symbol 瞬间反向是可能的,SafetyKernel 无法兜底

### 1.2 行情/特征共享平面 — 建好了没启用
- `strategies/shared_data/underlying_plane.py` 241 行
- `grep -rn "enable_shared_underlying_data_plane" config.yml` → **0 命中**
- `qqq_intraday_strategy.py` **不 import** `shared_data`,仍自己 `SFS1sReader`
- **含义**: "多策略共用" 是写了代码没启用,生产上还是各读各的

### 1.3 股票 / 期权 Kernel 契约分叉
- `options_kernel/types.py:60,74,140`: `ExecutionProfile / KernelPositionState / KernelIntentRecord`
- `stocks_kernel/` 里**无** `KernelIntentRecord / KernelPositionState` 同名类型
- `stocks_kernel` 走 `policy + gate + module + runtime` DSL
- `options_kernel` 走 `plugin` 类
- **含义**: 跨资产新策略被迫双写,"随意拼接" 成口号

### 1.4 IBKR 身份字段在 strategy 层未暴露
- `services/exec/safety_kernel.py / execution_service.py / adapters/ib_adapter.py` 已写 `ib_client_id / ib_session_role / ib_connection_epoch`
- 但策略层(monolith 或 plugin)**没有消费**这些字段做本地 epoch guard
- **含义**: 策略层仍假设 IB session 稳定,reconnect 后可能基于旧 epoch 发单

### 1.5 启动时 ghost order 仍可能带病进入交易
- 当前 ghost 处理更偏向 `report / quarantine / alert`
- 若启动时 broker truth 已存在 open order,而本地 DB 无对应行,系统仍缺“带 ACK 的启动闸门”
- **含义**: 现在更像“能识别 ghost”,不是“禁止带 ghost 启动交易”

### 1.6 生产级零事件门槛尚未被写成 hard gate
- 当前多为“机制存在、shadow 可触发、脚本会 BLOCK”
- 但还缺“连续 N 个交易日无此类生产事件”的门槛
- **含义**: 现在能证明“设计了防线”，还不能证明“事故从生产中消失”

---

## 2. 4 根主柱(对应 4 个能力)

| 主柱 | 解决的问题 | 核心产物 |
|---|---|---|
| **柱 A: Kernel Unified Contracts** | 可插拔、随意拼接 | `libs/strategy/kernel_contracts/` + `BaseStockPlugin` |
| **柱 B: Shared Data Plane Mandatory** | 多策略共享行情/特征 | monolith publish + kernel mandatory consume |
| **柱 C: System-Level Flip Guard + Ghost Closure** | 反向反复开单 + 幽灵订单 | `services/exec/flip_guard.py` + DDL 单点化 + quarantine root closure |
| **柱 D: Runtime Identity Propagation** | IB 同步 / 下单管理 | `IBHandle.call(epoch)` 强制 + epoch 贯穿 intent/fill |

工期基线:

1. 柱 A: 1 周
2. 柱 B: 1 周
3. 柱 C: 2 周
4. 柱 D: 1.5 周

---

## 2.5 终态依赖链

下面这些是必须显式承认的依赖，不允许在计划里“默认以后自然会做”：

1. **柱 A 彻底版** 依赖 `QQQ_KERNEL_MIGRATION_EXECUTION_PLAN` Stage 5 之后:
   - QQQ live 已切到 kernel
   - `StrategyAlgorithm` 不再作为新策略入口
   - `stocks_kernel` 旧 DSL 开始退役
2. **柱 B 彻底版** 依赖 monolith 自己也改成从 shared plane 读取:
   - 不是只 publish
   - 而是读写都经 plane
3. **柱 C.2 彻底版** 依赖 startup reconcile gate:
   - 启动时 broker truth / DB diff 未收敛就禁止交易
4. **柱 D 彻底版** 依赖 master truth session 成为最终 identity authority:
   - 本地 `origin_ib_epoch` 不能作为唯一真相源

这些依赖若未满足，对应主柱只能宣称“过渡态解决”，不能宣称“根治”。

---

## 3. 详细落地计划

### 柱 A — Kernel Unified Contracts(股票 / 期权共享契约层)

**目标**: 股票和期权两个 kernel 共用同一套 intent/position/execution record 类型,plugin 接口对齐。

**当前阶段结论**:

1. 本柱当前解决的是“统一契约与统一入口”。
2. 本柱当前**不能**单独宣称“彻底消灭双轨制”。
3. 彻底版需要删除旧 DSL 和 `StrategyAlgorithm` 绕行入口。

**动作**:

1. **新建 `libs/strategy/kernel_contracts/`**
   - `types.py`: 先按 byte-for-byte 方式搬迁 `options_kernel/types.py` 的 `ExecutionProfile / KernelPositionState / KernelIntentRecord / Signal / EntryPlan / Selection` 到公共位置,再做资产扩展。第一阶段不得改字段语义。
   - `plugin.py`: 定义 `BaseKernelPlugin` 抽象基类,统一生命周期:`on_init / on_features / on_signal / on_intent / on_fill / on_shutdown`。
   - `audit.py`: 统一 audit record schema(后续 parity 工具所有 kernel 共用)。

2. **`options_kernel` 接入**
   - `options_kernel/types.py` 改为从 `libs/strategy/kernel_contracts/types` 重导出,保持字节级兼容。
   - `BaseOptionPlugin` 继承 `BaseKernelPlugin`,保留期权特有方法(strike selection / expiry selection)作为 mixin。

3. **`stocks_kernel` 补 Plugin Facade**
   - 新建 `stocks_kernel/plugin.py`: `BaseStockPlugin(BaseKernelPlugin)`,内部持有 `policy/gate_pack/module_config/template` 组装句柄。
   - 用户写一个新股票策略 = 写一个 `MyStockPlugin(BaseStockPlugin)` 类 + config 一段 plugin_params(不用手动拼 policy/gate/module)。
   - `ComposableStocksKernelStrategy.__init__` 检测 config 里的 `plugin_cls`,若存在就走 plugin 路径,否则走旧的 policy 路径(兼容)。

4. **终态删除计划(Remove Alternatives)**
   - D+0: 禁止新增策略直接走 `StrategyAlgorithm` 原生新入口
   - D+30: 新股票策略必须使用 `BaseStockPlugin`
   - D+45: `stocks_kernel` 旧 DSL 标记 deprecated，禁止新增实例
   - D+90: 在 QQQ Stage 5 完成后，提交删除 PR，移除旧 DSL 的公共入口
   - D+120: `StrategyAlgorithm` 降级为 legacy compatibility only，新增策略 lint fail

**Hard Gate**:

- [ ] `libs/strategy/kernel_contracts/types.py` 导入后,options_kernel 所有单测通过
- [ ] `stocks_kernel` 可以同时加载旧 policy 实例和新 plugin 实例,两者不相互影响
- [ ] 有一个 example plugin(如 `strategies/stocks_kernel/plugins/example_breakout.py`)能作为新策略模板跑通 dry_run
- [ ] 股票 kernel 与期权 kernel 各至少一条 intent/position 流程跑通共享 record 类型
- [ ] 新增股票策略入口只有 `BaseStockPlugin`，禁止新增 DSL-only 策略

**生产事件级 Hard Gate**:

- [ ] 连续 10 个交易日，`kernel_contracts_schema_mismatch_total = 0`
- [ ] 连续 10 个交易日，新增策略实例中 `strategy_entrypoint_bypass_total = 0`

**风险 & 回退**:
- 类型迁移若字段漂移会导致期权 live 炸,必须 byte-for-byte 搬;迁移 PR 必须先在期权 shadow 上跑 3 日零差异才合并
- 回退: 保留 `options_kernel/types.py` 原文件作为 fallback re-export,一键切回

**Owner**: Claude/Codex
**预计工期**: 1 周(Stage 1 内并行)

---

### 柱 B — Shared Data Plane Mandatory

**目标**: 同一账户内同一 symbol,**系统只读一次** SFS/历史数据,monolith 和所有 kernel 消费同一份,偏离 0。

**当前阶段结论**:

1. 当前计划能做到“强制接入 shared plane”。
2. 当前计划**还不能**单独宣称“彻底删除私有数据读链”。
3. 彻底版必须让 monolith 自己也从 plane 读，并最终禁止策略层直接 import `SFS1sReader`。

**动作**:

1. **QQQ monolith 接入 publish**
   - 在 `qqq_intraday_strategy.py` 的实际特征/行情刷新链路中发布 `shared_underlying_data_plane().publish_underlying(...)`
   - 不改业务逻辑,只加一次 publish
   - publish 的 namespace = `"global"`(跨策略共享,不是 per-strategy)

2. **kernel 消费侧强制优先**
   - `stocks_kernel/strategy.py:2434-2451` 的 `_resolve_runtime_snapshot` 已经有 shared plane 消费代码
   - 改动: 当 `enable_shared_underlying_data_plane: true` 时,**先查 plane,再查 local cache**(当前反过来)
   - 新增 `shared_plane_mandatory: true` 模式: 若 plane miss,不 fallback,直接 `data_not_ready`,避免两套数据并存

3. **config 默认值**
   - 在 `config.yml` 顶层加 `shared_underlying_data_plane:\n  enabled_default: true\n  namespace: global`
   - 所有 kernel 实例默认继承
   - monolith 通过一次性 config 字段 `extras.publish_to_shared_plane: true` 启用

4. **CI/Regression Test**
   - 新测试 `tests/integration/strategies/test_shared_plane_consistency.py`:
     - mock 同一 tick,让 monolith 先接 features,立刻 check plane 里的 QQQ last_price
     - 然后让 kernel shadow 读 plane,应得到**完全相同**的 last_price
     - 任何字节级差异 → fail

5. **终态删除计划(Remove Alternatives)**
   - D+0: 所有新策略禁止直接 import `SFS1sReader`
   - D+30: 增加 CI lint，扫描 `strategies/` 下直接 import `SFS1sReader`
   - D+60: QQQ monolith 切到 plane read path
   - D+90: `SFS1sReader` 从策略层 export 移除，降级为 plane 内部 source

**Hard Gate**(生产级):

- [ ] `qqq_intraday_001`(monolith)已接入 publish,连续 5 个交易日 plane 里 QQQ/TQQQ/SQQQ 的 snapshot 数量 >= 每交易日应有 tick 数 * 0.999
- [ ] `qqq_kernel_shadow_legacy_on_001` 消费 plane 后,与 monolith 本地读的 SFS 值**字节级一致**(同一 tick、同一价格、同一 ts_epoch_s)
- [ ] 拔掉 plane 所有策略应立即 `data_not_ready`(验证 mandatory 模式)
- [ ] QQQ monolith 在终态切换后也从 plane 读，不再只 publish 不 consume

**生产事件级 Hard Gate**:

- [ ] 连续 10 个交易日，`shared_plane_readback_diff_total = 0`
- [ ] 连续 10 个交易日，`shared_plane_miss_fallback_total = 0`

**风险 & 回退**:
- monolith publish 若阻塞 `on_features` 会影响现网性能 → publish 必须用 `asyncio.create_task` fire-and-forget,加 Prometheus 指标 `shared_plane_publish_latency_ms`
- 回退: `publish_to_shared_plane: false` 一键关,不影响 monolith 业务

**Owner**: Claude/Codex
**预计工期**: 1 周(与柱 A 并行,在 QQQ 迁移 Stage 1 前完成)

---

### 柱 C — System-Level Flip Guard + Ghost Closure

#### C.1 Flip Guard(反向反复开单系统级防线)

**目标**: 任何 symbol 在 flat 事件发生后的 N 秒内,**SafetyKernel 必须拒绝任何反向开单 intent**,不依赖策略层自觉。

**当前阶段结论**:

1. 当前计划会建立系统级 flip guard。
2. 但若只挂在 `SafetyKernel` 上，还不足以宣称“任何能到 broker 的 intent 都被检查”。
3. 彻底版必须把检查点压到最终提交边界，并补同 bar reversal 和豁免摩擦。

**设计**:

1. **新建 `services/exec/flip_guard.py`**
   ```python
   class FlipGuardRegistry:
       """
       Global, process-wide, system-level flip guard.
       Persisted to PostgreSQL table core.core_flip_guard for recovery.
       """
       def record_flat(symbol, strategy_id, at_ts) -> None
       def register_position_open(symbol, side, strategy_id, at_ts) -> None
       def check_intent(symbol, proposed_side, strategy_id, now_ts) -> FlipGuardDecision
       # FlipGuardDecision.decision in {"ALLOW", "BLOCK_FLIP_COOLDOWN", "BLOCK_SAME_BAR_REVERSAL"}
   ```

2. **PostgreSQL 持久化**
   - 新迁移 `db/pg/migration_20260421_flip_guard.ddl`:
     ```sql
     CREATE TABLE core.core_flip_guard (
         symbol TEXT NOT NULL,
         last_flat_ts TIMESTAMPTZ,
         last_side TEXT,
         last_strategy_id TEXT,
         updated_at TIMESTAMPTZ DEFAULT now(),
         PRIMARY KEY (symbol)
     );
     ```
   - 跨重启不丢 cooldown 状态

3. **SafetyKernel 强制调用**
   - `safety_kernel.py` 所有 intent 入口先调 `FlipGuardRegistry.check_intent`
   - 拒绝的 intent 不进 broker,写 audit + Prometheus 指标 `flip_guard_blocks_total{symbol, reason}`

3.1 **最终提交边界检查**
   - 在 `ExecutionService.submit` 最底层再次执行 `FlipGuardRegistry.check_intent`
   - 目标是保证“任何能到 broker 的 intent 都必须经过 FlipGuard”

4. **参数 / 豁免**
   - 默认 cooldown: `flip_guard_cooldown_sec: 60`
   - 同根同标的(SOXL↔SOXS, SPXL↔SPXS, TQQQ↔SQQQ)必须走同一个 cooldown bucket(抽象 `root_symbol`)
   - 期权 vs 股票的反向(如 call vs put,同 underlying)单独一条 bucket:`flip_guard_underlying_cooldown_sec`
   - 豁免仅通过 config `flip_guard_exempt_strategy_ids: []`,必须带 `expires_at`，每次使用都发告警

4.1 **same-bar reversal 规则**
   - 增加独立规则 `BLOCK_SAME_BAR_REVERSAL`
   - 按 `bar_id(symbol, 60s)` 或策略统一 bar 标识做幂等键

4.2 **root_symbol authority**
   - 优先从 broker / contract metadata 自动推导 `root_symbol`
   - 手工映射表只能做 override，不能作为主真相源

5. **幂等**
   - 同 symbol 同 strategy_id 同 bar 内重复 intent,只算一次
   - 利用既有 `decision_id` 做 dedupe

**Hard Gate**:

- [ ] 在 shadow 模式下灌入 "10 秒内 LONG flat → 立刻 SHORT open" 场景,FlipGuard 必须 BLOCK
- [ ] 连续 5 个交易日,生产 `flip_guard_blocks_total` 的所有 block 事件有人工 review,确认无误杀(任何误杀 = 参数回调)
- [ ] 跨重启恢复测试: 记录 flat → 重启进程 → 60 秒内反向开单仍被 block
- [ ] 连续 5 个交易日 0 次“flat 后立即反向 reopen”漏拦截
- [ ] same-bar reversal fault injection 必须被独立 blocker 拦截
- [ ] 豁免使用必须带 `expires_at` 且每次都发告警

**生产事件级 Hard Gate**:

- [ ] 连续 10 个正常交易日，`flat_to_reverse_reopen_total = 0`
- [ ] 连续 10 个正常交易日，`flip_guard_exemption_used_total = 0`

#### C.2 Ghost Closure(幽灵订单闭环,上轮遗留 P0)

**动作**:
- 把上一轮 IBKR 整改里 `_ensure_ib_order_truth_schema` 的 runtime DDL 移到启动时的 `ensure_schema()` 单点(一次性调用),禁止 `execution_service` 和 `runtime_reconciler` 各自 DDL
- `core_order_quarantine` 跨 option↔underlying 传染规则: 新增 `root_symbol` 字段,按 root 查询;quarantine 生效时同 root 的其他 symbol 也进入 `advisory` 状态,禁止新开反向
- 启动时执行 broker truth pull -> 本地 DB diff -> 未知 order 自动 quarantine
- 未完成人工 ACK 前，系统不得进入可交易态
- 定义 quarantine 双向传染矩阵:
  - option -> stock
  - stock -> option
  - leveraged ETF 同根互锁
- `auto_cleanup = False` 升级为:
  - `quarantine + manual_ack_required`
  - 或显式声明该类问题只做到报告，不宣称自动闭环

**Hard Gate**:
- [ ] runtime 日志中 DDL 执行次数 = 1/启动
- [ ] 期权 quarantine 同 root 的股票 intent 在 SafetyKernel 被拒绝
- [ ] 启动时若存在 unknown broker order，则系统进入 `startup_blocked`，直到 quarantine + ACK 完成
- [ ] quarantine 传染矩阵有单测和集成测试覆盖

**生产事件级 Hard Gate**:

- [ ] 连续 10 个交易日，`ghost_order_detected_total = 0`
- [ ] 连续 10 个交易日，`startup_blocked_by_unknown_broker_order_total = 0`

**Owner**: Claude/Codex
**预计工期**: 2 周(含持久化、迁移、回归)

---

### 柱 D — Runtime Identity Propagation

**目标**: 策略层在提交 intent 时必须带 `ib_connection_epoch`,broker 层在 fill 回来时必须校验 epoch 未变,变了就拒绝入账。

**当前阶段结论**:

1. 当前计划能建立 epoch 传播和拒绝机制。
2. 但如果 `origin_ib_epoch` 仍允许策略自由填写，它还不是真正 authority。
3. 彻底版必须由当前 IB handle 注入 epoch，并由 master truth session 做交叉校验。

**动作**:

1. **Strategy Context 暴露 epoch**
   - `libs/strategy/base.py` 的 `StrategyContext` 增加只读属性 `current_ib_session()` → `(role, client_id, epoch)`
   - monolith 和 kernel plugin 都可读

2. **`IBHandle.call(epoch)` 运行时强制**
   - `services/exec/adapters/ib_adapter.py` 所有对外方法包一层 `_require_epoch(expected_epoch)`,不匹配立即 raise `IBHandleEpochMismatch`
   - 调用点(`execution_service`, `safety_kernel`, `runtime_reconciler`)全部改为 `call(epoch=current_epoch)`

3. **Intent envelope 携带 epoch**
   - `DecisionEnvelope` 加字段 `origin_ib_epoch: Optional[int]`
   - SafetyKernel 若发现 `envelope.origin_ib_epoch != current_ib_epoch` → reject + 告警(说明策略在 reconnect 期间提了单)
   - `origin_ib_epoch` 必须由当前 session handle 注入，不允许策略层自由伪造

4. **Fill 回流校验**
   - fill event 携带 broker 端的 client_id / permId,handler 检查与 `core_order.ib_client_id / ib_perm_id` 一致;不一致走 quarantine

5. **master truth cross-check**
   - `core_order.ib_perm_id` / `ib_client_id` 必须可被 master truth session 的 broker open-order / execution truth 反查验证
   - 本地写入值不能作为唯一 authority

6. **终态实现方式**
   - 优先通过 `IBHandle` 统一代理注入 epoch 检查
   - 避免每个 adapter 方法手工包 `_require_epoch`

**Hard Gate**:

- [ ] 在集成测试中模拟 IB 断线重连,重连前策略提交的 intent 在新 epoch 下必须被 SafetyKernel 拒绝(epoch mismatch)
- [ ] 连续 5 个交易日 `ib_epoch_mismatch_rejects_total == 0`(正常情况下不应触发)
- [ ] 所有 `adapters/ib_adapter.py` 的公开方法 grep `_require_epoch` 覆盖率 = 100%
- [ ] `origin_ib_epoch` 来源只能是当前 handle 注入，策略层手工赋值会被测试拒绝
- [ ] master truth session 可反查验证 `ib_perm_id / ib_client_id`

**生产事件级 Hard Gate**:

- [ ] 连续 10 个正常交易日，`ib_epoch_mismatch_rejects_total = 0`
- [ ] reconnect 演练日，`ib_epoch_mismatch_rejects_total >= 1`

**Owner**: Claude/Codex
**预计工期**: 1.5 周

---

## 4. Remove Alternatives 时间线

这是“从架构根源解决”的必选项，不是 cleanup wish list。

1. D+0
   - 禁止新增策略直接 import `SFS1sReader`
   - 禁止新增策略直接以 `StrategyAlgorithm` 作为新入口
2. D+30
   - `BaseStockPlugin` 成为新股票策略唯一入口
   - `shared_underlying_data_plane` lint 开始阻止策略层私有数据读链
3. D+45
   - `stocks_kernel` 旧 DSL 标记 deprecated，禁止新增实例
   - `flip_guard` 豁免必须带 expiry，未带 expiry 直接 fail
4. D+60
   - QQQ monolith 切到 plane read path
   - startup ghost reconcile gate 启用
5. D+90
   - 提交删除 PR：移除策略层 `SFS1sReader` 暴露
   - 提交删除 PR：移除 `stocks_kernel` 旧 DSL 的公共入口
6. D+120
   - `StrategyAlgorithm` 降级为 legacy compatibility only
   - 新策略若仍直接使用 `StrategyAlgorithm`，CI fail

---

## 5. 总时间线(与 QQQ 迁移计划对齐)

```
Week 0 (now)     : 柱 C.2 Ghost Closure DDL 单点化 + 四柱接口冻结
Week 1           : 柱 A Kernel Contracts 抽取(options_kernel 先搬)
Week 2           : 柱 B Shared Plane monolith 接入 + mandatory 模式
Week 3-4         : 柱 C.1 FlipGuard 建表、注入 SafetyKernel、shadow 验证
Week 4-5.5       : 柱 D Runtime Identity Propagation
Week 6+          : 作为 QQQ 迁移 Stage 1-4 的前置能力验收

---

## 6. 验收标准

只有下面项目全部满足，才能宣布四柱硬化通过：

1. 柱 A:
   - 股票/期权 kernel 共用同一套 `ExecutionProfile / KernelPositionState / KernelIntentRecord`
   - `BaseStockPlugin` 能跑一个 example 策略
2. 柱 B:
   - QQQ monolith、QQQ kernel shadow、QQQ option consumer 在同一 tick 下读到同一份共享 underlying 数据
   - mandatory 模式下 plane miss 会 fail-closed
3. 柱 C:
   - 连续 5 个交易日 0 次“flat 后立即反向 reopen”漏拦截
   - DDL 执行次数 = 1/启动
   - quarantine 可按 `root_symbol` 传播 advisory block
4. 柱 D:
   - `IBHandle.call(epoch)` 覆盖全部 IB adapter 公开调用
   - intent/fill/order 都携带并校验 epoch
   - epoch mismatch 一律拒绝、审计、告警

同时还必须满足：

5. 替代路径已有明确删除日期，而不是无限兼容保留
6. 每根柱子至少一条“连续 N 个生产交易日无此类事件”的门槛通过

---

## 7. 回退原则

1. 柱 A 回退:
   - 保留 `options_kernel/types.py` re-export fallback
   - `stocks_kernel` 保留旧 policy 路径
2. 柱 B 回退:
   - `publish_to_shared_plane: false`
   - `shared_plane_mandatory: false`
3. 柱 C 回退:
   - `flip_guard_enabled: false`
   - quarantine root advisory 可独立关闭
4. 柱 D 回退:
   - 仅允许回退到 shadow/replay，不允许在 live 下关闭 epoch 校验

---

## 8. D-Day 0 立即动作

每根柱子的 D-Day 0 立即动作如下，且每柱 PR 合并前都需要 GO/NO-GO：

1. 柱 A:
   - 列出 `options_kernel/types.py` 全部类型和字段
   - 冻结第一版 `kernel_contracts` 接口
2. 柱 B:
   - 确认 monolith 发布点
   - 冻结 shared plane mandatory 模式字段名
3. 柱 C:
   - 起 `core_flip_guard` DDL
   - 列出 SafetyKernel 所有 intent 入口
   - 列出 `_ensure_ib_order_truth_schema` 所有调用点并计划单点化
4. 柱 D:
   - 列出所有 `ib_adapter` 公开方法
   - 列出 `DecisionEnvelope` / fill / order 的 epoch 传播点

---

## 9. 一句话执行摘要

QQQ 迁移主计划解决“怎么迁”，本计划解决“迁过去以后系统是否真的干净、整洁、高效、可收敛”。

四柱 A/B/C/D 不通过，就不能宣称下面这些问题被彻底解决：

1. 可插拔股票/期权策略
2. 多策略共享同一标的行情/特征
3. 幽灵订单与反向反复开单
4. IB 同步、下单管理、epoch 一致性
Week 4           : 柱 D Runtime Identity Propagation 全系统 epoch
Week 5           : 所有柱联调,QQQ 迁移 Stage 1 启动前置条件全部具备
```

与 QQQ 迁移 Stage 对应:
- Stage 0(现在): 柱 C.2 + 柱 A 的准备
- Stage 1(signal authority): 柱 A 柱 B 作为前置依赖
- Stage 2(shadow bring-up): 柱 C.1 必须先 ready,shadow 和 monolith 同时受 flip guard 保护
- Stage 3(lifecycle parity): 柱 D 是硬闸门,epoch 不一致直接 BLOCK
- Stage 4(half-step cutover): 4 根柱子全部稳定至少 10 个交易日

---

## 5. Hard Gate 汇总(新增,与 QQQ 迁移 gate 合并评审)

| Gate | 柱 | 来源 | 评估方式 |
|---|---|---|---|
| Kernel contracts 抽取后 options 全部单测通过 | A | 本计划 | CI |
| stocks_kernel 可加载 plugin 实例 | A | 本计划 | CI + example plugin |
| monolith publish 到 shared plane 无延迟影响 | B | 本计划 | Prometheus `shared_plane_publish_latency_ms p99 < 5ms` |
| shared plane 字节级一致 | B | 本计划 | integration test |
| FlipGuard 在 shadow 场景中 BLOCK 验证 | C.1 | 本计划 | shadow replay |
| FlipGuard 跨重启持久化 | C.1 | 本计划 | integration test |
| DDL 单点化 | C.2 | 本计划 | 启动日志 DDL count = 1 |
| Quarantine 跨 root 传染 | C.2 | 本计划 | integration test |
| IB epoch mismatch reject 机制 | D | 本计划 | integration test |
| `IBHandle.call(epoch)` 覆盖率 100% | D | 本计划 | grep audit |

状态跟踪统一到 `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_STATUS.md`(本计划旁)。

---

## 6. 禁止项

1. ❌ 在柱 C.1 未落地前,接受任何"依靠策略层自己管反向防抖"的新策略上线
2. ❌ 在柱 B 未落地前,新增策略可以自建 `SFS1sReader`(必须走 shared plane)
3. ❌ 在柱 A 未落地前,新增跨资产(equity+option)策略可以绕过共享 record 类型
4. ❌ 在柱 D 未落地前,任何 ib_adapter 公开方法新增时不带 epoch 参数
5. ❌ 用 "shadow 没触发" 证明 FlipGuard/epoch guard "不需要"—— 必须在 fault-injection 测试中主动触发,确认 BLOCK 行为

---

## 7. 与主计划的接口

- **主纲**: `STOCKS_KERNEL_PLATFORM_UPGRADE_MASTER_PLAN.md` — 本计划是对它 §4 "产品分层" 的可执行补强
- **QQQ 迁移**: `QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md` — 本计划的柱 A/B/C/D 是 Stage 1-4 的前置/并行能力
- **IBKR 纪律**: `IBKR_RECONNECT_HARDENING_PLAN.md` — 柱 D 是 PKG-1..PKG-5 在策略层的收口

---

## 8. 立即动作(D-Day 0)

- [ ] 建 `docs/implementation/PLUGGABLE_ARCHITECTURE_HARDENING_STATUS.md` 状态文件
- [ ] 柱 A: 列出 `options_kernel/types.py` 所有类型,规划搬迁 PR
- [ ] 柱 B: 在 `qqq_intraday_strategy.py` 找到 `on_features` 入口,规划 publish 注入点
- [ ] 柱 C.1: 确认 `core.core_flip_guard` 表名未冲突,起草迁移 DDL
- [ ] 柱 D: grep `adapters/ib_adapter.py` 所有公开方法清单,规划 `_require_epoch` 包裹顺序

**签字节点**: 四根柱子每柱 PR 合并前,各自需要用户 GO/NO-GO。
