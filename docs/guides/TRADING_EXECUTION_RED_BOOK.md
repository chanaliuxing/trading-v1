# Trading Execution Red Book

目的：把股票、ETF、期权实盘里最容易出事故的地方，沉淀成统一的开发与运维红宝书。  
范围：持仓管理、订单管理、下单、保护单、平仓、IBKR 同步、DB 收敛、审计、上线与回滚。  
原则：先定义不变量，再谈实现；先修 root cause，再谈补丁；先保证状态收敛，再追求策略复杂度。

## 0. 总结论

要把 `QQQ` 股票做T、股票 shadow、期权策略都做稳，核心不是“多加几条 if”，而是把整条链路做成下面 8 件事：

1. 单一真相源
2. 明确状态机
3. 幂等提交
4. broker truth 优先
5. 保护单强约束
6. 平仓优先级绝对最高
7. 全链路可审计
8. 异常只降级，不瞎修正

一句话总结：

`ownership + idempotency + protection invariant + broker truth + state-machine discipline + full audit`

只有这套纪律成立，系统才不会靠猜测运行。

## 1. 核心原则

1. 单一事实源，但允许分层缓存。  
   Broker 是订单和仓位是否还活着的最终真相源；DB 是系统内部状态、审计和账本真相源；策略内存 `ctx` 只是运行态缓存，不能反过来覆盖前两者。

2. 所有订单和仓位都必须可追溯到策略身份。  
   任何 live order、fill、position、reservation、reconciliation correction 都必须能追到 `strategy_id / symbol / side / trigger_id / order_ref / broker_order_id / perm_id / order_role`。

3. 任何自动修复都必须满足固定顺序。  
   先确认 broker truth，再改 DB，最后才允许推动策略状态。

4. 任何非终态都必须最终收敛。  
   `SUBMITTED / PRE_SUBMITTED / PENDING_CANCEL / HELD` 不能无限期存在。必须有自动收敛路径和人工兜底路径。

5. 不允许“看起来成功，实际未成交”或“broker 已无单，DB 还活着”长期存在。  
   这是幽灵订单、重复开仓、反向开仓、资金占用异常的根源。

6. `accepted != filled` 必须是全系统硬约束。  
   `broker_submit_ack`、`Submitted`、`PreSubmitted` 只能证明 broker 接受了订单，不能证明持仓已经成立。  
   任何策略只要把 `accepted` 当成 `live`，就会触发幽灵持仓、重复开仓、假持仓保护单、错误 EOD 平仓。

7. 保护单必须以 broker-side 为准，不能只靠策略内逻辑止损。  
   逻辑止损只能作为补充，不可替代券商真实保护单。

8. 对可能还活着的订单，宁可先 `skip + audit + freeze`，也不要直接 terminalize。  
   对可能已持仓但无保护单的情况，宁可先补保护或 kill switch，也不能继续正常交易。

9. 平仓逻辑必须绝对高于开仓逻辑，保护单必须绝对高于收益优化逻辑。  
   flatten、hard stop、风险保护不能和 entry、追涨、logic retreat 抢控制权。

10. reconciler 只能修投影，不能替代执行链正确性。  
   reconciler 是兜底，不是主路径。它可以修 ghost / orphan / mismatch、补最终一致性、发告警，但不应变成“系统日常靠它收尾”。

11. 测试态开关必须与生产态显式隔离。  
   `force_test_entry`、盘后数据 profile、放宽 trade count、临时 session、特殊 cutoff，不得静默留在生产配置。

12. terminal orphan 不是 noise，必须先 quarantine 再处置。  
   只要 broker 侧还挂着一个 DB 已经判 terminal 的 order，尤其是期权 `STOP/TP` child order，就必须立即冻结该 `strategy_id + symbol` 的新开仓；不能等它下一次成交后再靠 `POSITION_MISMATCH_DETECTED` 发现。

## 1.1 六大铁律

1. 所有策略先定义 ownership，再写逻辑。
2. 所有订单先定义 role，再做 cleanup/reconcile。
3. 所有自动修复先看 broker truth，再改 DB。
4. 所有 live position 强制满足 protection invariant。
5. 所有 flatten 绝对高于 entry 和收益管理。
6. 所有异常必须能从 audit/event 直接还原。
7. 所有状态转换必须显式区分 `accepted` 与 `filled/live`。

## 2. 系统必须满足的不变量

### 2.1 持仓不变量

1. `broker position` 与 `DB position` 必须周期性对账。
2. 对账发现差异时，系统必须进入“可解释状态”：
   - 要么自动修正
   - 要么 quarantine
   - 要么报警并阻断新开仓
3. 平仓后，仓位和 reservation 必须一起归零。
4. 不允许“账户净仓 flat，但策略仓位未 flat”长期存在。
5. 持仓不一致时，默认动作不是继续开单，而是 `freeze new entry + protect/flatten decision`。
6. 重启恢复时，必须先 reconcile position，再恢复交易。
7. 如果 broker 有仓、策略缺少 entry_px / ATR / stop context，必须进入 `recovery/protection-first` 模式。

### 2.1.1 单一持仓状态机

持仓状态至少分成：

1. `intent_open`
2. `pending_fill`
3. `live`
4. `pending_exit`
5. `closed`
6. `rejected`

禁止把 `accepted` 直接跳成 `live`。

### 2.1.1 持仓 ownership 约束

1. 策略内 `ctx` 持仓只是缓存。
2. `core_position` 是系统账本。
3. `IB positions` 是 broker 真相。
4. 三者必须有明确同步方向，不能互相覆盖乱写。
5. 禁止用 account-level broker position 直接写 strategy-scoped DB position。
6. 多策略同 symbol 时，必须先定义：
   - 谁允许持仓
   - 谁允许挂保护单
   - 谁允许 flatten
   - 谁能对 broker 仓位做 adopt/reconcile

### 2.2 订单不变量

1. 每一笔 entry/exit/protective order 都必须有唯一身份。
2. `order_ref` 可以用于语义识别，但不能作为唯一真相。
3. `perm_id` 一旦可用，必须被持久化并参与归因。
4. `broker_order_id` 只在单 broker session 内相对稳定，不能单独承担全局归因。
5. 订单一旦终态，所有镜像状态都必须终态：
   - DB row
   - reservation
   - local live-order cache
   - reconciliation working set
6. 订单必须按 role 管理，不能只按 symbol 管。
7. cancel/cleanup 必须带 role 过滤，不能“全扫”。
8. `broker_order_id` 不能单独作为唯一键，至少要和 `order_ref / perm_id / symbol` 联合看。
9. 对期权内部符号，cleanup/cancel/flatten 不能退化成股票路径。  
   `OPT:SPY:20260402:655:C` 这类符号必须按真实期权 contract 取消和 flatten，不能只按 underlying `SPY` 或 `Stock(...)` 处理。

### 2.2.2 单一订单主键体系

每笔单至少同时有：

1. `decision_id`
2. `order_ref`
3. `position_lot_id`
4. `broker_order_id`
5. `perm_id`

缺任何一个，都应视为 traceability 降级。

### 2.2.1 最低角色集合

至少区分：

1. `ENTRY`
2. `STOP`
3. `TP1` / `TP`
4. `FLATTEN`
5. `MANUAL_FLATTEN`
6. `NETTING`
7. `ADOPTED`

新增 order role 时，必须同步更新：

1. strategy cleanup/filter
2. reconciler classification
3. audit fields
4. manual ops 文档

### 2.3 风控不变量

1. 预算必须单一来源。  
   推荐只从策略级 `capital_limit_usd` 派生 `planned_qty`、`risk_budget`、`reservation_limit`。

2. 风控预算、下单数量、保护单数量必须一致。  
   不允许 entry 下了 200 股，保护单只挂 1 股，或 reservation 还按旧预算占用。

3. 任何资金占用都必须和有效 working order 一一对应。  
   没有 working order，就不该存在 `HELD reservation`。

4. live position 必须始终满足 protection invariant。  
   只要有仓，就必须有有效的 stop/protection 流程处于 active 状态。

5. 保护单必须是 `position-attached`，不是 `order-attached`。  
   保护的是仓位，不是某次下单事件。否则 partial fill、重启、改单、rebind 时一定出错。

### 2.4 审计不变量

1. 每次信号评估、gate 决策、下单、broker ack、fill、cancel、reconcile correction 都必须留审计。
2. 审计字段必须可串链：  
   `strategy_id -> trigger_id -> decision_id -> trace_id -> order_ref -> broker_order_id -> perm_id -> fill`
3. Fail-open 的观测逻辑不能吞掉关键异常。  
   例如 broker error、价格非法、保护单拒绝，必须显式记录。
4. 告警必须区分 noise alert 和 correctness alert。  
   `still_open skip terminalize` 不是 `live order 被误终态化` 一个级别的问题。

### 2.4.1 最低审计字段

每笔 entry / exit / fill / OCO / cancel 至少要审计：

1. `strategy_id`
2. `symbol`
3. `decision_id`
4. `order_ref`
5. `position_lot_id`
6. `broker_order_id`
7. `perm_id`
8. `intent_action`
9. `close_only`
10. `flatten_action`
11. `status`
12. `reason`
13. `qty`
14. `price`
15. `trace_fallback_used`

## 3. 按链路拆解必须做好什么

### 3.1 持仓管理

必须做到：

1. 持仓只有一个 authoritative mapping。  
   策略内 `ctx` 持仓只是缓存；`DB core_position` 是系统账本；`IB positions` 是 broker 真相；三者要有明确同步方向，不能互相覆盖乱写。

2. 策略只能管理自己负责的 symbol/position lane。  
   不能出现 account-level 仓位直接写回 strategy-scoped position。

3. 多策略同 symbol 时，必须先定义归属规则。  
   谁允许持仓、谁允许挂保护单、谁允许 flatten、谁能 adopt/reconcile。

4. 重启恢复时，先 reconcile position，再恢复交易。  
   broker 有仓但策略没有 entry_px / ATR / stop context 时，必须先进入 `recovery/protection-first`。

critical：

1. 最大风险不是“持仓看错一次”，而是“错仓后继续正常交易”。
2. 所以持仓不一致时，默认动作不该是继续开单，而该是 `freeze new entry + protect/flatten decision`。

### 3.2 订单管理

必须做到：

1. 每张单都要有稳定 identity。  
   至少包括 `idempotency_key / broker_order_id / perm_id / order_ref / order_role`。

2. 订单要分角色管理，不能只按 symbol 管。  
   至少区分 `ENTRY / STOP / TP / FLATTEN / MANUAL_FLATTEN / NETTING / ADOPTED`。

3. cancel 不能“全扫”，必须带角色过滤。  
   flatten 后又扫掉自己刚发的 flatten 单，会把小错升级成大事故。

4. 订单终态不能凭推测写。  
   ghost 不等于 filled；snapshot miss 不等于 cancelled；local cancel accepted 不等于 broker removed。

5. terminal orphan 取消未确认时，必须立即 freeze lane。  
   这条规则在夜盘和正常交易时间都成立，不能区分 session。因为根因不是时间段，而是 broker 侧残留订单仍然活着。

critical：

1. 订单管理最怕“角色不分 + 终态乱写”。
2. 一旦把保护单误写没、或者把 flatten 单误 cancel，整个策略会从小错直接升级成大事故。
3. 对期权 child order，最大的 root cause 不是“cleanup 没跑”，而是“broker 侧残留单仍然活着，但系统没有先 freeze lane”。

### 3.2.1 期权残留单专项纪律

必须做到：

1. broker 期权仓位、broker 期权 open orders、DB 订单、DB 仓位必须用同一套内部符号对齐。
2. `cleanup tool` 必须支持内部期权符号：
   - broker position 识别
   - open order 取消
   - flatten contract 构造
3. 发现 `IB non-zero / DB zero` 的期权仓位时，默认动作是 quarantine，不是继续开仓。
4. 发现 terminal DB orphan 仍在 broker 活着时，默认动作是：
   - cancel attempt
   - fresh probe
   - cancel not confirmed => quarantine
5. 这套纪律在 `RTH / afterhours / overnight` 都必须一致，不能只在 overnight cleanup 里生效。

### 3.3 下单

必须做到：

1. 下单必须幂等。  
   重试不应发第二张 live order；restart/replay 不应重复发单。

2. 下单前必须做 lane gate。  
   必须检查：
   - 当前是否已持仓
   - 当前是否有 working entry order
   - 当前是否有 flatten in progress
   - 当前是否在 protection recovery 模式

3. 下单成功的定义要分两步。  
   - broker accepted
   - db persisted submitted  
   如果 broker accepted 但 DB persist failed，必须立刻补救或 cancel broker。

4. 入场去重必须同时看。  
   - 当前策略 active lot
   - 当前策略 pending fill
   - 当前策略 pending exit
   - 最近 submit key

critical：

1. 下单不是“调 broker.placeOrder 就完了”。
2. 真正的事故都发生在 `broker accepted / db not updated / strategy thinks failed` 这种半成功状态。

### 3.4 止盈止损与保护单

必须做到：

1. live position 必须始终有 protection invariant。  
   只要有仓，就必须有保护 stop 流程处于 active 状态。

2. 保护单是风控对象，不是普通订单。  
   ghost/cleanup 不应轻易 terminalize；不应被普通 open-order sweep 扫掉；重启后要优先检查是否缺失。

3. 保护单生命周期必须与父单、持仓、broker session 强绑定。  
   必须满足：
   - 父单 fill 后才生效
   - 持仓平掉后必须撤掉
   - 重启恢复时必须验证 broker 侧是否仍真实存在
   - 不能只靠 `broker_order_id`，必须同时绑定 `strategy_id / symbol / order_ref / perm_id`

4. TP / trailing / logic retreat 的优先级必须清楚。  
   `hard stop` 风险优先；`tp/trailing` 属于收益管理；`logic_retreat` 是次级退出。

5. stop 自愈必须有限制。  
   可以补一次、可以短时间 retry，但不能无限补 stop 导致重复挂单。

critical：

1. 对股票和期权都一样：`持仓在、保护不在` 是一号高危事故。
2. 所有收益优化逻辑都不能压过这个 invariant。

### 3.5 平仓管理

必须做到：

1. flatten 是最高优先级动作。  
   一旦进入 flattening active，所有新 entry、补 stop、TP 管理都应降级或停止。

2. `close_only` 和 `flatten_action` 必须分开。  
   - `close_only`: 这单不允许增仓
   - `flatten_action`: 这是系统级清仓动作  
   这两个语义不能混。

3. 收盘强平必须走独立 flatten 语义，不能复用普通 exit。  
   `EOD flatten` 必须额外保证：
   - `close_only=true`
   - `flatten_action=true`
   - 先 cancel 同 symbol 工作单
   - 再 flatten 真仓位
   - quote 不可用时有明确 fallback

4. 平仓要先定义 scope。  
   必须明确是：
   - 平本策略持仓
   - 平 account 仓位
   - 平 symbol 净仓  
   三者不能混用。

5. 平仓后的 open-order cleanup 只能清相关保护单。  
   不能把刚发出去的 flatten 单也扫掉。

6. 平仓完成后，必须重新核对 broker qty == 0。  
   不能只看 DB 状态。

critical：

1. 绝大多数“平仓异常”都不是平仓指令本身错，而是平仓前后还有别的订单在竞争同一 symbol。

### 3.6 订单同步 / IB 同步

必须做到：

1. 对账要分 3 类。  
   - `ghost`: DB 有，IB 没有
   - `orphan`: IB 有，DB 没有
   - `mismatch`: 双方都有但状态不同

2. 所有自动修复都要 fresh broker truth check。  
   尤其是 ghost cleanup，不做这一层一定会误杀活单。

3. open-order snapshot 不要过度信单一缓存。  
   必须允许 fresh probe，不能只依赖本地 live order cache。

4. 对账修复不能越权。  
   reconciler 不应擅自分配 strategy-scoped 持仓；只能做状态校正、orphan adopt、风险告警。

5. cleanup 只能按顺序做。  
   - 先确认 broker 真仓位
   - 再确认 broker open orders
   - 先撤工作单
   - 再平真实仓位
   - 最后才修 DB 投影

critical：

1. reconciler 的职责不是“让系统看起来干净”，而是“让系统更接近 broker 真相”。
2. 一旦把“自动修正”做得比 broker truth 还激进，就一定出事故。

### 3.7 审计与可观测性

必须做到：

1. 每笔交易要能串完整链路。  
   至少有：
   - gate evaluated
   - entry allowed/blocked
   - submit requested
   - broker submit ack
   - order_submitted
   - fill event
   - protective_stop submitted
   - exit reason
   - flatten submitted
   - final state

2. 审计必须可按 `trace_id / decision_id / order_id / broker_order_id / order_ref` 反查。

3. `order_ref / decision_id / position_lot_id / perm_id` 必须贯穿整条链。  
   几乎所有“对不上”的问题，本质都来自归因链断裂。

4. runtime alert 不能只说“有问题”。  
   必须带：
   - strategy_id
   - symbol
   - order_id / broker_order_id
   - db_state
   - ib_state
   - reason
   - fresh_probe result

5. 审计要区分 correctness alert 和 noise alert。

critical：

1. 没有证据链的系统，只能靠猜。
2. 很多排查时间，本质都耗在“链路里字段不够”。

## 4. 最容易出事故的 10 类问题

### 4.1 幽灵订单

表现：

1. broker 已无单，DB 仍 `SUBMITTED`
2. reservation 仍 `HELD`
3. strategy 认为还有 pending order

根因：

1. broker terminal event 未回写
2. local pending cache 未清理
3. reconciler 只看 DB，不看 broker fresh snapshot
4. terminal order 被错误重新收养/adopt

必须做的事：

1. terminal convergence 必须统一
2. broker 无单时，DB 活单必须进入终态或 quarantine
3. adopted/orphan 逻辑必须避开 terminal row
4. stale local protective pending 必须自动失效

### 4.2 反复开仓

表现：

1. 同方向每几秒重复发 entry
2. execution 被反复 `entry_isolation_blocked`
3. 同一信号腿不断生成新 intent

根因：

1. `same_strategy_pending_orders` 注入错误
2. gate 看不到真实 working orders
3. 刚取消/刚拒绝后没有冷却窗口
4. `max_trades_per_day` 只是配置，未真实执行
5. flatten/exit in progress 时仍允许 entry 继续尝试
6. `accepted` 被错误当作 `filled/live`
7. DB 查询异常被当成 `db_pending=0`，把“未知”误判成“干净”
8. runtime guard 只会放大 pending，不会纠正上游 stale pending

必须做的事：

1. pending/open count 由运行时实时计算，而不是只依赖 feature 输入
2. 同策略同票未终态订单存在时，禁止重复 entry
3. 增加 rearm/cooldown，阻止秒级重复进场
4. `max_trades_per_day` 必须在真实下单路径生效
5. 下单前必须检查 lane gate
6. `pending_fill` 未终结前，不得进入 `live` 管理逻辑
7. `db_pending` 探测失败时必须 fail-closed，禁止自动 cleanup stale lot
8. runtime guard 必须能覆盖错误上游值（包含从 `2/1 -> 0` 的纠偏）
9. pending 明细审计必须按 symbol 或 exec lane 隔离，不能共用全局 detail

### 4.3 反向开仓

表现：

1. 本应平仓，却又开了反向新仓
2. 长仓未清干净时又去开空

根因：

1. entry 与 flatten/exit 逻辑没有严格顺序
2. strategy scope position 与 account net position 混用
3. foreign position isolation 不严格

必须做的事：

1. close-only 必须按策略拥有仓位判断，不能只看账户净仓
2. 开反向前，必须确认原方向仓位、活单、保护单全部终态
3. 持仓归属必须按策略隔离
4. flatten scope 必须先定义

### 4.4 止盈止损单异常

表现：

1. 保护单只存在于本地，不在 broker
2. OCO 没挂上
3. 一个 child 成交后兄弟单没撤
4. offhours 单型不兼容或 tick 不合法

根因：

1. 保护单只做了逻辑计划，没做 broker child order
2. 下单后没有确认 broker 可见性
3. `perm_id` 还没稳定就写入 0
4. stop/limit 价格未按 `minTick` 归一化
5. stale `SL/TP` 子单没有被完整回收

必须做的事：

1. 保护单必须 broker-side 落地
2. 挂单后必须 fresh probe 确认 broker 真的看得到
3. `perm_id=0/None` 不得当有效值使用
4. 所有价格在 broker submit 前按 tick size 标准化
5. OCO 生命周期要有专门测试
6. stop 自愈必须有限次、限速、可审计
7. 子单回收必须和持仓归零、父单终态一起验证

### 4.5 平仓异常

表现：

1. 平仓单发出后仓位未归零
2. 平仓后 DB 还留保护单/entry 活单
3. flatten 时又被 session/time gate 错误拦住

根因：

1. flatten 与 close-only 约束设计不清
2. 保护单未先撤或撤销未收敛
3. flatten 没有绝对优先级
4. flatten 前后还有别的 order role 在竞争同一 symbol
5. EOD flatten 复用了普通 exit 语义

必须做的事：

1. flatten 应独立于普通 entry gate
2. flatten 优先级高于新开仓
3. flatten 后强制复查 broker positions/open orders/DB orders/reservations
4. flatten 后 open-order cleanup 只能清相关保护单
5. EOD flatten 必须有完成确认，不能只发不验

### 4.6 仓位不同步

表现：

1. broker qty 与 DB qty 不一致
2. 账户仓位和策略仓位对不上

根因：

1. fill fanout 漏写
2. broker exec event 丢失
3. reconciliation 没有按 symbol/strategy 正确归因
4. 手工单/外部单进入账户但未隔离
5. broker 真相源没有被放在本地投影之前

必须做的事：

1. 定时对账 broker positions vs DB positions
2. mismatch 必须分级：
   - 可自动修正
   - 需 quarantine
   - 需人工介入
3. 对外部/未知仓位必须 quarantine，不能默认纳入策略
4. 自动修复前必须先做 fresh broker truth check

### 4.7 下单异常

表现：

1. pipeline approved 但 broker 没单
2. broker submit ack 了但立刻被拒
3. reservation failed / invalid price / session rejected

根因：

1. 风控额度未释放
2. session 配置与市场时段不匹配
3. 数据快照缺失导致 qty 回落为 1 或直接错误下单
4. broker error 没有足够可观测性

必须做的事：

1. 预算和 reservation 必须按 working orders 实时释放
2. RTH / PRE / POST / OVERNIGHT 必须显式配置和校验
3. 没有 exec symbol snapshot 时禁止 fallback 到危险默认值
4. 所有 broker error 都要结构化记录
5. 拆分 broker accepted 与 db persisted submitted
6. quote 不可用时，flatten 没有明确 fallback

### 4.8 order 管理与 IB 同步异常

表现：

1. IB 有单，DB 没单
2. DB 有单，IB 没单
3. old clientId 下的单看不见

根因：

1. 只依赖本地 live order cache
2. 未做 fresh open-order probe
3. probe 和 owner-client 语义不清
4. orphan/adopt 规则不严谨

必须做的事：

1. 周期性 fresh open-order probe
2. owner-client 与 probe-client 逻辑分离
3. orphan 处理必须分三类：
   - 可匹配 DB
   - terminal duplicate
   - truly unknown orphan
4. terminal duplicate 不得重新 adopt 成活单
5. 禁止用“当前 snapshot 看不到”直接推断 broker 不存在
6. 不要在脏状态下随便起一个新 SafetyKernel 做清理，可能把 stale `SL/TP` 子单重新激活

### 4.9 审计缺失

表现：

1. 下单失败但找不到为什么
2. 无法串起 signal -> submit -> fill -> position

根因：

1. trace 字段传递不全
2. 关键事件没有落 audit
3. 只打非结构化日志

必须做的事：

1. 所有关键链路统一结构化日志
2. 强制传递 `strategy_id / trace_id / order_ref / broker_order_id / perm_id`
3. 审计查询工具必须能按小时、symbol、trace、strategy 拉链
4. 没有最低审计字段的事件，应视为可观测性降级

### 4.10 测试配置污染生产

表现：

1. 开盘后仍被夜盘 cutoff 挡住
2. force test entry 还在不断触发
3. max_trades_per_day 被放大

根因：

1. 临时测试配置未回滚
2. 测试态与生产态没有清晰边界

必须做的事：

1. 所有临时开关必须有回滚记录
2. 开盘前必须跑生产态配置核对
3. 测试态和生产态 profile 显式分离

## 5. 股票和期权都必须遵守的统一设计要求

1. 统一身份模型。  
   股票和期权都必须使用同一套 trace / order / fill / reservation 归因规则。

2. 统一状态机。  
   entry、exit、protective、flatten、cancel、reject、fill、reconcile correction 需要同一套终态定义。

3. 统一审计与运维入口。  
   查询工具、cleanup 工具、manual flatten 工具都要兼容股票和期权。

4. 统一隔离语义。  
   至少要支持：
   - 同策略防重
   - 跨策略隔离
   - 外部仓位隔离
   - 未知仓位 quarantine

5. 统一保护语义。  
   股票和期权都要支持 broker-side protective order，且支持恢复后重建。

6. 统一持仓状态语义。  
   `pending_fill` 和 `live` 必须显式区分，任何策略都不能跳过这一层。

7. 统一异常降级语义。  
   遇到不确定状态时，默认进入：
   - freeze new entry
   - protect or flatten decision
   - audit and alert  
   而不是自动猜测并继续交易。

### 5.1 期权额外更严的点

1. 期权 symbol identity 更复杂。  
   `internal symbol / IB contract / expiry / strike / right` 必须稳定一一映射。

2. orphan adopt 更危险。  
   不能因为标的一样，就误认成同一策略订单。

3. 平仓与保护更容易跨腿串单。  
   dual-leg / spread / stack 需要明确 parent-child 关系。

4. 数据延迟、合约切换、到期、流动性问题，会放大 not-ready 与 wrong-reconcile 风险。
5. option OCO 更复杂，保护单与父仓位绑定必须比股票更严格。

## 6. 以后开发必须遵守的规则

1. 任何新策略上线前，先定义 position ownership 和 order ownership。
2. 同 symbol 多策略，必须先定义隔离边界，再写策略逻辑。
3. 新增 order role 时，必须同步更新：
   - strategy cleanup/filter
   - reconciler classification
   - audit fields
   - manual ops 文档
4. 新增自愈逻辑时，必须同时加：
   - dedupe
   - throttle
   - kill switch
   - audit event
5. 所有 auto-terminalize / auto-cancel / auto-flatten 逻辑，都必须先回答：
   - broker truth 从哪里来
   - fresh probe 失败时怎么办
   - 是否会误伤保护单/平仓单
6. 禁止用 account-level broker position 直接写 strategy-scoped DB position。
7. 禁止用“当前 snapshot 看不到”直接推断 broker 不存在。
8. 禁止在 flatten 后无过滤地扫 symbol 全部 open orders。
9. 禁止让 `logic_retreat`、审计自愈、reconcile 修复，与 stop/flatten 并发争抢同一仓位。
10. 任何 restart recovery 都必须先保护，再恢复交易。
11. 新策略只写 signal / entry plan / exit plan，不要自己写一套 position/order 真相。
12. option 与 stock 可以共用执行骨架，但不能共用脆弱假设。

## 7. 运维巡检清单

### 7.1 开盘前

1. `IB` 连接正常。
2. 账户仓位符合预期。
3. 活动订单都有归属。
4. `DB core_position` 与 broker 仓位一致。
5. `DB core_order` 无残留 `SUBMITTED/NEW` 僵尸单。
6. 保护单审计链正常写入。
7. 关键策略 state 新鲜。
8. 行情/特征 warmup 充足。
9. 生产态配置核对通过：
   - 无 `force_test_entry_*`
   - `allowed_sessions` 正确
   - `data_profile` 正确
   - `no_new_after_et / flatten_before_close_et` 正确
   - `max_trades_per_day` 正确

### 7.2 盘中

1. 每次有持仓但无 stop，立即 high severity。
2. 每次 ghost/orphan/mismatch，都要先看 fresh probe。
3. 每次 unexpected inventory，都先 freeze new entry。
4. 同 symbol 多策略若都活跃，重点看 ownership 冲突。
5. flatten in progress 时，不允许新 entry 抢控制权。
6. 第一笔单必须盯完整闭环：
   - signal ready
   - pipeline approved
   - broker submit ack
   - fill received
   - pending_fill -> live
   - OCO attached
   - recon still clean

### 7.3 收盘后

1. 所有 open orders 清零核对。
2. 所有非零仓位核对。
3. 当天 runtime alert 分类复盘。
4. ghost/orphan 是否有 unresolved 残留。
5. state file / audit file / order_event 是否完整。
6. 收盘前重点看：
   - `close_only / flatten_action` 是否触发
   - working orders 是否被先 cancel
   - 收盘后 recon 是否 0 mismatch

## 8. 事故排查 SOP

发生异常时，必须按下面顺序做，不要一上来改代码。

1. 查 broker 真实仓位和 open orders。
2. 查 DB positions / orders / reservations。
3. 查最近 1 小时 audit 链路。
4. 判断问题属于：
   - 策略信号问题
   - gate/risk 阻断问题
   - broker submit 问题
   - fill/reconcile 收敛问题
   - ownership/role 问题
5. 先收敛状态，再恢复交易。
6. 对不确定订单，优先 `skip + audit + freeze`，不要抢着 terminalize。
7. 只有在 root cause 明确后，才改代码。
8. DB 探测失败时，不得把未知状态当 clean；默认保持阻断。
9. 看到下面这些信号，先停看，不要继续交易：
   - `fill_context_missing`
   - `adopted_reconciler` 接管本应归策略的单
   - `POSITION_OPEN` 但无 fill
   - broker 有仓 / DB 无仓
   - DB 有仓 / broker 无仓

## 9. 代码改动红线

1. 不要为了排障改系统封装脚本。
2. 不要把策略专有补丁写进全局基础设施，除非问题本身是全局 root cause。
3. 不要靠人工 cleanup 作为长期方案。
4. 不要引入二义性身份：
   - 截断 `order_ref`
   - `perm_id=0` 也写入
   - note 里混 JSON 和纯文本
5. 不要让 fail-open 的观测逻辑吞掉风险信号。
6. 不要让 logic retreat、审计自愈、reconcile 修复，与 stop/flatten 并发争抢同一仓位。
7. 不要把 reconciler 当成日常执行链的一部分来依赖。
8. 不要把上游 feature 的 pending count 当最终真相；runtime 必须可纠偏。
9. 不要把 runtime pending detail 挂在单一全局字段，必须有 lane 级隔离。

## 10. 必须补齐的自动化测试

1. 同策略重复 entry 防重测试。
2. 反向开仓隔离测试。
3. broker-side protective OCO 挂单测试。
4. stop 成交后 TP 自动取消测试。
5. broker 无单但 DB 活单时的自动收敛测试。
6. stale local protective pending 清理测试。
7. `perm_id` 延迟可用时的归因测试。
8. tick size 归一化测试。
9. startup recovery 对已有仓位重建保护测试。
10. session/profile/config 回滚测试。
11. flatten 优先级压过 entry/logic retreat 的测试。
12. 多策略同 symbol ownership 隔离测试。
13. orphan adopt 不越权测试。
14. 持仓不一致时自动 freeze new entry 测试。
15. `accepted != filled` 状态机测试。
16. `close_only` 与 `flatten_action` 语义分离测试。
17. protection position-attached 而不是 order-attached 的恢复测试。
18. fill handler 幂等测试：
   - 同一 `exec_id/fill_id` 只处理一次
   - partial fill 与 terminal status 分别去重

## 11. 建议长期建设项

1. 增加统一的生产态配置检查器。
2. 增加开盘前红线检查自动任务。
3. 增加统一 incident dashboard：
   - orphan count
   - mismatch count
   - stale working order age
   - reservation without broker order
   - broker order without DB row
4. 增加策略级 incident score，超过阈值自动切 close-only 或 quarantine。
5. 增加统一 ownership registry，显式声明：
   - strategy -> symbols
   - strategy -> order roles
   - strategy -> flatten scope
6. 增加统一 position state machine contract，禁止策略私自扩展不兼容状态。

## 12. 本次事故群暴露出的具体教训

1. 测试态配置留在线上，会直接把开盘逻辑挡死。
2. 没有 broker-side 保护单，策略进程一旦异常就没有真正风险兜底。
3. `perm_id` 不稳定、`order_ref` 被截断，会直接削弱 reconciliation。
4. broker 已无单但 DB 仍 `SUBMITTED`，是所有幽灵订单问题的核心症状。
5. pending-order guard 必须看真实 working state，不能看空洞 feature。
6. note 字段的数据契约必须统一，不能一部分 JSON、一部分纯文本。
7. flatten 后若无角色过滤扫单，极易误伤 flatten 自己或关键保护单。
8. 持仓不一致时继续开新仓，比看错一次仓更危险。
9. reconciler 的职责不是让界面干净，而是让系统更接近 broker 真相。
10. `accepted` 被错当 `filled`，会把所有后续问题放大成系统性事故。
11. 保护单是仓位的风险附件，不是某次下单事件的附件。
12. “DB 异常 = clean”是危险假设，必须 fail-closed。
13. runtime guard 如果不能把 stale 值纠偏回真实值，隔离 gate 会长期假性阻断。
14. pending 明细若没有 lane 级隔离，盘中审计会误导排障方向。

## 13. 新问题追加模板

以后每次遇到新问题，都按下面模板追加到本文件末尾。

```md
## Incident: <日期> <简短标题>

### 现象
- 

### 影响
- 

### 根因
- 

### 直接修复
- 

### 长期修复
- 

### 新增约束 / 不变量
- 

### 新增测试
- 

### 是否需要更新上线清单
- 是 / 否
```

## 14. 结论

要防止幽灵订单、反复开仓、反向开仓、止盈止损异常、平仓异常、仓位不同步、order 与 IB 不同步，真正有效的办法不是加更多补丁，而是：

1. 把 ownership、身份、状态机、保护单、对账、审计做成统一契约。
2. 让所有提交都是幂等的，所有非终态都必须自动收敛。
3. 让 protection invariant 和 flatten priority 成为硬约束。
4. 让所有测试态配置都能被显式发现并回滚。
5. 让 broker、DB、audit 三条线始终能互相证明。
6. 让 `accepted != filled`、`close_only != flatten_action`、`protection is position-attached` 成为系统级规则。

这份文档以后就是开发和运维的基准线。任何新策略、新 broker 适配、新保护逻辑、新对账逻辑，都应该先对照它审查，再上线。
