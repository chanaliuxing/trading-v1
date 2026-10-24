# INDEX_SUPER_WIN_V2_REFACTOR_PLAN

> Status: superseded by [INDEX_SUPER_WIN_STOCKS_KERNEL_REWRITE_PLAN.md](./docs/INDEX_SUPER_WIN_STOCKS_KERNEL_REWRITE_PLAN.md).
> 
> This document reflects the older `replica/v2` path. The active direction is now a clean rewrite into `stocks_kernel`, not continued extension of the replica branch.

## 1. 背景

当前 `index_super_win_001` 已停止加载，现网仅保留：

- `qqq_intraday_001`
- `qqq_0dte_opt_live_001`

本次重构目标不是修补旧 `index_super_win`，而是基于现有执行链路，重新设计一个新的 `index_super_win v2`。

核心要求：

1. 不能影响现有 `qqq_intraday_001` 的运行行为
2. 新策略必须模块化、解耦、可插拔
3. 新策略要支持并行运行多个股票代码
4. 新策略要能和现有 `qqq` 策略同时运行
5. 不能再出现持仓冲突、策略冲突、IBKR 状态不同步、幽灵订单、反复开仓、反向开仓


## 2. 最高约束

本项目的第一约束不是“尽快复刻 qqq”，而是“零影响现网”。

必须遵守以下硬规则：

1. 不修改现有 `QQQIntradayStrategy` 的交易行为
2. 不复用 `qqqv1` 的订单命名空间
3. 不复用 `qqq` 的 runtime state 文件
4. 不重定义现有 execution isolation 的默认语义
5. 不改变现有 runtime reconciler 对 `qqq` 订单的认单逻辑
6. 所有新共享能力默认 `opt-in`
7. 现有 `qqq_intraday_001` 默认不接入新架构


## 3. 当前问题结论

从最近日志和代码结构看，当前系统存在以下关键问题：

### 3.1 策略状态与账户状态耦合

现有 `qqq` 策略在单个类内同时承担：

- 信号计算
- 标的映射
- 持仓状态维护
- broker position 同步
- protective stop 自愈
- orphan order 清理协同

这导致策略类既是信号引擎，又是账户状态机，无法安全扩展为多标的、可插拔模型。

### 3.2 订单归属协议是 qqq 私有协议

当前 `order_ref` 使用 `qqqv1:{strategy_id}:{symbol}:{kind}:TRG:{trigger}` 规则。

`runtime_reconciler` 会基于 `order_ref` 认单和清理 orphan。

如果新策略继续沿用或半复用这套协议，存在以下风险：

- 新策略订单被旧 `qqq` 逻辑误认
- 新策略 orphan 被错误取消
- 旧 `qqq` stop order 被新策略或共享逻辑误处理

### 3.3 现网 qqq 自身仍存在状态漂移

重启后日志仍显示：

- `qqq_position_sync_ctx_nonzero_broker_flat_using_broker`
- `Orphan orders detected`
- `orphan_cancelled_due_to_db_terminal`

这说明当前共享基础层还不是稳定真相源。

结论：

- 不能把当前 `qqq` 直接复制成新 `index`
- 必须先抽基础设施，再挂新策略


## 4. 重构目标

新的 `index_super_win v2` 必须具备以下能力：

### 4.1 运行能力

- 支持单策略单标的
- 支持单策略多标的
- 支持多实例并行
- 支持与现有 `qqq_intraday_001` 并行

### 4.2 安全能力

- 不认领不属于自己的 broker 仓位
- 不因为 pipeline approved 就宣布开仓成功
- 不因为重启丢失本地状态而错误 flatten 或误开仓
- 不因为 orphan order 导致重复保护单、重复开仓或反向开仓

### 4.3 工程能力

- 策略逻辑与账户协调逻辑分离
- 标的映射可配置
- 运行时状态可替换
- order identity 规则可扩展
- broker 同步策略可独立升级


## 5. 非目标

当前阶段不做以下事情：

1. 不重写现有 `qqq_intraday_001`
2. 不在第一阶段修复所有 `qqq` 现有问题
3. 不替换现有 IB 连接管理器
4. 不把所有旧策略统一迁移到 v2


## 6. 新架构原则

新架构采用四层拆分：

### 6.1 Strategy Signal Layer

职责：

- 只做行情信号判断
- 只做策略条件计算
- 只输出“目标仓位意图”

禁止：

- 自己宣布已成交
- 自己认领 broker 仓位
- 自己直接作为账户真相源

### 6.2 Instrument Mapping Layer

职责：

- 信号标的与执行标的映射
- 支持单标的直连
- 支持 signal symbol 与 exec symbol 不同

示例：

- `signal_symbol = AAPL`
- `exec_symbol = AAPL`

或：

- `signal_symbol = QQQ`
- `exec_symbol = TQQQ`

### 6.3 Trading Coordination Layer

职责：

- 持仓 ownership
- 订单 ownership
- trigger / order_ref / parent-child identity
- 运行时状态存储

这一层必须成为策略和 broker 之间的中间层。

### 6.4 Broker Truth Layer

职责：

- broker positions snapshot
- broker open orders snapshot
- DB order ledger 对账
- orphan adoption / cancellation

真相顺序必须固定为：

1. broker
2. DB ledger
3. runtime state
4. strategy local memory


## 7. 模块拆分方案

建议新增以下模块，不修改现有 `qqq` 模块：

### 7.1 新策略模块

- `strategies/index_super_win_strategy.py`

职责：

- 纯策略逻辑
- 多标的信号循环
- 构造意图，不做账户真相判断

### 7.2 运行态模块

- `services/strategy_runtime_v2/runtime_store.py`

职责：

- 保存 `strategy_id + symbol` 的运行态
- 替代散落 json 作为主运行态接口
- 支持未来从文件迁移到 DB / Redis

### 7.3 持仓归属模块

- `services/strategy_runtime_v2/ownership_service.py`

职责：

- 定义哪个策略拥有哪个 symbol 的 live position
- broker 有仓但 ownership 不明时进入 quarantine
- 禁止策略直接认领未知仓位

### 7.4 订单身份模块

- `services/strategy_runtime_v2/order_identity.py`

职责：

- 统一生成 `trigger_id`
- 统一生成 `order_ref`
- 统一 parent-child 关系
- 定义新策略独立 namespace

建议新 namespace：

- `iswv2:{strategy_id}:{symbol}:{kind}:TRG:{trigger}`

禁止使用：

- `qqqv1:*`

### 7.5 Broker 快照模块

- `services/strategy_runtime_v2/broker_snapshot_service.py`

职责：

- 提供稳定的 broker positions / open orders 读取接口
- 为策略层屏蔽 IB 细节
- 为 reconciler 提供统一读视图

### 7.6 通用保护单模块

- `services/strategy_runtime_v2/protection_service.py`

职责：

- live position 对应保护单存在性检查
- 缺失时自愈补单
- 与 runtime reconciler 协同处理 ghost stop

这层必须独立于具体策略实现。


## 8. 状态模型

新策略不再依赖单个类内的 `_is_traded/_exec_symbol/_expected_entry_qty` 作为最终真相。

建议状态拆分如下：

### 8.1 Strategy Intent State

策略表达：

- 想开仓
- 想平仓
- 想调整仓位

这只是意图，不是事实。

### 8.2 Coordination State

协调层记录：

- 当前 trigger
- 当前 active order refs
- 当前 ownership
- 当前 runtime lifecycle

### 8.3 Broker State

broker 快照记录：

- live positions
- live open orders
- avg cost
- terminal / working 状态

### 8.4 Reconciled State

由协调层输出：

- `flat`
- `pending_entry`
- `live`
- `pending_exit`
- `quarantine`
- `broker_unknown`

只有 reconciled state 能驱动下一步动作。


## 9. 关键安全规则

### 9.1 开仓成功判定

禁止使用：

- pipeline approved == 开仓成功

必须改为：

- execution accepted / submitted
- broker / DB 可追踪
- ownership 成立

之后才能进入 `live`

### 9.2 仓位认领规则

如果 broker 有仓但 runtime flat：

- 不允许策略直接认领
- 必须检查 ownership
- ownership 不明确则进入 quarantine

### 9.3 幽灵订单规则

如果 broker 有单但 DB 终态：

- 由 reconciler 取消
- 策略本身不直接推断“我还有单”

### 9.4 反向仓位保护

如果 broker live position 与目标方向冲突：

- 禁止继续发 entry
- 必须先进入 flatten / quarantine
- 不允许通过单次意图覆盖

### 9.5 多标的隔离

必须至少按以下维度隔离：

- `strategy_id`
- `symbol`
- `trigger_id`
- `order_ref namespace`

不能只按 `strategy_id` 隔离。


## 10. 配置设计原则

新 `index_super_win v2` 的配置必须独立，不允许混用旧 `qqq` 的 runtime 文件。

必须独立的配置项：

- `module`
- `class`
- `symbols`
- `signal_symbol`
- `exec_symbol`
- `runtime_store_key`
- `order_namespace`
- `obs_snapshot_path`
- `diag_log_path`
- `seed_path`

共享但只读复用的能力：

- IB 连接管理
- execution service
- runtime reconciler
- market data service


## 11. 实施顺序

### Phase 0: 冻结现网

- 保持 `qqq_intraday_001` 不改行为
- `index_super_win_001` 保持 disabled
- 对现有 `qqq` 记录基线日志和基线行为

### Phase 1: 新建 v2 基础模块

新增：

- `runtime_store.py`
- `ownership_service.py`
- `order_identity.py`
- `broker_snapshot_service.py`

要求：

- 全部为新模块
- 默认无旧策略接入

### Phase 2: 新建 `index_super_win` 新版本

新增：

- `strategies/index_super_win_strategy.py`

要求：

- 只输出意图
- 不直接维护账户真相
- 支持多标的

### Phase 3: 接入 protection / reconciliation adapter

要求：

- ghost order 可收敛
- stop order 存在性可校验
- 启动恢复不误认仓

### Phase 4: shadow 验证

要求：

- 与 `qqq_intraday_001` 并行
- 不下真实冲突单
- 比较信号、意图、ownership、orphan 情况

### Phase 5: 小规模 live

要求：

- 单标的 live
- 再扩展多标的
- 再验证与 `qqq` 并行


## 12. 测试矩阵

上线前必须覆盖以下场景：

### 12.1 基础场景

1. 单策略单标的
2. 单策略多标的
3. 多实例多标的
4. 新 `index` + 现有 `qqq` 并行

### 12.2 恢复场景

5. 启动时 broker 已有持仓
6. 启动时 broker 有 working stop
7. 启动时 broker 有 orphan order
8. 下单后立即重启
9. stop 提交后重启

### 12.3 异常场景

10. entry isolation blocked
11. broker position stale
12. DB 有单 broker 无单
13. broker 有单 DB 无单
14. protective stop 丢失
15. IB 短时断连恢复

### 12.4 风险场景

16. 重复 entry 意图
17. 反向仓位出现
18. flatten 幂等
19. orphan cancel 与策略动作并发
20. 多标的下同策略部分成交


## 13. 上线门禁

满足以下条件前不得 live：

1. 不存在跨策略 order namespace 冲突
2. shadow 环境无 ownership 误认
3. 重启恢复后无 ghost position hijack
4. stop 缺失可自动恢复或进入安全 flatten
5. 与 `qqq_intraday_001` 并行运行时无串单
6. 多标的并行时无反向仓位和重复 entry


## 14. 风险清单

### 风险 1

新共享层被旧 `qqq` 隐式接入

控制方式：

- 全部新增模块
- 旧配置不改
- 新配置显式启用

### 风险 2

新策略复用旧 `qqqv1` 订单协议

控制方式：

- 新 namespace
- 新 identity service
- 测试中检查 `order_ref` 前缀

### 风险 3

新策略仍用策略内状态认仓

控制方式：

- 运行态统一走 runtime store + ownership service

### 风险 4

新策略上线后与 `qqq` 在同 symbol 上竞争

控制方式：

- ownership gate
- opt-in entry policy
- shadow 先行


## 15. 下一步执行项

下一轮直接进入实现前设计拆解：

1. 输出 `v2` 模块接口定义
2. 定义 `runtime state schema`
3. 定义 `ownership state machine`
4. 定义 `order_ref / trigger_id` 新协议
5. 定义首批测试文件与用例清单


## 16. 最终结论

新的 `index_super_win` 不能从现有 `qqq` 代码直接复制，也不能通过修改 `qqq` 公共逻辑来实现。

正确路线是：

1. 冻结现网 `qqq`
2. 新建独立 v2 模块
3. 先抽共享协调层
4. 再挂新策略
5. 先 shadow，再 live

只有这样，才能同时满足：

- 不影响现有策略
- 模块化、解耦、可插拔
- 支持并行多标的
- 与现有 `qqq` 同时运行
- 不再反复出现持仓、订单和 IB 同步问题
