好的，我把你和 gemini 的方案「取长补短」后，整理成一份**可以马上落地**的最终执行方案（不写代码，只给清晰的改造点、接口约定、订单拓扑与验收标准）。本版遵循你的最新取舍：**仅用 IBKR 扫描+L1 250ms 内存价、放弃低频再锚、改为分批高位止盈**。

---

# 一页总纲（TL;DR）

* **数据**：09:27 ET 用 **IBKR Scanner** 拉 Top50 → 轻量过滤/打分 → **仅 Top3** 建立 tick 与 1m/5m 内存聚合；**完全不用 ClickHouse 做决策**。
* **指标**：VWAP / RVOL / ORB5 / PM_H 全部来自 **PriceCache**（Kafka→内存聚合，250ms 刷新）。
* **执行**（09:33–09:35 ET）：满足“四确认”即下单：**价>VWAP、突破≥max(PM_H, ORB5_H)、RVOL_1m&5m≥2、spread≤0.25%**。
* **订单拓扑**：**父单 STP LMT 入场** + **多档止盈 LMT** + **单一止损 STP**，**全部放同一 OCA 组，OCA Type=ReduceByAmount**（TP 部分成交后，IBKR 自动按成交量**减少**止损/其余子单数量）→ **无需再锚与改单**。
* **风控**：单笔风险=净值 0.5%，10:10 ET **强平**，Scanner 失败**当日不做**。

> 关键点：借助 **IBKR 的 OCA “ReduceByAmount”** 语义，把“分批高位止盈 + 剩余头寸保护止损自动减量”一次性声明完成，**不需要在策略里做撤单/改单**。

---

## 1. 模块改造与职责

### 1) `PriceCache`（新增，libs/cache/price_cache.py）

**职责**：低延迟行情心脏。消费 Kafka L1 tick（≈250ms），对 **Top3** 做内存快照与分钟聚合，暴露查询接口给策略。

* **输入**：`Tick{symbol, last, bid, ask, size, ts_us}`（来自 Kafka / ib_market_data 回调）。
* **维护**：

  * `PriceSnapshot`：`last/bid/ask/bidSize/askSize/ts_us`
  * `MinuteRollup`：`vwap_1m、vol_1m、rvol_1m、rvol_5m、pm_high/pm_low（04:00–09:30）、orb5_high/low（09:30–09:35）`
* **API（给 StrategyContext）**：

  * `price(sym) -> PriceSnapshot`
  * `minute_stats(sym) -> {vwap, rvol_1m, rvol_5m, pm_high, orb5_high, spread}`
* **实现要点**：

  * 以 **纽约时区**滚动 1m（DST 友好）；
  * RVOL 先用“近样本滚动均量”做简化，足够稳；
  * 服务必须在 **04:00 ET 前**启动以累积 PM_H/L（若当日首次启动晚于 04:00 → 不交易该票）。

### 2) `StrategyContext`（小改）

* 新增只读接口直连 PriceCache：

  * `ctx.get_price(symbol)`
  * `ctx.get_minute_stats(symbol)`
* 不改历史慢路径；策略**不依赖** CH 取价。

### 3) 扫描封装（复用/轻改 scan_top_gainers.py）

* **09:27 ET** 调 IBKR **Scanner**：`scanCode="TOP_PERC_GAIN"`, `instrument="STK"`, `locationCode="STK.US.MAJOR"`, `rows=50`。
* **过滤**：`price≥$5`、`spread≤0.25%`、`pm_notional≥$10M`（若返回无该字段，做一次**单次快照**估算即可）。
* **打分**（简单且稳定）：涨幅 40% + 盘前额 40% +（可选）催化 20% → 取 **Top3**。
* 将 **Top3** 注册到 PriceCache（tick + 1m/5m 聚合）。

> 按你的要求：**不做兜底自算**；Scanner 异常/空 → 当日不做（记录原因）。

---

## 2. 策略状态机与条件

### 时间窗（ET）

* **SCAN**：09:27 扫描 → Top3
* **OBSERVE**：09:30–09:32 打 ✅（量强/贴近 PM_H/价差≤0.30%）
* **EXECUTE**：09:33–09:35 四确认通过 → 下单
* **MANAGE**：09:37–09:40 观察（无需再锚，有 OCA 自动减量）
* **FORCE EXIT**：≤10:10 全平

### 执行四确认（来自 PriceCache）

1. `price.last > vwap_today`
2. `price.last ≥ max(pm_high, orb5_high)`
3. `rvol_1m ≥ 2.0 且 rvol_5m ≥ 2.0`
4. `spread_pct ≤ 0.25%`

---

## 3. 订单拓扑（一次性声明，零再锚）

> 目标：**父入场 + 多个分批止盈 + 单一止损**，当 TP 部分成交时，**止损与未成交 TP 自动减量**，无需策略改单。

### 3.1 结构

* **Parent**：`STP_LMT`

  * `stop_px = breakout_level * (1 + 0.002)`
  * `limit_px = stop_px * (1 + 0.001)`
* **Children（同一 OCA 组）**：

  * **StopLoss**：`STP`（建议市价触发，防卡单），`qty = full`，`stop_px = max(VWAP_now, PM_L*0.99)`
  * **TP1..TPn**：`LMT`（多档），`qty_i`、`limit_px_i`
* **OCA 组设置**（关键）：

  * `oca_group = "G_{strategy}_{symbol}_{date}"`
  * `oca_type = ReduceByAmount`（IBKR 的 OCAType=2），**当任一子单部分/全部成交时，其它子单（包含止损）的数量按成交量**自动减少，避免超卖/裸奔
  * 子单 `transmit=False` 链接，最后一个 `transmit=True` 原子提交

> 若执行层未暴露 OCAType 字段：在 `OrderStyle.extras` 或适配层补充 `oca_type="REDUCE_BY_AMOUNT"` 透传即可（无需改引擎核心）。

### 3.2 止盈档位（默认）

* **百分比版**：

  * TP1：`+3.5%`，30%
  * TP2：`+5.5%`，35%
  * TP3：`+8.0%`，35%
* **高波动票（TSLA/SMCI/NVDA）**可切 **ATR 版**：

  * `+1.5×ATR_1m / +2.5×ATR_1m / +3.5×ATR_1m`（对应 30/35/35%）

> 因为用了 **OCA ReduceByAmount**，**无需**在 TP 成交后手动改止损数量；IBKR 会按成交量自动减至剩余头寸。

---

## 4. 风控与仓位

* **单笔风险**：`R = 0.5% × 账户净值`
* **股数**：`shares = floor( R / (entry - stop_px) )`（若 `entry≤stop` 则放弃）
* **价差超限**：观察期 `≤0.30%`，执行期 `≤0.25%`
* **宏观事件**：CPI/FOMC/非农等高冲击前 30 分钟默认 **不做**
* **强平**：10:10 ET 无条件平仓，撤销全部未成交子单

---

## 5. 集成点（对现有工程的最小改动）

1. **`ib_market_data.py` → PriceCache**

   * 在 tick 回调中 `await price_cache.handle_tick(tick)`（或同步调用 + 内部协程排队）。
   * 增加 `subscribe_ticks(symbols)` 把 Top3 注册到 PriceCache。

2. **`StrategyContext`**

   * 暴露 `get_price()/get_minute_stats()` 两个只读方法（直连 PriceCache 单例）。

3. **`scan_top_gainers.py`**

   * 提供 `scan_top_gainers_ibkr(rows=50)` 方法（09:27 调用一次，返回基本字段）。
   * **只返回 Top3**，策略拿到后完成订阅即可。

4. **执行/适配层**

   * `DecisionEnvelope → IBKR` 映射中，支持在 **子单**携带：

     * `oca_group` 与 `oca_type=REDUCE_BY_AMOUNT`
     * `parentId`（指向父入场）与 `transmit` 链
   * 若当前已有“经典 bracket（止盈1+止损1）”封装：

     * 扩展为“**多 TP + 单 Stop 的 OCA ReduceByAmount**”模板（无需 AMEND 支持）。

---

## 6. 配置（建议默认值）

| 键                         |                   默认 | 说明        |
| ------------------------- | -------------------: | --------- |
| `scan_time_et`            |           `09:27:00` | 盘前扫描      |
| `entry_start_time_et`     |           `09:33:00` | 执行窗起      |
| `entry_end_time_et`       |           `09:35:00` | 执行窗止      |
| `force_exit_time_et`      |           `10:10:00` | 强平        |
| `scanner_rows`            |                 `50` | IBKR 扫描条数 |
| `price_min`               |                 `$5` | 硬过滤       |
| `pm_notional_min`         |               `$10M` | 盘前额门槛     |
| `spread_max_observe/exec` |      `0.30% / 0.25%` | 观察/执行     |
| `breakout_buffer_pct`     |              `0.20%` | 触发缓冲      |
| `limit_buffer_pct`        |              `0.10%` | 限价缓冲      |
| `tp_levels_pct`           | `[0.035,0.055,0.08]` | 止盈档       |
| `tp_qtys_pct`             |   `[0.30,0.35,0.35]` | 分摊        |
| `risk_per_trade_pct`      |              `0.005` | 单笔风险      |

---

## 7. 验收标准（上线必测）

**功能**

* 09:27 ET 能产出 **Top3**（日志含入围/淘汰原因）。
* Top3 的 VWAP/RVOL/PM/ORB 指标来自 **PriceCache（内存）**，无 CH 查询。
* 09:33–09:35 触发四确认→一次性下发 **父入场 + 多 TP + 单 Stop**，**同一 OCA 组 + OCAType=ReduceByAmount**。
* 任一 TP 部分/全额成交后，**止损数量自动减少**（TWS 侧可视化验证，不需要策略改单）。
* 10:10 ET **仓位为 0，挂单清空**。

**延迟/执行**

* 价信号→提交意图 ≤ **150ms**；提交→IB 回执 ≤ **300ms**（环境相关）。
* 入场滑点日均 ≤ **10bp**（连续 5–10 个交易日采样）。

**稳定性**

* Scanner 异常→“当日不做”且告警；
* PriceCache 延迟 >1s → 放弃该票并记录；
* 无“裸奔”窗口（父单成交后的 **≤200ms** 内，子单/OCA 生效）。

---

## 8. 上线步骤（3–5 天）

* **D1**：接 Kafka→`PriceCache` 与 `ctx.get_price/minute_stats`；写好指标滚动（1m/5m）。
* **D2**：接 IBKR Scanner（09:27 定时）、Top3 订阅到 PriceCache；策略读内存指标跑四确认。
* **D3**：适配“**多 TP + 单 Stop** 的 **OCA ReduceByAmount**”订单模板；纸盘连通 TWS/Gateway 验证 OCA 行为。
* **D4–D5**：干跑测试（3 个交易日），记录滑点/触发/TP 部分成交后的止损减量是否如预期；若稳定 → 小额实盘。

---

## 9. 重要边界与备忘

* **必须启用 OCAType=ReduceByAmount**：否则“多 TP + 单 Stop”会造成数量不一致或需要策略层改单（与“无再锚”目标冲突）。
* **PM_H 可靠性**：服务需在 04:00 ET 前启动；若当日首次启动晚于 04:00 且 PM_H 不足 → **不做该票**。
* **止损类型**：优先 `STP`（市价触发）避免卡单；TP 才用 `LMT`。
* **不做兜底自算**：严格按你要求，Scanner 失败→当天不做。
* **未来可选**：TP1 成交后**一次性**把止损抬 BE（仅一次 CANCEL+NEW，不属于追踪再锚；如暂不改引擎，先不做）。

---

如果你认可这版，我们就按此落地：

* D1–D2 先把 **PriceCache + Scanner + Top3 订阅** 跑起来；
* D3 把 **OCA ReduceByAmount** 的订单模板接到 `DecisionEnvelope → IBKR`；
* 用纸盘完整走一遍时间窗，确认 **TP 部分成交 → 止损自动减量**链路无误，再小额实盘。
好嘞，继续把“最终方案”打磨到可上线水准。下面给你一份**落地蓝图 + 接口契约 + 订单编排 + 测试与运维跑法**。不写代码，但每一处都标清“要做什么、在哪儿做、怎么验”。

---

# 0) 总体路线（与你当前取舍完全一致）

* **数据**：仅用 **IBKR Scanner**（09:27 ET）+ **Kafka→内存 L1 tick（≈250ms）**。指标全部由内存聚合提供，不用 ClickHouse 做决策。
* **执行**：09:33–09:35 ET 满足“四确认”即下单。
* **订单**：父单 **STP_LMT** + **多档 TP(LMT)** + **单一 SL(STP)**，**同一 OCA 组 + OCAType=Reduce**（ReduceByAmount）。**不做追踪再锚**。
* **风险**：单笔 0.5% 账户净值，10:10 ET 强平；Scanner 失败**当日不做**。

---

# 1) 模块与接口契约

## 1.1 PriceCache（新增模块，libs/cache/price_cache.py）

**职责**：消费 Kafka L1，维护快照与 1m/5m 聚合，给策略提供低延迟指标。

* **输入事件**（来自 ib_market_data / Kafka）
  `Tick{symbol, last, bid, ask, last_size, bid_size, ask_size, ts_us}`

* **内部数据**

  * `PriceSnapshot{last, bid, ask, bidSize, askSize, ts_us}`
  * `MinuteRollup{vwap, vol_1m, rvol_1m, rvol_5m, pm_high/pm_low(04:00–09:30), orb5_high/low(09:30–09:35)}`

* **外部 API（由 StrategyContext 暴露）**

  * `ctx.get_price(symbol) -> PriceSnapshot | None`
  * `ctx.get_minute_stats(symbol) -> MinuteRollup`（字段至少包含：`vwap, rvol_1m, rvol_5m, pm_high, orb5_high, spread_pct`）

* **运行约束**

  * 服务应 **04:00 ET 前**启动以累积 PM_H/L。若晚启动且 PM_H 缺失 → **放弃该票**（策略要能优雅跳过）。
  * 仅对 **Top3** 做订阅（tick + 1m/5m 聚合），避免路数/节流风险。

## 1.2 StrategyContext（小改）

新增只读方法直连 PriceCache：

* `get_price(symbol)`
* `get_minute_stats(symbol)`

> 历史慢路径依然保留，但策略**不依赖**它做开盘决策。

## 1.3 扫描封装（复用 scan_top_gainers.py）

* **函数**：`scan_top_gainers_ibkr(rows=50) -> List[Candidate]`
  `Candidate = {symbol, last, changePct, pmNotional?, spread?, ...}`

* **过滤**（必须项）

  * `price ≥ $5`
  * `spread ≤ 0.25%`（可用一次性快照测）
  * `pmNotional ≥ $10M`（若扫描返回无此字段，可做一次性快照估算）

* **打分**（简洁稳健）

  * `score = 0.4*changePct + 0.4*pmNotionalNorm + 0.2*catalystFlag`
    → 取 Top3，交由 PriceCache 订阅。

> 你的要求：**不做兜底自算**；Scanner 异常/空 → **当日不做**，写明原因。

---

# 2) 策略条件与时间窗口（ET）

* **SCAN**（09:27）：执行扫描 → 产出 Top3 → 注册到 PriceCache。
* **OBSERVE**（09:30–09:32）：标记 ✅（量强 / 贴近 PM_H / spread ≤ 0.30%）。
* **EXECUTE**（09:33–09:35）：触发“四确认”即下单：

  1. `price.last > vwap_today`
  2. `price.last ≥ max(PM_H, ORB5_H)`
  3. `rvol_1m ≥ 2.0 且 rvol_5m ≥ 2.0`
  4. `spread_pct ≤ 0.25%`
* **MANAGE**（09:37–09:40）：仅观察与统计（订单由 OCA 自动协同，**无需再锚**）。
* **FORCE EXIT**（≤10:10）：无条件平仓，撤销未成交子单。

---

# 3) 订单编排（一次性声明，零改单）

> 关键：利用 **OCA 组的“Reduce/ReduceByAmount”语义**，当任何 TP 成交（部分/全部）时，**剩余子单（含止损）的数量自动减少**，避免超卖和手工改单。

## 3.1 括号/组结构

* **Parent（入场）**：`STP_LMT`

  * `stop_px = level * (1 + breakout_buffer)`（`level = max(PM_H, ORB5_H)`；默认 +0.2%）
  * `limit_px = stop_px * (1 + limit_buffer)`（默认 +0.1%）
  * `tif="DAY"`，`outsideRth=True`（如需）
  * `transmit=False`（作为父单）
* **Children（全部同一 OCA 组）**：

  * **StopLoss**：`STP`（建议市价触发），`qty = full`，`stop_px = max(VWAP_now, PM_L*0.99)`，`parentId=Parent`，`ocaGroup=...`，`ocaType=Reduce`，`transmit=False`
  * **TP1..TPn**：`LMT`，`qty_i`、`limit_px_i`，`parentId=Parent`，`ocaGroup=...`，`ocaType=Reduce`，**最后一个 `transmit=True`**（原子提交整组）

> **OCA 类型命名**在各实现里可能叫 `Reduce / ReduceByAmount`；数值枚举请以你适配层为准（常见为 “2=Reduce”）。**落地时务必在适配层确认常量名/枚举值**，确保“按成交量减少其它订单数量”的语义生效。

## 3.2 止盈档位（默认）

* **百分比版**：`TP1 +3.5%（30%）/ TP2 +5.5%（35%）/ TP3 +8.0%（35%）`
* **高波动票 ATR 版**（TSLA/SMCI/NVDA 可用）：`+1.5×ATR_1m / +2.5×ATR_1m / +3.5×ATR_1m`（对应 30/35/35%）

## 3.3 风控配仓

* `R = 0.5% × 账户净值`
* `shares = floor( R / (entry - stop_px) )`（若 `entry ≤ stop` → 放弃）
* 观察期 `spread ≤ 0.30%`；执行期 `≤ 0.25%`

---

# 4) 执行层映射（DecisionEnvelope → IBKR）

**意图字段建议**：

* `order_style.kind`: `"STP_LMT" | "STP" | "LMT"`
* `order_style.stop_px / limit_px / tif / outside_rth`
* `extras`：

  * `oca_group: str`（统一 OCA 字符串）
  * `oca_type: "REDUCE"`（或枚举），**务必传递**
  * `parent_ref / parent_client_id`（由执行器解引用为 `parentId`）
  * `transmit: bool`
  * （可选）`good_after_time`（若你要用时间锁，通常不需要）

**执行器/适配层需保证**：

* 能生成**稳定的 clientOrderId**，并把父->子的 `parentId` 串好；
* 按 “父 transmit=False → 子 transmit=False … → **最后一个子 transmit=True**” 的顺序提交；
* 正确把 OCA 字段落到 IBKR 的 `OcaGroup / OcaType`；
* 若目前模板只支持“1 TP + 1 SL”括号：**扩展为 N TP + 1 SL + OCA Reduce**，其余保持不变。

---

# 5) 监控与告警（必须落地）

**关键日志事件**

* `SCAN_TOP3`（含入围/淘汰原因）
* `EXEC_CHECKS`（四确认各布尔与数值）
* `ENTRY_PLACED`、`CHILDREN_PLACED`（含 OCA 组号）
* `TP_FILLED(partial)`（记录成交数量与剩余子单自动减量后的状态）
* `STOP_FILLED`、`FORCE_EXIT`
* `STAND_DOWN`（Scanner 异常/空或 PM_H 缺失等）

**指标**

* Tick 延迟（最新 tick ts_us 与当前时间差）
* VWAP/RVOL 新鲜度（分钟结算延迟）
* 执行延迟（触发→下单）、入场滑点
* OCA 组里订单数量一致性（应与持仓匹配）

**告警**

* Scanner 异常/空 → 告警并当日停做
* PriceCache 延迟 > 1s 或 PM_H 缺失 → 放弃该票并记录
* 09:35 未入场 → 当日不做（信息级）
* OCA 提交失败 / 子单与头寸不一致 → 严重告警

---

# 6) 测试清单（纸盘先行）

**单元/组件**

* PriceCache：1m 滚动、PM_H/ORB5 计算正确性（跨分钟/边界）
* Spread 计算与阈值门控
* RVOL 近样本简化算法（波动时不抖动）

**集成（IBKR 纸盘）**

* 09:27 扫描 → Top3 → 订阅成功
* 09:33–09:35 四确认触发 → 一次性提交**父+多 TP+SL**，同一 OCA 组
* 随机触发 TP1/TP2 **部分成交**：验证止损与其余 TP **自动减量**（TWS 中持仓/挂单数量一致）
* 10:10 强平：仓位 0，挂单清空

**对照场景**

* 宽价差（>0.25%） → 拒绝入场
* PM_H 缺失（服务晚启动） → 拒绝该票
* 扫描返回空 → 当日停做
* LULD/Halt → 强平路径验证（可模拟）

---

# 7) 配置建议（可放 config.yml）

```yaml
strategies:
  - id: premarket_gainer_v1
    module: strategies.premarket_gainer_v1.PremarketGainerV1
    params:
      cadence_s: 60
      ttl_ms: 2000
      extras:
        scan_time_et: "09:27:00"
        entry_start_time_et: "09:33:00"
        entry_end_time_et: "09:35:00"
        force_exit_time_et: "10:10:00"
        scanner_rows: 50
        price_min: 5.0
        pm_notional_min: 10000000
        spread_max_observe: 0.0030     # 0.30%
        spread_max_exec: 0.0025        # 0.25%
        breakout_buffer_pct: 0.002     # +0.20%
        limit_buffer_pct: 0.001        # +0.10%
        rvol_min: 2.0
        risk_per_trade_pct: 0.005
        tp_levels_pct: [0.035, 0.055, 0.08]
        tp_qtys_pct:   [0.30, 0.35, 0.35]
```

---

# 8) 上线步骤（3–5 天）

* **D1**：接 Kafka→PriceCache；在 StrategyContext 暴露 `get_price/minute_stats`。
* **D2**：Scanner 封装（09:27 调用）→ Top3 订阅 PriceCache；策略读取内存指标跑四确认。
* **D3**：执行层落地“**多 TP + 单 SL** 的 **OCA Reduce**”模板；纸盘连通验证**部分成交→自动减量**。
* **D4–D5**：干跑 3 个交易日：观察滑点、成交率、OCA 自动减量一致性；稳定后小额实盘。

---

# 9) 重要边界与建议

* **OCA 类型务必确认**：适配层需要映射到 IBKR 的“按成交量减少其余订单”的枚举（常称 Reduce/ReduceByAmount）。不要用 Cancel 类型。
* **止损建议 STP（市价触发）**：避免 `STP_LMT` 卡单；TP 才用 LMT。
* **服务启动时间**：确保 04:00 ET 前启动以积累 PM_H/L。
* **不做兜底自算**：严格按你的决定；Scanner 不可用即停做。

---

如果你要，我可以把上面这套**订单模板（父+多TP+SL+OCA Reduce）**的 `DecisionEnvelope.extras` 字段清单，连同 **IBKR 适配映射表** 和 **纸盘测试脚本步骤**直接列成“对接清单”，你照单核对即可。需要的话说一声，我就把那张“映射表 + 回归步骤卡片”也给你补上。
太好了，那我把我们刚刚定下的方案补齐成两张“**对接清单**”+“**回归步骤卡片**”。按这个执行，就能把“父入场 + 多档止盈 + 单一止损 + OCA 自动减量（无再锚）”一把梭上线。

---

# 一、执行适配对接清单（DecisionEnvelope → IBKR 映射）

> 目标：一次性提交 **Parent(STP LMT)** + **Stop(STP)** + **TP1..TPn(LMT)**；**子单同一 OCA 组，OCAType=Reduce**（俗称 ReduceByAmount），确保 TP 部分/全部成交后，**止损与其余 TP 自动减量**；无需策略改单。

## A) Parent（入场单）

* `order_style.kind`: `"STP_LMT"`
* `order_style.stop_px` → **IB**: `stopPrice`（底层等价 `auxPrice`）
* `order_style.limit_px` → **IB**: `lmtPrice`
* 其它：

  * `tif="DAY"`
  * `outside_rth=True`（需要就开）
  * `transmit=False`（父单不传输）
  * **不设置 OCA**（OCA 只给子单）

## B) StopLoss（子单 1）

* `order_style.kind`: `"STP"`（建议市价触发，避免卡单）
* `order_style.stop_px` → **IB**: `stopPrice`
* 其它：

  * `parentId=<Parent orderId>`（由执行器用父单 clientId 映射）
  * `ocaGroup="<统一字符串>"`
  * `ocaType="REDUCE"`（你的适配层若用枚举，通常取 **2**；请以本地常量为准）
  * `transmit=False`

## C) TakeProfits（子单 2..n）

* `order_style.kind`: `"LMT"`
* `order_style.limit_px` → **IB**: `lmtPrice`
* 其它：

  * `parentId=<Parent orderId>`
  * `ocaGroup="<同组名>"`
  * `ocaType="REDUCE"`
  * `transmit`: **最后一个 TP 用 `True`**，其余为 `False`（确保**原子提交**）

## D) DecisionEnvelope.extras（建议字段规范）

* `extras.oca_group`: `str`（如：`"G_{strategy}_{symbol}_{yyyymmdd}"`）
* `extras.oca_type`: `"REDUCE"`（或枚举值；由适配层映射为 IB 的 OcaType）
* `extras.parent_ref`: `decision_id` 或内部引用（执行器据此回填 `parentId`）
* `extras.transmit`: `True|False`
* （可选）`extras.tag`: `{"bracket_id": "...", "leg": "PARENT|STOP|TP1|TP2|..."}` 便于日志与对账
* （可选）`extras.outside_rth`: `True|False`

> ⚠️ 注意
>
> * IBKR 的 OCA “Reduce”/“ReduceByAmount”在不同 SDK 里命名略异，**常见枚举为 2**（ReduceWithBlock）。请以你的适配层常量为准。
> * **OCA 只设置在子单（止损与所有 TP）**；父单只负责触发子单。
> * 提交顺序：**Parent(transmit=False) → Stop/TP…(transmit=False) → 最后一个 TP(transmit=True)**。

---

# 二、回归步骤卡片（纸盘/小额实盘前的必测清单）

## 0) 预备

* 启动 `PriceCache`（**04:00 ET 前**），确认 PM_H/L 在日志中持续更新。
* 开启 IBKR 纸盘/测试环境，确保**美股 L1/盘前权限**与 Scanner 可用。

## 1) 扫描与订阅

* **09:27 ET** 调用 Scanner（TOP_PERC_GAIN，rows=50）。
* 日志输出 `SCAN_TOP3`，含每只淘汰/入围原因（价、spread、pmNotional）。
* 仅对 Top3 建立 tick + 1m/5m 聚合订阅（日志：`SUBSCRIBE_TOP3`）。

**验收**：Top3 有连续的 `PriceCache` 快照（tick 延迟 < 500ms），分钟聚合每分钟滚动。

## 2) 四确认门槛

* **09:33–09:35**，日志打印 `EXEC_CHECKS`：

  * `price > vwap`、`breakout >= max(PM_H, ORB5_H)`、`rvol_1m/5m >= 2`、`spread <= 0.25%`
* 全通过 → 打 `ENTRY_PLACED`，包含 level/缓冲价与 sizing 依据。

**验收**：不满足条件不下单；满足后在 150–300ms 内提交意图。

## 3) 订单拓扑与 OCA 行为

* 观察 TWS/Gateway：出现 1 个父单 + N 个子单（1 止损 + 多 TP），子单同一 OCA 组。
* 触发成交后（可手动撮合/小量市价）：

  * **TP1 部分成交** → 检查**止损与余下 TP 的数量自动减量**（不应超卖/裸奔）。
  * **TP2/TP3 成交** → 按成交量继续自动减量；止损数量始终 = 当前净持仓。

**验收**：全程**无 CANCEL/AMEND**，订单数量与实际持仓**始终一致**。

## 4) 强平

* **10:10 ET**：策略提交 `FORCE_EXIT`（MKT），随后 `CLOSE_ORDERS` 撤销余单。
* 终态：持仓 0、挂单 0。

**验收**：日志有 `FORCE_EXIT_OK`；TWS 无剩余订单/持仓。

## 5) 异常分支

* Scanner 返回空 / 失败 → `STAND_DOWN(reason="scanner_empty")`，**当日不做**。
* `PriceCache` 延迟 > 1s / PM_H 缺失 → `SKIP_SYMBOL(symbol, reason)`，不下单。
* 价差超限（>0.25%）→ 明确拒单日志，不报错。
* LULD/Halt → 直接 `FORCE_EXIT` 路径验证（可模拟）。

---

# 三、参数落盘（建议默认）

```yaml
premarket_gainer_v1:
  scan_time_et: "09:27:00"
  entry_start_time_et: "09:33:00"
  entry_end_time_et: "09:35:00"
  force_exit_time_et: "10:10:00"
  scanner_rows: 50
  price_min: 5.0
  pm_notional_min: 10000000
  spread_max_observe: 0.0030     # 0.30%
  spread_max_exec: 0.0025        # 0.25%
  breakout_buffer_pct: 0.002     # +0.20%
  limit_buffer_pct: 0.001        # +0.10%
  rvol_min: 2.0
  risk_per_trade_pct: 0.005
  tp_levels_pct: [0.035, 0.055, 0.08]
  tp_qtys_pct:   [0.30, 0.35, 0.35]
```

---

# 四、上线注意点（最后确认）

* **OCAType**：确保映射到 IB 的“按成交量减少其余订单”的枚举/常量（多数实现为 **2**）。
* **止损用 STP**（不要 STP LMT），防止卡单；TP 用 LMT。
* **父/子提交顺序 + transmit**：严格执行原子提交模式。
* **仅 Top3 订阅**：避免路数/节流；指标来自内存聚合，**不走 ClickHouse**。
* **服务启动时间**：04:00 ET 前，PM_H 不完整就 **跳过该票**。

---

需要的话，我可以把“**样例 DecisionEnvelope（父/子 3 条）**”和“**适配层 IBKR 字段对照表**”也给你写成模板，直接贴到代码里就能对参接入。
太棒。按我们已经定好的“**父入场 + 多档止盈 + 单一止损 + OCA 自动减量（无再锚）**”方案，下面给你两张可直接对参接入的“**样例 DecisionEnvelope 模板**”与“**IBKR 字段映射表**”。照着填就能跑。

---

# 样例 DecisionEnvelope（父 + 止损 + 3 个止盈）

> 约定：
>
> * **OCA 只给子单（止损/止盈）**，父单不设 OCA。
> * **最后一个子单 `transmit=True`** 触发整组原子提交。
> * `parent_ref` 用父单的 `decision_id` 关联，执行器把它解成 IB 的 `parentId`。
> * `oca_type` 传 **"REDUCE"**（或你的适配层常量，表示按成交量减少其余子单）。

假设：`symbol="TSLA"`, `entry_level = max(PM_H, ORB5_H) = 248.00`
缓冲：`breakout +0.20%` → `stop_px=248.496`；`limit +0.10%` → `limit_px=248.74496`（实际请按最小跳价/tick 四舍五入）
风控 sizing 计算略。

```json
// 1) Parent 入场单（STP LMT）——不设 OCA
{
  "decision_id": "dec-parent-123",
  "strategy_id": "<your_strategy_id>",
  "symbol": "TSLA",
  "target_qty": 100,                      // 正数=买入
  "order_style": {
    "kind": "STP_LMT",
    "stop_px": 248.50,
    "limit_px": 248.99,
    "tif": "DAY"
  },
  "freshness": {"ttl_ms": 30000, "dlb_ms": 1000},
  "extras": {
    "leg": "PARENT",
    "bracket_id": "brkt-tsla-20251016",
    "transmit": false                     // 父单不传
  }
}

// 2) 子单：止损 STP（建议市价触发）
{
  "decision_id": "dec-sl-456",
  "strategy_id": "<your_strategy_id>",
  "symbol": "TSLA",
  "target_qty": -100,                     // 全量保护
  "order_style": {
    "kind": "STP",
    "stop_px": 244.80                     // max(VWAP_now, PM_L*0.99)
  },
  "freshness": {"ttl_ms": 30000, "dlb_ms": 1000},
  "extras": {
    "leg": "SL",
    "bracket_id": "brkt-tsla-20251016",
    "parent_ref": "dec-parent-123",
    "oca_group": "G_pre_v1_TSLA_20251016",
    "oca_type": "REDUCE",                 // 关键：按成交量减少其余子单
    "transmit": false
  }
}

// 3) 子单：TP1 LMT（30%）
{
  "decision_id": "dec-tp1-789",
  "strategy_id": "<your_strategy_id>",
  "symbol": "TSLA",
  "target_qty": -30,                      // 100 * 0.30
  "order_style": {
    "kind": "LMT",
    "limit_px": 257.70                    // entry_limit * (1 + 0.035)
  },
  "freshness": {"ttl_ms": 30000, "dlb_ms": 1000},
  "extras": {
    "leg": "TP1",
    "bracket_id": "brkt-tsla-20251016",
    "parent_ref": "dec-parent-123",
    "oca_group": "G_pre_v1_TSLA_20251016",
    "oca_type": "REDUCE",
    "transmit": false
  }
}

// 4) 子单：TP2 LMT（35%）
{
  "decision_id": "dec-tp2-abc",
  "strategy_id": "<your_strategy_id>",
  "symbol": "TSLA",
  "target_qty": -35,
  "order_style": {"kind": "LMT", "limit_px": 262.20},
  "freshness": {"ttl_ms": 30000, "dlb_ms": 1000},
  "extras": {
    "leg": "TP2",
    "bracket_id": "brkt-tsla-20251016",
    "parent_ref": "dec-parent-123",
    "oca_group": "G_pre_v1_TSLA_20251016",
    "oca_type": "REDUCE",
    "transmit": false
  }
}

// 5) 子单：TP3 LMT（35%）——最后一个子单 transmit=True
{
  "decision_id": "dec-tp3-def",
  "strategy_id": "<your_strategy_id>",
  "symbol": "TSLA",
  "target_qty": -35,
  "order_style": {"kind": "LMT", "limit_px": 268.00},
  "freshness": {"ttl_ms": 30000, "dlb_ms": 1000},
  "extras": {
    "leg": "TP3",
    "bracket_id": "brkt-tsla-20251016",
    "parent_ref": "dec-parent-123",
    "oca_group": "G_pre_v1_TSLA_20251016",
    "oca_type": "REDUCE",
    "transmit": true                      // 原子提交整组
  }
}
```

**提交顺序必须是**：Parent(transmit=false) → SL(transmit=false) → TP1(false) → TP2(false) → **TP3(true)**。
**效果**：任一 TP（哪怕部分成交）都会让**止损和其它 TP 的数量**自动按成交量**减少**，避免超卖与手工改单。

---

# IBKR 字段映射表（适配层对照）

| 我方抽象                         | IBKR 字段                  | 说明                                                          |
| ---------------------------- | ------------------------ | ----------------------------------------------------------- |
| `order_style.kind="STP_LMT"` | `orderType="STP LMT"`    | 触发价 `stopPrice`（或 auxPrice），限价 `lmtPrice`                   |
| `order_style.kind="STP"`     | `orderType="STP"`        | 市价触发止损，**推荐**防卡单                                            |
| `order_style.kind="LMT"`     | `orderType="LMT"`        | 固定价格止盈                                                      |
| `order_style.kind="MKT"`     | `orderType="MKT"`        | 强平使用                                                        |
| `stop_px`                    | `stopPrice` / `auxPrice` | 视 SDK 命名                                                    |
| `limit_px`                   | `lmtPrice`               | —                                                           |
| `tif`                        | `tif`                    | 一般 `"DAY"`                                                  |
| `extras.parent_ref`          | `parentId`               | 由执行器把 `decision_id` 解成 IB `parentId`                        |
| `extras.oca_group`           | `ocaGroup`               | 同一组字符串（子单必填）                                                |
| `extras.oca_type="REDUCE"`   | `ocaType`                | **必须**映射为“按成交量减少其余订单”的类型（常称 Reduce/ReduceByAmount；数值枚举依适配层） |
| `extras.transmit`            | `transmit`               | 最后一个子单 **True**，其余 False                                    |
| `outside_rth`                | `outsideRth`             | 需要盘前/盘后生效再开                                                 |

> 小贴士：
>
> * 有的 SDK 把 OCA 枚举拆成 `OcaType.REDUCE`、`OcaType.REDUCE_WITH_BLOCK` 等；**务必选择“按成交量减少其余订单”**的那个常量。
> * **OCA 只设置在子单**（止损+所有 TP）。父单作为触发者，不设 OCA。
> * 所有价格请按 **最小跳价**（tick size）取整，避免被 TWS 拒单。

---

## 最后核对（落地检查三件事）

1. **PriceCache**：09:30 起 `vwap/rvol_1m/rvol_5m` 每分钟滚动；04:00–09:30 累积 `pm_high`；09:30–09:35 累积 `orb5_high`。
2. **四确认**：价>VWAP、破位≥max(PM_H, ORB5_H)、RVOL 双达标、spread≤0.25%。
3. **OCA Reduce 生效**：TP 部分成交后，止损和其余 TP 的数量**自动**减少，持仓与挂单数量始终一致，全程**无 CANCEL/AMEND**。


