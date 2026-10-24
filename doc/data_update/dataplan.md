我把方案按你现有代码与脚本“重新审视+落地化”，给出一份**不破坏现状、可渐进上线**的完整执行计划，并从交易员视角把“真问题/坑点”一并纳入验收项。下面内容**不含代码**（你说等你提示后再给代码），但每一步都落到“要做什么 + 怎么验证”。

---

# 执行计划（兼容现状、渐进落地）

## 目标回顾（三条铁律）

1. **每个分辨率只有一个权威写源**（IBKR直写或自聚合，二选一，不双写同表）。
2. **统一入库链路**：Kafka → ClickHouse，表引擎 `ReplacingMergeTree(ver)` 幂等覆盖。你现有 `ch_writer` 已按此实现（消费 `md.bar.5s.v2` → 写 `bars_5s`，幂等批量）。
3. **双保险修复**：盘中缺口→`BackfillService` 小回补；盘后**离线对账**一网打尽（你现有 BackfillService 已具备消费 gap+发 bar、并带节流/并发控制的骨架） 。

---

## Phase 0｜保守上线的“护栏”与配置开关（当天可做）

* **特性开关**：在 `config.yml` 增加：

  * `event_bus.provider = kafka`（你已经有占位并默认 kafka）；
  * 为各 writer/回补/特征发布器加 `enabled` 布尔开关与 `topic/table` 参数（现 `ch_writer` 已能接收 `topic/group_id/table_name`，只需把默认写死→改成从配置注入）。
* **回滚策略**：所有新表以“_new”/“_ibkr/ _agg”命名；读侧走视图（`bars_{res}_current`）。回滚=切回旧视图/旧表即可（无代码回滚）。

**验收**：服务启动不报错；不开启任何新写源也不影响现网。

---

## Phase 1｜Writer 参数化 + 多实例部署（低风险，先只扩一个周期）

**要做**

1. 将 `ClickHouseBarWriter` 的 `topic/group_id/table_name` 完全**参数化读取 config**（已经基本具备）。
2. 在进程管理（`run_system.py`）按配置**启动多个 writer 实例**：

   * 先加一个：`md.bar.1m.ibkr` → `bars_1m_ibkr`（或过渡期先写老表 `bars_1m`，见 Phase 2）。
3. BackfillService 默认仍只处理 5s（保守），参数保持现状（含`group_id`、RTH、并发/退避等） 。

**验收**

* Kafka 内有分 topic；ClickHouse 对应表有新增行；消费/入库延迟、错误率可观测（批量写/幂等已在 writer 内实现）。
* 关闭新 writer 后，一切恢复旧状（无副作用）。

---

## Phase 2｜ClickHouse 分表 + 统一视图（不改读侧代码）

**要做**

1. 为 IBKR 有原生周期的分辨率（1m/5m/15m/30m/1h/4h/1d）建**两张表**：`bars_{res}_ibkr` 与 `bars_{res}_agg`；引擎 `ReplacingMergeTree(ver)`，主键 `(symbol, ts_start)`。
2. 创建 **统一口径视图** `bars_{res}_current`：

   * `UNION ALL` 两表并加 `src_priority`，`LIMIT 1 BY (symbol, ts_start)` 实现“选优”，达成**不双写同表**也能共存与切换（按蓝图）。
3. 迁移：如当前只有 `bars_1m` 一张老表，先 `INSERT INTO bars_1m_ibkr SELECT ... FROM bars_1m`，再让读方改指向 `bars_1m_current`。
4. 维持你现有的 15s/15m 物化视图链路不变（`bars_5s`→`mv_bars_15s`→`bars_15s`；`bars_5s`→`mv_bars_15m`→`bars_15m`），仅增加 `current` 视图对齐口径（你已有 `mv_bars_15s/15m` 模版可复用） 。

**验收**

* 策略/特征侧查询从 `bars_{res}_current` 读，结果与旧表一致或更优；
* 一键切换权威源=改视图内 `src_priority`；
* FINAL 查询或 `LIMIT 1 BY` 去重有效，历史/回补不会“抖”。

---

## Phase 3｜“自动小回补 + 离线大回补”（双保险补齐）

**要做**

1. **小回补（常驻）**：新增 `catchup_service`：每 N 分钟对“今天+昨天”对齐**理论时间轴**与 `bars_{res}_current`，发现短缺段（<1h）就发“回补请求”到 Backfill 队列（你现 BackfillService 已提供 `schedule_job` 接口与优先队列，直接复用）。
2. **离线大回补（定时）**：在收盘后运行，对“交易日×symbol×分辨率”做**全面对账**，按小时切片拉 IBKR，统一**发 Kafka** 到对应 `md.bar.{res}.ibkr`，**不直写 CH**（确保入口一致）。
3. Backfill/对账**限流/重试/抖动**沿用你现有实现（`Semaphore`、退避等），避免 IBKR pacing（代码里已内置并发/间隔）。

**验收**

* 缺口率<阈值（按 symbol/res/day）；
* Backfill 成功率、DLQ 为零或在阈内；
* 盘后对账后，第二天开盘前无“历史空洞”。

---

## Phase 4｜数据湖补齐（与现有 `ticks_raw` 平滑共存）

**要做**

1. 你现有 `trading.ticks_raw` 已能沉淀 trade/quote，并带 TTL/分区（可视为最小可用“lake”） 。先**沿用**，待稳定后再逐步迁至 `lake.*` 命名。
2. VIX：已预留 `trading.refs_vix_ticks` 表，落地 1m bar/特征需要即可从此派生（或把 VIX 直接当作普通 symbol 进 `bars_1m_*`）。
3. 新增 `lake_writer`（复用 ch_writer 模板），独立消费原始 tick/quote 主题写入 `ticks_raw`，与 Bars 写入解耦。

**验收**

* `ticks_raw` 7~30 天可查；BBO 质量可计算 `spread_bps`；VIX 序列可被 1m 特征引用。

---

## Phase 5｜特征层与发布（先稳离线，再补流式）

**要做**

1. 表：你已定义 `features_1m/5m/15m/...`（包括 ATR/RSI/BB/ADX/RVOL、vwap_day、spread_bps 等常用指标）——满足大多数脚本所需。示例：`features_1m` 包含 `atr_14,rsi_14,bband..., obv, minute_of_day, vwap_day` 等；`features_5m` 含 `ema12/ema26/macd` 等；`features_15m` 含部分 VIX 相关字段（若需可下沉到 1m） 。
2. 计算：你现在线上发布器只发布 5s/10s 特征到 Kafka（`md.features.5s/10s.v1`），保持不变；1m/5m 先**用 ClickHouse SQL 计算入表 + 读视图**，待验证后再考虑发布到 Kafka（`feature_publisher` 目前仅支持 5s/10s，可后续扩展） 。
3. **修正一个易错点**：现有 RVOL 的 SQL 使用 `now()` 做同分钟聚合窗口，回测时会偏（应基于 `ts_start` 的 minute-of-day 分区来算“同刻均量”）。我们会在 DDL/视图里改成以 `(toHour(ts_start), toMinute(ts_start))` 分组的窗口函数，避免历史重算偏移（你当前示例里确实出现对 `now()` 的依赖）。

**验收**

* 用 CH 一条 SQL 拉 `features_1m` 即可满足策略；
* 5s/10s 流式特征不受影响；
* RVOL/EMA/MACD 等指标在 T+0、T-1 的一致性通过抽样比对。

---

## Phase 6｜监控与告警（面向交易员的“感知”）

* 缺口率、回补延迟、DLQ 计数、writer 插入失败率、IBKR pacing 告警（这些指标大多可在 Writer/Backfill 的日志与 counters 中采集）。
* 策略侧健康：1m/5m 最新时间是否更新、VIX 最新值时间是否滞后（`config.yml` 里有 VIX fallback 设置，也要监控“真的在 fallback”）。

---

# 策略数据覆盖性核对（你的脚本 → 我们提供）

> 目标：**让策略“只管拉”**。统一从 `bars_{res}_current` 和 `features_{res}` 读取，不再关心底层是 IBKR 还是聚合。

## 通用需求（所有脚本的共性）

* **分辨率**：1m、5m（少量 15m）、以及 5s（内部聚合/开盘细节）。
* **指标**：ATR14、RSI14、Bollinger(20,2)、EMA(12/26)、MACD、OBV、MFI、Donchian、高低突破、VWAP（含**当日 vwap_day**）、RVOL（含**分钟时段 RVOL**）、`spread_bps`、`close_to_mid_bps`（需 L1 BBO 支撑）。
* **参照系**：`SPY/QQQ`（5m）与 `VIX`（1m）。策略注册中明确需订阅市场基准与 VIX（你的 TSLA 策略在初始化就订阅了 TSLA(1/5m)、SPY/QQQ(5m)、VIX(1m)）；`config.yml` 中也配置了 `market_regime_symbols` 和 VIX fallback 值 。

**我们提供**

* 1m/5m Bars：`bars_1m_current / bars_5m_current`（视图）；
* 1m/5m Features：`features_1m / features_5m`（SQL 计算入表，后续可流式发布）；
* BBO 支撑：`ticks_raw`（quote 字段齐全，可计算 spread/中间价）；
* VIX：`refs_vix_ticks` +（可选）`bars_1m_{ibkr|agg}` 中的 “VIX” 作为普通 symbol 处理。

## 脚本级核对（抽样说明）

* **tsla_v7_2**：依赖 1m/5m bar + 基准 SPY/QQQ(5m) + VIX(1m) 的 `close/vwap/atr/rsi/rv_20` 等；事件侧还会用**成本门槛**（基于 L1 半点价差 P95，需 BBO）——上述均可由 `features_1m/5m` + `ticks_raw` 提供；订阅侧保持不变（FeaturePublisher 先不改，策略也能直接从 CH 读 `features_1m/5m`）。
* **amzn/msft/amd day trader & nvda mean reversion**：脚本里重复计算 EMA/RSI/BB/ATR/VWAP/ORB 等——我们将**把这些指标搬到 CH 特征层**，脚本改为“只取现成列”，大幅消除重复（你已有 features 表列定义覆盖这些指标）。
* **历史抓取脚本**：`load_hist_*` 已能产出 `open/high/low/close/volume/vwap/bid_last/ask_last/mid_last` 字段，能无缝写入新表结构（我们兼容这些字段）。

**结论**：

* 你的策略所需**核心数据/指标**，在我们提供的 `bars_*_current + features_*` 里**都能覆盖**。
* 仅有两点需要**微调/增强**：

  1. **VIX**：确保 1m 级别有条线可读（用 `refs_vix_ticks` 下沉或把 VIX 当普通 symbol 进 `bars_1m_*`，两种均可）。
  2. **RVOL 同刻均量计算修正**：去掉 `now()` 依赖，改用 `minute_of_day` 分区（避免历史/回测漂移）。

---

# 交易员视角的“真实问题”与我们的处理

1. **开盘前几根 1m 不稳定**（跳价/对敲/碎片成交）：

   * 方案：5s→1m 的聚合采用**“延迟发射+轻微迟到容忍”**（你已有 emit_buffer / watermark 逻辑），同时若迟到则打 `quality=REPAIRED` 标签，可在策略侧过滤或降权 。
2. **RTH vs 盘前/盘后**：

   * 方案：缺口探测仅在 RTH 内（已实现），小回补/大回补默认 `use_rth=True`（可配置） 。
3. **IBKR pacing/速率限制**：

   * 方案：Backfill 内已做信号量与最小间隔，且分片（小时级）拉取，失败重试+抖动（保留并显式监控）。
4. **VIX/基准滞后**（节假日/半日市错位）：

   * 方案：VIX 有 fallback 值；此外把 VIX 作为普通 symbol 写入 1m，策略端优先用**实时** VIX（表内最新时间戳），若滞后则降权或暂停（用你策略中的 `pause.theta_vix`/`kill switch` 参数结合）。配置里已有停机/阈值参数位点。
5. **成本门槛/滑点估算**（按分钟槽的半点价差分布）：

   * 方案：用 `ticks_raw` 的 quote 计算 minute-of-day 的 half-spread P95，并缓存到 `market_cost_profile` 表，策略取值时即刻可用（TSLA 脚本已有估算逻辑，我们后续把它表化）。
6. **早收市/交易日历**：

   * 方案：对账与窗口生成统一走“交易日历”，开盘/收盘时间以交易所日历为准（catchup/reconciler 共用）。

---

# 统一读取口径（给策略的“一个入口”）

1. **Bars**：`SELECT * FROM bars_{res}_current WHERE symbol=? AND ts_start BETWEEN ? AND ?`（res ∈ 5s/1m/5m...）。
2. **Features**：`SELECT * FROM features_{res} WHERE symbol=? AND ts BETWEEN ? AND ?`。
3. **基准/衍生**：SPY/QQQ/VIX 与个股一样读取（或 `refs_vix_ticks` 下沉成 1m 表/视图）。

> 这样你的策略就不再写一堆重复指标计算，**直接拉**就行（`history_features` 也能用这俩视图/表来实现/替换）。

---

# 每一步的“可交付/验收清单”

* **P0**：配置开关生效；关闭新特性=零影响。
* **P1**：新增 1m writer 实例写入一张新表；Kafka/CH 都看到稳定增量；延迟 < 2s。
* **P2**：建好 `bars_{res}_ibkr/_agg` 与 `bars_{res}_current`，策略/特征查询切到视图后结果一致；切源=改 `src_priority` 即生效。
* **P3**：`catchup_service` 把“今天+昨天”缺口压到阈值；盘后对账把“历史缺口”清零。
* **P4**：`ticks_raw` 连续积累 7–30 天；可生成 minute-of-day spread P95；VIX 1m 可读。
* **P5**：`features_1m/5m` 一条 SQL 提供完整指标；RVOL 同刻算法修正通过回测/抽样对比；（可选）扩展特征发布到 Kafka。
* **P6**：Grafana 面板/告警就绪：缺口率、回补延迟、DLQ、pacing、VIX 滞后等。

---
详细的落地策略在同文件夹里面的md文件里面，文件名数字1.md 开始，你可以按照顺序进行落地。